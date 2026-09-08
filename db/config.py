# Shared configuration for both the ingestion scripts and the API.
#
# Loads `.env` once (via python-dotenv) so every entrypoint (run.py, the
# individual ingestion scripts run standalone, and the API) sees the same
# environment regardless of how it was invoked.

import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://unclaimed:changeme@localhost:5432/unclaimed"
)

# Async driver variant of DATABASE_URL, used by the FastAPI app. SQLAlchemy's
# async engine needs the `+asyncpg` dialect; the rest of the codebase (Docker
# env vars, docker-compose, ingestion scripts) only ever deals with the plain
# `postgresql://` form, so we derive the async URL here instead of asking for
# a second env var.
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    ASYNC_DATABASE_URL = DATABASE_URL
elif DATABASE_URL.startswith("postgresql://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
else:
    ASYNC_DATABASE_URL = DATABASE_URL

MOBILITY_API_TOKEN = os.environ.get("MOBILITY_API_TOKEN", "")

MOBILITY_API_BASE_URL = os.environ.get(
    "MOBILITY_API_BASE_URL", "https://api.mobilitydatabase.org/v1"
)

TRANSIT_REGIONS_URL = os.environ.get(
    "TRANSIT_REGIONS_URL", "https://transitapp.com/region"
)

REST_COUNTRIES_BASE_URL = os.environ.get(
    "REST_COUNTRIES_BASE_URL", "https://restcountries.com/v3.1"
)

# Confidence threshold (0-100) above which a mobility agency's municipality is
# considered matched to a Transit-covered city (Step 4).
MATCH_CONFIDENCE_THRESHOLD = float(os.environ.get("MATCH_CONFIDENCE_THRESHOLD", "85"))
