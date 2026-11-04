# Vima build plan and Claude handover

Nov 5, 2026 · @Godwin

Claude Code builds Vima in eight phases, starting with an audit of the Superset deployment. Nothing gets written until that audit is done and you have answered the open decisions.

## How to use this doc

Vima is an AI BI app that sits on the existing Apache Superset. Superset runs the queries and enforces data access. Claude edits each dashboard by patching a JSON spec, and Vima's own frontend draws everything. The published demo is the target for look and behaviour.

Hand Claude Code these four things:

1. **This doc.** Its plan, rules and spec contract are the brief.
2. **The reference demo:** `vima-reference-demo.html`, plus the live demo page. It's working code for the spec model, the stacked chart slots, the checks, Claude's JSON patches, the filters and the Vima panel. It was built against fake data, so treat it as a reference implementation, not production code.
3. **Access, read-only first:**
   - the Superset deployment repo or config (`superset_config.py`, docker-compose or Helm values)
   - the Superset URL and a non-admin service account
   - a read-only connection to the analytics database
4. **Two or three dashboards from the current manual BI.** These are the acceptance targets.

I haven't seen your Superset code or deployment, so nothing in this plan assumes how it is set up. Phase 0 finds that out.

## Phase 0: Audit

The audit changes nothing. It produces `AUDIT.md`, a dataset catalogue and a list of gaps. Every later phase builds on those.

**A. Superset deployment.** Read the config and call the REST API with the service account.

| Area | What to check | Why Vima needs it |
| --- | --- | --- |
| Version and install | Superset version; docker-compose, Helm or pip; `superset_config.py`; feature flags | Decides which API endpoints and options exist, and whether the built-in MCP server is available |
| Auth and roles | Auth type (DB, OAuth, LDAP); roles; row-level security rules; which datasets each role sees | Vima users must map to Superset roles, so data access is enforced by Superset, never only by Vima |
| Datasets and metrics | Every dataset: columns, types, saved metrics, time columns; exported as `catalog.json` | This is Vima's semantic layer. Claude may only use what it lists |
| Query API | `POST /api/v1/chart/data` with a saved metric, a group-by and an `IN` filter, compared with the same query in Explore | Proves the headless query path, row limits and filter format |
| Distinct values | A columns-only query for filter options, with a parent filter applied | Cascading filters depend on it |
| Performance | Cache backend (Redis or none); async queries (Celery); `ROW_LIMIT`; timeouts; time taken by the slowest dataset | Sets caching and aggregation needs, and how many widgets can load at once |
| Geodata | Is PostGIS available; which boundary levels exist (county, sub-county, ward); join codes; point tables with lat/lng | Decides how map boundaries are served and joined |
| Security | Admin credentials in config or env; CORS; HTTPS; who can reach the API | The Vima server must hold only a non-admin token |

**B. Vima reference demo.** List what carries over unchanged, and what gets replaced:

- **Keep:** the spec shape; slots and stacks; the check rules; JSON-patch edits that address widgets by id; the fix-and-retry loop; the panel and per-chart prompts; light and dark themes; chart option builders.
- **Replace:**
  - the in-browser data and metric functions, with Superset queries through the Vima server
  - the browser's Claude calls, with server-side Anthropic API calls
  - browser storage, with Postgres
  - the saved basemap image, with live map tiles
  - the single HTML file, with Next.js components
  - the hand-written checks, with a Zod schema

**C. Current manual BI.** List the dashboards in use, who uses them, and how often they are refreshed. Pick two or three as acceptance targets for Phase 3.

**Done when:** `AUDIT.md` and `catalog.json` exist, every row in the table above has an answer, and the open decisions below have been put to you.

## Target architecture

There are three tiers. The browser draws, the Vima server holds every credential and every rule, and Superset owns the data and who may see it.

&#91;embedded content: Vima architecture · browser, server, Superset, Postgres, Claude\]

Claude gets the catalogue and the spec, plus small samples from `sample_data` when it asks. It never receives full query results.

