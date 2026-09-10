# Step 2 — Pull the Mobility Database.
#
# Calls the Mobility Database's GTFS feeds endpoint with pagination and
# upserts every feed into the `mobility_agencies` table.
#
# Auth: the Mobility Database API (api.mobilitydatabase.org/v1) does not take
# the long-lived token you get from your account directly as a bearer token —
# that token is a *refresh* token. You exchange it for a short-lived access
# token via POST /v1/tokens, then send that access token as
# `Authorization: Bearer <access_token>` on the actual data calls. We read the
# long-lived token from MOBILITY_API_TOKEN and do that exchange here so the
# rest of the script (and anyone re-running it) only has to deal with one env
# var, matching what the brief asked for. See LOG.md for why this differs
# from a literal "send MOBILITY_API_TOKEN as the bearer token" reading.
#
# Defensive by design: every network call and every field lookup is wrapped
# so a single bad page or a schema field that doesn't match what we expect
# logs a warning and moves on rather than crashing the whole pull.

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.config import MOBILITY_API_BASE_URL, MOBILITY_API_TOKEN
from db.models import MobilityAgency
from db.session import SessionLocal
from ingestion.geo import country_name_to_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mobility_db")

PAGE_SIZE = 100
REQUEST_TIMEOUT = 30.0


