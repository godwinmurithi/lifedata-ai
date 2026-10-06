# Vima dashboard design baseline — v1

Status: implemented. Visual benchmark: Buikwe Sandbox / Population discovery.
Live library: https://sandbox.lifehealth.app/lifedata-ai/dashboards/?design-library=1
Benchmark: https://sandbox.lifehealth.app/lifedata-ai/dashboards/2e75e425-ae09-4cd8-acf6-6fa4dcfabae3/

## Exact screen template

Use the `buikwe-workspace` publication preset for new custom client dashboards.
It imports the actual `population.css` and `Pie` renderer used by Buikwe, retaining
its five-column, three-row screen, top selector strip, gauge, compact chart panels,
bottom tabs, central map, lower sequential flow and authorized records table.
`workspace` configuration maps the current client's fields and questions into
those fixed visual positions; position names such as `blood` are slot identifiers,
not required clinical fields. The raw source chart must belong to the current
Superset dashboard. All projections are checked against that client's dataset.
Shared filters update the gauge, all question cards, map counts, flow and details.
Array responses count each selected option once per record. Capped records are
explicitly labeled, never reported as a complete dataset.

The live SINCEP publication uses this exact template with five assessments,
four countries, all 16 source chart references, and additional recorded multiple
answer questions. Its map uses the actual recorded countries' boundaries;
Site points can be placed at country centroids when requested, explicitly labeled as centroid placements rather than surveyed coordinates. Country geometries are reused from Superset's
existing country-map assets; other clients require boundaries for their own
recorded geography. The generic card grid remains an explicit alternate layout.

## Presentation and data are separate

The benchmark and library are styling/layout guides only. Their chart IDs,
datasets, queries, filters and clinical fields are not dependencies of new
publications. Vima inspects the current client's dataset and creates executable
charts for that client's questions, then applies the shared presentation.
Charts created through MCP may have saved `params` but no `query_context`.
The read tool and custom viewer reconstruct supported simple categorical,
raw-table and KPI queries from that chart's own dataset and params. They retain
filters and row limits. Complex plugin transformations, unsupported filters and
malformed definitions require an explicit executable chart definition rather
than guessing data logic. Existing saved query contexts remain authoritative.

## Client data is variable

The benchmark defines presentation, not a fixed dataset. The 47 facilities are
Buikwe's current records, not a requirement or limit. Each client supplies their
own facility list, count, level taxonomy, bed counts, coordinates and questions.
Map titles, extent, selectors and level legends use those records. New recorded
levels receive distinct colors and keep their actual labels; filtering retains
the full client's color assignments. Missing levels remain explicit. Numeric
levels and known HC tiers affect base marker size; unfamiliar named levels use
a neutral base size plus recorded beds rather than an invented clinical ranking.
Maps require usable coordinates; they are not mandatory in every dashboard.
The source-building script under `scripts/build_population_dashboards.py` is the
Buikwe deployment's data adapter, not the universal facility list for Vima.

## Reuse contract

New custom publications automatically use `vima-buikwe-v1`. Vima receives the
baseline instructions in its system prompt; `publish_dashboard` exposes and
validates customization. Superset remains the query and permissions engine.
The library uses demonstration data and the same components as publications.

- Typography: Inter, Segoe UI, Arial, sans-serif. Compact 11–13px supporting
  labels; 13px card titles; 22px page heading; large readable KPI values.
- Canvas: pale blue-gray #eef3f7; ink #233748; quiet border #d7e1e9;
  burgundy accent #a43b5d. Dark mode uses dark surfaces with readable labels.
- Cards: 9px corner radius, quiet shadow, 8px grid gaps, 12-column layout.
- Layout: headline metrics first; related questions in side cards; central
  wide map when geographic data exists; supporting trends and detail below.
  Mobile stacks cards and scrolls rather than compressing the desktop view.
- Yes/No: Buikwe-style donut, teal #13a58a Yes, coral #e15b6c No,
  gray #94a3b8 missing. Always show counts and percentages. Neither response
  color implies medical risk. Missing answers are never counted as No.
- Categories: short categorical donuts; horizontal bars for longer labels;
  lines for time series; single-number cards for headline metrics; tables
  for detail. Unsupported renderers retain the live table fallback.
- Grouping: 2–6 related variables per tabbed card where possible; clear subject
  title; short tab labels; most useful variable first. Tab rows scroll and
  support arrows, Home and End. Specialized population/facility presets
  keep their existing filter behavior and domain layouts.
- Maps: level-specific facility colors; size combines level and recorded beds;
  keep legends, labels and missing-value handling explicit.

## Prompt customization

“Publish using the Vima baseline, put donation questions in one tabbed card,
and make Yes blue and No amber.”

Vima saves the real source chart IDs and passes:

```json
{
  "design": {
    "baseline": "vima-buikwe-v1",
    "colors": {"yes": "#2563eb", "no": "#f59e0b"},
    "groups": [{"title": "Participation", "chart_ids": [101, 102, 103]}]
  }
}
```

IDs above are illustrative. Groups must use actual charts in the publication;
a chart belongs to at most one group. Palette accepts 2–12 six-digit hex colors.
Semantic colors override palette entries for Yes/No/missing answers. Republish
without `design` to preserve saved settings; pass a new design object to replace
those settings; `design: {}` resets to defaults. Preserve edited chart layouts.

## Implementation library

- `lifedata_ai/frontend/design-system.tsx`: tokens, semantic colors,
  `ResponseDonut`, keyboard-operable `ChartTabs`, interactive `DesignLibrary`.
- `lifedata_ai/frontend/design-system.css`: baseline shell, cards, tabs,
  responsive behavior, shared donut presentation.