| Layer | Choice |
| --- | --- |
| App | Next.js (App Router), TypeScript, Tailwind, shadcn/ui |
| Grid | CSS grid filling the window, from the spec's `grid`; react-grid-layout only if you later want drag-to-move |
| Charts | Apache ECharts inside Vima's own components |
| Map | MapLibre GL, Deck.gl, PostGIS or PMTiles boundaries |
| State | Zustand for filter selections, TanStack Query for fetching and caching |
| Spec | Zod schema, JSON Schema export, JSON Patch |
| Data | Superset REST API through the server proxy; Postgres for Vima's own data |
| AI | Anthropic API on the server, with tool use |
| Tests | Vitest for the schema and patches; Playwright end-to-end, including the one-screen check |

```text
vima/
  app/                 pages, /api/query, /api/agent, /api/dashboards
  components/          Grid, Slot, StackTabs, Kpi, charts/, MapWidget, Table, VimaPanel
  lib/spec/            schema.ts, checks.ts, patch.ts, catalog.ts
  lib/superset/        client.ts, toChartData.ts
  lib/agent/           prompt.ts, tools.ts
  db/                  migrations
  docs/                VIMA_PLAN.md, AUDIT.md, catalog.json
  reference/           vima-reference-demo.html
```

## Product rules and the Dashboard Spec

The spec is the contract. Claude edits only the spec, the renderer draws only the spec, and every change passes the checks first.

**Product rules (hard checks, not guidelines):**

1. **One screen.** A dashboard fits the window with no page scrolling. Only a table may scroll, inside its own card.
2. **Map is optional.** It's an ordinary widget type, at most one per dashboard. Many dashboards have none.
3. **More charts than slots go in stacks.** Widgets with the same layout share a slot, up to 8 per slot. Their names sit side by side under the card, and clicking a name brings that chart to the front. KPI cards don't stack.
4. **Edits can target one chart.** Vima can change anything from the theme down to one chart's colour, labels, type, target line or position.
5. **Superset owns data access.** The browser never calls Superset, and Vima never uses an admin account.

**Spec shape** (as in the demo):

```json
{
  "id": "facility-performance", "title": "Facility performance", "version": 7,
  "theme": { "mode": "light", "accent": "#2a78d6" },
  "grid": { "cols": 24, "rows": 7, "kpi_row": true },
  "filters": [ { "id": "f_region", "source": { "dataset_id": 100, "column": "region" } },
               { "id": "f_county", "source": { "dataset_id": 100, "column": "county" }, "depends_on": ["f_region"] } ],
  "widgets": [
    { "id": "w11", "type": "chart", "chart": "line", "title": "Reporting rate", "tab": "Reporting",
      "query": { "dataset_id": 42, "metric": "reporting_rate", "dim": "visit_month", "series": null, "top": null },
      "filter_map": { "f_region": "region", "f_county": "county" },
      "style": { "color": "#1baf7a", "target": 90 },
      "interactions": { "click_to_filter": null },
      "layout": { "x": 20, "y": 1, "w": 4, "h": 2 } }
  ]
}
```

**Widget types:** `kpi`, `chart` (bar, hbar, line, area, donut, pie, heatmap, pyramid, scatter), `map`, `table`. Other types are added to the catalogue first, then to the schema.

**Checks**, run on every save and every patch:

- Every metric, column and dataset exists in `catalog.json`, and every metric and dimension pairing is allowed there.
- Each layout sits inside the grid, and widgets overlap only as exact stacks. KPIs go only in the KPI row, with `h = 1`.
- There is at most one map, and colours are `#rrggbb`.
- Filter dependencies form no cycle.
- A test query runs for every changed widget and must return without error.

**Edits** use JSON Patch with widget ids in the path, for example `/widgets/w11/style/color`, and each edit creates a new version. If a patch fails the checks, the errors go back to Claude for one retry. If it still fails, nothing changes.

## Build phases

Each phase ends with something you can open and check. Claude Code stops for your review at every "Done when" before starting the next phase.

1. **Phase 1: Foundation.** Set up the Next.js (TypeScript) repo, sign-in, and the Vima Postgres database: users, roles, dashboards, dashboard versions, conversations, audit log. Build the Superset client with a cached, non-admin token, and the `POST /api/query` proxy that turns a widget spec into a `chart/data` call.
   - Done when: a widget query through Vima returns the same numbers as the same query in Superset Explore, for two datasets.
