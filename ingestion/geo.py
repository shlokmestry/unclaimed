# Shared helper: country display name -> ISO 3166-1 alpha-2 code.
#
# Used by mobility_db.py (fallback, if the API doesn't give us a country_code
# directly) and transit_cities.py (the scraped/fallback city list only has
# country names). Doing this locally with `pycountry` means Step 4's
# "country code as a primary filter" doesn't need a network call or an
# external geocoding API — pycountry ships its ISO data offline. See LOG.md.

from __future__ import annotations

import logging

import pycountry

logger = logging.getLogger("geo")

# pycountry's official names don't always match the common names sources use
# (e.g. Mobility Database / Transit app say "United States", pycountry's
# `name` is "United States of America"). A small manual override table is
# far more reliable here than fuzzy-matching country names.
_COUNTRY_NAME_OVERRIDES = {
    "united states": "US",
    "usa": "US",
    "u.s.a.": "US",
    "united states of america": "US",
    "uk": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "south korea": "KR",
    "north korea": "KP",
    "russia": "RU",
    "vietnam": "VN",
    "czech republic": "CZ",
    "czechia": "CZ",
    "bolivia": "BO",
    "venezuela": "VE",
    "tanzania": "TZ",
    "iran": "IR",
    "syria": "SY",
    "laos": "LA",
    "moldova": "MD",
    "brunei": "BN",
    "ivory coast": "CI",
    "cote d'ivoire": "CI",
}

_cache: dict[str, str | None] = {}


def country_name_to_code(name: str | None) -> str | None:
    """Best-effort country name -> ISO alpha-2 code. Returns None (and logs
    once per unresolved name) rather than raising, so a country we can't
    resolve just ends up unmatched in Step 4 instead of crashing ingestion."""

    if not name:
        return None

    key = name.strip().lower()
    if key in _cache:
        return _cache[key]

    code = _COUNTRY_NAME_OVERRIDES.get(key)

    if not code:
        try:
            match = pycountry.countries.lookup(name)
            code = match.alpha_2
        except LookupError:
            code = None

    if not code:
        logger.warning("Could not resolve country name %r to an ISO code", name)

    _cache[key] = code
    return code
