# Deploying Unclaimed to Railway

This guide covers deploying the `api` service (FastAPI, built from `api/Dockerfile`) and a
managed Postgres database to [Railway](https://railway.app), and running the one-off
`run.py` ingestion pipeline against the deployed database.

It was written and then re-verified by reading Railway's own docs (`docs.railway.com`), CLI
reference pages, and the live `railway.schema.json` schema, as of September 2026 (see "Sources
consulted" at the bottom). Railway's CLI/dashboard/deprecation status change fairly often —
anywhere this guide flags something as unconfirmed or tells you to double-check the dashboard,
treat that as a live checkpoint, not a formality.

## 0. What Railway needs to know about this repo

The Dockerfile is at `api/Dockerfile`, not the repo root, and it expects a **build context
of the repo root** (it does `COPY ingestion/ ./ingestion`, `COPY db/ ./db`, `COPY run.py .`,
etc. — exactly mirroring `docker-compose.yml`'s `context: .` / `dockerfile: api/Dockerfile`).

Railway's Dockerfile builder supports a custom Dockerfile location via the `dockerfilePath`
field in `railway.json`, and — per Railway's own docs — that path is resolved relative to
the **build context Railway uses**, which defaults to the repository root when no separate
"Root Directory" is configured on the service. That default context is exactly what this
Dockerfile needs, so:

- A `railway.json` has been added at the repo root (already created as part of this task):

  ```json
  {
    "$schema": "https://railway.com/railway.schema.json",
    "build": {
      "builder": "DOCKERFILE",
      "dockerfilePath": "api/Dockerfile"
    },
    "deploy": {
      "healthcheckPath": "/health",
      "restartPolicyType": "ON_FAILURE"
    }
  }
  ```

  - `builder: "DOCKERFILE"` + `dockerfilePath: "api/Dockerfile"` tells Railway exactly which
    Dockerfile to use, without needing to set the service's "Root Directory" setting at all.
  - **Do not set the service's Root Directory to `api/`** in the Railway dashboard. That
    setting changes the build *context*, and if it's set to `api/`, the Dockerfile's
    `COPY ingestion/ ./ingestion` / `COPY db/ ./db` / `COPY run.py .` lines will fail because
    those directories won't exist relative to that context. Leave Root Directory unset
    (repo root) and let `dockerfilePath` alone point into the subdirectory — this is the one
    genuine gotcha here, since it's easy to reach for "Root Directory" as the fix for a
    non-root Dockerfile and get it backwards.
  - `healthcheckPath: "/health"` and `restartPolicyType: "ON_FAILURE"` are optional
    niceties, not required — they just tell Railway to use the app's existing `/health`
    endpoint for post-deploy health checks and to restart the service on crashes, roughly
    mirroring the `restart: unless-stopped` in `docker-compose.yml`. Remove them if you'd
    rather manage that from the dashboard.

- No `nixpacks.toml` is needed — Nixpacks/Railpack is Railway's buildpack-style builder for
  repos *without* a Dockerfile; since this repo already has one and we've pointed Railway at
  it explicitly, Railpack is never invoked (assuming Railway actually reads `railway.json` —
  see the warning immediately below).

### Important — re-verify this before trusting `railway.json` alone

Railway's own **[Infrastructure as Code](https://docs.railway.com/infrastructure-as-code)**
docs (re-checked September 2026) state that **Config as Code (`railway.json`/`railway.toml`)
is deprecated**, and — critically — **"New services cannot opt into Config as Code."**
Existing files are only still read "for existing (legacy) services," and even that support
"stops being read on 2026-12-01 (hard cutoff)." The
[Config as Code reference page](https://docs.railway.com/reference/config-as-code) carries the
same deprecation banner. Since this guide has you create a brand-new project and a brand-new
`api` service (`railway init`, `railway add --repo`), the `api` service is a **new** service in
Railway's terms — meaning `railway.json`'s `build`/`deploy` settings may simply be **ignored**,
and Railway could silently fall back to Railpack instead of the Dockerfile (a much worse
failure mode than an outright error, since a Python repo with a `requirements.txt` at the root
may look buildable to Railpack even though it has no idea about `api.main:app`).

`railway.json` is left in this repo because it's harmless and costs nothing, but **do not rely
on it alone**. Immediately after step 4 creates the `api` service, do one of the following:

- **Preferred, non-deprecated fallback:** set the Dockerfile path via the documented
  `RAILWAY_DOCKERFILE_PATH` service variable, which works independently of Config as Code's
  status ([source](https://docs.railway.com/builds/dockerfiles#custom-dockerfile-path)):

  ```bash
  railway variable set 'RAILWAY_DOCKERFILE_PATH=/api/Dockerfile' --service api
  ```

- Or check whether `railway.json` was actually honored: open the deployment's details page in
  the dashboard and look for the small file icon next to the Builder/Dockerfile Path settings
  (Railway shows this next to any setting sourced from a config file —
  [source](https://docs.railway.com/reference/config-as-code#config-source-location)). If it's
  missing, set **Settings → Build → Builder: Dockerfile** (with the custom path, or the
  `RAILWAY_DOCKERFILE_PATH` variable above) manually.

`healthcheckPath`/`restartPolicyType` from `railway.json` are subject to the same caveat, but
they're low-stakes either way: `ON_FAILURE` (with up to 10 retries) is already Railway's
default restart policy, and a missing healthcheck just means Railway skips the zero-downtime
health-check-before-cutover behavior rather than failing the deploy
([source](https://docs.railway.com/deployments/restart-policy),
[source](https://docs.railway.com/deployments/healthchecks)). Set them manually in the
service's Settings tab if you want them and they didn't take effect from the file.

## 1. Prerequisites

- Push this repo to GitHub (Railway's GitHub-based deploys pull from a repo it has access
  to via its GitHub App — a local-only repo won't work for that flow).
- Install the Railway CLI and log in:

  ```bash
  npm i -g @railway/cli
  # or: brew install railway
  railway login
  ```

  (`railway login --browserless` if you're in an environment without a browser, e.g. SSH.)
- Have your Mobility Database refresh token ready (`MOBILITY_API_TOKEN` — see README.md
  "Manual setup"). Nothing else in `.env.example` requires a manual value; everything else
  either has a default in `db/config.py` or is only used by the local `db` container
  (Railway's managed Postgres replaces that, so `POSTGRES_USER`/`POSTGRES_PASSWORD`/
  `POSTGRES_DB`/`POSTGRES_HOST`/`POSTGRES_PORT` are **not** needed on Railway — only
  `DATABASE_URL`, which Railway's Postgres plugin generates for you).

## 2. Create the Railway project

From the repo root:

```bash
railway init
```

This creates a new Railway project and links the current directory to it. (If a project
already exists — e.g. you created it in the dashboard first — use `railway link` instead to
link this directory to it.)

## 3. Provision Postgres

```bash
railway add --database postgres
```

This adds a managed Postgres service to the project and deploys it immediately. It
automatically populates that service's own variables: `DATABASE_URL`, `PGHOST`, `PGPORT`,
`PGUSER`, `PGPASSWORD`, `PGDATABASE` (and, only if you later enable **Public Access** on
it — Settings → Networking — `DATABASE_PUBLIC_URL`). You do not need to set any Postgres
credentials yourself.

By default the Railway CLI names this service `Postgres` — confirm the exact name with:

```bash
railway status
```

(or check the project in the Railway dashboard). The rest of this guide assumes it's named
`Postgres`; adjust the reference variable below if yours differs.

## 4. Create the `api` service from the GitHub repo

```bash
railway add --repo <your-github-username>/<your-repo-name>
```

This creates a new service in the project sourced from that GitHub repo (the first time you
do this, Railway may prompt you to authorize its GitHub App for the repo/org if it isn't
already connected — that step happens in the browser). `railway.json` at the repo root
*may* make Railway build this service with the Dockerfile builder using `api/Dockerfile`
automatically — **but don't assume that** and skip straight to step 5. Because this is a
newly created service, go do the check-and-fallback in the "Important" box under step 0
right now (either set `RAILWAY_DOCKERFILE_PATH`, or confirm the config-file icon is present
on the deployment details page) before moving on.

Confirm the service name Railway assigned (usually the repo name) with `railway status`;
this guide calls it `api` below — substitute your actual service name.

## 5. Wire up environment variables on the `api` service

Railway lets one service reference another service's variables with `${{ServiceName.VAR}}`
syntax, which keeps the actual Postgres URL out of your hands entirely and stays correct if
Railway ever rotates it.

```bash
railway variable set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' --service api
railway variable set 'MOBILITY_API_TOKEN=<your-real-token>' --service api
```

Add any of the optional overrides from `.env.example` only if you actually need to deviate
from the defaults baked into `db/config.py` (`MOBILITY_API_BASE_URL`,
`TRANSIT_REGIONS_URL`, `WORLD_BANK_BASE_URL`, `MATCH_CONFIDENCE_THRESHOLD`). `API_PORT` is
also not needed — see the port gotcha below.

> **CLI syntax (confirmed):** `railway variable set KEY=value [--service NAME]` is the current
> subcommand — `railway variable` also accepts the aliases `variables`/`vars`/`var`. The older
> `railway variable --set "KEY=value"` flag form still works but is explicitly documented as
> deprecated. ([source](https://docs.railway.com/cli/variable)) If you'd rather not put a
> secret on the command line / in shell history, pipe it in instead:
> `echo "$MOBILITY_API_TOKEN" | railway variable set MOBILITY_API_TOKEN --service api --stdin`.

Railway does **not** read your local `.env` or `.env.example` files automatically for either
build or deploy — every variable the app needs has to be set explicitly via the dashboard or
`railway variable set`.

## 6. Deploy

If the service was created with `railway add --repo ...`, Railway should already have
kicked off a build+deploy automatically (and will auto-deploy on every push to the
connected branch going forward). To watch it or trigger one manually:

```bash
railway up --service api
```

(`railway logs --service api` to tail logs from the running deployment.)

### Gotcha: the container's port is hardcoded, not read from `$PORT`

Railway normally injects a `PORT` environment variable and expects your process to bind to
`0.0.0.0:$PORT`. This repo's `api/Dockerfile` does not do that — its `CMD` is the exec-form
`["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]`, which is a fixed port
and (being exec-form, not shell-form) would not expand `$PORT` even if the variable were
referenced. Railway's dashboard shows a "Generate Domain" prompt once it detects a service is
listening correctly, which suggests some auto-detection of the listening port happens — but
this repo's fixed single port (8000) should make that detection unambiguous either way. To be
safe, don't rely on auto-detection; set the target port explicitly when you generate the
domain in step 8, which is a documented, confirmed CLI flag
([source](https://docs.railway.com/cli/domain)):

```bash
railway domain --service api --port 8000
```

(If a domain already exists and traffic isn't routing, `railway domain update <domain>
--service api --port 8000` changes the target port on an existing domain instead.)

Separately, Railway's own healthcheck mechanism (not domain routing) also uses the injected
`PORT` variable to know which port to probe; if your app doesn't listen on `PORT` (as here),
Railway's docs say to set a `PORT` variable yourself so healthchecks target the right port
([source](https://docs.railway.com/deployments/healthchecks#configure-the-healthcheck-port)):

```bash
railway variable set PORT=8000 --service api
```

This task intentionally does not modify `api/Dockerfile`, so this is flagged as a
verify-after-first-deploy item rather than "fixed" here.

## 7. Run the one-off pipeline (`run.py`) against the deployed database

This is the part most worth getting right: **`railway run` executes locally**, injecting the
target service's environment variables into your local shell — it does *not* run inside
Railway's infrastructure. Railway's Postgres `DATABASE_URL` uses an internal hostname
(`*.railway.internal`) that only resolves over Railway's private WireGuard network between
services in the same project — your laptop is not on that network, so `railway run python
run.py` would inject the right-looking `DATABASE_URL` but the connection would simply hang
or time out.

Two ways to actually reach it:

**Option A (recommended) — run it inside the already-deployed `api` container over SSH.**
The `api` service's container already has the code and dependencies from `api/Dockerfile`
(including `run.py`, copied in at `COPY run.py .`), and it *is* on Railway's private
network, so its `DATABASE_URL` resolves correctly:

```bash
railway ssh --service api -- python run.py
```

(Omit `-- python run.py` to drop into an interactive shell in the container instead, if you
want to poke around first.) Run this only after step 6's deploy has succeeded at least once.

**Option B — enable Public Access on the Postgres service and run locally.** In the
Postgres service's Settings → Networking, enable **Public Access** (this creates a TCP
proxy and a `DATABASE_PUBLIC_URL` variable, and will incur some egress cost). Then, from
your machine, with a Python environment that has `requirements.txt` installed:

```bash
DATABASE_URL="$(railway run --service Postgres printenv DATABASE_PUBLIC_URL)" python run.py
```

(Or just `railway run` after temporarily pointing the `api` service's `DATABASE_URL` at
`${{Postgres.DATABASE_PUBLIC_URL}}` for this one run.) Option A avoids exposing Postgres to
the public internet at all and needs no extra setup, so prefer it unless you have a reason
to run the pipeline from your own machine.

`run.py` is idempotent (per README.md — every step upserts/replaces rather than duplicating
rows), so re-running it later to refresh data is safe; just repeat step 7 whenever you want
fresh data.

## 8. Get the public URL

```bash
railway domain --service api --port 8000
```

Running this with no existing domain generates a free `*.up.railway.app` Railway domain for
the service and prints it, explicitly targeting port 8000 per the gotcha in step 6 above. Run
`railway domain list --service api` later (or check the dashboard) to see existing domains
without creating a new one. Update the "Live URL" line in `README.md` with whatever it prints.

Verify the deploy:

```bash
curl https://<your-domain>.up.railway.app/health
```

## Quick reference — full command sequence

```bash
railway login
railway init
railway add --database postgres
railway add --repo <you>/<repo>
railway variable set 'RAILWAY_DOCKERFILE_PATH=/api/Dockerfile' --service api   # see step 0 warning
railway variable set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' --service api
railway variable set 'MOBILITY_API_TOKEN=<token>' --service api
railway variable set PORT=8000 --service api                                  # for healthchecks
railway up --service api
railway ssh --service api -- python run.py
railway domain --service api --port 8000
```

## Sources consulted

This guide was re-verified against Railway's live documentation on 2026-09-08 (fetched
directly via `curl`, and the schema via `WebFetch`, rather than trusted from training data —
Railway's CLI/dashboard/deprecation status change often enough that this is worth repeating
before every real deploy):

- https://railway.com/railway.schema.json (redirects to
  https://backboard.railway.app/railway.schema.json) — the actual JSON Schema; used to confirm
  every field name/type/enum in `railway.json` directly, not just from prose docs.
- https://docs.railway.com/reference/config-as-code — confirms field names/values, and carries
  the Config as Code deprecation banner.
- https://docs.railway.com/infrastructure-as-code — confirms "New services cannot opt into
  Config as Code" and the 2026-12-01 hard cutoff for existing/legacy services.
- https://docs.railway.com/builds/dockerfiles — `RAILWAY_DOCKERFILE_PATH` service variable as
  the non-deprecated way to point at a custom Dockerfile path.
- https://docs.railway.com/deployments/monorepo and
  https://docs.railway.com/builds/build-configuration — confirm Root Directory defaults to `/`
  and, when set, changes what "all build and deploy commands operate within," i.e. the build
  context — the basis for the Root Directory warning in step 0.
- https://docs.railway.com/guides/cli, https://docs.railway.com/cli/add,
  https://docs.railway.com/cli/variable, https://docs.railway.com/cli/ssh,
  https://docs.railway.com/cli/domain, https://docs.railway.com/cli/run — current CLI command
  and flag syntax for `add`, `variable set`, `ssh`, `domain`, and `run`.
- https://docs.railway.com/reference/variables — confirms the `${{NAMESPACE.VAR}}` service
  variable reference syntax.
- https://docs.railway.com/databases/postgresql — confirms `PGHOST`/`PGPORT`/`PGUSER`/
  `PGPASSWORD`/`PGDATABASE`/`DATABASE_URL`/`DATABASE_PUBLIC_URL` variable names and that the
  toggle is called "Public Access," not "Public Networking."
- https://docs.railway.com/networking/private-networking (and
  .../private-networking/how-it-works) — confirms the `SERVICE_NAME.railway.internal` private
  DNS naming behind the `railway run`-executes-locally gotcha in step 7.
- https://docs.railway.com/networking/domains/working-with-domains — Railway-provided domain
  generation flow and target-port behavior.
- https://docs.railway.com/deployments/healthchecks and
  https://docs.railway.com/deployments/restart-policy — healthcheck `PORT` behavior and
  confirmation that `ON_FAILURE` is already Railway's default restart policy.
- https://docs.railway.com/guides/fastapi — consulted for comparison, but flagged as
  **not fully up to date**: as of this check it still describes `railway.json` as the
  recommended way to configure a fresh FastAPI deploy, which is inconsistent with the
  Infrastructure as Code page's "new services cannot opt into Config as Code" statement. The
  dedicated reference pages (config-as-code, infrastructure-as-code) were treated as
  authoritative over this tutorial page.
