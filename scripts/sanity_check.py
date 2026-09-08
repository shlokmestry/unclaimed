"""Read-only sanity-check for the pipeline's output tables.

Connects to the same Postgres database the ingestion pipeline
(mobility_db.py -> transit_cities.py -> matching.py -> enrichment.py) writes
to, using the same `db.session.SessionLocal` / `db.models` pattern as the
ingestion scripts, and runs a battery of data-quality checks against
`mobility_agencies`, `transit_covered`, and `uncovered_agencies`.

Every check below is a SELECT -- this script never inserts, updates, or
deletes a row. Safe to run at any point, including mid-pipeline-run (checks
that depend on a later step's output will simply report accordingly, e.g.
row counts of 0 before that step has run).

Prints one [PASS] / [WARN] / [FAIL] line per check plus a final one-line
overall verdict. A [FAIL] means something that should structurally never
happen (a broken invariant, an out-of-range score); a [WARN] means a value
that's *plausible* but suspicious enough to want a human's eyes on it before
trusting the data for a pitch/deployment.

Run inside the API container, same pattern as the other one-off scripts in
this repo (see README.md):

    docker-compose run --rm api python scripts/sanity_check.py
"""

from __future__ import annotations

import sys

from sqlalchemy import func

from db.models import MobilityAgency, TransitCovered, UncoveredAgency
from db.session import SessionLocal

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# Thresholds for the "suspicious value" WARN checks below. Not specified by
# the brief -- chosen conservatively so a real bug trips them while normal
# data variance doesn't.
SUSPICIOUS_POPULATION_NULL_FRACTION = 0.5  # >50% NULL population -> WARN
SUSPICIOUS_COUNTRY_CODE_MIN_FRACTION = 0.9  # <90% non-null country_code -> WARN


class Report:
    """Tiny accumulator so the final verdict line can tally every check
    without each check having to thread counts through by hand."""

    def __init__(self) -> None:
        self.counts = {PASS: 0, WARN: 0, FAIL: 0}

    def record(self, status: str, label: str, detail: str = "") -> None:
        self.counts[status] += 1
        line = f"[{status}] {label}"
        if detail:
            line += f" -- {detail}"
        print(line)

    def section(self, title: str) -> None:
        print(f"\n--- {title} ---")

    def verdict(self) -> int:
        print("\n" + "=" * 70)
        print(
            f"OVERALL: {self.counts[PASS]} checks passed, "
            f"{self.counts[WARN]} warnings, {self.counts[FAIL]} failures"
        )
        print("=" * 70)
        return 1 if self.counts[FAIL] else 0


def fmt_pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a (0 rows)"
    return f"{numerator}/{denominator} ({100.0 * numerator / denominator:.1f}%)"


def fmt_row(name, country, quality_score, population, opportunity_score) -> str:
    name_str = (name or "(no name)")[:40]
    country_str = country or "?"
    quality_str = "?" if quality_score is None else f"{quality_score}"
    population_str = "?" if population is None else f"{population}"
    opportunity_str = "?" if opportunity_score is None else f"{opportunity_score}"
    return (
        f"{name_str:<40} {country_str:<20} "
        f"quality={quality_str:<8} population={population_str:<12} "
        f"opportunity={opportunity_str}"
    )


