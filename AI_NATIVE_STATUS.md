# AI-Native Superset — Discovery Findings (2026-09-25)

## TL;DR

The "AI-native Superset" system this task describes **already exists** on this
box, under a different name and a different architecture than the task
assumes. It is called **LifeData AI Copilot** (`~/projects/lifedata-ai`), it
is deployed against the real staging Superset/DB (not a sandbox with fake
data), and per its own README it considers itself through "Phase 4" of its
own roadmap (chart/dashboard CRUD, layout, filters, beautification, themes —
all done). Executing the pasted 13-phase plan literally would duplicate or
actively undermine work that's already there. See "Divergence from the task's
assumptions" below before doing anything further.

## Environment map

| Component | Where | Notes |
|---|---|---|
| Staging Superset | `superset-staging4cti` container, `superset-staging` compose project (`~/projects/superset-staging/docker-compose.yml`) | Superset **6.1.0**, port `127.0.0.1:8089`, public via nginx → `sandbox.lifehealth.app` |
| Production Superset | `superset4cti`, compose project `superset`, port `0.0.0.0:8088` | **Separate instance, same Docker host. Not in scope. Never touch.** |
| Orchestrator ("Vima" in the task = **LifeData AI**) | `lifedata-ai4cti` container, image `lifedata/orchestrator:sandbox`, source at `~/projects/lifedata-ai` | Django app, injected into Superset pages via nginx `sub_filter` (same-origin panel), not a chat sidecar calling MCP |
| Staging metadata + analytics DB | `db-staging4cti`, Postgres 15/PostGIS 3.5, `127.0.0.1:5433` (also reachable in-network as `db-staging4cti:5432`) | DB `cti_geonode_data`; schemas: `superset` (Superset metadata), `forms`, `lifedata_ai` (orchestrator's own audit tables), `public` |
| Reverse proxy | `nginx4cti_geonode`, compose project `cti_geonode` | Terminates TLS for `sandbox.lifehealth.app`, routes `/` → Superset, `/lifedata-ai/` → orchestrator, injects the panel `<script>` tag |
| superset-chat (older project, per memory) | `superset-chat-web4cti` / `superset-chat-api4cti` | Still running (2 months up) — appears to be a **prior, separate** attempt at a Superset chat assistant. Relationship to lifedata-ai not yet reconciled — needs a decision, see open questions. |
| dbt project | searched, **not found** anywhere under `~/projects` | Task's Phase 4 (dbt/MetricFlow → certified Superset metrics) has no dbt source to sync from on this box |
| GeoNode instance | `django4cti_geonode` / `celery4cti_geonode`, GeoNode 4.4.4 | Present, but **not wired to Superset auth today** |
| MCP / `fastmcp` | checked inside `superset-staging4cti` | **Not installed.** Boot log: `fastmcp is not installed, skipping MCP initialization`. Confirms Superset SIP-214/MCP is not in play here — and the orchestrator was deliberately built *not* to use it (see below). |
| Celery worker / beat / dedicated Redis for staging Superset | checked via `docker compose ls` | Staging Superset compose project shows only **1 running service** (the web container itself) — no separate worker/beat container for staging. A system-wide Redis is listening on 6379 (shared native install per earlier memory), used for Superset caching. Thumbnails/async likely not wired for staging today — needs verification, not yet confirmed broken or working. |

## Version / deployment facts

- Superset: **6.1.0**, pinned, Docker (`superset-staging-superset` image built from `~/projects/superset-staging/`, no local git repo — no `.git` inside container, dockerfile builds off the pinned upstream image + `psycopg2-binary` per lifedata-ai's own README notes — that Dockerfile actually lives under `lifedata-ai/deploy/superset/`, worth reconciling with the separate `superset-staging` compose project which builds its own image; **two Superset build definitions exist for what may be the same logical deployment — needs reconciling, not yet done**).
- Orchestrator: Django + `uv`, source bind-mounted (autoreload, no rebuild needed for code changes), Anthropic model `claude-opus-5` (already verified against current model catalog per their own README — not stale).
- `~/projects/lifedata-ai` has **no `.git`** — not currently tracked as its own repo (has `.gitignore` but no `.git`). The task's hard rule "work on branch `feat/ai-native-staging`" has nowhere to attach until this is resolved.
- No dedicated Celery worker/beat/flower containers found for the staging Superset project specifically (thumbnails, async charts, scheduled reports need checking, not assumed).

## Divergence from the task's assumptions — read before continuing

The pasted plan was written for a system that doesn't call Superset's MCP
server, doesn't authenticate through GeoNode OAuth, and doesn't use
row-level-security as its privacy boundary. What's actually here:

1. **No MCP anywhere, by design, not by gap.** The orchestrator has its own
   tool-use loop (`lifedata_ai/agent/loop.py`) with a small custom tool set
   (`read`, `write`, `ask_user`, `review_dashboard_design`), not MCP tool
   calls. Phase 2's "wire Vima to Superset MCP tools" doesn't have an MCP
   server to wire to, and given the next point, adopting MCP wholesale could
   *weaken* the current privacy control unless MCP's tool surface is audited
   against it first.

2. **The privacy boundary is a hand-maintained allowlist, and it's already
   holding back real risk.** `lifedata_ai/semantics/registry.yaml` (the
   "sandbox" registry, live on this deployment) explicitly documents that
   **5 of 17 registered Superset datasets hold row-level, identifiable data**
   — names, DOB, gender, disability, income, individual health-screening
   responses (sickle cell questionnaire), HbA1c/glucose/blood group, staff
   emails — and deliberately **excludes those 5 from the registry** so the
   validator refuses to chart them at all. The file also records a known,
   *unfixed* gap: `get_chart_data` replays an existing chart's stored query
   verbatim and isn't gated by the registry, so a table chart someone already
   built over the `farmers` dataset would still hand row-level names to the
   model. **This is real PHI/PII-adjacent exposure risk today, independent of
   this task** — flagging per the task's own Hard Rule 5 (no real
   patient-identifiable data may reach an LLM without going through a privacy
   filter). There is no separate "Vima privacy filter" — the registry
   allowlist *is* the filter, and it has a documented hole.

3. **Auth is Superset's own FAB self-registration + manual admin approval**
   (`custom_sm.py` — no GeoNode OAuth2, no group→role mapping). Phase 1's
   GeoNode OAuth integration would be new work, not a config tweak, and
   would need to coexist with or replace this approval flow.

4. **Writes already run as the user, but via a different mechanism than
   "OAuth token to an MCP tool."** The documented "critical invariant" is
   that write actions execute in the **user's own browser session** (cookie +
   CSRF), verified end-to-end (400 without CSRF token, 201 with it) — the
   orchestrator itself holds no Superset write credentials. This already
   satisfies the spirit of Hard Rule 5 / Phase 2's "never a service account"
   requirement, just structurally differently than the MCP-based design the
   task describes.