- `lifedata_ai/dashboard_design.py`: versioned schema and validation.
- `lifedata_ai/tools/publications.py`: defaults and persistent customization.
- `lifedata_ai/agent/prompts.py`: instructions Vima uses on every request.

Extend this library for new visual capabilities, with a versioned design
contract for incompatible changes. Do not create per-dashboard CSS or a new
chart implementation for components already represented here. Specialized
presets share tokens and semantic color customization while retaining domain
interactions; arbitrary source datasets use the general card and tabs renderer.

## Permanent default for new dashboards

Vima must configure the existing `buikwe-workspace` page, not generate new frontend
files. The publishing tool rejects new publications that omit both a workspace
configuration and an explicitly requested alternate layout. Existing publications
retain their saved template when updated.

Dataset mapping is separate from presentation: use current-client fields, raw source
chart, selectors, grouped panel tabs and detail columns. Set `record_label` and
`entity_label` to the dataset's terms. If site or country fields are absent, use an
empty string; the central geographic panel becomes a record overview in the same
position. Never invent geography or workflow stages.

Tab renderers support categorical and Yes/No donuts, horizontal bars and lines.
Numeric tabs specify `measure` and `aggregation` (`sum` or `avg`); the category or
chronologically sortable time bucket is `field`. Derive appropriate time buckets in
the current client's dataset. Missing or non-finite measures are excluded from
numeric aggregates, not turned into zeros. Shared selections apply to all panels.
The adapter currently caps loaded records at 10,000 and labels that cap; larger
datasets require an aggregate adapter for complete totals. Country geometries must
be available; unavailable boundaries are explicitly listed.

## Editing one chart

Vima uses `edit_dashboard_chart_style` with the source `dashboard_id` alone to
inspect exact editable targets. It then patches a single target: `gauge`,
`chart:<id>`, or a returned `field:<name>` for variables without standalone charts.
For example, `target="gauge", style={"color":"#16a34a"}` persists a green gauge.
Use `colors` for a single Yes/No chart and `palette` for categorical responses.
Nested semantic color patches merge with the existing target override. Other
charts, shared filters, queries, layout and saved customization remain intact.
`reset=true` removes only the selected override. The tool requires ownership of
an existing custom publication and current access to its Superset dashboard.
Workspace, facility-network, population-discovery and ordinary chart publications are supported. Facility templates expose individual service fields and KPI targets. Custom-page context explicitly declares dashboard_surface=custom; native Superset CSS/theme calls are refused there. It follows the same audited local-publication execution path as publishing and
returns a publication event after a successful edit.

## Geographic sources for workspace maps

Use optional `facility_chart_id`, `boundary_chart_id` and `region_label` in the
`buikwe-workspace` configuration. Discover current-client datasets first and add
raw geographic reference charts to that dashboard. These are data sources, not
benchmark styling dependencies. The publisher and viewer verify membership and
query them with the caller's own Superset session.

Facility sources expose real coordinates (`lat`/`lng`, `lat`/`lon` or
`latitude`/`longitude`), a facility name, level and recorded bed count. The map
reuses the original level color taxonomy, white outlines and radius calculation
`sqrt(level_radius² + beds)` capped at 24 pixels. Missing beds do not become
invented capacity. Unknown client levels use their recorded labels and stable
colors from the full permitted reference inventory. Counts and bounds are derived
from that inventory, never fixed to 47 facilities or to Buikwe.

Boundary sources expose `county`, `subcounty` and `geometry_geojson` polygons.
The server dissolves polygons by recorded county and subcounty. County, subcounty
and facility layers can be switched independently. Set workspace `county_field`
and `subcounty_field` to corresponding assessment fields when their names differ;
include those fields in selectors or detail columns for shared area filtering.
Facility names connect source assessments to their permitted reference locations.
The page reports unmatched names, invalid coordinates/polygons and saved source
limits explicitly. Geographic reference queries preserve their saved filters.


### Custom-only publishing is the default

Vima publishes new dashboards directly to the custom UI. It does not create a Superset dashboard or a second native layout unless the user explicitly requests Superset or both destinations. Superset remains the permission-scoped data engine. Reuse existing raw record/geographic charts where suitable; otherwise create only the minimal reusable sources. The workspace derives all chart panels, metrics and tab groups from those sources rather than saving one native chart per variable.

Call `publish_dashboard` with `title`, `theme` and workspace `configuration`, omitting `dashboard_id`. The returned `publication_id` addresses later updates and `edit_dashboard_chart_style` calls. Source chart permissions are rechecked for each viewer. `dashboard_id` remains supported for importing existing native dashboards. Existing publications are retained; this change does not delete previously created native dashboards.


### Automatic layout and administrative hierarchy

Workspace panels automatically fill the two sides of the central map. Fewer chart groups expand to occupy available space; an odd final group spans both side columns. All eleven groups preserve benchmark reading order. Cards are never fabricated to fill gaps, and field/chart styling identities stay stable. The same layout adapts to small screens.

`discover_administrative_boundaries(region)` searches the caller's accessible datasets and returns administrative fields, geometry fields and geographic samples. Confirm country coverage before selecting a reference. Workspace configuration accepts `administrative_boundaries`, each with `chart_id`, `geometry_field`, and `levels` ordered largest to smallest. Each level defines `field` (the dashboard name), `source_field` (the actual reference column), and `label`. Geometries must be WGS84 GeoJSON polygons.

The data adapter joins permitted polygons to actual facility coordinates, adding geographic fields without modifying the primary dataset. Cascading filters use the selected parent cohort; parent changes clear descendant selections. Source names are authoritative, ambiguous borders and missing coordinates stay unassigned, and placeholder area names are treated as missing. Map layers are independently switchable by hierarchy level, preserving level colours and bed-based facility marker sizes. Reference limits and unmatched locations are reported.
