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

**Follow-up finding from a dedicated security review pass, fixed**: the
country-filter `<select>` in `frontend/index.html`'s `populateCountryFilter()`
built its `<option>` list via unescaped `innerHTML` interpolation of
`a.country` — the same class of stored-XSS the first review pass had already
fixed for the table body (`renderTable()`), but this call site was missed.
Same fix: route it through the existing `escapeHtml()` helper.

**Known data limitation, not a bug**: the World Bank's population dataset
excludes Taiwan entirely (it doesn't publish indicators for Taiwan as a
separate country). Agencies there get `population = NULL` and score purely
on `quality_score`. Confirmed via the final sanity check: 12/2,939 (0.4%) of
uncovered agencies hit this — small enough to leave as-is rather than adding
a special case for one territory.

**Aggregator feeds found dominating the top of the ranking, fixed** (found
during a pre-outreach sanity pass on the live deployed data, not during
development): the Mobility Database treats one GTFS feed as one "agency"
row, but some feeds are actually regional/national open-data platforms
(`DELFI Germany-wide scheduled timetable data`, `BODS UK aggregate feed`,
`Trafiklab`), transport ministries, or multi-operator bundles (a feed name
listing 9+ separate agencies). Their sheer scale (the #1-ranked row had
677,741 stops and 23,819 routes — no single real transit agency has that;
the dataset's median is ~219 stops / ~15 routes) pushed them to the very
top of `opportunity_score`, ahead of real single-agency opportunities like
MBTA/SEPTA, and 53 of them were counted in the "Ready" stat used in the
outreach email's headline number.

Added `is_probable_aggregator` (`ingestion/enrichment.py`,
`migrations/002_add_aggregator_flag.sql`), flagged when a feed's name lists
>= 2 comma-separated operators, or `route_count > 3000`, or
`stop_count > 20000` — thresholds picked by inspecting the actual
distribution (2nd-highest real single-agency route_count in the dataset is
well under 500; every flagged row is 3-100x that). Flags 111 of 2,939 rows
(3.8%). Flagged rows are **not deleted or hidden from the full
dashboard/API** — the data is still real and potentially useful (Transit
might genuinely want to know DELFI exists as a platform-level integration,
just not as a "sign up this one agency" lead) — they're excluded from
`/stats.ready_to_onboard` (1,117 → 1,064) and from the frontend's "Top
Opportunities" cards and the pitch page's top-20 table, since those are
specifically meant to be individually actionable single-agency leads. The
migration backfills existing Supabase rows with the same heuristic via SQL
(`LENGTH(name) - LENGTH(REPLACE(name, ',', ''))`) rather than re-running the
full multi-hour enrichment pipeline just for this — verified the backfilled
counts match the Python heuristic exactly (111 flagged, 53 flagged-and-Ready)
before treating it as done.

One coincidental oddity noticed but not chased further: `Rursee-Schifffahrt
KG` (a small German boat-tour operator) and `DELFI Germany-wide scheduled
timetable data (GTFS)` have identical stop_count/route_count/
opportunity_score (552,956 / 29,607 / 68.21) despite different feed URLs.
Checked `enrichment.py` for a caching bug that could explain it (none found
— only population is cached, keyed by country, which is correct) and it's
plausible the small operator's Mobility Database feed is itself a re-export
of DELFI's national aggregate data rather than an independent one. Both are
now flagged as probable aggregators regardless (stop_count alone clears the
threshold by 27x), so it doesn't affect the fix above, but worth knowing
the underlying stats aren't independently verified in this case.

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

## Deployment: switched from Railway to Vercel + Supabase

`DEPLOY.md`/`railway.json` (above) were fully written and independently
verified, but Railway's free trial had already been used on this account and
continuing there meant paying. Rebuilt the deploy on entirely free
infrastructure instead:

- **Database**: Supabase (Postgres) instead of Railway's managed Postgres —
  free forever at this scale, no time limit (unlike e.g. Render's free
  Postgres, which is deleted after 90 days). Used the **Transaction pooler**
  connection (port 6543, IPv4) rather than the direct connection (port 5432)
  — Supabase's direct connections are IPv6-only for new projects, which this
  environment couldn't route to at all (`Network is unreachable`).
- **API**: Vercel, as a Python serverless function, instead of an
  always-on Docker container. This is a materially different runtime model
  than the Dockerfile-based deploy the whole rest of this project uses
  locally: no persistent process, each request is a fresh invocation. Added:
  - `vercel.json` — rewrites every path to `/api/main` (the FastAPI app),
    since Vercel's Python runtime otherwise only serves it at `/api/main`.
  - `.python-version` (`3.12`) — pins a modern enough interpreter for this
    codebase's `X | None` union-type syntax; Vercel's default wasn't
    guaranteed to be 3.10+.
  - `db/async_session.py`: `connect_args={"statement_cache_size": 0}` on the
    asyncpg engine — required against a PgBouncer/Supavisor transaction
    pooler (Supabase's, but the same fix applies to Neon/Railway poolers
    too), since a prepared statement can otherwise get reused on a different
    physical connection than the one that created it, causing intermittent
    "prepared statement does not exist" errors under real traffic.
  - The pipeline (`run.py`) still cannot and does not run on Vercel —
    serverless function time limits are far shorter than the ~1-1.5 hour
    enrichment run takes. It keeps running locally/in Docker as before,
    just pointed at Supabase's `DATABASE_URL` instead of local Postgres.
    Already-validated data was copied over directly (`pg_dump`/`psql`)
    rather than re-run from scratch, since the pipeline had already been
    verified end-to-end against local Postgres.
- **Frontend**: Vercel static hosting (`frontend/index.html`, `rootDirectory:
  frontend`) — no changes needed, it was already a plain static file.
- Both Vercel projects were created manually via the dashboard rather than
  through the available Vercel API/MCP connector — the connector's token
  consistently got `403 forbidden` on project creation specifically (a
  team-role/permission restriction on that connection, unrelated to the
  GitHub App repo-access grant, which was also required separately). Once a
  project exists, configuration/redeploys work fine through the connector.
- Vercel deploys from the **`main`** branch by default. All of this project's
  work happened on `dev` and `main` was still at the Step-1 scaffold commit,
  so the first deploy attempt built nothing (`no "functions", "static", or
  "services" directory`). Fixed by merging `dev` into `main`.

## Post-launch rework: opportunity score, readiness, and a Transit-facing UI

After the first deploy, the brief expanded significantly: a better-weighted
opportunity score using real signals, a readiness classification, an agency
detail view, richer stats, a full visual rebuild, and a print-friendly pitch
page. Written as code only per that request — nothing in this section was
executed, migrated, re-enriched, or deployed; see the manual steps list.

**New Mobility Database API surfaces used** — both verified against live
responses before writing code, not assumed:
- `latest_dataset.downloaded_at` on a GTFS feed record — feed freshness.
- `/v1/gtfs_rt_feeds`, where each realtime feed's `feed_references` array
  lists the static feed id(s) it corresponds to — used to build a set of
  "has realtime" static feed ids in one paginated pull per enrichment run,
  rather than a per-agency lookup (~3,000x fewer calls).

**Where `feed_last_updated` is captured**: on `MobilityAgency` during Step 2
(`mobility_db.py` already fetches `latest_dataset` for `feed_url`; this just
also keeps `downloaded_at`), then copied onto `UncoveredAgency` during Step 5.
Not re-fetched per-agency during enrichment — Step 2 already has it for every
feed in one pass.

**route_count/stop_count/trip_count** are parsed from the same already-open
GTFS zip used for quality scoring (`score_feed()` now returns a dict with
these alongside `quality_score`/`missing_files`, instead of the old 2-tuple)
— no second download per agency.

**Opportunity score weights** (population 25%, quality 20%, realtime 25%,
freshness 20%, network size 10%) are exactly what was specified. Two scaling
decisions within that: `has_realtime` maps straight to 100/0 before
weighting (the "then normalised" instruction, read as "put it on the same
0-100 scale as everything else"); `network_size_score` (route_count) uses
**plain linear min-max**, not `normalize_populations()`'s log scale — route
counts don't span the multi-order-of-magnitude range population does, so log
scaling isn't needed and wasn't asked for. New helper `normalize_linear()` in
`ingestion/enrichment.py`, tests in `tests/test_enrichment.py`.

**readiness_status** implements the three-way rule exactly as specified,
with one explicit priority choice: "Dead Feed" is checked before "Ready" —
an unreachable or >2-year-stale feed can't be "Needs Work" just because it
happens to also fail a Ready criterion. `quality_score > 70` (not `>=`) per
the literal "above 70" wording — tested at the boundary
(`test_boundary_quality_exactly_70_is_not_ready`).

**`GET /agencies/{id}/rank`** — a new endpoint, not explicitly requested but
needed for the detail view's "Rank among all uncovered agencies" (Improvement
3). Counts rows with a strictly higher `opportunity_score` rather than
fetching and ranking all ~2,939 rows client-side just to place one agency.

**`/stats` response shape changed** (`top_5_countries` → `ready_to_onboard`,
`realtime_count`, `largest_market`) since Improvement 4 replaces the stat
tiles it fed. This is a breaking API change for any other consumer — none
exist yet (the dashboard is the only client), so no versioning was added.
Updated `tests/test_api_integration.py`'s stats assertions and its
country-filter test (which previously sourced its test country from
`top_5_countries`, now sources it from a live `/agencies` response instead).

**Agency detail routing uses a hash fragment (`#/agency/{id}`), not a real
`/agency/{id}` path.** The brief said "no new HTML file needed," and a
static single-page site serving real path segments needs a server-side SPA
rewrite (all paths → index.html) or a direct navigation to `/agency/123`
404s. A hash fragment never reaches the server, so it's bookmarkable and
back/forward-safe with zero extra Vercel config — no `frontend/vercel.json`
needed. Trade-off: it's `#/agency/123`, not literally `/agency/123` — flagged
here rather than silently deviating from the literal path shape asked for.

**Pitch view is `?view=pitch` on the same `index.html`** (also as specified)
— a query param is checked before the hash so the two routes don't collide.
Print stylesheet (`@media print`) flips the page to a light, ink-friendly
palette regardless of the dashboard's dark-by-default theme, and hides the
"back to dashboard" link (`.no-print`) since it's meaningless on paper/PDF.

**Design tokens**: same real Transit product palette/type-scale established
earlier in this log (brand green `#27a559`, verified-contrast text greens,
their neutral gray scale, `ui-sans-serif` fallback stack), now applied
dark-first unconditionally (not gated behind `prefers-color-scheme`) per
"dark background stays."

**Migration**: `migrations/001_add_scoring_and_readiness_fields.sql` —
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` throughout, so it's idempotent
and safe to run against both the local Postgres and Supabase, in either
order, repeatably. This is the project's first migration file; every
earlier schema change went through `Base.metadata.create_all()` on a fresh
table, which doesn't retrofit existing tables — an explicit migration was
unavoidable once real data already existed to preserve.

## Post-deploy review pass — bugs found and fixed

Ran a dedicated code-review pass after the opportunity-score rework/dashboard
rebuild was live. Fixed four real issues (two in `frontend/index.html` found
directly by re-reading the routing/rank-fetch code myself before the review
even finished; two in `ingestion/mobility_db.py`, pre-existing since Step 2
but never caught before real production runs surfaced the second one):

- **`frontend/index.html`**: the "Rank" fetch in the agency detail view had
  no guard against out-of-order responses — navigating between two agencies
  quickly could let a slower response for the first one overwrite the rank
  shown for the second. Fixed with a monotonic request token
  (`detailRequestToken`).
- **`frontend/index.html`**: `showView()` was only called on `loadData()`'s
  success path, so a failed initial load left a `?view=pitch` or
  `#/agency/id` visitor looking at the dashboard's error state instead of
  being routed to the view their URL actually asked for. Moved routing
  outside the try/catch; `renderPitch()` now has its own error state and a
  bounded 15s timeout on its "wait for data" poll (was unbounded — could
  spin forever if data never arrived).
- **`frontend/index.html`**: the table sort comparator's null-handling used
  an `Infinity`/`-Infinity` sentinel meant to push null values to one end,
  but for a string column the later type-coercion branch stringified that
  sentinel to `"infinity"` and sorted it alphabetically as text — so a null
  value landed wherever `"infinity"` fell alphabetically (between names
  starting with roughly A-I and J-Z) rather than consistently at the end.
  Rewrote null-handling as its own branch before any type coercion.
- **`ingestion/mobility_db.py`**: `upsert_agencies()` committed once after
  its entire per-record loop, but the except branch called
  `session.rollback()` on failure — which rolls back the *whole current
  transaction*, not just the failed statement. One bad record near the end
  of a run could silently discard every successful insert/update that
  preceded it in the same session, exactly contradicting the "one bad row
  must not abort the batch" comment already on that code. Fixed by
  committing per-record.
- **`ingestion/mobility_db.py`**: `main()` returned normally (no exception)
  when it couldn't obtain an access token, which made a missing/expired
  `MOBILITY_API_TOKEN` look like a successful (empty) Step 2 to `run.py`'s
  `run_step()` — which only halts the pipeline on an exception — letting
  Steps 3-5 run against a stale table instead of stopping. Now raises
  `RuntimeError` instead.

**Reviewed and dismissed as non-issues:**
- The review flagged `READY_MIN_ROUTES`/`READY_MIN_STOPS` using strict `>`
  as possibly unintentional given the constants are named "MIN". Checked
  against the literal brief: "route_count above 5" / "stop_count above 20"
  — "above" means strict greater-than, matching `quality_score > 70`'s
  already-documented and boundary-tested treatment of "above 70" the same
  way. Working as specified, not a bug.
- The review also flagged `renderPitch()`'s wait loop treating a
  legitimately-empty (but successfully loaded) `/agencies` response the
  same as "still loading," polling forever. This exact function had already
  been rewritten with a bounded 15s timeout during the same session (see
  above) before the review's result came back — re-checked against the
  current code and confirmed no longer reproducible; the only residual gap
  is that the timeout's error message ("timed out") would be slightly
  misleading in the specific empty-but-not-erroring case, which is cosmetic
  rather than a functional bug.
