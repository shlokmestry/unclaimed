# Step 5 — Enrich uncovered agencies.
#
# For every mobility_agencies row marked is_covered = False (from Step 4):
#   - downloads its GTFS feed zip, scores feed quality (0-100), and counts
#     routes/stops/trips
#   - checks whether it has a linked GTFS-Realtime feed
#   - looks up its country's population as a market-size proxy
#   - computes a weighted opportunity_score and a readiness_status
# and stores the result in `uncovered_agencies`.
#
# Every per-agency step (download, zip parsing, population lookup, realtime
# lookup) is wrapped so one dead feed or one failed lookup degrades that
# agency's score instead of stopping the run.
#
# Opportunity-score rework (see LOG.md for the full writeup): the original
# formula was normalised_population*0.6 + quality_score*0.4. Replaced with a
# 5-component weighted formula using real signals from the Mobility Database
# (GTFS-RT cross-reference, feed freshness, route-network size) rather than
# just population + static-file completeness.

from __future__ import annotations

import csv
import io
import logging
import math
import sys
import zipfile
from datetime import datetime, date, timezone

import httpx

from db.config import MOBILITY_API_BASE_URL, WORLD_BANK_BASE_URL
from db.models import MobilityAgency, UncoveredAgency
from db.session import SessionLocal
from ingestion.mobility_db import get_access_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("enrichment")

FEED_TIMEOUT = 30.0
POPULATION_TIMEOUT = 15.0
REALTIME_PAGE_SIZE = 100

REQUIRED_FILES = ["stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt"]
POINTS_PER_REQUIRED_FILE = 15  # 5 files * 15 = 75

# Opportunity-score component weights — must sum to 1.0 (see LOG.md for why
# each was picked and how each component is scaled to 0-100 before weighting).
WEIGHT_POPULATION = 0.25
WEIGHT_QUALITY = 0.20
WEIGHT_REALTIME = 0.25
WEIGHT_FRESHNESS = 0.20
WEIGHT_NETWORK_SIZE = 0.10

# Readiness thresholds (Improvement 2)
READY_MIN_QUALITY = 70
READY_MAX_STALE_DAYS = 180
READY_MIN_ROUTES = 5
READY_MIN_STOPS = 20
DEAD_MAX_STALE_DAYS = 730  # >2 years or unknown => Dead Feed

# Population cache keyed by country_code (fallback: country name), shared
# across all agencies in a run so we hit World Bank once per country rather
# than once per agency.
_population_cache: dict[str, int | None] = {}


def _find_member(names: list[str], filename: str) -> str | None:
    for entry in names:
        if entry == filename or entry.endswith("/" + filename):
            return entry
    return None


def _count_rows(zf: zipfile.ZipFile, member: str) -> int:
    with zf.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")
        reader = csv.reader(text)
        next(reader, None)  # header
        return sum(1 for _ in reader)


def _calendar_has_future_dates(zf: zipfile.ZipFile, member: str) -> bool:
    today_str = date.today().strftime("%Y%m%d")
    with zf.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(text)
        for row in reader:
            end_date = (row.get("end_date") or "").strip()
            if end_date and end_date >= today_str:
                return True
    return False


