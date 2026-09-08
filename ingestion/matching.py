# Step 4 — Matching logic.
#
# For every mobility_agencies row, fuzzy-matches its municipality against
# Transit's covered cities (transit_covered) and writes is_covered,
# match_confidence, matched_to back onto the row.

from __future__ import annotations

import logging
import re
import sys
from collections import defaultdict

from rapidfuzz import fuzz

from db.config import MATCH_CONFIDENCE_THRESHOLD
from db.models import MobilityAgency, TransitCovered
from db.session import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("matching")

# Words stripped out during normalization because they're generic transit/
# administrative terms that add noise to a fuzzy match rather than signal
# (e.g. "Chicago Transit Authority" vs "Chicago" should still match highly).
# The brief named city/metro/transit/authority explicitly; the rest are
# reasonable additions in the same spirit — see LOG.md.
STOPWORDS = {
    "city",
    "metro",
    "metropolitan",
    "transit",
    "transportation",
    "authority",
    "district",
    "municipality",
    "municipal",
    "county",
    "region",
    "regional",
    "township",
    "area",
}

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize(name: str | None) -> str:
    """lowercase -> strip punctuation -> drop stopwords -> collapse whitespace."""

    if not name:
        return ""

    text = name.lower()
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in text.split() if t and t not in STOPWORDS]
    text = " ".join(tokens)
    return _WHITESPACE_RE.sub(" ", text).strip()


def build_covered_index(covered_rows: list[TransitCovered]) -> dict[str, list[tuple[str, str]]]:
    """country_code -> list of (normalized_city_name, original_city_name).
    Rows with no country_code are dropped from the index — they don't
    de-risk cross-country matching per the brief's "country code as a
    primary filter", so we can't safely candidate-pool them, and are logged
    once as a count rather than per-row noise."""

    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    skipped = 0
    for row in covered_rows:
        if not row.country_code:
            skipped += 1
            continue
        norm = normalize(row.city_name)
        if norm:
            index[row.country_code].append((norm, row.city_name))

    if skipped:
        logger.warning(
            "%d transit_covered rows have no country_code and were excluded from matching",
            skipped,
        )
    return index


def best_match(
    normalized_municipality: str, candidates: list[tuple[str, str]]
) -> tuple[str | None, float]:
    """Returns (best original city name, best WRatio score) among candidates,
    or (None, 0.0) if there are no candidates to compare against."""

    if not normalized_municipality or not candidates:
        return None, 0.0

    best_name = None
    best_score = 0.0
    for norm_candidate, original_candidate in candidates:
        score = fuzz.WRatio(normalized_municipality, norm_candidate)
        if score > best_score:
            best_score = score
            best_name = original_candidate

    return best_name, best_score


def run_matching() -> list[dict]:
    """Runs the full match and updates mobility_agencies in place. Returns
    the list of per-row match results (for logging) — not persisted beyond
    that."""

    with SessionLocal() as session:
        agencies = session.query(MobilityAgency).all()
        covered_rows = session.query(TransitCovered).all()
        covered_index = build_covered_index(covered_rows)

        results = []
        missing_country_code = 0

        for agency in agencies:
            if not agency.country_code:
                missing_country_code += 1
                agency.is_covered = False
                agency.match_confidence = 0.0
                agency.matched_to = None
                results.append(
                    {
                        "name": agency.name,
                        "municipality": agency.municipality,
                        "matched_to": None,
                        "confidence": 0.0,
                        "is_covered": False,
                    }
                )
                continue

            candidates = covered_index.get(agency.country_code, [])
            normalized_municipality = normalize(agency.municipality)
            matched_name, score = best_match(normalized_municipality, candidates)

            is_covered = score >= MATCH_CONFIDENCE_THRESHOLD

            agency.is_covered = is_covered
            agency.match_confidence = round(score, 2)
            # Kept even below threshold — useful for auditing near-misses —
            # but only when there was at least one candidate in-country.
            agency.matched_to = matched_name

            results.append(
                {
                    "name": agency.name,
                    "municipality": agency.municipality,
                    "matched_to": matched_name,
                    "confidence": round(score, 2),
                    "is_covered": is_covered,
                }
            )

        session.commit()

    if missing_country_code:
        logger.warning(
            "%d mobility_agencies rows have no country_code and were marked uncovered by default",
            missing_country_code,
        )

    return results


def main() -> None:
    results = run_matching()

    covered = sum(1 for r in results if r["is_covered"])
    uncovered = len(results) - covered

    logger.info("Matching complete: %d covered, %d uncovered (of %d total)", covered, uncovered, len(results))

    examples = results[:10]
    logger.info("Example matches:")
    for r in examples:
        logger.info(
            "  %r (municipality=%r) -> matched_to=%r confidence=%.2f is_covered=%s",
            r["name"],
            r["municipality"],
            r["matched_to"],
            r["confidence"],
            r["is_covered"],
        )


if __name__ == "__main__":
    sys.exit(main())