def get_access_token(client: httpx.Client) -> str | None:
    """Exchange the long-lived MOBILITY_API_TOKEN (refresh token) for a
    short-lived access token. Returns None (and logs) on any failure so the
    caller can decide how to proceed instead of crashing."""

    if not MOBILITY_API_TOKEN:
        logger.error(
            "MOBILITY_API_TOKEN is not set — get a free token from "
            "https://mobilitydatabase.org and add it to .env"
        )
        return None

    try:
        resp = client.post(
            f"{MOBILITY_API_BASE_URL}/tokens",
            json={"refresh_token": MOBILITY_API_TOKEN},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        access_token = data.get("access_token") or data.get("accessToken")
        if not access_token:
            logger.error("Token exchange succeeded but no access_token in response: %s", data)
            return None
        return access_token
    except httpx.HTTPError as exc:
        logger.error("Failed to exchange MOBILITY_API_TOKEN for an access token: %s", exc)
        return None


def _extract_page(payload: Any) -> list[dict]:
    """The v1/gtfs_feeds response is documented as a bare JSON array, but we
    defensively unwrap a couple of common wrapper shapes too in case that
    changes, so a schema tweak degrades to an empty page instead of a
    crash."""

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "feeds", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    logger.warning("Unrecognized gtfs_feeds response shape, treating as empty page: %r", type(payload))
    return []


def _first_location(feed: dict) -> dict:
    locations = feed.get("locations")
    if isinstance(locations, list) and locations:
        return locations[0] or {}
    return {}


def _feed_url(feed: dict) -> str | None:
    latest_dataset = feed.get("latest_dataset") or {}
    hosted_url = latest_dataset.get("hosted_url")
    if hosted_url:
        return hosted_url
    source_info = feed.get("source_info") or {}
    return source_info.get("producer_url")


def _feed_last_updated(feed: dict) -> datetime | None:
    """The Mobility Database's own `latest_dataset.downloaded_at` timestamp
    (ISO 8601, e.g. "2026-08-21T00:00:47.858940Z") — when MobilityData last
    pulled a fresh copy of this feed, used as the "last updated" signal for
    the opportunity-score freshness component. Verified against a live feed
    record before writing this; see LOG.md."""

    latest_dataset = feed.get("latest_dataset") or {}
    raw = latest_dataset.get("downloaded_at")
    if not raw:
        return None
    try:
        # datetime.fromisoformat doesn't accept a trailing "Z" before 3.11;
        # this codebase targets 3.11+ (see api/Dockerfile, .python-version)
        # but normalize it anyway since it's a one-line defensive parse.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Could not parse latest_dataset.downloaded_at %r for feed %r", raw, feed.get("id"))
        return None


def parse_feed(feed: dict) -> dict:
    """Map one Mobility Database GTFSFeed object to our column shape.
    Every lookup is defensive (.get with fallback) — an unexpected or
    missing field becomes None rather than a KeyError."""

    location = _first_location(feed)
    country = location.get("country")
    country_code = location.get("country_code")
    if country and not country_code:
        country_code = country_name_to_code(country)

    return {
        "source_id": feed.get("id"),
        "name": feed.get("provider") or feed.get("feed_name"),
        "country": country,
        "country_code": country_code,
        "subdivision_name": location.get("subdivision_name"),
        "municipality": location.get("municipality"),
        "feed_url": _feed_url(feed),
        "feed_status": feed.get("status"),
        "feed_last_updated": _feed_last_updated(feed),
    }


def fetch_all_feeds(client: httpx.Client, access_token: str) -> list[dict]:
    """Pull every page of /v1/gtfs_feeds. Stops when a page comes back
    shorter than PAGE_SIZE (last page) or empty. A single failed page is
    logged and treated as the end of the pull rather than raising, so
    whatever was fetched before the failure is still persisted."""

    feeds: list[dict] = []
    offset = 0
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        try:
            resp = client.get(
                f"{MOBILITY_API_BASE_URL}/gtfs_feeds",
                headers=headers,
                params={"limit": PAGE_SIZE, "offset": offset},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Request failed at offset %d: %s — stopping pagination", offset, exc)
            break

        try:
            page = _extract_page(resp.json())
        except ValueError as exc:
            logger.error("Could not decode JSON at offset %d: %s — stopping pagination", offset, exc)
            break

        if not page:
            break

        feeds.extend(page)
        logger.info("Fetched %d feeds (offset %d)", len(page), offset)

        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return feeds


def upsert_agencies(records: list[dict]) -> int:
    """Upsert parsed feed dicts into mobility_agencies, keyed on source_id.
    A row with a null source_id (couldn't determine the feed's own id) is
    inserted unconditionally rather than upserted, since there's nothing to
    de-dupe on. Returns the number of rows written."""

    if not records:
        return 0

    written = 0
    with SessionLocal() as session:
        for record in records:
            try:
                if record.get("source_id"):
                    stmt = pg_insert(MobilityAgency).values(
                        **record, created_at=datetime.utcnow()
                    )
                    stmt = stmt.on_conflict_do_update(
                        constraint="uq_mobility_agencies_source_id",
                        set_={
                            "name": stmt.excluded.name,
                            "country": stmt.excluded.country,
                            "country_code": stmt.excluded.country_code,
                            "subdivision_name": stmt.excluded.subdivision_name,
                            "municipality": stmt.excluded.municipality,
                            "feed_url": stmt.excluded.feed_url,
                            "feed_status": stmt.excluded.feed_status,
                            "feed_last_updated": stmt.excluded.feed_last_updated,
                        },
                    )
                    session.execute(stmt)
                else:
                    session.add(MobilityAgency(**record))
                # Committed per-record rather than once at the end of the
                # loop: session.rollback() in the except branch below rolls
                # back the *entire* current transaction, not just the
                # statement that failed. With a single commit at the end, one
                # bad record near the end of a run would silently discard
                # every successful insert/update that preceded it in the
                # same session — exactly what "one bad row must not abort
                # the batch" is supposed to prevent. Found in a review pass;
                # see LOG.md.
                session.commit()
                written += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort the batch
                logger.error("Failed to upsert record %r: %s", record.get("source_id"), exc)
                session.rollback()
                continue

    return written


def main() -> None:
    with httpx.Client() as client:
        access_token = get_access_token(client)
        if not access_token:
            # Raise rather than return — run.py's run_step() only stops the
            # pipeline on an exception. Returning normally here made a
            # missing/expired token look like a successful (empty) Step 2 to
            # run.py, which would then carry on running Steps 3-5 against a
            # stale mobility_agencies table instead of halting. Found in a
            # review pass; see LOG.md.
            logger.error("Aborting Mobility Database pull — no access token available.")
            raise RuntimeError("Could not obtain a Mobility Database access token")

        raw_feeds = fetch_all_feeds(client, access_token)

    logger.info("Pulled %d raw feed records from the Mobility Database", len(raw_feeds))

    records = []
    for feed in raw_feeds:
        try:
            records.append(parse_feed(feed))
        except Exception as exc:  # noqa: BLE001 — a malformed feed must not abort the pull
            logger.error("Failed to parse feed %r: %s", feed.get("id"), exc)
            continue

    written = upsert_agencies(records)
    logger.info("Inserted/updated %d rows in mobility_agencies", written)


if __name__ == "__main__":
    sys.exit(main())
