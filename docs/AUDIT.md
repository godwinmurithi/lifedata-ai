# Vima Phase 0 — Superset deployment audit

Date: 2026-11-04. Read-only throughout: every finding below came from `GET`
requests, `SELECT` queries against the Superset metadata schema via the
existing `superset_app` Postgres role, and reading config/docs already on
disk. Nothing in Superset, its config, or any database was changed. Full
commands are given inline so every row is reproducible.

**Audit target:** `superset-staging4cti` (`sandbox.lifehealth.app`), the
Superset instance `lifedata-ai4cti` (this repo's own running deployment)
already talks to. Production (`superset4cti`, `dashboards.lifehealth.app`)
was inspected only for comparison (see Auth row and the Security row) — see
**Open decision 0** below on which instance Vima actually targets.

**Service account used:** `eval_runner` (Alpha role, non-admin). There is
**no** pre-existing shared "Vima service account" — see the note under
"Auth and roles" and **Open decision 1**. `eval_runner` is this project's
own established non-admin test identity (`evals/run_evals.py`), already used
for exactly this kind of live, end-to-end, non-destructive proof query, so it
was reused rather than inventing a new one or escalating to admin.

---

## A. Superset deployment

| Area | Finding | Evidence |
| --- | --- | --- |
| **Version / install** | `apache/superset:6.1.0`, Docker Compose, with a locally-built image adding only a Postgres driver (upstream ships none). Custom `superset_config.py`, `custom_sm.py`, `custom_views.py`, `mcp_auth_bridge.py`. Native MCP service (Superset's own `superset/mcp_service/`, not a bolted-on tool) is running and JWT-authenticated. | `docker ps` → `superset-staging4cti`, `superset-staging-mcp4cti`, `superset-staging-worker4cti`, `superset-staging-beat4cti`, `superset-staging-redis4cti`; `~/projects/superset-staging/superset_config.py`; `~/projects/lifedata-ai/docs/superset-integration-findings.md` |
| **Auth and roles** | Staging: plain Flask-AppBuilder DB auth (`AUTH_TYPE: 1`), self-registration on but new accounts are created **inactive** pending admin approval (`custom_sm.py`). Production (`dashboards.lifehealth.app`) was switched to **Authentik SSO** (`AUTH_OAUTH`) on 2026-10-30 (`superset_config.py.bak-pre-sso` dated that day) — DB-login is explicitly kept alive in parallel for service accounts only. 8 Superset roles exist: `Admin, Public, Alpha, Gamma, sql_lab, Guest, RLS: District Wakiso, RLS: District Iganga`. 19 active users. **Two users (`wakiso_analyst`, `iganga_analyst`) hold district-named RLS roles, but `row_level_security_filters`, `rls_filter_roles` and `rls_filter_tables` are all empty (0 rows) — these roles currently apply zero row restriction.** `DASHBOARD_RBAC` is on but `dashboard_roles` has 0 rows — no dashboard is role-restricted either. In short: the RBAC/RLS *plumbing* is switched on, nothing is actually configured. | `SELECT id,name FROM ab_role`; `SELECT count(*) FROM row_level_security_filters` → 0; `SELECT count(*) FROM dashboard_roles` → 0; `~/projects/superset/superset_config.py` (Authentik block); `~/projects/superset/Dockerfile.bak-pre-sso` |
| **Datasets and metrics** | 280 datasets registered across 5 database connections. Exported in full to `docs/catalog.json`. | SQL export below, `docs/catalog.json` |
| **Query API** | Proved live: `POST /api/v1/chart/data`, aggregate `COUNT(id)` grouped by `district` on dataset 21 (`scd_screening_v2`) → `200`, 50 rows, server-generated SQL visible in the response. Row-limited query (two-dim groupby, 1000 limit) took 566 ms. Matches `docs/superset-integration-findings.md`'s earlier findings against this same version: empty body → `500` not `400`; malformed datasource → `404`. | `eval_runner` session, request/response captured below |
| **Distinct values** | Proved live: same endpoint, columns-only query (no metric) on `district`, `row_limit: 100` → `200`, 100 distinct values returned, no parent filter needed to demonstrate the shape (dataset has no natural parent dimension above district). Confirms the "columns-only, no metrics" pattern Vima's cascading filters need. | Request/response captured below |
| **Performance** | Redis-backed query cache (`CACHE_DEFAULT_TIMEOUT: 300s`), dedicated Redis per staging instance. `GLOBAL_ASYNC_QUERIES` is **off** — [intentionally, per a documented bug]: enabling it 404'd every chart load because the async job's cache key and the result-fetch key never matched, confirmed by direct Redis inspection (`AI_NATIVE_STATUS.md`). Celery worker + beat exist but are wired to thumbnails only, not query execution. `ROW_LIMIT: 1000`, `SQL_MAX_ROW: 100000`, `SUPERSET_WEBSERVER_TIMEOUT: 60s`. No systematic slowest-dataset benchmark was run in this pass (280 datasets, most are small CSV-scale imports) — the one proof query against a non-trivial virtual dataset (two raw tables UNIONed + a normalization CTE) returned in 566 ms uncached. | `superset_config.py` CACHE_CONFIG/CeleryConfig; login-page bootstrap JSON (`ROW_LIMIT`, `SQL_MAX_ROW`, `SUPERSET_WEBSERVER_TIMEOUT`); `AI_NATIVE_STATUS.md` Phase 12 notes |
| **Geodata** | PostGIS 3.5 is available and used for real geometry (e.g. dataset 24 `buikwe_subcounties_map`, built via `ST_AsGeoJSON(geometry)` over dataset 2). But **boundary polygons for maps are not served by joining a Superset/PostGIS boundary table** — they come from a separate system this repo already built: `lifedata_ai/boundary_catalogue.py` pulls from `geoboundaries.org`, caches GeoJSON per country/admin-level under `.boundary-cache/` (193 layers, 26,768 area features across all 54 African countries, stored in `BoundaryArea`/`BoundaryRelease` Django models — see `docs/boundary-catalogue-and-adaptive-dashboards.md`). Superset's own map viz types in active use: `deck_scatter` (21 charts), `deck_polygon` (7), `mapbox` (1), `country_map` (1, essentially unused — no catalogued dataset carries an ISO boundary-code join, so it was deliberately avoided as a guaranteed-blank-map trap). | `boundary_catalogue.py`; `docs/boundary-catalogue-and-adaptive-dashboards.md`; viz_type distribution query below; `AI_NATIVE_STATUS.md` "Maps" section |
| **Security** | HTTPS: real Let's Encrypt certs on both `sandbox.lifehealth.app` and `dashboards.lifehealth.app`. CORS: not configured anywhere (`ENABLE_CORS` absent from both `superset_config.py` files → defaults off) — **same-origin is load-bearing**, confirmed by the docker-compose comment in this very repo ("Do NOT browse Superset directly on another port — cookies are not shared"). CSRF is enforced and verified end-to-end (`docs/superset-integration-findings.md`: write without `X-CSRFToken` → `400`, with it → `201`). Admin credentials: not found in plaintext in any config read; `SUPERSET_SECRET_KEY`/JWT secrets are env-injected, not hardcoded. **One real gap found**: `~/projects/superset/.env` (production) is mode `664` (`geonode:geonode`), world-readable on this box, versus staging's `.env` at a correct `600`. Low severity on this single-admin box today, but it's the one place `SUPERSET_SECRET_KEY` sits outside Docker, and it gets worse as more sudo accounts are added (`godwin`/`aremo`/`douglas`, already provisioned). | `curl` cert check; `grep CORS` (no match) in both configs; `stat -c '%a %U:%G'` on both `.env` files |

### Query API proof (full exchange)

Login: `GET /login/` (200) → `POST /login/` as `eval_runner` with the page's
own CSRF token → session cookie issued. Fresh API CSRF via
`GET /api/v1/security/csrf_token/`.

Request:
```json
POST /api/v1/chart/data
{
  "datasource": {"id": 21, "type": "table"},
  "queries": [{
    "columns": ["district"],
    "metrics": [{"expressionType": "SIMPLE", "aggregate": "COUNT",
                 "column": {"column_name": "id"}, "label": "count"}],
    "groupby": ["district"], "orderby": [], "row_limit": 50
  }],
  "result_format": "json", "result_type": "full", "force": true
}
```
Response: `200`, `rowcount: 50`, `colnames: ["district", "count"]`, e.g.
`{"district": "Iganga", "count": 1148}`, `{"district": "Kampala", "count": 260}`
… (full response saved; truncated here for length).

### Distinct-values proof (full exchange)

Request: same shape, `metrics: []`, `row_limit: 100`.
Response: `200`, `rowcount: 100`, `colnames: ["district"]`, 100 distinct
district names (`Kotido`, `Abim`, `Other`, `Butebo`, …).

### Dataset/metric export query

```sql
select json_agg(t) from (
  select tb.id as dataset_id, tb.table_name, tb.schema, tb.database_id,
         db.database_name, tb.sql is not null as is_virtual, tb.main_dttm_col,
         (select json_agg(json_build_object('column_name',c.column_name,
            'type',c.type,'is_dttm',c.is_dttm,'groupby',c.groupby,
            'filterable',c.filterable,'description',c.description) order by c.id)
          from table_columns c where c.table_id = tb.id) as columns,
         (select json_agg(json_build_object('metric_name',m.metric_name,
            'expression',m.expression,'metric_type',m.metric_type,
            'description',m.description,'d3format',m.d3format) order by m.id)
          from sql_metrics m where m.table_id = tb.id) as metrics
  from tables tb join dbs db on db.id = tb.database_id
  order by tb.database_id, tb.id
) t;
```
Run via `docker exec -i db-staging4cti psql "postgresql://superset_app:***@localhost:5432/cti_geonode_data?options=-csearch_path%3Dsuperset"`.
→ `docs/catalog.json` (280 datasets, 5 database connections).

### The 5 database connections — the single biggest finding in this audit

```
id | name                                     | host              | database          | scope
 1 | PostgreSQL                               | db4cti_geonode    | cti_geonode_data   | PRODUCTION
 2 | life_health                              | db4cti_geonode    | life_health        | PRODUCTION
 3 | GeoNode PostGIS (prod read - staging)     | db4cti_geonode    | cti_geonode_data   | PRODUCTION
 4 | Staging Postgres (db-staging4cti)         | db-staging4cti    | cti_geonode_data   | staging, genuinely isolated
 5 | GeoNode Core (cti_geonode)                | db4cti_geonode    | cti_geonode        | PRODUCTION
```

**4 of the 5 database connections on the "staging" Superset instance point at
the live production Postgres host (`db4cti_geonode`).** Only connection 4
is isolated. Decrypted directly via `superset shell` (`Database.sqlalchemy_uri`,
read-only). This confirms and extends a finding this project already made for
itself on 2026-09-25 (`AI_NATIVE_STATUS.md`, then 19/22 datasets) — it still
holds at the current scale (280 datasets) and across all 5 connections, not
just the ones audited then. The team's own prior decision was **"leave it as
is"**: no connection gets touched, but all production-backed datasets are
out of scope for AI-driven writes, RLS, and anything that assumes "staging
data." **Vima inherits this constraint — see Open decision 0.**

---

## B. Vima reference demo — carries over vs. must be replaced

Cross-checked against the actual demo file (`docs/design/vima-reference-demo.html`,
782 lines, single-file ECharts/D3/topojson app) rather than the plan's summary alone.

**Carries over as designed, confirmed in the demo code:**
- The spec shape (`id/title/version/theme/grid/filters/widgets`) and its
  `validate()` function — every check in the plan's "Checks" list is already
  implemented there (grid bounds, stack-overlap rule with an 8-per-slot cap,
  KPI-row placement, hex-color validation, at-most-one-map, dim/metric
  pairing via `DIM_ONLY`).
- Slots/stacks/tabs (`slotKey`, `SLOTS`, name-tab switching, `activate()`).
- JSON-Patch with widget-id path addressing (`applyOps`), including the
  `/widgets/-` append and remove-by-id forms.
- The fix-and-retry loop: `ask()` retries once on validation failure, feeding
  the errors back into the same prompt (`attempt < 2`).
- The panel (dock/float), per-chart `✦` scope, suggested prompts
  (`EXAMPLES`/`chartSuggestions`), light/dark theme toggle, undo/reset.
- Chart option builders per type (`chartOpt`) — bar/hbar/line/area/donut/
  heatmap/pyramid, including the pyramid's hard-coded `age_group`×`sex`
  requirement.

**Must be replaced, per the plan, confirmed by what's actually hard-coded:**
- `D` (the ~538 KB inline fake dataset) and `metric()`/`dimCats()`/`restrict()`
  → real Superset queries through the Vima server.
- `window.claude.use('sample')` (browser-side Claude calls, gated on the page
  running on claude.ai) → server-side Anthropic calls. Note: this repo
  **already has** a working server-side agent loop (`lifedata_ai/agent/`) —
  see the reuse list below.
- `localStorage` (`store.get/set`) → Postgres.
- The embedded Kenya counties TopoJSON + base64 basemap image (also on line
  177, alongside `D`) → live map tiles. This repo already has a working,
  non-Kenya-specific replacement (the boundary catalogue, see part A).
- The single HTML file → Next.js components, as scoped.
- The hand-written `validate()` → a Zod schema. The existing `validate()` is
  a legitimate reference for *what rules to port*, not code to reuse.

**Not mentioned in the plan's keep/replace list, but found during this audit
and relevant to scoping Phase 3+:** the demo supports 9 chart types + kpi/map/
table (11 widget kinds total). Real usage on this Superset instance spans 17
distinct `viz_type`s (`echarts_timeseries_bar` 263, `big_number_total` 248,
`pie` 191, `table` 105, `deck_scatter` 21, `echarts_timeseries_line` 15,
`big_number` 8, `gauge_chart` 8, `deck_polygon` 7, `deck_multi` 4, `funnel` 3,
`dist_bar` 3, `heatmap_v2` 2, `histogram_v2` 2, `country_map` 1, `treemap_v2` 1,
`mapbox` 1, `waterfall` 1, `sunburst_v2` 1, `cal_heatmap` 1, `box_plot` 1).
The demo's catalogue covers the big three (bar-family, pie/donut, table) well
but has no answer for gauge, funnel, treemap, sunburst, waterfall, histogram,
or calendar-heatmap — all in real use today. Not a blocker for Phase 0, but
Phase 2's Zod schema should decide now whether these are in scope or
explicitly deferred, rather than discovering the gap mid-migration.

---

## B′. The elephant in the room: this is not a greenfield build

**This audit's most important finding isn't in the Phase 0 table.** The
`lifedata-ai` repository already contains a product it calls, internally,
**"VIMA"** — `docs/vima-design-system.md`, `docs/vima-dashboard-quality-research.md`,
`docs/custom-dashboards-and-uploads.md`. It is live at
`sandbox.lifehealth.app/lifedata-ai/dashboards/` right now:

- A **custom dashboard UI** that renders its own chart cards — "Superset
  remains the query and permissions engine; published pages render their own
  chart cards, without a Superset dashboard iframe" (`custom-dashboards-and-uploads.md`).
  Each data request forwards the viewer's own Superset session — the exact
  per-user-identity invariant the new `Vima_Plan.md` also specifies.
- A **design system** (`vima-design-system.md`) with a fixed template
  (`buikwe-workspace`), its own CSS, slot-based layout.
- A **chat agent** (`lifedata_ai/agent/`) that already builds and publishes
  dashboards server-side via tool calls, with a documented 18-phase build
  history (`AI_NATIVE_STATUS.md`) covering native MCP read/write, per-user
  RLS groundwork, embedded per-tenant dashboards (guest tokens), self-healing
  charts, an eval harness, and more.
- A **boundary catalogue** already serving the exact capability
  `Vima_Plan.md`'s Phase 5 scopes.
- Its own **improvement plan** (`docs/improvement-plan.md`) scoring the
  current build 6/10 functionality, 5/10 UI/UX, **3/10 ease of use** for
  non-technical executives, with a 14–17 day remediation estimate — almost
  certainly the actual reason a clean, formally-specified rewrite
  (`Vima_Plan.md`'s Next.js/Zod/JSON-Patch architecture) was commissioned.

So `Vima_Plan.md` is best read as: **a disciplined rewrite of a system that
already exists and is already in real (if rough) use**, not a new product
being laid on top of bare Superset. That changes the shape of every phase
after this one — most urgently Open decisions 0 and 1 below.

---

## C. Current manual BI

62 dashboards exist on staging; most are QA/demo artifacts
(`[ untitled dashboard ]`, `QA-2026-09-07-dash-a`, duplicate
"Health facility census overview" ×4, "Iganga-Mayuge HDSS (old copy, unused)").
17 are `published = true`. By slice count and apparent real use:

| Dashboard | Slices | Notes |
| --- | --- | --- |
| Sickle Cell Disease Services — Buikwe District, Uganda (`scd-buikwe`) | 18 | Known production-pattern dashboard, PHI-adjacent (sickle cell) |
| Sickle Cell Screening Analytics (v2) (`scd-screening-v2`) | 14 | On the one genuinely isolated staging connection (id 4) |
| Buikwe HF Dashboard | 19 | Has a working LifeConnect widget per prior memory |
| Trans Nzoia Healt Facility | 22 | Largest facility-level dashboard |
| LifeHealth Sandbox · Population discovery | 55 | Largest slice count on staging; the named visual benchmark for `vima-design-system.md` |
| SINCEP - Africa | 37 | HDSS template, explicitly "has not been rebuilt or replaced" by the Oct 20 boundary work |

**Recommendation for Phase 3 acceptance targets:** `scd-buikwe` (Sickle Cell,
Buikwe), `Buikwe HF Dashboard`, and `LifeHealth Sandbox · Population discovery`
— they're real, actively-referenced (the last is literally the design
benchmark already in use), and between them exercise KPI, map, stacked
charts, and a table. Avoid `scd-screening-v2` as a *primary* target despite
being the cleanest data source — it's the one dataset pair with no
production-data entanglement, which makes it easy but not representative;
use it as the *secondary/regression* target instead, precisely because it's
simple and clean.

---

## Open decisions

### From the plan

0. **(New — not in the plan's list, but blocking everything else) Which
   system is Vima?** Is `Vima_Plan.md` a from-scratch Next.js rewrite that
   will eventually **replace** `lifedata-ai`'s existing custom dashboard UI,
   chat agent, and boundary catalogue — or a second, parallel system?
   **Recommendation:** rewrite-in-place. The existing boundary catalogue,
   agent tool scaffolding, and per-user-session identity model are already
   correct and hard-won (see `superset-integration-findings.md`'s 7 corrections
   to a near-identical spec); re-derive the Zod schema and Next.js renderer
   against them rather than rebuilding the Superset integration layer from
   zero. Treat `AI_NATIVE_STATUS.md` and `docs/runbook.md` as inputs to
   Phase 1, not as a separate legacy system to ignore.

1. **Data access.** The plan assumes a shared non-admin "service account."
   **The existing system already answered this differently and deliberately
   rejected that pattern**: `superset_config.py` states in-line that a shared
   MCP dev user is "the 'service account' pattern the orchestrator's identity
   invariant forbids," and instead mints a short-lived per-user JWT
   (`sub` = the requesting user's own verified Superset username) for every
   MCP call, with REST reads forwarding the user's own session cookie.
   **Recommendation:** keep the existing per-user model. It is Superset's
   own RBAC enforcing access, exactly as the plan's rule 5 ("Superset owns
   data access") requires, and rebuilding it with a shared service account
   would be a regression, not a simplification.

2. **Hosting.** No CORS configuration exists anywhere in this deployment;
   same-origin is already load-bearing for the existing panel injection.
   **Recommendation:** serve Vima's Next.js app reverse-proxied under the
   same origin (`sandbox.lifehealth.app`, eventually `dashboards.lifehealth.app`),
   the same pattern `lifedata-ai` already uses, rather than a separate domain
   that would require turning CORS on.

3. **Acceptance targets.** See part C above: `scd-buikwe`, `Buikwe HF
   Dashboard`, `LifeHealth Sandbox · Population discovery`, with
   `scd-screening-v2` as a secondary/regression target.

4. **Users and roles.** 19 active Superset users exist today across `Admin`,
   `Alpha`, `Gamma`, `Public`, `sql_lab`, plus two **unenforced** district-RLS
   roles. **Recommendation:** decide whether Vima launches before or after
   RLS is actually wired up (right now `wakiso_analyst`/`iganga_analyst` see
   everything their base role sees, despite the role names implying
   district-scoping) — launching Vima on top of this silently inherits a
   false sense of row-level security.

### Raised by this audit, not in the plan

5. **Production-data exposure.** 4 of 5 Superset database connections on
   staging read the live production Postgres host. The team's own prior
   decision (2026-09-25) was to leave this as-is but scope production-backed
   datasets out of AI-driven writes and RLS. **Recommendation:** Vima inherits
   the same scope limit explicitly, in writing, in Phase 1 — don't
   re-litigate it per-dataset, but don't let it be silently forgotten either
   as the dataset count has grown from 22 to 280 since that decision.

6. **RLS/RBAC is configured in name only.** Two roles are literally named
   for district-level RLS and zero filters back them; `DASHBOARD_RBAC` is on
   with zero dashboards restricted. **Recommendation:** before Phase 4
   (filters) or any pilot rollout, either wire real `row_level_security_filters`
   rows for these roles or rename them so nobody — including a future Vima
   user — reads "RLS: District Wakiso" as an existing guarantee.

7. **`~/projects/superset/.env` (production) is mode 664, not 600.**
   World-readable on a box with multiple sudo accounts. **Recommendation:**
   `chmod 600` as routine hygiene; unrelated to Vima directly but found
   during this audit and cheap to fix.

8. **The PHI privacy gate is still absent.** Already tracked in memory
   (`project_cti_geonode_lifedata_ai_copilot`) and reconfirmed here: no
   `registry.yaml` or equivalent allowlist exists in this repo anymore — it
   was deliberately removed 2026-09-25, "gate later, in production." Vima's
   catalogue-driven design (Claude may only use what `catalog.json` lists)
   is actually a good enforcement point to reintroduce gating cheaply —
   worth deciding now whether Phase 1's catalogue loader filters
   `catalog.json` against a restored allowlist, rather than treating this as
   a Phase-7 hardening afterthought again.

---

## Gaps / not fully answered in this pass

- No systematic performance benchmark across all 280 datasets — only one
  representative query was timed (566 ms, virtual dataset, uncached). If a
  specific dataset is known to be the slow one in practice, re-run this
  measurement against it before Phase 1 commits to caching assumptions.
- Production Superset (`dashboards.lifehealth.app`) was only compared at the
  config level (auth type), not probed live with the same proof queries —
  Phase 0's table targets "the existing Superset deployment" and Decision 0
  needs resolving before it's clear whether prod needs its own audit pass too.
- `catalog.json` reflects staging's 280 datasets as of this run. It has not
  been cross-checked dataset-by-dataset against `lifedata_ai`'s (now-removed)
  semantic allowlist concept — see Open decision 8.
