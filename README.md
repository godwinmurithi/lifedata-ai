# LifeData AI Copilot

A conversational AI copilot layered onto an Apache Superset analytics
deployment. You ask for a chart, a dashboard, or an explanation in plain
language; the copilot inspects live Superset metadata, proposes **validated**
actions, and your own browser session executes them.

Built to [`docs/lifedata-ai-copilot-spec.md`](docs/lifedata-ai-copilot-spec.md).

**Superset version (pinned): `apache/superset:6.1.0`**
**Database: PostGIS 3.5 on PostgreSQL 16**

---

## The critical invariant

> **Write actions are always executed by the user's own browser session, never
> by a service account.**

The orchestrator holds no Superset credentials with write access. It validates
and signs an action envelope; the panel executes it same-origin with the user's
session cookie and CSRF token. The copilot therefore *cannot* exceed the
logged-in user's Superset permissions.

This is verified, not assumed — Superset returns `400 "The CSRF token is
missing."` for a write without a token and `201` with one. See
[docs/superset-integration-findings.md](docs/superset-integration-findings.md).

The privacy boundary is enforced the same way. Superset connects to the
analytics database as a **read-only role that cannot see the operational
tables at all**:

```
superset_ro→ SELECT count(*) FROM reporting.rpt_patient_geography;   →  12
superset_ro→ SELECT count(*) FROM public.patient;   →  ERROR: permission denied
```

Patient geography is aggregated to district *in the view*, with districts under
5 patients suppressed. There is no patient identifier and no point geometry to
select, so sub-district grouping and point maps are impossible by construction —
not merely rejected by the validator.

---

## Architecture

Everything runs in Docker.

```
Browser  →  http://localhost:8080   (nginx — the single same-origin entry point)
              ├── /              → superset:8088
              │     nginx sub_filter injects /lifedata-ai/static/panel/panel.js
              └── /lifedata-ai/  → django:8000   (the orchestrator)

postgres (PostGIS) ── lifedata  : LifeHealth tables (public) + reporting views
                  └── superset  : Superset's own metadata
redis              ── Superset caching
```

Same-origin routing is load-bearing. **Do not browse Superset on another port**
— the panel is not injected there and the session cookie is not shared.

Source is bind-mounted into the `django` container, so editing files in your IDE
triggers the dev server's autoreload without a rebuild.

---

## Setup

```powershell
# 1. Environment file
Copy-Item .env.example .env
#    Fill in ANTHROPIC_API_KEY, and generate the secrets:
#      openssl rand -base64 42     → SUPERSET_SECRET_KEY  (Superset REFUSES to
#      openssl rand -base64 42     → DJANGO_SECRET_KEY     boot with a weak key)

# 2. Build and start everything
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build

# 3. Orchestrator migrations
docker compose --env-file .env -f deploy/docker-compose.yml `
  exec django uv run --no-sync python manage.py migrate

# 4. Synthetic LifeHealth data + reporting views
docker compose --env-file .env -f deploy/docker-compose.yml `
  exec django uv run --no-sync python manage.py seed_lifehealth

# 5. Register the reporting views as Superset datasets
docker compose --env-file .env -f deploy/docker-compose.yml `
  exec django uv run --no-sync python manage.py register_superset_datasets
