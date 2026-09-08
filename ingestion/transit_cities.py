# Step 3 — Get Transit app's covered cities.
#
# Scrapes https://transitapp.com/region for the list of cities/regions
# Transit supports and stores them in `transit_covered`.
#
# The real DOM of that page can't be inspected from this sandbox (no network
# access here — see LOG.md), so this scraper tries a few generic strategies
# in order of specificity and, if none of them find a plausible number of
# cities, falls back to a hardcoded list of ~50 well-known Transit-covered
# cities. That fallback is what keeps `python run.py` usable end-to-end even
# if the scraper's selectors need adjusting once run against the live page —
# adjust the selectors in `_STRATEGIES` after inspecting the real markup.

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from db.config import TRANSIT_REGIONS_URL
from db.models import TransitCovered
from db.session import SessionLocal
from ingestion.geo import country_name_to_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("transit_cities")

REQUEST_TIMEOUT = 30.0

# Minimum number of parsed entries before we trust the scrape over the
# fallback list. Transit covers hundreds of cities, so a "successful" parse
# that only found a handful of entries is more likely a broken selector than
# a genuinely short page.
MIN_PLAUSIBLE_CITIES = 20

# Hardcoded fallback — Transit's ~50 best-known covered cities. Compiled from
# general knowledge of Transit app's coverage, not scraped; treat as an
# approximation to correct once the live scrape is verified. See LOG.md.
FALLBACK_CITIES: list[tuple[str, str, str]] = [
    ("New York", "United States", "New York"),
    ("Los Angeles", "United States", "California"),
    ("Chicago", "United States", "Illinois"),
    ("San Francisco", "United States", "California"),
    ("Boston", "United States", "Massachusetts"),
    ("Washington", "United States", "District of Columbia"),
    ("Philadelphia", "United States", "Pennsylvania"),
    ("Seattle", "United States", "Washington"),
    ("Atlanta", "United States", "Georgia"),
    ("Miami", "United States", "Florida"),
    ("Denver", "United States", "Colorado"),
    ("Portland", "United States", "Oregon"),
    ("Minneapolis", "United States", "Minnesota"),
    ("Detroit", "United States", "Michigan"),
    ("Baltimore", "United States", "Maryland"),
    ("Pittsburgh", "United States", "Pennsylvania"),
    ("San Diego", "United States", "California"),
    ("Phoenix", "United States", "Arizona"),
    ("Dallas", "United States", "Texas"),
    ("Houston", "United States", "Texas"),
    ("Austin", "United States", "Texas"),
    ("Charlotte", "United States", "North Carolina"),
    ("Nashville", "United States", "Tennessee"),
    ("New Orleans", "United States", "Louisiana"),
    ("Salt Lake City", "United States", "Utah"),
    ("Sacramento", "United States", "California"),
    ("San Jose", "United States", "California"),
    ("Honolulu", "United States", "Hawaii"),
    ("Columbus", "United States", "Ohio"),
    ("Indianapolis", "United States", "Indiana"),
    ("Milwaukee", "United States", "Wisconsin"),
    ("Cincinnati", "United States", "Ohio"),
    ("Cleveland", "United States", "Ohio"),
    ("Kansas City", "United States", "Missouri"),
    ("St. Louis", "United States", "Missouri"),
    ("Tampa", "United States", "Florida"),
    ("Orlando", "United States", "Florida"),
    ("Las Vegas", "United States", "Nevada"),
    ("Toronto", "Canada", "Ontario"),
    ("Montreal", "Canada", "Quebec"),
    ("Vancouver", "Canada", "British Columbia"),
    ("Ottawa", "Canada", "Ontario"),
    ("Calgary", "Canada", "Alberta"),
    ("Edmonton", "Canada", "Alberta"),
    ("Winnipeg", "Canada", "Manitoba"),
    ("Quebec City", "Canada", "Quebec"),
    ("Halifax", "Canada", "Nova Scotia"),
    ("Victoria", "Canada", "British Columbia"),
    ("Paris", "France", "Île-de-France"),
    ("Sydney", "Australia", "New South Wales"),
]


