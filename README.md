# Unclaimed

Unclaimed — finds transit agencies with public GTFS feeds not yet covered by Transit app, ranked by opportunity.

## How it works

- Pulls every public GTFS feed from the [Mobility Database](https://mobilitydatabase.org), and separately gets the list of cities Transit app already covers, then fuzzy-matches agencies to covered cities (country-code-filtered, `rapidfuzz` scored) to find the ones Transit doesn't serve yet.
- Scores each uncovered agency's feed quality (0-100, based on which required GTFS files are present, row counts, and whether service dates extend into the future) and pulls its country's population as a market-size proxy.
- Ranks every uncovered agency by an opportunity score — 60% normalized population, 40% feed quality — so the dashboard surfaces the biggest, best-documented gaps first.

## Manual setup

The pipeline needs one credential you have to obtain yourself (everything
else runs with no manual steps):

- **Mobility Database token** — sign up free at https://mobilitydatabase.org,
  copy your refresh token from your account settings, and set it as
  `MOBILITY_API_TOKEN` in `.env`. `ingestion/mobility_db.py` exchanges it for
  a short-lived access token at request time — see `LOG.md`.

## Run locally

```
cp .env.example .env
# fill in MOBILITY_API_TOKEN in .env — see "Manual setup" above
docker-compose up --build
```

- API: http://localhost:8000 (`/agencies`, `/agencies/{id}`, `/stats`, `/health`)
- Postgres: localhost:5432
- Frontend: open `frontend/index.html` directly in a browser (it's a static file — no server needed; it talks to the API via `fetch`)

Then run the pipeline (creates tables + pulls/matches/enriches data) inside the API container, so it shares the container's network access to `db`:

```
docker-compose run --rm api python run.py
```

Re-run `run.py` any time to refresh the data — every step is idempotent (re-running upserts/replaces rather than duplicating rows).

## Running tests

```
python3 -m pip install --user pytest httpx
python3 -m pytest tests/
```

Most of `tests/` are unit tests that don't need anything running. `tests/test_api_integration.py` is different — it's a black-box smoke test suite that makes real HTTP requests against a live API instance, so `docker-compose up` (or an equivalent deployment) needs to be running first:

```
docker-compose up --build   # in another terminal
python3 -m pytest tests/test_api_integration.py -v
```

By default it targets `http://localhost:8000`. To run it against a deployed instance instead (e.g. Railway), point it at that URL with `API_BASE_URL`:

```
API_BASE_URL=https://your-app.up.railway.app python3 -m pytest tests/test_api_integration.py -v
```

## Live URL

YOUR_RAILWAY_URL

## Project layout

```
db/            shared SQLAlchemy models + sync/async sessions (ingestion + API)
ingestion/     Steps 2-5: mobility_db.py, transit_cities.py, matching.py, enrichment.py
api/           Step 6: FastAPI backend
frontend/      Step 7: static dashboard (index.html)
run.py         Step 8: runs the full pipeline in order
```

See `LOG.md` for build decisions not explicitly specified in the original brief.