5. **A second, older "superset-chat" stack is still running** alongside this
   one (per prior memory, a earlier attempt at the same goal). Two live
   candidate systems for "the AI-native Superset" exist on this box and I
   have not yet reconciled which one is meant to be extended.

## Open questions before Phase 0 can proceed responsibly

- Given LifeData AI Copilot already covers a large chunk of Phases 0–5
  (chart/dashboard CRUD, layout, theming, provenance-ish action records),
  should the remaining phases be **re-scoped onto this existing system**
  rather than built as a parallel MCP-based track?
- What should happen to `superset-chat` (the older stack)? Retire, or is it
  serving a different purpose?
- Is the `get_chart_data`-bypasses-the-registry gap something to fix now
  (it's a pre-existing, real PHI exposure risk, arguably more urgent than any
  of the 13 phases)?
- No dbt project exists on this box — Phase 4's "sync from dbt/MetricFlow"
  has nothing to sync from. Is dbt planned elsewhere, or should the existing
  `registry.yaml` be treated as the semantic layer instead?
- `lifedata-ai` isn't a git repo yet — where should it live (own repo? folded
  into one of the 5 tracked repos?) before a `feat/ai-native-staging` branch
  can be created?

**No destructive, migration, or auth-changing commands have been run.** Only
read-only discovery (`docker ps/inspect`, file reads, `git log`, `\dn`/`\dt`)
has happened so far.