def main() -> int:
    report = Report()

    with SessionLocal() as session:
        # ---- 1. Row count sanity ----
        report.section("1. Row counts")
        mobility_count = session.query(func.count(MobilityAgency.id)).scalar() or 0
        transit_count = session.query(func.count(TransitCovered.id)).scalar() or 0
        uncovered_count = session.query(func.count(UncoveredAgency.id)).scalar() or 0

        report.record(
            PASS if mobility_count > 0 else FAIL,
            "mobility_agencies row count > 0",
            f"count={mobility_count}",
        )
        report.record(
            PASS if transit_count > 0 else FAIL,
            "transit_covered row count > 0",
            f"count={transit_count}",
        )
        report.record(
            PASS if uncovered_count > 0 else FAIL,
            "uncovered_agencies row count > 0",
            f"count={uncovered_count}",
        )

        mobility_uncovered_count = (
            session.query(func.count(MobilityAgency.id))
            .filter(MobilityAgency.is_covered.is_(False))
            .scalar()
            or 0
        )
        report.record(
            PASS if uncovered_count == mobility_uncovered_count else FAIL,
            "uncovered_agencies count matches mobility_agencies WHERE is_covered=False",
            f"uncovered_agencies={uncovered_count}, "
            f"mobility_agencies(is_covered=False)={mobility_uncovered_count}",
        )

        # ---- 2. No unmatched (is_covered IS NULL) rows ----
        report.section("2. Matching completeness")
        unmatched = (
            session.query(func.count(MobilityAgency.id))
            .filter(MobilityAgency.is_covered.is_(None))
            .scalar()
            or 0
        )
        report.record(
            PASS if unmatched == 0 else FAIL,
            "no mobility_agencies rows with is_covered IS NULL",
            f"unmatched={unmatched} (matching.py should have run on every row)",
        )

        # ---- 3. Score bounds: uncovered_agencies.quality_score / opportunity_score ----
        report.section("3. Score bounds (uncovered_agencies)")
        for col, label in (
            (UncoveredAgency.quality_score, "quality_score"),
            (UncoveredAgency.opportunity_score, "opportunity_score"),
        ):
            out_of_bounds = (
                session.query(func.count(UncoveredAgency.id))
                .filter((col < 0) | (col > 100))
                .scalar()
                or 0
            )
            report.record(
                PASS if out_of_bounds == 0 else FAIL,
                f"uncovered_agencies.{label} within [0, 100]",
                f"out_of_bounds={out_of_bounds}",
            )

            null_count = (
                session.query(func.count(UncoveredAgency.id))
                .filter(col.is_(None))
                .scalar()
                or 0
            )
            report.record(
                PASS if null_count == 0 else WARN,
                f"uncovered_agencies.{label} has no NULLs",
                f"null_count={null_count} (enrichment.py always computes a value; NULLs would be unexpected)",
            )

        # ---- 4. match_confidence bounds ----
        report.section("4. Score bounds (mobility_agencies)")
        mc_out_of_bounds = (
            session.query(func.count(MobilityAgency.id))
            .filter(
                (MobilityAgency.match_confidence < 0)
                | (MobilityAgency.match_confidence > 100)
            )
            .scalar()
            or 0
        )
        report.record(
            PASS if mc_out_of_bounds == 0 else FAIL,
            "mobility_agencies.match_confidence within [0, 100]",
            f"out_of_bounds={mc_out_of_bounds}",
        )

        # ---- 5. No duplicate uncovered_agencies per mobility_agency_id ----
        report.section("5. Duplicate uncovered_agencies rows")
        dup_rows = (
            session.query(
                UncoveredAgency.mobility_agency_id, func.count(UncoveredAgency.id)
            )
            .group_by(UncoveredAgency.mobility_agency_id)
            .having(func.count(UncoveredAgency.id) > 1)
            .all()
        )
        report.record(
            PASS if not dup_rows else FAIL,
            "uncovered_agencies is 1:1 with mobility_agency_id",
            f"duplicate_mobility_agency_ids={len(dup_rows)}"
            + (f" (e.g. {dup_rows[:5]})" if dup_rows else ""),
        )

        # ---- 6. Coverage distribution ----
        report.section("6. Coverage distribution")
        covered_count = (
            session.query(func.count(MobilityAgency.id))
            .filter(MobilityAgency.is_covered.is_(True))
            .scalar()
            or 0
        )
        matched_total = covered_count + mobility_uncovered_count
        print(
            f"    covered={fmt_pct(covered_count, mobility_count)}, "
            f"uncovered={fmt_pct(mobility_uncovered_count, mobility_count)}, "
            f"unmatched(is_covered IS NULL)={fmt_pct(unmatched, mobility_count)}"
        )
        if matched_total == 0:
            report.record(
                WARN,
                "covered fraction is computable",
                "no rows with is_covered set yet -- matching.py may not have run",
            )
        else:
            covered_fraction = covered_count / matched_total
            if covered_fraction in (0.0, 1.0):
                report.record(
                    WARN,
                    "covered fraction is not suspiciously 0% or 100%",
                    f"covered_fraction={covered_fraction:.1%} of matched rows -- "
                    "this exact failure mode happened before during this build "
                    "(see LOG.md, Step 3 country-code bug: 0/4,548 covered)",
                )
            else:
                report.record(
                    PASS,
                    "covered fraction is not suspiciously 0% or 100%",
                    f"covered_fraction={covered_fraction:.1%} of matched rows",
                )

        # ---- 7. population NULL fraction ----
        report.section("7. Population coverage (uncovered_agencies)")
        pop_null = (
            session.query(func.count(UncoveredAgency.id))
            .filter(UncoveredAgency.population.is_(None))
            .scalar()
            or 0
        )
        if uncovered_count == 0:
            report.record(
                WARN, "population NULL fraction is computable", "no uncovered_agencies rows"
            )
        else:
            pop_null_fraction = pop_null / uncovered_count
            status = WARN if pop_null_fraction > SUSPICIOUS_POPULATION_NULL_FRACTION else PASS
            report.record(
                status,
                "population NULL fraction is not suspiciously high",
                f"{fmt_pct(pop_null, uncovered_count)} NULL "
                f"(threshold: >{SUSPICIOUS_POPULATION_NULL_FRACTION:.0%})",
            )

        # ---- 8. country_code coverage ----
        report.section("8. country_code coverage")
        mobility_cc = (
            session.query(func.count(MobilityAgency.id))
            .filter(MobilityAgency.country_code.isnot(None))
            .scalar()
            or 0
        )
        transit_cc = (
            session.query(func.count(TransitCovered.id))
            .filter(TransitCovered.country_code.isnot(None))
            .scalar()
            or 0
        )
        for label, non_null, total in (
            ("mobility_agencies.country_code", mobility_cc, mobility_count),
            ("transit_covered.country_code", transit_cc, transit_count),
        ):
            if total == 0:
                report.record(WARN, f"{label} non-null fraction is computable", "0 rows")
                continue
            fraction = non_null / total
            status = WARN if fraction < SUSPICIOUS_COUNTRY_CODE_MIN_FRACTION else PASS
            report.record(
                status,
                f"{label} non-null fraction is not surprisingly low",
                f"{fmt_pct(non_null, total)} non-null "
                f"(threshold: <{SUSPICIOUS_COUNTRY_CODE_MIN_FRACTION:.0%})",
            )

        # ---- 9. Top / bottom opportunity_score ----
        report.section("9. Top 10 / bottom 10 by opportunity_score (for eyeballing)")
        cols = (
            UncoveredAgency.name,
            UncoveredAgency.country,
            UncoveredAgency.quality_score,
            UncoveredAgency.population,
            UncoveredAgency.opportunity_score,
        )
        top10 = (
            session.query(*cols)
            .filter(UncoveredAgency.opportunity_score.isnot(None))
            .order_by(UncoveredAgency.opportunity_score.desc())
            .limit(10)
            .all()
        )
        bottom10 = (
            session.query(*cols)
            .filter(UncoveredAgency.opportunity_score.isnot(None))
            .order_by(UncoveredAgency.opportunity_score.asc())
            .limit(10)
            .all()
        )
        print("  Top 10:")
        for row in top10:
            print("   ", fmt_row(*row))
        print("  Bottom 10:")
        for row in bottom10:
            print("   ", fmt_row(*row))

        if uncovered_count == 0:
            report.record(
                WARN,
                "top/bottom opportunity_score rows available for review",
                "no uncovered_agencies rows to rank",
            )
        elif not top10:
            report.record(
                WARN,
                "top/bottom opportunity_score rows available for review",
                "uncovered_agencies rows exist but none have a non-null opportunity_score",
            )
        else:
            report.record(
                PASS,
                "top/bottom opportunity_score rows printed for manual review",
                "informational -- eyeball the lists above for sanity, no automatic pass/fail on ranking quality",
            )

        # ---- 10. Missing feed_url ----
        report.section("10. Missing feed_url (uncovered_agencies)")
        missing_feed_url = (
            session.query(func.count(UncoveredAgency.id))
            .filter(
                (UncoveredAgency.feed_url.is_(None)) | (UncoveredAgency.feed_url == "")
            )
            .scalar()
            or 0
        )
        report.record(
            WARN if missing_feed_url > 0 else PASS,
            "uncovered_agencies with missing/empty feed_url",
            f"{fmt_pct(missing_feed_url, uncovered_count)} -- these can't be enriched with a real quality_score",
        )

    return report.verdict()


if __name__ == "__main__":
    sys.exit(main())
