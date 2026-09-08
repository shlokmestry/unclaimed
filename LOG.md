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

## Shared `db/` package (introduced in Step 2)

- Added a top-level `db/` package (`models.py`, `session.py`, `async_session.py`,
  `config.py`) instead of duplicating SQLAlchemy setup inside `ingestion/` and
  `api/`. Table metadata is engine-agnostic in SQLAlchemy 2.0, so one set of ORM
  models is shared by the sync engine (ingestion scripts, table creation in
  `run.py`) and the async engine (the API, per Step 6's requirement).
- `db/config.py` centralizes `.env` loading and derives the API's
  `postgresql+asyncpg://` URL from the single `DATABASE_URL` env var, so
  docker-compose/`.env` only need to define one connection string.
- Code targets Python 3.11 (matches `api/Dockerfile`'s base image) and uses
  `X | None` union type hints throughout. Only run these scripts inside the
  Docker container (`docker-compose run --rm api python run.py`) — besides the
  Python version, `DATABASE_URL` points at the Postgres hostname `db`, which
  only resolves inside the compose network.

## Step 2 — Mobility Database ingestion

- **Auth flow differs from a literal reading of the brief.** The Mobility
  Database API doesn't accept the long-lived token from your account directly
  as a bearer token — that token is a *refresh* token, exchanged for a
  short-lived access token via `POST /v1/tokens`. `mobility_db.py` does that
  exchange internally so `MOBILITY_API_TOKEN` (the refresh token) is still the
  only thing you need to set, matching the brief's intent even though the
  literal mechanism (`Authorization: Bearer <MOBILITY_API_TOKEN>` directly) is
  not what the real API expects.
- No internet access was available in this sandbox, so the exact JSON shape of
  `/v1/gtfs_feeds` responses (and of the `/v1/tokens` exchange) could not be
  verified against a live call. The parsing (`parse_feed`) is written against
  the documented Mobility Database Catalog schema as best known, but every
  field lookup uses `.get()` with a fallback and a page whose shape doesn't
  match falls back to an empty page (logged) rather than crashing. **Flag for
  verification**: once you have a real token, run Step 2 alone first and check
  the row counts/field values in `mobility_agencies` look right before running
  the full pipeline — if field names have drifted, only `parse_feed()` and
  `_feed_url()` in `mobility_db.py` need adjusting.
- Added a `source_id` column (the Mobility Database's own feed id) and a
  `country_code` column to `mobility_agencies`, beyond the columns literally
  listed in the brief:
  - `source_id` + a unique constraint makes re-running ingestion an upsert
    instead of appending duplicate rows every run.
  - `country_code` (ISO 3166-1 alpha-2) is needed for Step 4's "country code
    as a primary filter" requirement — pulled from the feed's `locations[0]`
    when present, else derived from the country name via `ingestion/geo.py`.
- A feed can list multiple `locations`; only the first is used per agency
  (the table has one row per feed, not per location).
- `feed_url` prefers the latest dataset's `hosted_url` (a stable, MobilityData-
  hosted copy of the GTFS zip) over `source_info.producer_url` (the agency's
  own URL, more likely to be dead) since Step 5 downloads this URL directly.

## Step 3 — Transit covered cities

- This sandbox has no internet access, so `transitapp.com/region`'s real DOM
  could not be inspected. `transit_cities.py` tries three generic scraping
  strategies in order (a `__NEXT_DATA__` JSON blob, heading+list grouping,
  city-link anchors) and only trusts a strategy's result if it finds at least
  `MIN_PLAUSIBLE_CITIES` (20) entries — otherwise it falls through to the next
  strategy, and finally to the hardcoded fallback list. **Flag for
  verification**: after running Step 3 against the live page, check
  `transit_covered`'s row count and a few sample rows; if it's suspiciously
  small, the live DOM doesn't match any of the three strategies and the
  selectors in `_STRATEGIES` need to be rewritten by hand against the real
  markup (view-source is enough — no scraping framework changes needed).
- The hardcoded fallback list (~50 cities) was compiled from general knowledge
  of which cities Transit app supports, not scraped or verified against a live
  source — treat it as an approximation. It's a safety net for pipeline
  reliability (per the brief's "so the pipeline never breaks"), not a
  source of truth; correct/expand it if the real Transit coverage differs.
- `transit_covered` is fully replaced (cleared, then re-inserted) on every run
  rather than accumulated, since it's a reference snapshot re-derived from the
  source each time, not history. Same reasoning applied to `uncovered_agencies`
  in Step 5.
- Added `country_code` to `transit_covered` (not in the brief's literal column
  list) for the same reason as `mobility_agencies.country_code` in Step 2 —
  Step 4 needs a code, not a free-text name, to filter on.

## Step 4 — Matching

- `STOPWORDS` includes the four words the brief named ("city", "metro",
  "transit", "authority") plus several more in the same spirit
  (metropolitan, transportation, district, municipality/municipal, county,
  region/regional, township, area) — these are all generic administrative/
  transit terms that add noise to a fuzzy match rather than signal.
- Mobility agencies (or Transit-covered cities) with no resolvable
  `country_code` are excluded from candidate matching entirely — the brief
  asks for country code as "a primary filter... to avoid false positives",
  so an unknown country is treated as unmatchable (marked `is_covered=False`,
  `match_confidence=0`) rather than risking a cross-country false match.
- `matched_to` and `match_confidence` are stored for the best in-country
  candidate even when the score is below the 85 threshold (so `is_covered`
  is `False` but `matched_to`/`match_confidence` are still populated) — useful
  for auditing near-misses; not in the brief but harmless since `is_covered`
  is still the authoritative field.

## Step 5 — Enrichment

- **Population normalization uses log10 scaling before min-max**, not a
  direct linear min-max of raw population. Country population spans several
  orders of magnitude (low millions to 1B+); a linear min-max would crush
  nearly every country to a score near 0 relative to the largest one or two.
  Log-scaling first spreads the 0-100 range meaningfully across the actual
  distribution of countries in the data while still normalizing to 0-100 as
  specified.
- Population lookups are cached per country for the duration of one
  enrichment run (REST Countries is called once per distinct country, not
  once per agency).
- `missing_files` is stored as a native Postgres `JSON` column (a list of
  filenames) rather than a delimited string, so the API/frontend can consume
  it directly without a parsing step.
- A required GTFS file is located by filename anywhere in the zip (top-level
  or inside a subfolder, e.g. `some-agency/stops.txt`), since GTFS zips
  commonly nest their contents in one directory.
- `calendar.txt` "has future dates" is judged by any row's `end_date` being
  today or later (string-compared as `YYYYMMDD`, which sorts correctly for
  same-length date strings) — not `start_date`, since a feed with only past
  start dates but an end date in the future still represents active service.
- **Bug found during the first live pipeline run, fixed**: REST Countries
  (`restcountries.com`) now 301-redirects `/v3.1/...` requests to a new host
  (`files-03.restcountries.com/...`), and `httpx.get()` doesn't follow
  redirects by default. Every population lookup was silently failing (caught,
  logged, `population=None`) until `follow_redirects=True` was added to the
  request in `fetch_population()`.

## Step 3 — corrected after inspecting the live page (post-first-run)

The first live pipeline run completed with **0 covered agencies out of 4,548**
— clearly wrong (the brief's whole premise is that most agencies *are*
covered somewhere). Root cause was in the original (pre-network-access)
`_parse_headings_and_lists` strategy: the real page nests **country (`<h2>`)
→ region (`<h3>`, optional) → cities (`<ul><li>`)**, but that strategy
treated every `<h2>`/`<h3>` heading as a country indiscriminately. For the US
section, that meant state headings like `"Alabama🦋"` got stored as the
*country* — `country_name_to_code()` correctly failed to resolve
`"Alabama🦋"` to anything, `country_code` came back null for 916/918
`transit_covered` rows, and Step 4's country-code filter then had essentially
no real candidates to match against for any country.

Fixed by fetching and inspecting the real page (network access to the actual
site is available in this environment, unlike the earlier assumption — see
the chat) and rewriting `transit_cities.py`'s primary strategy
(`_parse_region_hierarchy`) around the confirmed structure:

- `<h2>` = country, `<h3>` = region (always, regardless of the decorative
  emoji each carries) — walked in document order as a state machine rather
  than via sibling/parent lookups, since regions sit in a `<ul>` immediately
  after their `<h3>` but countries with no region breakdown (UK, Australia,
  most of Europe) have their `<ul>` further down inside a wrapping `<div>`,
  not as the `<h2>`'s next sibling.
- Every country heading carries a **flag emoji**, which is just two Unicode
  regional-indicator characters encoding the ISO alpha-2 code — decoded
  directly (`_decode_flag`) instead of guessing the code from display text.
  This is more reliable than the `pycountry`-based `country_name_to_code()`
  fallback (still used for the hardcoded FALLBACK_CITIES list, which has no
  emoji to decode) and sidesteps name-mismatch issues entirely for the
  scraped path.
- Verified against a real fetch of the live page: 1,228 cities across 37
  countries, all with a correctly decoded country code, before trusting the
  rewrite enough to re-run the pipeline.
- The old `_parse_headings_and_lists` strategy was removed outright (proven
  wrong for this real site, and keeping it as a "fallback" risked silently
  reintroducing the exact same bug) rather than kept as a lower-priority
  strategy.

## Code review pass (before deployment)

Ran a full review of Steps 2-10 while the corrected enrichment run was in
progress. Fixed:

- **XSS in `frontend/index.html`**: agency `name`/`country`/`municipality`/
  `feed_url` come from external GTFS feed metadata (not sanitized anywhere
  upstream) and were interpolated directly into `innerHTML`, with `feed_url`
  also inlined into an `onclick` attribute. Added an `escapeHtml()` helper for
  the text fields and switched the "View Feed" button to a `data-feed-url`
  attribute read by a delegated click handler instead of inline `onclick`.
- **CORS**: `allow_credentials=True` combined with `allow_origins=["*"]` is an
  invalid combination for credentialed requests and was pointless anyway (the
  API has no cookies/auth) — set to `False`.
- **`db/config.py`**: `ASYNC_DATABASE_URL` derivation only matched
  `postgresql://` exactly; generalized to a regex that also handles
  `postgresql+<anydriver>://` so a non-default sync driver in `DATABASE_URL`
  doesn't silently produce a broken async URL.
- **`ingestion/enrichment.py`**: `score_feed()`'s `ZipFile` was never closed;
  wrapped in `try`/`finally`. Also added a warning log in `run_enrichment()`
  if any `mobility_agencies` rows still have `is_covered = NULL` (matching.py
  hasn't run for them yet) — they're silently excluded by the `is_covered =
  False` filter, which is correct once the pipeline has run in order, but is
  worth surfacing if enrichment is ever run standalone out of order.
- **`ingestion/transit_cities.py`**: removed accidental string-literal quotes
  around the `CityEntry` type alias's `str | None` members (a no-op at
  runtime, but meant static type-checking silently saw a string constant
  instead of a real type). Also hardened `_parse_region_hierarchy` to ignore
  an `<a>` nested inside an `<h2>`/`<h3>` — not currently the case on the live
  page (verified against the real fetch), but would otherwise misread a
  linked heading as one of its own cities if the markup ever changes that way.
- **`README.md`**: added the "Manual setup" section it and `.env.example`
  were already both referring readers to, which didn't actually exist.

**Not fixed, deliberately**: enrichment's per-agency feed downloads and
population lookups run strictly sequentially (`httpx` synchronous calls in a
loop). They're independent per agency and could be parallelized (e.g.
`httpx.AsyncClient` + a semaphore) for a meaningful speedup — with thousands
of uncovered agencies this is the pipeline's biggest time cost. Left as-is
for this build rather than rewriting and re-verifying a multi-thousand-feed
run right before deployment; worth doing as a follow-up if the pipeline is
re-run often.

## Step 6 — API

- `sort_by` is validated against a whitelist of real columns
  (`SORTABLE_COLUMNS`) and mapped to the actual ORM column server-side,
  rather than interpolating the query param into an `order_by()` clause, to
  avoid building a raw SQL/attribute-injection surface from user input.
- Added an `order` query param (`asc`/`desc`, default `desc`) alongside
  `sort_by` — the brief didn't specify direction control, but "sorted by
  opportunity_score desc by default" implies ascending should also be
  reachable once you can choose a different `sort_by`.

## Step 7 — Frontend

- Consulted the `dataviz` skill for the stat-tile/palette conventions used
  here (proportional figures for the big stat-tile numbers, tabular-nums
  reserved for the table's numeric columns, the documented light/dark token
  set) even though this dashboard has no charts.
- `API_BASE` in `frontend/index.html` auto-detects `localhost` and otherwise
  falls back to the literal string `YOUR_RAILWAY_URL` — update that constant
  (or the README's placeholder) once the API is deployed. The frontend is a
  static file with no build step, so this is a manual find-and-replace rather
  than an env var.
- Country filter options and all three sort/column interactions are computed
  client-side from the single `/agencies` response (fetched once on load)
  rather than re-querying the API per interaction — simpler and fast enough
  at this data scale (hundreds to low thousands of agencies, not millions).
- The quality-slider and country-dropdown re-filter the already-fetched data
  in place; only the initial page load hits the network.

## Step 9 — Docker/config

- `api/Dockerfile` now also copies `db/` and `run.py` into the image (needed
  once the API started importing from the shared `db` package, and so
  `docker-compose run --rm api python run.py` works without a rebuild).
  `docker-compose.yml`'s `api` service volume-mounts `./db` and `./run.py`
  for the same live-reload-friendly dev workflow already used for `./api`
  and `./ingestion`.
- Added `asyncpg` (API's async engine) and `pycountry` (offline country
  name → ISO code lookups, Steps 2-4) to `requirements.txt`.
- `.env.example` documents every var `db/config.py` reads, including ones
  that already have working defaults (`MOBILITY_API_BASE_URL`,
  `TRANSIT_REGIONS_URL`, `REST_COUNTRIES_BASE_URL`,
  `MATCH_CONFIDENCE_THRESHOLD`) — left commented as optional overrides so
  they're discoverable without forcing you to set them.