---

# Phase 0–2 implementation update (2026-09-25, same day)

Decisions made with the user after the discovery above, before any of this
was built:

- **GeoNode OAuth (open question above): skipped.** Keep Superset's existing
  self-registration + manual-approval auth (`custom_sm.py`) as-is. Not
  revisited below.
- **Native MCP: adopt it, not the custom tool loop.** Superset 6.1.0 ships a
  real, extensive first-party MCP implementation (`superset/mcp_service/` —
  `list_datasets`, `get_dataset_info`, `generate_chart`, `update_chart`,
  `get_chart_data`, `list_dashboards`, `generate_dashboard`, `execute_sql`,
  screenshots, OAuth2/JWT auth), gated only behind installing the `fastmcp`
  PyPI extra. Turning it on is config + a dependency, not a Superset core
  fork (satisfies the task's Hard Rule 6). Decided to switch to it rather
  than keep extending the hand-built tool loop.
- **The registry.yaml allowlist gate: explicitly NOT ported in front of MCP,
  for now.** This was a direct instruction, given with the consequence
  spelled out and confirmed twice: *"switch to native mcp, remove the gating
  fix, we will gate later on in production."* Concretely, this means
  `GetChartDataTool`'s MCP path (see below) can return row-level data from
  any of the 5 PII-bearing datasets identified in the discovery section
  above, to any user whose Superset role can already reach that chart/
  dataset via ordinary Superset RBAC. See "What the no-gate decision
  actually exposes today" below for what was verified, not assumed, about
  the current blast radius.

## Phase 0: native MCP enabled on staging Superset — DONE

**Where:** `~/projects/superset-staging/` (own git repo now, see below).

**What changed:**
- `Dockerfile`: added `fastmcp>=3.1.0,<4.0`.
- `docker-compose.yml`: new `superset-mcp` service, same image, running
  `superset mcp run --host 0.0.0.0 --port 5008`. **No `ports:` — not on the
  host, not public.** Reachable only by name (`superset-staging-mcp4cti:5008`)
  from containers on `cti_geonode_default`/`db-staging_default`, i.e. by
  `lifedata-ai4cti`.
- `superset_config.py`: `MCP_AUTH_ENABLED = True`, HS256 JWT
  (`MCP_JWT_SECRET`, shared with the orchestrator via `.env` on both sides —
  generated with `openssl rand -base64 42`, never committed).
- `mcp_auth_bridge.py` (new, mounted like `custom_sm.py`): **a real,
  isolated patch**, not pure config — flagged per Hard Rule 6. Registered via
  `MCP_AUTH_FACTORY`. Fixes two confirmed bugs/gaps in Superset 6.1.0's
  shipped MCP+JWT integration (both reproduced directly against the running
  service before writing the fix, not guessed):
  1. `MCP_JWT_DEBUG_ERRORS = True` uses `DetailedJWTVerifier`, which calls
     `self.jwt.decode(...)` — an attribute that doesn't exist on the class.
     Every token fails validation with a generic "Token validation failed,"
     valid or not. Avoided (left at default `False`); the plain `JWTVerifier`
     validates correctly.
  2. Even with a verifying token, the verified identity never reaches
     `g.user` outside Preset's proprietary SaaS middleware —
     `default_user_resolver()` in `mcp_config.py` is defined but never called
     from anywhere in the auth path (traced every call site). Without a fix,
     JWT auth only works as `MCP_DEV_USERNAME` — a single shared account,
     which is exactly the "service account" pattern forbidden. Fixed with a
     `contextvars.ContextVar` (not `g` — `mcp_auth_hook` deliberately pushes
     a fresh Flask app context per tool call, which resets `g`) plus a
     monkeypatch of `get_user_from_request`, the one function actually
     consulted per call.

**Tested:** minted a JWT for `sub=admin`, connected over `streamable-http`,
called `list_datasets` through the `search_tools`/`call_tool` dispatcher —
got real staging dataset metadata back, RBAC intact (Superset's own
`security_manager.can_access`, per-user). Then did the same as `aremo`
(non-admin, Alpha role) — succeeded independently, confirming per-user
identity, not a shared token. Web Superset smoke-tested after the rebuild:
login page 200, `/health` OK, unauthenticated `/api/v1/dashboard/` correctly
401s, `/sqllab/` redirects — no regressions from adding the MCP service
alongside it.

**How to turn off:** `docker compose stop superset-mcp` (orchestrator falls
back to the REST client automatically once `LIFEDATA_AI_USE_MCP_READS=false`
is set — see below) or delete the `superset-mcp` service block from
`docker-compose.yml` entirely; nothing else depends on it existing.

**Git:** `~/projects/superset-staging` had no `.git` either (same gap noted
in discovery for `lifedata-ai`) — initialized, committed baseline + this
work, branch `feat/ai-native-staging`.

## Phase 2 (partial): orchestrator read tools wired to MCP — DONE

**Where:** `~/projects/lifedata-ai/`.

`tools/base.py`'s own docstring anticipated exactly this swap: *"Spec §2
requires that tools sit behind a thin interface so a Superset MCP backend
can replace the direct REST calls later without the agent loop changing."*
That held — `agent/loop.py` (1347 lines) was not touched at all.

**What changed:**
- `superset_mcp_client.py` (new): mirrors `superset_client.py`'s (REST)
  method names, but mints a short-lived per-user JWT and calls Superset's
  MCP tools instead of REST endpoints. Translates MCP's response shape back
  into the same REST-shaped dicts `tools/superset_read.py` already parses,
  so most tool bodies needed **zero changes**.
- `tools/base.py`: `ToolContext.superset_mcp_client()` +
  `ToolContext.use_mcp_reads` (reads `settings.LIFEDATA_AI_USE_MCP_READS`,
  **feature flag, default on, per hard rule 4**).
- `tools/superset_read.py`: 6 of 8 read tools (`list_databases`,
  `list_datasets`, `get_dataset`, `list_charts`, `get_chart`,
  `list_dashboards`) now pick MCP or REST per the flag, one line each,
  output shape unchanged either way.
- **`get_dashboard` stays REST-only, on purpose, regardless of the flag.**
  Verified: MCP's `DashboardInfo` schema has no `position_json` field or
  equivalent, so the layout-outline feature (rows/columns/tabs/widget
  widths — `_summarise_position`) has no MCP source in this Superset
  version. Said so rather than silently dropping it (Hard Rule 7).
- **`get_chart_data` gets a real MCP path**, calling Superset's own
  `get_chart_data` tool by chart id (it builds and runs the query
  server-side — richer than the REST path's replay-a-stored-query-context
  approach, and comes with a free `summary`/`insights` the REST path never
  had). **This is the one tool where the registry gate mattered and was
  removed** — see `GetChartDataTool._run_mcp`'s docstring in the code, which
  says so explicitly, and the section below on what that exposes today.

**Two real bugs found and fixed while testing against live data, before
calling this done** (not caught by reading the code — only by running it):
1. MCP's list tools default to `page_size=10`. Undetected, this would have
   silently under-reported `list_datasets` (17+ datasets → 10) and
   `list_charts` (50+ → 10) below the tools' own, intentional
   `MAX_LISTING_ITEMS=50` cap — the wrong kind of truncation, silent instead
   of flagged. Fixed by requesting `page_size=100` explicitly, matching what
   the REST client always did.
2. `has_stored_query_context` (surfaced by `get_chart` to tell the model
   whether `get_chart_data` will work) was derived from a REST-only field
   that's always empty under MCP, so it would have always reported `False`
   — telling the model a chart's data can't be read when, via MCP, it
   actually can. Fixed by deriving it from `form_data` presence instead
   under the MCP path.

**Tested (satisfies the task's Phase 2 acceptance test — "same question,
two users, scope-correct answers"):** ran the actual `Tool` classes (not
just the client) through the real Django app as two different real Superset
users, `admin` and `aremo` (Alpha role, not Admin) — both got real,
independent results (`list_datasets`, `list_charts`, `get_chart`,
`get_chart_data`), each authenticated by their own minted JWT resolving to
their own `g.user`, not a shared identity.

**Full pytest suite: not run.** This sandbox deployment's Postgres user
correctly lacks `CREATEDB` on the shared staging database (`db-staging4cti`
also serves other live projects), so `pytest-django`'s test-DB bootstrap
fails on `setup_databases()` regardless of anything in this change —
confirmed this is pre-existing, not something broken here. `ruff check` is
clean on every touched file. Live, end-to-end testing against the real
running stack (above) is the actual verification for this phase.

**How to turn off:** set `LIFEDATA_AI_USE_MCP_READS=false` in
`deploy/sandbox/.env` and recreate the container (`docker compose up -d
django` — a plain `restart` does **not** reload `.env`, confirmed the hard
way mid-session). Every read tool falls back to the original REST client
with zero code changes needed. `get_dashboard` is unaffected either way.

**Git:** `~/projects/lifedata-ai` had no `.git` — initialized, committed
baseline, branch `feat/ai-native-staging`, then this work as a second
commit on that branch.

## What the no-gate decision actually exposes today — verified, not assumed

Checked directly against live staging data rather than reasoning about it in
the abstract:

- Of **50 registered charts**, **30 read one of the 5 datasets** the
  registry deliberately excludes (mostly the `farmers` dataset, id 4, and
  `farmers_crop_breakdown`, id 6).
- Of those 30, **29 are aggregate charts** (pie/bar/big-number — grouped
  counts, averages) — e.g. a "Gender" pie chart over `farmers` returns
  `{Female: 507, Male: 848}`, not individual rows. Confirmed by running it.
- **One, chart 65 ("Farmer Records"), is a raw-mode `table` chart** —
  `query_mode: "raw"`, listing individual columns
  (`agro_tracker_id, gender_norm, age_band_norm, other_crop,
  scale_of_production, income_num, yield_num, employees_num, created_at`)
  with no aggregation. This is the exact shape the registry.yaml gate was
  built to stop. **Currently it fails via MCP** — Superset's own
  `get_chart_data` tool looks for a `columns` key in `form_data` and this
  chart's raw-mode field is named `all_columns`, so it errors
  `MissingQueryContext` instead of returning rows. **This is an accidental
  Superset-side parsing quirk, not a safety control** — it is not something
  this project built or can rely on, and a different raw-mode chart, a
  future Superset point release, or a different viz plugin's field-naming
  convention could easily not hit it. Treat the exposure as real and only
  incidentally not triggered by this one example.
- `execute_sql` (raw SQL Lab) is registered as an MCP tool on the Superset
  side but was **not** wired into any `Tool` the orchestrator's agent loop
  can call — the model has no path to it today. If a future phase adds it,
  it would bypass this entire discussion (any table the calling user's
  Superset role can reach becomes queryable directly), and should not be
  added without the gating conversation happening first.

## Toggle summary

| Component | Off switch |
|---|---|
| Native MCP service on Superset | `docker compose stop superset-mcp` (in `~/projects/superset-staging/`) |
| MCP auth bridge patch | Remove `MCP_AUTH_FACTORY` line from `superset_config.py` — falls back to `MCP_DEV_USERNAME`/no-JWT-identity behavior, do not do this without also disabling the MCP service, since that reintroduces a shared-identity path |
| Orchestrator using MCP for reads | `LIFEDATA_AI_USE_MCP_READS=false` in `deploy/sandbox/.env` + `docker compose up -d django` |
| registry.yaml gate on get_chart_data | Not currently togglable — it simply isn't there on the MCP path. Re-adding it means porting the `dataset_by_superset_id` check from `GetChartDataTool._run_rest` into `_run_mcp` (or into `superset_mcp_client.py` itself, so it also covers any future MCP tool) |

## Still open (not decided, not built)

- Phases 8, 9, 13 (react-dnd→@dnd-kit migration, remaining chart-plugin
  wrappers, dependency cleanup) require forking Superset's actual frontend/
  dependency tree — a standing cost this project has otherwise avoided
  entirely. Raised as a fork-in-the-road question; not yet answered.
- RLS (Phase 1 minus GeoNode) — still nothing built. Could be done on
  Superset's native roles now that MCP/RBAC is confirmed working per-user,
  independent of the GeoNode question.
- The older `superset-chat` stack (`superset-chat-web4cti`/
  `superset-chat-api4cti`) — still running, still unreconciled with this
  project.
- No dbt project exists on this box — Phase 4's semantic-layer sync has
  nothing to sync from; `registry.yaml` remains the closest thing to a
  semantic layer.
- Producing the actual registry-gate fix for `get_chart_data` under MCP
  (see toggle table above) — deferred to "production," per instruction, not
  attempted.
- Trust & evals (Phase 6), thumbnails (Phase 5), client actions/canvas
  (Phase 7), and everything in Phases 10–12 — none started.

---

# Full write-path authorization (2026-09-25, same day, continued)

**Instruction, verbatim, after the read-path work above:** *"why are you
making decisions for us, give mcp full authorization, again this is staging
and we are testing it out, we will decide which limitations to put
ourselves. Please MCP needs to work fully with the chatbot"* — followed by
confirmation to deprioritize RLS and the `superset-chat` question in favor of
this. Proceeded without further gating questions on MCP scope/authorization
decisions specifically, per that instruction.

## What was actually missing

The read-path swap (previous section) only covered background lookups
(`list_datasets`, `get_chart`, etc.). The chatbot's actual core capability —
building charts and dashboards — still ran entirely through the original
browser-executed REST path, untouched. That gap, not anything MCP-specific,
is what the user was seeing as "limitations."

## What's now wired to MCP

`create_chart`, `create_dashboard`, `add_chart_to_dashboard` — the three
action types with both a real MCP tool to call and a payload shape this
system can translate correctly:

- **`tools/mcp_chart_config.py`** (new): translates our validated chart
  params into MCP's own typed `config` schema (a *different* shape again —
  MCP's `generate_chart`/`update_chart` take a discriminated union keyed on
  `chart_type`: `xy`, `pie`, `table`, `big_number`, not Superset's raw
  form_data and not our own `{metrics, groupby, adhoc_filters}` shape
  either). Covers all 5 viz types `semantics/registry.yaml`'s
  `viz_allowlist` permits — the entire set this system's validator lets the
  model create in the first place, so this is complete coverage of the
  reachable surface, not a partial slice. Returns `None` (→ fall back to the
  REST path) when a specific payload can't be translated, rather than
  guessing.
