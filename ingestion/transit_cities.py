# Step 3 — Get Transit app's covered cities.
#
# Scrapes https://transitapp.com/region for the list of cities/regions
# Transit supports and stores them in `transit_covered`.
#
# The real page structure was inspected directly (see LOG.md — this replaced
# an earlier version written without network access, which mis-parsed the
# page): it's a Next.js app-router page with country sections as <h2>
# headings (flag emoji + name, e.g. "United States🇺🇸"), each followed by
# either region <h3> headings (flag/theme emoji + name, e.g. "Alabama🦋")
# whose next sibling <ul> holds that region's cities, or — for countries with
# no region breakdown (UK, Australia, most of Europe) — city <ul>s directly
# under the country with no <h3> in between. `_parse_region_hierarchy` walks
# h2/h3/city-link tags in document order as a small state machine to
# reconstruct country -> region -> city, and decodes the country's ISO code
# directly from its flag emoji (a flag is just two Unicode regional-indicator
# characters encoding the two letters) rather than guessing from text.
#
# Kept defensive despite now knowing the real structure: transitapp.com can
# still change its markup, so a hardcoded fallback list and a couple of
# looser strategies remain as a safety net (per the brief's "so the pipeline
# never breaks").

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
# fallback list. Transit covers well over a thousand cities (confirmed by
# inspecting the live page — see LOG.md), so a "successful" parse that only
# found a handful of entries is more likely a broken selector than a
# genuinely short page.
MIN_PLAUSIBLE_CITIES = 20

# A ("city", "country", "country_code", "region") entry. country_code is
# None for strategies that can't determine it directly (store_cities()
# falls back to ingestion.geo.country_name_to_code(country) in that case).
CityEntry = tuple[str, str, str | None, str | None]

# Regional-indicator flag emoji (two chars encoding the two ISO letters) plus
# the general emoji ranges transitapp.com decorates every heading with.
_FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]{2}")
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U0001F300-\U0001FAFF"  # symbols & pictographs
    "\U00002600-\U000027BF"  # misc symbols & dingbats
    "\U0001F000-\U0001F0FF"  # mahjong/dominoes/cards
    "\U0000FE0F"  # variation selector-16
    "\U0000200D"  # zero-width joiner
    "]+"
)

# Hardcoded fallback — Transit's ~50 best-known covered cities, used only if
# the live page is unreachable or every parsing strategy fails outright.
# Compiled from general knowledge, not scraped; see LOG.md.
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


def _decode_flag(text: str) -> str | None:
    """A flag emoji is two Unicode regional-indicator symbols, each offset
    from 'A'-'Z' by a fixed codepoint delta — decode it directly instead of
    guessing the country from display text."""

    match = _FLAG_RE.search(text)
    if not match:
        return None
    a, b = match.group()
    return chr(ord(a) - 0x1F1E6 + ord("A")) + chr(ord(b) - 0x1F1E6 + ord("A"))


def _clean_name(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _parse_region_hierarchy(soup: BeautifulSoup) -> list[CityEntry]:
    """Primary strategy, verified against the live page (see LOG.md).

    Walks <h2> (country), <h3> (region, optional), and city <a href="…/region/…">
    tags in document order as a state machine: each <h2> starts a new country
    (its ISO code decoded from its flag emoji), each <h3> starts a new region
    within it, and each city link is attributed to whichever country/region
    is currently open. This mirrors the page's actual nesting without relying
    on sibling/parent traversal, which the country and region levels don't
    use consistently (regions sit in a <ul> right after the <h3>; countries
    with no regions have their <ul>s further down inside a wrapping <div>)."""

    found: list[CityEntry] = []
    current_country: str | None = None
    current_country_code: str | None = None
    current_region: str | None = None

    # Excludes an <a> nested inside an <h2>/<h3> — on the live page headings
    # are plain text, but if a future markup change wraps a heading in its
    # own "/region/…" link, treating that as a city would wrongly add the
    # country/region's own name as one of its cities.
    nodes = soup.find_all(
        lambda tag: tag.name in ("h2", "h3")
        or (
            tag.name == "a"
            and tag.get("href")
            and re.search(r"/region/[^/]+", tag["href"])
            and not tag.find_parent(["h2", "h3"])
        )
    )

    for node in nodes:
        if node.name == "h2":
            raw = node.get_text()
            current_country = _clean_name(raw)
            current_country_code = _decode_flag(raw)
            current_region = None
        elif node.name == "h3":
            current_region = _clean_name(node.get_text())
        else:
            city = node.get_text(strip=True)
            if city and current_country:
                found.append((city, current_country, current_country_code, current_region))

    return found


def _parse_next_data(soup: BeautifulSoup) -> list[CityEntry]:
    """Fallback strategy: some Next.js pages embed page data as JSON in a
    <script id="__NEXT_DATA__"> tag (an older pattern than the one
    transitapp.com/region currently uses). Kept in case the site reverts."""

    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return []

    try:
        data = json.loads(script.string)
    except ValueError:
        return []

    found: list[CityEntry] = []

    def walk(node):
        if isinstance(node, dict):
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
                        None,
                        str(node[region_key]).strip() if region_key else None,
                    )
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _parse_city_links(soup: BeautifulSoup) -> list[CityEntry]:
    """Last-resort fallback: any anchor that looks like a per-city page link,
    with no country/region attribution at all (store_cities() will fail to
    resolve a country_code for these, so they end up excluded from
    matching — better than nothing if every structured strategy breaks)."""

    found: list[CityEntry] = []
    for anchor in soup.find_all("a", href=True):
        if re.search(r"/(city|region)/[^/]+", anchor["href"]):
            text = anchor.get_text(strip=True)
            if text:
                found.append((text, "", None, None))
    return found


_STRATEGIES = [_parse_region_hierarchy, _parse_next_data, _parse_city_links]


def scrape_transit_cities() -> list[CityEntry]:
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


def store_cities(cities: list[CityEntry]) -> int:
    """Replaces the full contents of transit_covered on every run. This is a
    reference list re-derived from the source each time (not accumulated
    history like mobility_agencies), so clearing stale rows first keeps a
    re-run idempotent instead of piling up duplicates. See LOG.md."""

    written = 0
    with SessionLocal() as session:
        session.query(TransitCovered).delete()

        for city_name, country, country_code, region in cities:
            try:
                session.add(
                    TransitCovered(
                        city_name=city_name,
                        country=country or None,
                        country_code=country_code or (country_name_to_code(country) if country else None),
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
        cities = [(city, country, None, region) for city, country, region in FALLBACK_CITIES]

    written = store_cities(cities)
    logger.info("Stored %d cities in transit_covered", written)


if __name__ == "__main__":
    sys.exit(main())
