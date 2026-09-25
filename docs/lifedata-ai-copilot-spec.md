# LifeData AI Copilot — Implementation Specification

**Document purpose:** Build specification for adding an AI conversational copilot to an existing Superset-based analytics deployment. Written to be handed to an AI coding agent (e.g. Claude Code) phase by phase. Each phase has explicit acceptance criteria; do not start a phase until the previous one passes.

**Status:** POC scope. Decisions locked: Anthropic Claude API as the LLM provider; Superset with database (Flask-AppBuilder DB) authentication; Superset 6.x (pin the exact minor version installed at build time and record it in `README`); nginx reverse proxy in front of the existing Superset container is permitted.

---

## 0. Context the coding agent must internalise

- Superset already runs in Docker and must remain **unmodified** — no forks, no patched images. All integration happens via (a) nginx-level HTML injection of a panel script, (b) Superset's REST API, (c) a new Django app that acts as the orchestrator.
- The orchestrator is added to the **existing Django project** in this repository as a new app named `lifedata_ai`. Reuse the project's existing settings, database, and deployment conventions. Do not introduce a second web framework.
- LifeHealth operational data and dynamic-form submissions are already in the database and already exposed to Superset via datasets/views. Data collection is out of scope.
- Superset 6 ships first-class theming (JSON theme objects, dark mode) — use it for the "apply theme" capability rather than raw CSS injection.
- Superset 6.x includes an official MCP effort; for this POC we call the Superset REST API directly from Python tool functions. Keep every tool behind a thin interface (`tools/base.py`) so an MCP backend can be swapped in later without touching the agent loop.

### Non-goals for the POC (do not build)

Voice control, multi-tenancy, patient-level AI analysis, free-form NL→SQL, arbitrary CSS generation, automatic ingestion of every dynamic form, cohort construction, clinical decision support, live collaborative editing, pixel-animation of layout changes.

---

## 1. Architecture

```text
Browser
  │  (same origin: https://analytics.example)
  ▼
nginx
  ├── /                → superset container (unchanged)
  │       sub_filter injects <script src="/lifedata-ai/static/panel.js">
  │       into text/html responses
  └── /lifedata-ai/*   → Django (existing project, new app `lifedata_ai`)

Django app `lifedata_ai` (orchestrator)
  ├── Agent loop against Anthropic Messages API (tool use)
  ├── Tool registry  (read tools run server-side; write tools are
  │                   validated and returned to the panel to execute)
  ├── Action validator (jsonschema + policy rules)
  ├── Semantics registry (datasets, grains, indicators, geography rules)
  ├── Conversation + audit + snapshot models
  └── Superset API client (httpx, per-request session cookie passthrough)

Panel (vanilla TS/JS bundle, no framework dependency on Superset internals)
  ├── Sidebar UI: chat history, input, action preview cards, undo
  ├── Page-context collector (URL, bootstrap data, filter state)
  └── Same-origin executor: performs validated write actions against
      Superset REST API with the user's own session + CSRF token
```

### Execution and identity model — the critical invariant

**Write actions are always executed by the user's own browser session, never by a service account.** The orchestrator holds no Superset credentials with write access.

1. Panel sends `{message, page_context}` to `POST /lifedata-ai/api/chat/`, cookies forwarded automatically (same origin).
2. Django middleware verifies identity by calling Superset `GET /api/v1/me/` with the forwarded `session` cookie (cache result per session for 60 s). Reject if unauthenticated. This is how DB auth is bridged — no shared SSO needed.
3. Agent loop runs. **Read tools** (metadata, aggregates) execute server-side using the same forwarded session cookie, so reads are also permission-scoped to the user.
4. **Write tools** do not execute server-side. The loop emits a validated, signed action envelope; the panel executes it same-origin (fetching a CSRF token from `GET /api/v1/security/csrf_token/` first) and POSTs the result back to `POST /lifedata-ai/api/action-result/`, which resumes the agent loop.
5. Every proposed action, validation verdict, execution result, and snapshot is written to the audit tables server-side.

Consequences: the AI can never exceed the logged-in user's Superset permissions; viewer/analyst/owner behaviour in the product brief falls out of Superset roles with zero extra role logic in the POC.