2. **Phase 2: Spec and checks.** Port the demo's spec into a Zod schema and generate JSON Schema from it. Build the catalogue loader on top of `catalog.json`, and the patch engine with widget-id paths. Add unit tests using the demo's three dashboards, plus broken specs that must be refused.
   - Done when: every check listed above has a passing test and a failing test.
3. **Phase 3: Renderer.** Build the one-screen grid with slots, stacks and name tabs. Add KPI cards with sparklines, the ECharts components, the table, the light/dark toggle, and empty and error states.
   - Done when: your acceptance dashboards are rebuilt from hand-written specs and fit 1366×768 and 1920×1080 with no page scroll.
4. **Phase 4: Filters.** Build cascading multi-select dropdowns fed by distinct-value queries, the per-widget `filter_map`, click-to-filter on charts, the period picker, and filter state in the URL.
   - Done when: picking a parent removes child values that are no longer valid, and a widget reloads only when a filter it uses changes.
5. **Phase 5: Map.** Use MapLibre and Deck.gl with a boundary registry served from PostGIS or PMTiles. Shade boundaries by joining on boundary codes, never names. Show points with server-side binning when there are many, zoom to the selection, and use a dark basemap in dark mode.
   - Done when: the map filters like any other chart, and the largest point dataset found in the audit pans smoothly.
6. **Phase 6: Vima agent.** Call the Anthropic API from the server, with tool use. The tools are `list_datasets`, `get_dataset`, `sample_data`, `get_spec` and `apply_patch`; `apply_patch` runs the checks and the test queries. Build the panel (docked or floating), per-chart ✦ scope, suggested prompts, chat history per dashboard, versions and undo.
   - Done when: every example prompt from the demo produces a valid change, and a refused patch never changes a dashboard.
7. **Phase 7: Hardening and rollout.** Add data-access tests for each role, rate limits, query timeouts and caching. Add Playwright end-to-end tests, including the one-screen check at both window sizes. Then run a pilot with real users and plan the switch-off of the manual BI.
   - Done when: the pilot users sign off and the manual BI's dashboards are matched in Vima.

## Decisions you need to make

Phase 1 can't start until the first four are answered. The rest can wait until their phase.

- [ ] **Data access:** does each Vima user map to their own Superset user and role, so Superset's row-level security applies directly? Or does Vima use one non-admin service account and add each role's scope filters on the server?
- [ ] **Hosting:** where the Vima app, its Postgres database and the boundary tiles run, and on which domain.
- [ ] **Acceptance targets:** which two or three current dashboards must be matched first.
- [ ] **Users and roles:** who uses Vima at launch, and which roles exist.
- [ ] **Boundaries:** which admin levels the maps need (county, sub-county, ward) and their source of truth.
- [ ] **Claude model and budget:** which model Vima calls, and a monthly usage cap.
- [ ] **Who can edit:** whether every user can ask Vima to change a dashboard, or only editors. Others would get personal copies.
- [ ] **Data freshness:** how often each dataset refreshes, which sets cache lifetimes.

## Kickoff prompt for Claude Code

Paste this into Claude Code in the Vima repo. Add this doc as `docs/VIMA_PLAN.md` and the demo as `reference/vima-reference-demo.html` first.

```text
You are the lead engineer for Vima, an AI BI app on top of our existing Apache Superset.
Read docs/VIMA_PLAN.md in full, then read reference/vima-reference-demo.html. It is the
reference implementation of the spec model, stacked chart slots, checks, JSON-patch edits and
the Vima panel. It was built on fake in-browser data.

Your task now is Phase 0 (Audit) only.
- Read-only: do not change Superset, its config, or any database. Use only the
  service account I give you.
- Audit the Superset deployment against every row of the Phase 0 table and write
  docs/AUDIT.md with an answer per row, the evidence (file, endpoint, query) and any gaps.
- Export every dataset with its columns, types and saved metrics to docs/catalog.json.
- Run one chart/data query and one distinct-values query and record the request and response.
- List what in the reference demo carries over and what must be replaced, as in Phase 0 part B.
- End with the open decisions from the plan, plus any new ones the audit raised,
  and your recommendation for each.

Then stop and wait for my review. Do not start Phase 1.
```

For each later phase, start a fresh session with the same first two lines and "Your task now is Phase N". Also include `AUDIT.md` and the answered decisions.
