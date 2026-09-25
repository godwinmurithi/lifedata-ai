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
