# Step 8 — Pipeline runner.
#
# Runs the full pipeline in order:
#   1. Create all database tables
#   2. Mobility Database ingestion
#   3. Transit covered-cities scraping
#   4. Matching
#   5. Enrichment
#
# Each step's start/end time is logged. If a step raises, the error is logged
# and the pipeline stops — later steps depend on earlier ones having written
# their tables, so continuing past a failed step would just fail louder.

import logging
import sys
from datetime import datetime

from db.models import Base
from db.session import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run")


def create_tables():
    Base.metadata.create_all(bind=engine)


def run_step(step_name: str, func) -> None:
    start = datetime.utcnow()
    logger.info("=== Starting: %s (%s) ===", step_name, start.isoformat())
    try:
        func()
    except Exception:
        logger.exception("Step %r failed — stopping pipeline", step_name)
        raise
    end = datetime.utcnow()
    logger.info(
        "=== Finished: %s (%s, took %.1fs) ===",
        step_name,
        end.isoformat(),
        (end - start).total_seconds(),
    )


def main() -> int:
    from ingestion import enrichment, matching, mobility_db, transit_cities

    steps = [
        ("Create database tables", create_tables),
        ("Mobility Database ingestion", mobility_db.main),
        ("Transit covered-cities scraping", transit_cities.main),
        ("Matching", matching.main),
        ("Enrichment", enrichment.main),
    ]

    for name, func in steps:
        try:
            run_step(name, func)
        except Exception:
            return 1

    logger.info("Pipeline complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
