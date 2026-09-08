# Build Log

Decisions made during the build that weren't explicitly specified in the brief.

## Step 1 — Docker + Postgres

- Used `postgres:15` (matches the required "Postgres 15") with a named volume
  `unclaimed_pgdata` so data survives container restarts.
- Added a Postgres healthcheck (`pg_isready`) and made the `api` service wait
  on `service_healthy` so the API container doesn't start before the DB is
  ready to accept connections.
- Put the Dockerfile at `api/Dockerfile` but set the build `context` to the
  repo root, since the API will need to import shared code from `ingestion/`
  (used in later steps for scheduled/triggered ingestion jobs from the API,
  if needed). requirements.txt lives at the repo root so both `api/` and
  `ingestion/` install from a single dependency list.
- Mounted `./api` and `./ingestion` as volumes into the `api` container for
  live-reload-friendly local development.
- `.env` (real secrets) is gitignored; `.env.example` is committed with
  placeholder values and documents every variable used by docker-compose.
- Left `MOBILITY_DATABASE_API_KEY` blank in `.env.example` — Step 2 will
  confirm whether the Mobility Database API actually requires auth for the
  read-only endpoints we need. If it does, I will stop and flag it per the
  brief's rule on APIs requiring authentication.
- `api/main.py` currently only exposes a `/health` endpoint as a smoke test
  for Step 1; the real endpoints (`/agencies`, `/agencies/{id}`, `/stats`)
  are built in Step 6.
- Created stub files for `ingestion/mobility_db.py`, `ingestion/transit_cities.py`,
  `ingestion/enrichment.py`, and `frontend/index.html` per the requested folder
  structure; each will be implemented in its corresponding step.
