# Deploying Unclaimed to Railway

This guide covers deploying the `api` service (FastAPI, built from `api/Dockerfile`) and a
managed Postgres database to [Railway](https://railway.app), and running the one-off
`run.py` ingestion pipeline against the deployed database.

It was written by reading Railway's own docs (`docs.railway.com`) and CLI reference pages
as of September 2026. Railway's CLI/dashboard change fairly often — anywhere this guide
says "verify with `--help`" or flags something as uncertain, treat that as a live checkpoint,
not a formality.

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
  it explicitly, Railpack is never invoked.

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
`PGUSER`, `PGPASSWORD`, `PGDATABASE` (and, only if you later enable Public Networking on
it, `DATABASE_PUBLIC_URL`). You do not need to set any Postgres credentials yourself.

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
already connected — that step happens in the browser). Because `railway.json` is at the
repo root, Railway will build this service with the Dockerfile builder using
`api/Dockerfile`, per the config in step 0 — you shouldn't need to touch build settings in
the dashboard at all.

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

> **CLI syntax note (uncertain / verify locally):** Railway's docs pages disagree on
> whether the current subcommand is `railway variable set KEY=value` or
> `railway variables --set "KEY=value"` — the CLI has renamed this a few times across
> versions. Run `railway variable --help` and `railway variables --help` and use whichever
> your installed CLI actually reports before trusting the exact command above verbatim.

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
referenced. Per Railway's docs, when a domain is first generated for a service, Railway
auto-detects the single port the container is actually listening on and uses that as the
"target port" — so this should work out of the box here, since 8000 is the only port the
container opens. If domain generation in step 8 doesn't route traffic correctly:

- Check the service's Settings → Networking for a "Target Port" field and set it to `8000`
  explicitly, or
- Set a `PORT=8000` variable on the service as a hint (`railway variable set PORT=8000
  --service api`) — the app itself ignores it, but Railway's own routing layer may use it as
  a fallback when it can't detect a listening port automatically.

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

**Option B — enable Public Networking on the Postgres service and run locally.** In the
Postgres service's Settings → Networking, enable Public Networking (this creates a TCP
proxy and a `DATABASE_PUBLIC_URL` variable, and will incur some egress cost). Then, from
your machine, with a Python environment that has `requirements.txt` installed:

```bash
DATABASE_URL="$(railway variable get DATABASE_PUBLIC_URL --service Postgres)" python run.py
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
railway domain --service api
```

Running this with no existing domain generates a free `*.up.railway.app` Railway domain for
the service and prints it. Run it again later (or check the dashboard) to see the domain
without creating a new one. Update the "Live URL" line in `README.md` with whatever it
prints.

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
railway variable set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' --service api
railway variable set 'MOBILITY_API_TOKEN=<token>' --service api
railway up --service api
railway ssh --service api -- python run.py
railway domain --service api
```

## Sources consulted

- https://docs.railway.com/builds/dockerfiles
- https://docs.railway.com/reference/config-as-code
- https://docs.railway.com/cli
- https://docs.railway.com/cli/ssh
- https://docs.railway.com/cli/add
- https://docs.railway.com/cli/variable
- https://docs.railway.com/databases/postgresql
- https://docs.railway.com/networking/private-networking/how-it-works
- https://docs.railway.com/guides/fastapi
- Railway Central Station / Help Station community threads on `railway run` vs private
  networking, and on target-port detection for public domains.