def _parse_next_data(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    """Strategy 1: many modern sites (Next.js) embed their page data as JSON
    in a <script id="__NEXT_DATA__"> tag. If transitapp.com/region does this,
    walk the parsed JSON for dict entries that look like city records."""

    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return []

    try:
        data = json.loads(script.string)
    except ValueError:
        return []

    found: list[tuple[str, str, str]] = []

    def walk(node):
        if isinstance(node, dict):
            keys = {k.lower() for k in node.keys()}
            name_key = next((k for k in node if k.lower() in ("city", "city_name", "name")), None)
            country_key = next((k for k in node if k.lower() in ("country", "country_name")), None)
            if name_key and country_key and isinstance(node[name_key], str):
                region_key = next(
                    (k for k in node if k.lower() in ("region", "state", "province")), None
                )
                found.append(
                    (
                        node[name_key].strip(),
                        str(node[country_key]).strip(),
                        str(node[region_key]).strip() if region_key else "",
                    )
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _parse_headings_and_lists(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    """Strategy 2: a common pattern for this kind of coverage page is a
    country/region heading (h2/h3) followed by a <ul> of city links."""

    found: list[tuple[str, str, str]] = []
    for heading in soup.find_all(["h2", "h3"]):
        country = heading.get_text(strip=True)
        if not country:
            continue
        sibling_list = heading.find_next_sibling(["ul", "ol"])
        if not sibling_list:
            continue
        for item in sibling_list.find_all("li"):
            city = item.get_text(strip=True)
            if city:
                found.append((city, country, ""))
    return found


def _parse_city_links(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    """Strategy 3: fall back to any anchor tag that looks like it points at a
    per-city page (href containing '/city' or '/region/')."""

    found: list[tuple[str, str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if re.search(r"/(city|region)/", href):
            text = anchor.get_text(strip=True)
            if text:
                found.append((text, "", ""))
    return found


_STRATEGIES = [_parse_next_data, _parse_headings_and_lists, _parse_city_links]


def scrape_transit_cities() -> list[tuple[str, str, str]]:
    """Fetch transitapp.com/region and try each parsing strategy in turn.
    Returns an empty list (never raises) if the request fails or every
    strategy comes back below MIN_PLAUSIBLE_CITIES — the caller decides
    whether to use the hardcoded fallback."""

    try:
        resp = httpx.get(
            TRANSIT_REGIONS_URL,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; UnclaimedBot/1.0)"},
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Failed to fetch %s: %s", TRANSIT_REGIONS_URL, exc)
        return []

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to parse HTML from %s: %s", TRANSIT_REGIONS_URL, exc)
        return []

    for strategy in _STRATEGIES:
        try:
            entries = strategy(soup)
        except Exception as exc:  # noqa: BLE001 — one strategy failing tries the next
            logger.warning("Scrape strategy %s raised: %s", strategy.__name__, exc)
            continue

        if len(entries) >= MIN_PLAUSIBLE_CITIES:
            logger.info("Scrape strategy %s found %d entries", strategy.__name__, len(entries))
            return entries
        elif entries:
            logger.warning(
                "Scrape strategy %s only found %d entries (below threshold of %d), trying next strategy",
                strategy.__name__,
                len(entries),
                MIN_PLAUSIBLE_CITIES,
            )

    return []


def store_cities(cities: list[tuple[str, str, str]]) -> int:
    """Replaces the full contents of transit_covered on every run. This is a
    reference list re-derived from the source each time (not accumulated
    history like mobility_agencies), so clearing stale rows first keeps a
    re-run idempotent instead of piling up duplicates. See LOG.md."""

    written = 0
    with SessionLocal() as session:
        session.query(TransitCovered).delete()

        for city_name, country, region in cities:
            try:
                session.add(
                    TransitCovered(
                        city_name=city_name,
                        country=country or None,
                        country_code=country_name_to_code(country) if country else None,
                        region=region or None,
                        created_at=datetime.utcnow(),
                    )
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort the batch
                logger.error("Failed to store city %r: %s", city_name, exc)
                continue
        session.commit()
    return written


def main() -> None:
    cities = scrape_transit_cities()

    if not cities:
        logger.warning(
            "Scraping %s produced no usable results — falling back to the hardcoded "
            "top-%d Transit cities list",
            TRANSIT_REGIONS_URL,
            len(FALLBACK_CITIES),
        )
        cities = FALLBACK_CITIES

    written = store_cities(cities)
    logger.info("Stored %d cities in transit_covered", written)


if __name__ == "__main__":
    sys.exit(main())
