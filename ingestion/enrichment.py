# Step 5 — Enrich uncovered agencies.
#
# For every mobility_agencies row marked is_covered = False (from Step 4):
#   - downloads its GTFS feed zip and scores feed quality (0-100)
#   - looks up its country's population as a market-size proxy
#   - computes an opportunity_score blending the two
# and stores the result in `uncovered_agencies`.
#
# Every per-agency step (download, zip parsing, population lookup) is wrapped
# so one dead feed or one failed lookup degrades that agency's score instead
# of stopping the run.

from __future__ import annotations

import csv
import io
import logging
import math
import sys
import zipfile
from datetime import datetime, date

import httpx

from db.config import REST_COUNTRIES_BASE_URL
from db.models import MobilityAgency, UncoveredAgency
from db.session import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("enrichment")

FEED_TIMEOUT = 30.0
POPULATION_TIMEOUT = 15.0

REQUIRED_FILES = ["stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt"]
POINTS_PER_REQUIRED_FILE = 15  # 5 files * 15 = 75

# Population cache keyed by country_code (fallback: country name), shared
# across all agencies in a run so we hit REST Countries once per country
# rather than once per agency.
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


def score_feed(feed_bytes: bytes) -> tuple[float, list[str]]:
    """Returns (quality_score, missing_files) for one GTFS zip's raw bytes.
    Never raises — a corrupt zip or unreadable member scores 0 for that
    component instead of aborting the whole score."""

    score = 0.0
    missing = []

    try:
        zf = zipfile.ZipFile(io.BytesIO(feed_bytes))
    except zipfile.BadZipFile as exc:
        logger.error("Feed is not a valid zip: %s", exc)
        return 0.0, list(REQUIRED_FILES)

    try:
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
                if _count_rows(zf, members["stops.txt"]) > 10:
                    score += 10
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not count stops.txt rows: %s", exc)

        if "routes.txt" in members:
            try:
                if _count_rows(zf, members["routes.txt"]) > 3:
                    score += 10
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not count routes.txt rows: %s", exc)

        if "calendar.txt" in members:
            try:
                if _calendar_has_future_dates(zf, members["calendar.txt"]):
                    score += 5
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not evaluate calendar.txt future dates: %s", exc)

        return score, missing
    finally:
        zf.close()


def fetch_feed_quality(feed_url: str | None) -> tuple[float, list[str]]:
    if not feed_url:
        logger.warning("No feed_url — scoring 0")
        return 0.0, list(REQUIRED_FILES)

    try:
        with httpx.Client(timeout=FEED_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(feed_url)
            resp.raise_for_status()
            return score_feed(resp.content)
    except httpx.TimeoutException:
        logger.error("Feed download timed out after %ss: %s", FEED_TIMEOUT, feed_url)
        return 0.0, list(REQUIRED_FILES)
    except httpx.HTTPError as exc:
        logger.error("Feed download failed (%s): %s", feed_url, exc)
        return 0.0, list(REQUIRED_FILES)


def fetch_population(country_code: str | None, country: str | None) -> int | None:
    """Country population from REST Countries, used as a market-size proxy
    since city-level population data isn't reliably available. Cached per
    run so repeated agencies in the same country cost one HTTP call."""

    cache_key = country_code or country
    if not cache_key:
        return None
    if cache_key in _population_cache:
        return _population_cache[cache_key]

    url = (
        f"{REST_COUNTRIES_BASE_URL}/alpha/{country_code}"
        if country_code
        else f"{REST_COUNTRIES_BASE_URL}/name/{country}"
    )

    population = None
    try:
        resp = httpx.get(
            url, params={"fields": "population"}, timeout=POPULATION_TIMEOUT, follow_redirects=True
        )
        resp.raise_for_status()
        data = resp.json()
        record = data[0] if isinstance(data, list) else data
        population = record.get("population")
    except httpx.HTTPError as exc:
        logger.error("Population lookup failed for %r: %s", cache_key, exc)
    except (ValueError, IndexError, AttributeError) as exc:
        logger.error("Could not parse population response for %r: %s", cache_key, exc)

    _population_cache[cache_key] = population
    return population


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

        enriched_rows = []
        for agency in uncovered:
            logger.info("Enriching agency %r (%s)", agency.name, agency.feed_url)

            quality_score, missing_files = fetch_feed_quality(agency.feed_url)
            population = fetch_population(agency.country_code, agency.country)

            enriched_rows.append(
                {
                    "mobility_agency_id": agency.id,
                    "name": agency.name,
                    "country": agency.country,
                    "municipality": agency.municipality,
                    "feed_url": agency.feed_url,
                    "population": population,
                    "quality_score": quality_score,
                    "missing_files": missing_files,
                }
            )

        pop_map = normalize_populations([r["population"] for r in enriched_rows])

        for row in enriched_rows:
            normalised_population = pop_map[row["population"]]
            row["opportunity_score"] = round(
                normalised_population * 0.6 + row["quality_score"] * 0.4, 2
            )

        # uncovered_agencies is fully re-derived from mobility_agencies +
        # live feed/population lookups each run, so it's cleared first —
        # same idempotency reasoning as transit_covered in Step 3.
        session.query(UncoveredAgency).delete()

        for row in enriched_rows:
            session.add(UncoveredAgency(created_at=datetime.utcnow(), **row))

        session.commit()

    return len(enriched_rows)


def main() -> None:
    count = run_enrichment()
    logger.info("Enriched %d uncovered agencies into uncovered_agencies", count)


if __name__ == "__main__":
    sys.exit(main())