- **`tools/mcp_write_dispatch.py`** (new): `action_type` → MCP tool call
  (`generate_chart`, `generate_dashboard`, `add_chart_to_existing_dashboard`).
- **`superset_mcp_client.py`**: write methods for the above.
- **`tools/superset_write.py`**: `WriteTool.run()` now decides, per proposal,
  `execution_mode = "mcp"` or `"browser"` — `LIFEDATA_AI_USE_MCP_WRITES`
  (default on) **and** a per-payload translatability check. A chart the
  translator can't handle still gets `"browser"` even with the flag on,
  rather than failing at confirm time. Everything upstream of that decision
  (schema validation, ownership check, ActionRecord/Snapshot creation for
  undo) is **unchanged** — only what happens when the user clicks confirm
  differs.
- **`views/execute_mcp_action.py`** (new) + `urls.py`: the confirm endpoint
  for `execution_mode="mcp"` actions. `action_result.py` only *records* what
  the browser already did; this one *performs* the write, server-side, via
  the caller's own minted MCP identity — same per-user invariant as the read
  path, never a service account. The request carries nothing but
  `action_id`: every detail of the write comes from the `ActionRecord` this
  server already validated in `WriteTool.run()`, so there is nothing
  user-supplied to tamper with.
- **`panel.js`**: `executeAction` branches on `execution_mode`. The `"mcp"`
  path (`executeMcpAction`) just POSTs `action_id` and shows the result,
  skipping the raw-calls-against-Superset loop entirely for these three
  action types.