### Privacy boundary

The Claude API only ever receives: dataset/chart/dashboard **metadata**, saved metric definitions, semantics-registry entries, **aggregate** query results (row-capped, see §5), active filters, and layout structure. No tool may return row-level records. Small-cell suppression is enforced **in the reporting views themselves** (e.g. `HAVING count(*) >= 5`), not in runtime code the agent could route around. Patient-geography datasets are flagged in the registry and the validator rejects any grouping below district level and any point-map viz type for them (§6).

---

## 2. Repository layout to create

```text
lifedata_ai/
  __init__.py
  apps.py
  models.py                 # Conversation, Message, ActionRecord, Snapshot
  middleware.py             # Superset session identity verification
  views/
    chat.py                 # POST /api/chat/  (SSE streaming response)
    action_result.py        # POST /api/action-result/
    conversations.py        # list/get/delete conversations
    undo.py                 # POST /api/undo/
  agent/
    loop.py                 # Anthropic Messages agent loop
    prompts.py              # system prompt assembly (incl. registry excerpt)
    context.py              # page-context normalisation
  tools/
    base.py                 # Tool interface: name, schema, mode: read|write
    registry.py             # tool discovery + Anthropic tool-spec export
    superset_read.py        # list_databases, list_datasets, get_dataset,
                            # get_chart, get_dashboard, get_chart_data,
                            # list_charts, list_dashboards, get_page_context
    superset_write.py       # create_chart, update_chart, create_dashboard,
                            # add_chart_to_dashboard, remove_chart_from_dashboard,
                            # move_dashboard_component, resize_dashboard_component,
                            # apply_dashboard_theme, add_native_filter,
                            # rename_chart, rename_dashboard, refresh_chart
    lifedata.py             # explain_indicator, recommend_dataset,
                            # review_dashboard_design, list_forms, get_form_schema
  validation/
    schemas/                # jsonschema per write action
    validator.py            # schema + policy checks (viz allowlist, geography,
                            # theme allowlist, no adhoc SQL, risk classification)
    layout.py               # semantic layout ops → position_json compiler
  semantics/
    registry.yaml           # datasets, grains, sensitivity, indicators, themes
    loader.py
  superset_client.py        # httpx wrapper, cookie passthrough, error mapping
  static/panel/             # TS source + built panel.js (esbuild, no React
                            # required; keep bundle < 150 KB gzipped)
  migrations/
  tests/
deploy/
  nginx/lifedata.conf       # injection + routing (template below)
```