```

Open **<http://localhost:8080>** and log in.

| Account | Username | Password | Role |
| --- | --- | --- | --- |
| Administrator | `admin` | see `.env` | Admin |
| Analyst | `analyst` | see `.env` | Alpha |

The analyst account exists to demonstrate the identity invariant — the copilot
is bounded by whatever that user can do in Superset.

> **`--env-file .env` is required on every compose command.** The compose file
> lives in `deploy/`, so without the flag Docker looks for `deploy/.env` and the
> run fails on the missing `SUPERSET_SECRET_KEY`. There is deliberately only one
> env file, at the repo root — a second copy drifts the moment you edit one.

---

## Everyday commands

Set `$dc = 'docker compose --env-file .env -f deploy/docker-compose.yml'` to shorten these.

| Task | Command |
| --- | --- |
| Start | `docker compose --env-file .env -f deploy/docker-compose.yml up -d` |
| Stop | `... down` |
| Reset everything (destroys data) | `... down -v` |
| Logs | `... logs -f superset` / `... logs -f django` |
| Django shell | `... exec django uv run --no-sync python manage.py shell` |
| psql | `... exec postgres psql -U lifedata -d lifedata` |
| Re-seed data | `... exec django uv run --no-sync python manage.py seed_lifehealth` |
| Rebuild views only | `... exec django uv run --no-sync python manage.py seed_lifehealth --views-only` |
| Tests | `... exec django uv run --no-sync pytest` |
| Browser tests | `LIFEDATA_LIVE=1 uv run pytest -m superset_live` (on the HOST) |
| Real-model check | `uv run pytest -m live_model` (billed, manual) |
| Pilot accounts | `... exec django uv run --no-sync python manage.py create_pilot_users --dry-run --user "Name:email"` |
| Lint / format | `uv run ruff check .` / `uv run ruff format .` |

The default `pytest` run is the unit suite plus the markdown tests, which need
only Chromium. The browser tests run on the **host**, not in the container: the
panel is injected by nginx and has to be exercised through the same origin a
real user sees.

Health and diagnostics endpoints:

- `GET /lifedata-ai/api/health/` — public liveness (reports PostGIS version, model, key presence)
- `GET /lifedata-ai/api/whoami/` — the verified Superset identity (401 when anonymous)
- `GET /lifedata-ai/api/diagnostics/superset/` — proves cookie passthrough works

---

## Configuration

All settings come from `.env` (git-ignored). See [.env.example](.env.example).

| Variable | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Required for the agent loop. Env only, never committed. |
| `LIFEDATA_AI_MODEL` | Model driving the loop. Default `claude-opus-5`. |
| `LIFEDATA_AI_EFFORT` | Reasoning depth: `low`…`max`. Default `medium` — see below. |
| `LIFEDATA_AI_MAX_TOOL_ROUNDS` | Hard stop on tool rounds per turn. Default 12. |
| `LIFEDATA_AI_MAX_WRITES_PER_TURN` | Proposed changes per turn. Default 14. |
| `LIFEDATA_AI_TURN_TIMEOUT_SECONDS` | Wall-clock bound on *model* work. Default 300. |
| `LIFEDATA_AI_ACTION_WAIT_SECONDS` | How long to await an auto-applied write. Default 30. |
| `SUPERSET_SECRET_KEY` | **Must be strong** — Superset refuses to boot otherwise. |
| `DJANGO_SECRET_KEY` | Orchestrator secret. |
| `POSTGRES_*` | Shared analytics/orchestrator database. |

`LIFEDATA_AI_EFFORT` defaults to `medium` for a measured reason rather than a
cost one: with tools attached, `high` returned `overloaded_error` on essentially
every request while `medium` succeeded. A dashboard build needs several
consecutive rounds, so one overload anywhere in the chain aborted the whole
thing. See [docs/runbook.md](docs/runbook.md), “Anthropic capacity”.

The turn timeout bounds **model** work. Time spent awaiting the browser is
measured and added back to the deadline — otherwise nine browser round trips
could hand the SDK a three-second timeout mid-build, and turn timeouts would
track network quality rather than model latency.

> The spec suggested `claude-sonnet-4-6` and said to verify model strings before
> hardcoding. Verified: the current default is `claude-opus-5`, and the API
> surface has moved — `temperature`/`top_p` and fixed thinking budgets are
> rejected on current models, with reasoning depth set via `output_config.effort`.

---

## Deviation from the spec

Spec §0 requires the Superset image to remain **unmodified**. That is not
achievable as written: `apache/superset:6.1.0` ships **no Postgres driver at
all**, so it cannot reach its own metadata database.

[`deploy/superset/Dockerfile`](deploy/superset/Dockerfile) derives from the
pinned image and installs `psycopg2-binary` — nothing else. No Superset source
is forked, patched or overridden, and all behaviour is still configured through
`superset_config.py`.

---

## Project layout

```
deploy/
  docker-compose.yml          # PostGIS + Superset + Redis + nginx + Django
  nginx/lifedata.conf         # routing + panel injection (sub_filter)
  superset/
    Dockerfile                # pinned image + Postgres driver (only deviation)
    superset_config.py        # config only — CSP, CSRF, auth, caching
    bootstrap.sh              # migrations + demo accounts, then hand off
  django/Dockerfile           # orchestrator image (GDAL/GEOS for GeoDjango)
  postgres/init/              # databases, PostGIS, read-only analytics role