## Deliberately NOT wired to MCP, and why

`update_chart`, `rename_chart`, `rename_dashboard`,
`remove_chart_from_dashboard`, the layout tools (move/resize), and native
filters — all still on the original REST/browser path, unconditionally,
regardless of the flag. Not an oversight:

- MCP's chart `config` has no concept of a *partial* update.
  `update_chart`'s whole contract is "merge these keys over the chart's
  existing configuration" (`tools/superset_write.py`'s own docstring on
  `UpdateChartTool`) — translating that correctly needs the chart's full
  current config, and MCP has no tool that hands back a chart in config
  form (`get_chart_info` returns form_data-ish fields, not a typed config).
  Getting a merge subtly wrong (silently dropping a filter, say) is a worse
  outcome than leaving this on a path that is already known-correct.
- MCP has no dashboard-layout-manipulation tools at all (move/resize/remove
  a component, add a native filter) — nothing to wire to. Same finding as
  `get_dashboard`'s `position_json` gap in the read-path section above.

## Verified, end-to-end, against live staging data

- `CreateChartTool` on dataset 18 (`buikwe_level1_facilities`, registered,
  non-PII), pie chart grouped by `subcounty` → `execution_mode` came back
  `"mcp"` → confirmed through the actual `execute_mcp_action` view function
  → **chart 327** exists in Superset with correct data (11 subcounties,
  correct counts) **and is correctly owned by the calling user** — MCP's
  `generate_chart` assigns ownership by default, which is actually *better*
  than the REST path (which needs `_owned_by_caller`, a manual workaround
  for a real Superset API quirk: `POST /api/v1/chart/` leaves `owners` empty
  unless told).