Register the app in `INSTALLED_APPS`, mount URLs at `/lifedata-ai/`, and add settings: `LIFEDATA_AI_SUPERSET_INTERNAL_URL`, `ANTHROPIC_API_KEY` (env only, never committed), `LIFEDATA_AI_MODEL` (default `claude-sonnet-4-6`; verify current model strings against https://docs.claude.com before hardcoding), `LIFEDATA_AI_MAX_TOOL_ROUNDS` (default 12).

---

## 3. nginx integration

```nginx
# deploy/nginx/lifedata.conf — adjust upstream names to the compose file
upstream superset   { server superset:8088; }
upstream lifedata   { server django:8000; }

server {
  listen 443 ssl;
  server_name analytics.example;

  location /lifedata-ai/ {
    proxy_pass http://lifedata;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;              # required for SSE streaming
  }

  location / {
    proxy_pass http://superset;
    proxy_set_header Host $host;
    proxy_set_header Accept-Encoding "";   # disable upstream gzip so
                                           # sub_filter can operate
    sub_filter '</head>'
      '<script defer src="/lifedata-ai/static/panel/panel.js"></script></head>';
    sub_filter_once on;
    sub_filter_types text/html;
  }
}
```

Notes: `Accept-Encoding ""` is mandatory or `sub_filter` silently does nothing on gzipped responses; re-enable compression at the nginx edge with `gzip on`. Superset's session cookie and CSRF flow are unaffected because everything stays on one origin. If Superset sets a Content-Security-Policy via Talisman that blocks the script, extend `TALISMAN_CONFIG` in `superset_config.py` (config change only — still no image modification).

---

## 4. Data model (Django)

- **Conversation**: `id (uuid)`, `superset_username`, `created_at`, `title`, `page_scope` (nullable dashboard/chart id it started on).
- **Message**: `conversation FK`, `role (user|assistant|tool)`, `content (json)`, `created_at`. Store raw Anthropic content blocks so the loop can be resumed faithfully.
- **ActionRecord**: `conversation FK`, `action_type`, `payload (json)`, `validation_result (json)`, `risk (low|preview)`, `status (proposed|approved|executed|failed|undone|cancelled)`, `executed_by_username`, `superset_object_type/id`, timestamps.
- **Snapshot**: `action FK`, `object_type (chart|dashboard)`, `object_id`, `state (json)`, `captured_at`. For charts capture `params` + `query_context` + `slice_name`; for dashboards capture `position_json` + `json_metadata` + `dashboard_title` + chart membership.

Undo = restore the most recent executed ActionRecord's snapshot for this conversation via a normal validated write action (so undo itself is audited and permission-checked). One-level undo per object is sufficient for the POC; refuse with a clear message if the object changed since the snapshot (compare `changed_on` from Superset).

---

## 5. Agent loop and tools

### Loop (`agent/loop.py`)

Standard Anthropic tool-use loop against `POST https://api.anthropic.com/v1/messages` with `tools=[...]` exported from the registry and `stream=True`, relayed to the panel over SSE. Hard stops: `LIFEDATA_AI_MAX_TOOL_ROUNDS`, 60 s wall clock per turn, and a per-turn cap of 3 write actions. When the model emits a write tool call:

1. Validate (§6). On failure, return the validation error to the model as the tool result so it can self-correct (max 2 retries per action).
2. If risk = `low` → return the signed action envelope to the panel for immediate execution; loop pauses awaiting `/action-result/`.
3. If risk = `preview` → emit a preview card (human-readable summary + the structured actions); loop ends. User's Apply triggers execution of the stored ActionRecords in order; Cancel marks them cancelled.

Action envelope: `{action_id, action_type, method, path, body, headers_required:[csrf], expires_at}` — signed with `django.core.signing` so the panel cannot alter it, expiring in 5 minutes. The panel executes exactly `method path body` and nothing else.

### Read tools (server-side, cookie passthrough)

`list_databases`, `list_datasets(q)`, `get_dataset(id)` (columns, types, descriptions, saved metrics), `list_charts(q)`, `get_chart(id)` (viz type, params, dataset), `list_dashboards(q)`, `get_dashboard(id)` (charts, `position_json` summarised, native filters, metadata), `get_chart_data(chart_id)` — calls `POST /api/v1/chart/data` with the chart's stored query context, `row_limit` forced ≤ 200, result truncated to ≤ 50 rows / 20 columns before being shown to the model. `get_page_context` echoes the normalised context the panel supplied.

### Write tools

Exactly the action list in §2 `superset_write.py`. Chart creation/update goes through `POST/PUT /api/v1/chart/` with `params` (form_data) that the validator has approved; dashboard membership and layout via `PUT /api/v1/dashboard/{id}` on `position_json`/`json_metadata`; native filters via `json_metadata.native_filter_configuration`. `apply_dashboard_theme` selects only from registry-approved themes (Superset 6 theme objects and/or pre-created CSS templates referenced by id — the model never emits CSS).

### LifeData tools

- `recommend_dataset(analysis_goal)` — ranks registry datasets by declared subject areas; returns dataset ids + why.
- `explain_indicator(name)` — returns the governed definition verbatim from the registry (e.g. malaria positivity = positive completed tests ÷ eligible completed tests, excluding cancelled/rejected; counts tests, not patients).
- `review_dashboard_design(dashboard_id)` — deterministic checks producing findings the model turns into a beautification proposal: KPI row present/at top, mixed widget heights in a row, > 8 slices in a pie, missing number formats, duplicate charts, title length > 60 chars, legend redundancy for single-series charts.
- `list_forms` / `get_form_schema(form_id)` — read the dynamic-forms metadata tables (form, version, questions, types, units, sensitivity flag) so the model can propose charts on the pre-built flattened reporting views. POC scope: wire **one or two** forms whose reporting views already exist; do not build a generic flattener.

### System prompt (`agent/prompts.py`)

Assemble per request: role and hard rules (interpretation transparency; never present suspected as confirmed, orders as results, prescriptions as dispensed; patients ≠ encounters; no diagnosis/treatment advice; missing data ≠ evidence of absence; always state the interpretation used, e.g. "I used unique patients with confirmed hypertension diagnoses in the selected period, grouped by facility"), the page context block, and a **relevant excerpt** of the semantics registry (registry entries for datasets matching the current page/query — not the whole file — to control token usage).

---

## 6. Validation policy (`validation/`)

Every write action must pass, in order:

1. **jsonschema** for its action type (strict, `additionalProperties: false`).
2. **Viz allowlist** (POC): `big_number_total`, `echarts_timeseries_line`, `echarts_timeseries_bar`, `bar` (horizontal via orientation), `pie`, `table`, `histogram_v2`, `deck_polygon`/choropleth for district-level maps only.
3. **No adhoc SQL**: `query_mode` must be aggregate; metrics must reference saved metrics or simple aggregations over registry-declared measure columns; reject any `adhocMetric` with `expressionType: SQL` and any `adhoc_filters` containing raw SQL clauses.
4. **Geography policy**: if the dataset's registry entry has `geography: patient`, reject group-bys below `district` and reject point/scatter/heat map viz types; `geography: facility` may use exact coordinates.
5. **Theme/palette allowlist**: `apply_dashboard_theme` and `color_scheme` values must exist in the registry (`lifedata_clinical_light`, `lifedata_executive_dark`, `lifedata_monitoring` to start).
6. **Risk classification** → `low`: rename, sort change, legend/label toggle, single chart create, add one chart to a dashboard the user owns, resize/move one component, refresh. `preview`: dashboard creation with > 2 charts, any action touching > 2 components, layout rebuilds, theme application, removals, anything on a dashboard the user does not own (ownership from `get_dashboard`).
7. **Layout compiler** (`validation/layout.py`): the model expresses layout semantically — `make_full_width(chart)`, `move_after(chart, target)`, `kpi_row([charts])`, `equalize_row(row)` — and the compiler alone mutates `position_json` (12-column grid; ROW/COLUMN/CHART/TAB node types; ids `CHART-xxxx` must be preserved, never regenerated). Write a round-trip property test: parse → compile no-op → byte-equal semantics. The model never emits raw `position_json`.

---

## 7. Panel (`static/panel/`)

Plain TypeScript, esbuild, zero runtime dependencies. Renders a right-hand sidebar (toggle button fixed bottom-right; sidebar 380 px, collapsible; do not obstruct Superset's own drawers). Features: conversation list + history, streaming assistant output (SSE), tool-activity indicators ("inspecting dataset…"), action preview cards with Apply/Cancel, per-action Undo buttons, error surfaces.

Page-context collector, recomputed on route change (Superset 6 is an SPA — hook `history.pushState`/`popstate`):

- URL parsing → `{page_type: dashboard|explore|chart_list|dataset_list|sqllab|other, dashboard_id?, chart_id?, dataset_id?}`
- Native filter state from the URL/permalink state where present
- Edit-mode flag from DOM (`.dashboard--editing` class or equivalent — verify against the pinned Superset version and record the selector in code comments)
- Selected widget: track last-clicked chart element's `data-test`/`chart-id` attribute; treat as "this chart" hint only, with the assistant confirming by name when acting on it.

Same-origin executor: `GET /api/v1/security/csrf_token/` (cache per session), then execute the envelope with `X-CSRFToken` + `Content-Type: application/json`, then POST outcome (status, response body excerpt, object id) to `/lifedata-ai/api/action-result/`. After success, refresh affected content: for the current dashboard trigger Superset's refresh, or fall back to a soft reload of the dashboard route.

---

## 8. Semantics registry — starter content (`semantics/registry.yaml`)

Structure (fill values from the actual Superset datasets during Phase 1):

```yaml
datasets:
  - name: rpt_encounters
    superset_dataset: <id or unique name>
    grain: encounter
    subject_areas: [encounters, facilities, utilisation]
    sensitivity: aggregate_ok
    geography: facility
    notes: "One row per completed encounter. Patient counts require COUNT(DISTINCT patient_id)."
  - name: rpt_lab_orders
    grain: lab_order
    subject_areas: [laboratory, turnaround]
    status_field: order_status   # ordered|collected|resulted|verified|cancelled|rejected
    notes: "Order ≠ completed test. TAT valid only where verified_at is not null."
  - name: rpt_patient_geography
    grain: patient
    geography: patient           # triggers district-floor + no-point-maps policy
indicators:
  - name: malaria_positivity
    definition: "positive completed malaria tests / eligible completed malaria tests"
    numerator: "result = positive AND status = verified"
    denominator: "status = verified AND NOT cancelled AND NOT rejected"
    unit: percent
    counts: tests               # not unique patients
themes:
  - id: lifedata_clinical_light
  - id: lifedata_executive_dark
  - id: lifedata_monitoring
layout_presets:
  - id: executive_overview      # KPI row → trends → breakdowns → map/detail
  - id: laboratory_performance
```

This file is the single highest-leverage artifact: interpretation (prompt) and enforcement (validator) both read it. Keep it under version control and reviewed like code.

---

## 9. Build phases with acceptance criteria

**Phase 1 — Identity, context, read-only assistant.** nginx conf + panel skeleton + middleware + agent loop + read tools + registry loader. *Accepts when:* a logged-in DB-auth user opens any dashboard, opens the panel, asks "what datasets are available for laboratory analysis?" and gets an answer grounded in live Superset metadata naming real datasets; "explain this chart" works from page context; an anonymous request to `/lifedata-ai/api/chat/` is rejected; all tool calls appear in the audit log with the username.

**Phase 2 — Chart create/update + undo.** Write-action pipeline end-to-end, chart schemas for the allowlisted viz types, snapshots, undo, action-result resumption. *Accepts when:* "create a monthly chart of encounters by facility" produces a saved chart via the user's own session; "make it horizontal bars, top ten only" updates it; undo restores it; a request that would need raw SQL is refused with an explanation; a viewer-role user is cleanly told they cannot save charts (Superset 403 surfaced, not swallowed).

**Phase 3 — Dashboards, layout, filters, preview.** Dashboard creation, add/remove chart, semantic layout compiler, native filters, preview/confirm cards, per-turn write caps. *Accepts when:* "build a laboratory performance dashboard…" yields a preview card, Apply creates dashboard + charts + a sensible layout + date and facility filters; "make the trend full width" and "put the KPIs across the top" work on the live dashboard; layout round-trip tests pass; actions on an un-owned dashboard always preview.

**Phase 4 — Beautification, themes, semantics depth, one dynamic form.** `review_dashboard_design`, theme application via Superset 6 theming, indicator explanations, geography enforcement tests, one flattened form wired in. *Accepts when:* "beautify this dashboard" proposes concrete findings and Apply executes them; "apply the executive dark theme" works and an unapproved theme is refused; "map patient attendance" produces a district choropleth and a sub-district or point-map request on patient data is refused with the policy stated; "explain the malaria positivity chart" returns the governed definition; a chart is built from the wired dynamic form.

**Cross-phase engineering rules:** every validator rule gets a failing-case test; `@transaction.atomic` around any view that writes ActionRecord + Snapshot together; reference/id generation collision-safe; secrets only from env; `httpx` timeouts on every Superset call (connect 5 s, read 30 s); log Anthropic request ids in the audit trail for traceability.

---

## 10. Known Superset 6.x integration points to verify at build time

Pin the deployed version, then verify (and note in code comments): the exact `/api/v1/chart/data` query-context shape for the pinned version; `position_json` node schema; theming API surface (Superset 6 theme objects vs CSS templates — prefer theme objects, keep CSS templates as fallback); the `me` endpoint path (`/api/v1/me/`); CSRF endpoint behaviour with `WTF_CSRF` settings; SPA route-change hooks for the context collector; Talisman CSP config keys. Where the docs are ambiguous, read the Superset source for the pinned tag rather than guessing.