def score_feed(feed_bytes: bytes) -> dict:
    """Scores one GTFS zip's raw bytes. Returns a dict:
    {quality_score, missing_files, route_count, stop_count, trip_count}.
    Never raises — a corrupt zip or unreadable member scores 0 / None for
    that component instead of aborting the whole score.

    route_count/stop_count/trip_count are parsed from the same already-open
    zip used for quality scoring (Improvement 1) rather than re-downloading
    the feed a second time."""

    result = {
        "quality_score": 0.0,
        "missing_files": list(REQUIRED_FILES),
        "route_count": None,
        "stop_count": None,
        "trip_count": None,
    }

    try:
        zf = zipfile.ZipFile(io.BytesIO(feed_bytes))
    except zipfile.BadZipFile as exc:
        logger.error("Feed is not a valid zip: %s", exc)
        return result

    try:
        score = 0.0
        missing = []
        names = zf.namelist()
        members = {}
        for filename in REQUIRED_FILES:
            member = _find_member(names, filename)
            if member:
                members[filename] = member
                score += POINTS_PER_REQUIRED_FILE
            else:
                missing.append(filename)

        if "stops.txt" in members:
            try:
                stop_count = _count_rows(zf, members["stops.txt"])
                result["stop_count"] = stop_count
                if stop_count > 10:
                    score += 10
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not count stops.txt rows: %s", exc)

        if "routes.txt" in members:
            try:
                route_count = _count_rows(zf, members["routes.txt"])
                result["route_count"] = route_count
                if route_count > 3:
                    score += 10
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not count routes.txt rows: %s", exc)

        if "trips.txt" in members:
            try:
                result["trip_count"] = _count_rows(zf, members["trips.txt"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not count trips.txt rows: %s", exc)

        if "calendar.txt" in members:
            try:
                if _calendar_has_future_dates(zf, members["calendar.txt"]):
                    score += 5
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not evaluate calendar.txt future dates: %s", exc)

        result["quality_score"] = score
        result["missing_files"] = missing
        return result
    finally:
        zf.close()


def fetch_feed_quality(feed_url: str | None) -> dict:
    """Downloads and scores a feed. Always returns a dict with the keys
    score_feed() returns, plus `reachable` (bool) — whether the download
    itself succeeded (HTTP 200), independent of what's inside the zip.
    `reachable` feeds the readiness_status "Dead Feed" criterion."""

    base = {
        "quality_score": 0.0,
        "missing_files": list(REQUIRED_FILES),
        "route_count": None,
        "stop_count": None,
        "trip_count": None,
        "reachable": False,
    }

    if not feed_url:
        logger.warning("No feed_url — scoring 0")
        return base

    try:
        with httpx.Client(timeout=FEED_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(feed_url)
            resp.raise_for_status()
            scored = score_feed(resp.content)
            scored["reachable"] = True
            return scored
    except httpx.TimeoutException:
        logger.error("Feed download timed out after %ss: %s", FEED_TIMEOUT, feed_url)
        return base
    except httpx.HTTPError as exc:
        logger.error("Feed download failed (%s): %s", feed_url, exc)
        return base


def fetch_population(country_code: str | None, country: str | None) -> int | None:
    """Country population from the World Bank's open data API, used as a
    market-size proxy since city-level population data isn't reliably
    available. Cached per run so repeated agencies in the same country cost
    one HTTP call.

    Needs an ISO country code — the World Bank's indicator endpoint doesn't
    support free-text country name lookup, so a row missing country_code
    (rare: 1 of 4,548 agencies in the first full pull) returns None rather
    than guessing from the display name."""

    cache_key = country_code or country
    if not cache_key:
        return None
    if cache_key in _population_cache:
        return _population_cache[cache_key]

    if not country_code:
        logger.warning("No country_code for %r — cannot look up population without one", country)
        _population_cache[cache_key] = None
        return None

    population = None
    try:
        resp = httpx.get(
            f"{WORLD_BANK_BASE_URL}/country/{country_code}/indicator/SP.POP.TOTL",
            params={"format": "json", "per_page": "5"},
            timeout=POPULATION_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        payload = resp.json()
        # The API responds [meta, records] — records is None for an unknown
        # country code, and individual records can have a null value for
        # years the indicator wasn't reported, so take the first non-null one
        # (records come back most-recent-first).
        records = payload[1] if isinstance(payload, list) and len(payload) > 1 else None
        if records:
            for record in records:
                if record and record.get("value") is not None:
                    population = int(record["value"])
                    break
    except httpx.HTTPError as exc:
        logger.error("Population lookup failed for %r: %s", cache_key, exc)
    except (ValueError, IndexError, TypeError, AttributeError) as exc:
        logger.error("Could not parse population response for %r: %s", cache_key, exc)

    _population_cache[cache_key] = population
    return population


def fetch_realtime_source_ids() -> set[str]:
    """Pulls every page of /v1/gtfs_rt_feeds and returns the set of static
    GTFS feed ids (mobility_agencies.source_id values) that have at least
    one linked realtime feed, via each GTFS-RT feed's `feed_references` array
    — verified against a live API response before writing this (see LOG.md).
    One paginated pull for the whole run, not per-agency.

    Returns an empty set (logged) on any failure — has_realtime then comes
    out False for every agency rather than crashing enrichment over a
    feed that, by definition, isn't required for the static pipeline."""

    source_ids: set[str] = set()

    try:
        with httpx.Client() as client:
            access_token = get_access_token(client)
            if not access_token:
                logger.error("Could not get an access token — skipping realtime feed lookup")
                return source_ids

            headers = {"Authorization": f"Bearer {access_token}"}
            offset = 0
            while True:
                try:
                    resp = client.get(
                        f"{MOBILITY_API_BASE_URL}/gtfs_rt_feeds",
                        headers=headers,
                        params={"limit": REALTIME_PAGE_SIZE, "offset": offset},
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    page = resp.json()
                except httpx.HTTPError as exc:
                    logger.error("Realtime feed pull failed at offset %d: %s — stopping", offset, exc)
                    break

                if not isinstance(page, list) or not page:
                    break

                for rt_feed in page:
                    for ref in rt_feed.get("feed_references") or []:
                        if ref:
                            source_ids.add(ref)

                if len(page) < REALTIME_PAGE_SIZE:
                    break
                offset += REALTIME_PAGE_SIZE
    except Exception as exc:  # noqa: BLE001 — realtime lookup is a nice-to-have, never fatal
        logger.error("Unexpected error fetching realtime feeds: %s", exc)

    logger.info("Found %d static feeds with a linked realtime feed", len(source_ids))
    return source_ids


def normalize_populations(populations: list[int | None]) -> dict[int | None, float]:
    """Maps each distinct population value to a 0-100 score.

    Raw population is log10-scaled before min-max normalization: populations
    span several orders of magnitude (a few million to well over a billion),
    and a linear min-max would flatten every country except the one or two
    largest to near 0. Log-scaling first spreads the score meaningfully
    across the whole range while still normalizing to 0-100 as specified.
    See LOG.md. A missing population (lookup failed) maps to 0.
    """

    valid = [p for p in populations if p and p > 0]
    if not valid:
        return {p: 0.0 for p in populations}

    log_values = [math.log10(p) for p in valid]
    lo, hi = min(log_values), max(log_values)

    mapping: dict[int | None, float] = {}
    for p in set(populations):
        if not p or p <= 0:
            mapping[p] = 0.0
        elif hi == lo:
            mapping[p] = 100.0
        else:
            mapping[p] = round((math.log10(p) - lo) / (hi - lo) * 100, 2)

    return mapping


def normalize_linear(values: list[int | None]) -> dict[int | None, float]:
    """Plain min-max normalization to 0-100 (no log scale) — used for
    route_count's network_size_score. Route counts don't span the extreme
    multi-order-of-magnitude range population does (single digits to a few
    thousand, not millions to billions), so a linear scale is appropriate as
    asked rather than needing the log-scale treatment population gets."""

    valid = [v for v in values if v is not None and v >= 0]
    if not valid:
        return {v: 0.0 for v in values}

    lo, hi = min(valid), max(valid)

    mapping: dict[int | None, float] = {}
    for v in set(values):
        if v is None:
            mapping[v] = 0.0
        elif hi == lo:
            mapping[v] = 100.0
        else:
            mapping[v] = round((v - lo) / (hi - lo) * 100, 2)

    return mapping


def freshness_score(feed_last_updated: datetime | None) -> float:
    """100 if updated in the last 30 days, 75 / 90d, 50 / 180d, 25 / 1yr,
    0 if older or unknown — exactly the buckets specified for Improvement 1."""

    if feed_last_updated is None:
        return 0.0

    reference = feed_last_updated
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - reference).days

    if age_days <= 30:
        return 100.0
    if age_days <= 90:
        return 75.0
    if age_days <= 180:
        return 50.0
    if age_days <= 365:
        return 25.0
    return 0.0


def compute_readiness_status(
    quality_score: float | None,
    feed_last_updated: datetime | None,
    route_count: int | None,
    stop_count: int | None,
    reachable: bool,
) -> str:
    """"Ready" | "Needs Work" | "Dead Feed" per Improvement 2's exact rule.
    Dead Feed is checked first since it's a disqualifying/terminal state
    (an unreachable or multi-year-stale feed can't be "Needs Work")."""

    age_days: int | None = None
    if feed_last_updated is not None:
        reference = feed_last_updated
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - reference).days

    if not reachable or age_days is None or age_days > DEAD_MAX_STALE_DAYS:
        return "Dead Feed"

    is_ready = (
        quality_score is not None
        and quality_score > READY_MIN_QUALITY
        and age_days <= READY_MAX_STALE_DAYS
        and (route_count or 0) > READY_MIN_ROUTES
        and (stop_count or 0) > READY_MIN_STOPS
    )
    return "Ready" if is_ready else "Needs Work"


def run_enrichment() -> int:
    with SessionLocal() as session:
        unmatched = session.query(MobilityAgency).filter(MobilityAgency.is_covered.is_(None)).count()
        if unmatched:
            logger.warning(
                "%d mobility_agencies rows have is_covered = NULL (matching.py hasn't run yet "
                "for them) and will be skipped by this filter, not counted as uncovered",
                unmatched,
            )

        uncovered = (
            session.query(MobilityAgency).filter(MobilityAgency.is_covered.is_(False)).all()
        )

        if not uncovered:
            logger.info("No uncovered agencies to enrich")
            return 0

        realtime_source_ids = fetch_realtime_source_ids()

        enriched_rows = []
        for agency in uncovered:
            logger.info("Enriching agency %r (%s)", agency.name, agency.feed_url)

            feed_result = fetch_feed_quality(agency.feed_url)
            population = fetch_population(agency.country_code, agency.country)
            has_realtime = bool(agency.source_id and agency.source_id in realtime_source_ids)

            enriched_rows.append(
                {
                    "mobility_agency_id": agency.id,
                    "name": agency.name,
                    "country": agency.country,
                    "municipality": agency.municipality,
                    "feed_url": agency.feed_url,
                    "population": population,
                    "quality_score": feed_result["quality_score"],
                    "missing_files": feed_result["missing_files"],
                    "route_count": feed_result["route_count"],
                    "stop_count": feed_result["stop_count"],
                    "trip_count": feed_result["trip_count"],
                    "has_realtime": has_realtime,
                    "feed_last_updated": agency.feed_last_updated,
                    "_reachable": feed_result["reachable"],
                }
            )

        pop_map = normalize_populations([r["population"] for r in enriched_rows])
        route_map = normalize_linear([r["route_count"] for r in enriched_rows])

        for row in enriched_rows:
            normalised_population = pop_map[row["population"]]
            has_realtime_score = 100.0 if row["has_realtime"] else 0.0
            fresh_score = freshness_score(row["feed_last_updated"])
            network_size_score = route_map[row["route_count"]]

            row["opportunity_score"] = round(
                normalised_population * WEIGHT_POPULATION
                + row["quality_score"] * WEIGHT_QUALITY
                + has_realtime_score * WEIGHT_REALTIME
                + fresh_score * WEIGHT_FRESHNESS
                + network_size_score * WEIGHT_NETWORK_SIZE,
                2,
            )

            row["readiness_status"] = compute_readiness_status(
                row["quality_score"],
                row["feed_last_updated"],
                row["route_count"],
                row["stop_count"],
                row.pop("_reachable"),
            )

        # uncovered_agencies is fully re-derived from mobility_agencies +
        # live feed/population/realtime lookups each run, so it's cleared
        # first — same idempotency reasoning as transit_covered in Step 3.
        session.query(UncoveredAgency).delete()

        for row in enriched_rows:
            session.add(UncoveredAgency(created_at=datetime.utcnow(), **row))

        session.commit()

    ready_count = sum(1 for r in enriched_rows if r["readiness_status"] == "Ready")
    realtime_count = sum(1 for r in enriched_rows if r["has_realtime"])
    logger.info(
        "Enriched %d agencies: %d Ready, %d with realtime feeds",
        len(enriched_rows), ready_count, realtime_count,
    )

    return len(enriched_rows)


def main() -> None:
    count = run_enrichment()
    logger.info("Enriched %d uncovered agencies into uncovered_agencies", count)


if __name__ == "__main__":
    sys.exit(main())