- `CreateDashboardTool` with `chart_ids=[327]` → **dashboard 32** created,
  chart correctly attached — verified against `superset.dashboard_slices`
  directly in Postgres, not just trusting the API response.
- Fallback logic verified separately and directly: a `big_number_total`
  with two metrics, an `xy` chart with no derivable x-axis, and a `pie` with
  no dimension all correctly return `None` from the translator (→
  `execution_mode="browser"`), rather than crashing or half-translating.
- `ruff check` clean on every touched Python file; `node --check` clean on
  `panel.js`.
- Test chart 327 and test dashboard 32 are left on staging, clearly
  labeled (`"MCP Test..."`) — no delete tool exists on either the REST
  client or MCP's tool set to clean them up programmatically. Harmless, but
  worth knowing they're there.

**Known gap, flagged not fixed:** an MCP-created chart's REST-shaped
`get_chart()` reports `datasource_id: None` — MCP's chart doesn't populate
`form_data.datasource` in the `"{id}__type"` form the REST-shape adapter
parses. Doesn't affect `get_chart_data` or the write path itself, but means
`GetChartTool`'s `semantics` (registry) annotation is silently empty for
MCP-created charts specifically. Follow-up, not attempted here.

## How to turn off

`LIFEDATA_AI_USE_MCP_WRITES=false` in `deploy/sandbox/.env` +
`docker compose up -d django` — every write action falls back to the
original, unchanged browser-executed REST path. `update_chart` and the
layout tools are unaffected either way; they never used the MCP path in the
first place.