lifedata/                     # Django project (settings, urls, wsgi/asgi)
lifedata_ai/
  middleware.py               # Superset session identity verification
  superset_client.py          # httpx wrapper, cookie passthrough
  models.py                   # Conversation, Message, ActionRecord, Snapshot
  agent/
    loop.py                   # the tool-use loop; deferred resume lives here
    prompts.py                # stable prefix (catalogue) + volatile tail
    inflight.py               # one turn at a time, per conversation
  tools/                      # read, write, ask_user, review_dashboard_design
  validation/                 # action schemas, policy rules, layout compiler
  semantics/                  # registry.yaml + loader — prompt AND validator
  static/panel/               # panel.js, markdown.js, panel.css (no build step)
  sql/                        # operational schema + reporting views
  management/commands/        # seed_lifehealth, register_superset_datasets
docs/
  lifedata-ai-copilot-spec.md         # the build specification
  improvement-plan.md                 # the executive-fit work, and why
  runbook.md                          # operational facts code cannot enforce
  superset-integration-findings.md    # §10 verification record
```

---

## Data

Synthetic, reproducible from a fixed seed. Default generation:

| View | Rows | Notes |
| --- | --- | --- |
| `reporting.rpt_facilities` | 58 | Exact coordinates (institutions) |
| `reporting.rpt_encounters` | 8,775 | 18 months, seasonal malaria variation |
| `reporting.rpt_lab_orders` | 3,950 | Realistic status mix; TAT only where verified |
| `reporting.rpt_patient_geography` | 12 | District aggregate, small cells suppressed |
| `reporting.rpt_diabetes_screening` | 900 | One wired dynamic form |

District names are real public geographic entities; **all health data is
invented**. No real patients, facilities, or clinical records.

---

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Stack, pinned Superset, nginx injection, §10 verification, data, datasets, registry | ✅ Done |
| 1 | Identity, Superset client, registry, agent loop, read tools, panel | ✅ Done |
| 2 | Chart create/update + undo | ✅ Done |
| 3 | Dashboards, layout, filters, preview | ✅ Done |
| 4 | Beautification, themes, semantics depth, one dynamic form | ✅ Done |

Phase definitions and acceptance criteria: spec §9.

### Built for executives, not analysts

The copilot worked from the start; what it did not do was work for the people it
is for. Every prompt that succeeded early was schema-literate — *"from
rpt_lab_orders (dataset 3), counting lab_order_id grouped by facility_name"* —
and an executive will not write that. The work in
[docs/improvement-plan.md](docs/improvement-plan.md) closed that gap:

- **A catalogue in the prompt's cached prefix.** Every dataset id, column, label
  and saved metric, projected from `registry.yaml`. The model no longer needs two
  Superset round trips before it can name a column — and every hop it used to
  take was an invitation to describe a chart instead of building one.
- **Saved metrics.** Three of six governed indicators are ratios or filtered
  counts, structurally unrepresentable as `{aggregate, column}` and therefore
  previously unchartable. `--sync-metadata` projects them onto Superset, where
  they become names the copilot can simply use.
- **`ask_user`.** When two readings are equally defensible and produce
  materially different charts, the copilot asks — once, with plain-English
  choices — instead of guessing. The turn ends and resumes when the user
  answers, so the question survives a page reload.
- **`review_dashboard_design`.** The copilot checks its own work against the
  executive layout standard before saying it is done.

### Operations

Read [docs/runbook.md](docs/runbook.md) before deploying or debugging. It covers
the two things that will otherwise cost an afternoon: re-running
`--sync-metadata` after Superset's "Sync columns from source" wipes the curated
labels, and the gunicorn timeout that would kill every build turn.
