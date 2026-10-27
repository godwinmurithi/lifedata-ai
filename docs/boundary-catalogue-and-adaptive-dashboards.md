# Shared boundaries, adaptive dashboards and map presentation

Implemented in staging on 20 October 2026. SINCEP is one use case; its HDSS master dashboard has not been rebuilt or replaced by this change.

## African catalogue

Catalogue: https://sandbox.lifehealth.app/lifedata-ai/boundaries/

An authenticated, shared reference catalogue now contains country boundaries for all 54 African UN member states, plus available source territories and subnational layers. Initial import: **193 validated layers and 26,768 area features** out of 200 available provider layers. Libya ADM2 and Mauritius ADM2 are absent from the chosen provider inventory. Seven deeper layers are explicitly failed/deferred because their source files exceed the 50 MB importer limit: MDG ADM3/4, RWA ADM4/5, ZAF ADM3/4 and ZWE ADM3. Do not describe this as complete coverage of every administrative level.

Provider: geoBoundaries gbOpen. Source metadata, original per-layer licence, attribution, represented year, checksum and immutable release ID are retained. Collection membership does not replace the original licence information. Source reference years may be old, and provider level labels are not globally consistent; Uganda and Kenya demonstrate why national hierarchies must not be guessed from ADM numbers.

Storage is in VIMA's existing staging `lifedata_ai` database schema using `BoundaryRelease` and `BoundaryArea` (migration `0009_boundary_catalogue`). Raw files and inventory/coverage reports are cached under `.boundary-cache/`, excluded from git. Geometry is validated WGS84 polygon/multipolygon GeoJSON. Analysis retains original polygons; the geometry endpoint serves separate topology-preserving simplified display polygons. These are reference datasets in VIMA's catalogue, not automatically created Superset dataset objects.

The catalogue contains public geographic reference data only. It is available to authenticated VIMA users; private client records continue to be queried through the viewer's own Superset permissions. No client records are copied into the catalogue.

### Read and publish

- `GET /lifedata-ai/api/boundaries/?region=Kenya` returns ready layer summaries.
- `GET /lifedata-ai/api/boundaries/?coverage=1` returns coverage and failed layers.
- `GET /lifedata-ai/api/boundaries/<release_id>/?bbox=west,south,east,north` returns display GeoJSON, provenance and a checksum-derived ETag.
- `discover_administrative_boundaries(region)` checks the local catalogue first. `include_project_sources=true` also searches the caller's permitted Superset references.
- A workspace may save `boundary_catalogue: [{release_id, field, label}]`, ordered largest to smallest for each country. Up to 300 references support multi-country maps. These public references need no raw Superset chart.

Release IDs pin existing dashboards to their selected geometry. Later imports add releases rather than rewriting ready ones. Parent/child selector options are derived from the permitted record cohort after geographic enrichment; the catalogue does not assert an unverified nationwide parent-code crosswalk. Ambiguous points and missing locations remain unassigned. Joining uses the existing bounded workspace adapter; country-wide analytics above its record cap require aggregate charts.

### Maintenance

Run inside the VIMA container:

```bash
/app/.venv/bin/python manage.py sync_boundary_catalogue --refresh-index --levels 0 1 2 --workers 1
/app/.venv/bin/python manage.py sync_boundary_catalogue --levels 3 4 5 --workers 1
```

One worker is intentional: staging has a 768 MB memory limit. The initial four-worker import exceeded it; validated releases survived, and the serial retry completed the available layers within the importer limits. Imports use bounded database batches. Files over 50 MB need a future streaming importer or separately approved source adapter; they are never substituted with coarse geometry for analysis. Synchronization is an operator command, not an installed scheduled job. Ordinary dashboard use reads the local catalogue without re-downloading it.

## Adaptive dashboard composition

New general dashboards can use `composed-dashboard`; supplied templates are optional. Existing publications retain their saved presets/layouts, and domain workspace renderers remain available. SINCEP's proposed Overview / LifeHealth Sentinel / Geospatial Cluster views are project-specific and are not universal defaults.

`plan_dashboard(dataset_id, purpose)` reads the permitted schema and returns actual date/numeric/category/geographic candidates, composition options and quality requirements. It does not invent entity meanings, measures, targets, alert rules or statistics. The agent must inspect data and execute its source queries to verify those choices.

Publish actual executable chart IDs:

```json
{
  "title": "Project monitoring",
  "theme": "light",
  "configuration": {
    "preset": "composed-dashboard",
    "source_chart_id": 101,
    "chart_ids": [101, 102],
    "composition": {
      "purpose": "monitoring",
      "skin": "navy-teal",
      "subtitle": "Source-backed headline measures and reporting trends",
      "filters": [{"field": "country", "label": "Country", "chart_ids": [101, 102]}],
      "sections": [{"title": "Overview", "chart_ids": [101]}, {"title": "Trends", "chart_ids": [102]}]
    }
  }
}
```

IDs are illustrative. Every chart must be accessible. Filter fields must exist in every targeted dataset. Filter scopes are visible in the toolbar; criteria append to saved query filters and never replace them. Options are permission-scoped, limited to 200 and labelled if truncated. Sections cover each chart exactly once. The default layout puts available headline metrics first, gives trends/maps/tables more width, and adds no filler charts. Re-publishing preserves manual layout edits.

The versioned `vima-adaptive-v1` theme uses navy/teal, readable typography, aligned cards, responsive stacking and a restrained categorical palette. A warm skin is available. An existing design can be overridden explicitly. Aggregate charts run through Superset, so their whole-dataset metrics do not depend on the legacy workspace's 10,000 raw-record cap. Detail remains capped/paginated as disclosed by the viewer.

`review_dashboard_design(publication_id=...)` now supports custom publications, checks accessible chart definitions and returns actionable findings. It explicitly distinguishes configuration review from browser and numerical verification. This is the first general composition capability, not measured parity with another AI product. Advanced arbitrary map renderers, a general time-range control, automated screenshot critique/repair and broad real-model quality evaluation remain further work.

## Map popup, icon and color editing

`edit_dashboard_map_style(publication_id)` inspects the saved custom workspace map and returns its permitted source/derived fields and supported symbols. A second call applies a targeted `style` patch. Only the owner may edit; source access is rechecked. Existing country-coverage site maps also support these controls; centroid placements remain explicitly labelled proxies. Native Superset maps and other specialised map surfaces are explicitly unsupported by this first tool.

Supported settings:

- Ordered popup fields, labels, title field, text/number/boolean formats, hide-empty and select-button visibility.
- Marker shapes: circle, square, diamond, pin or icon.
- Built-in icons: hospital, clinic, person, home, laboratory, water, school and star.
- Marker color, radius, opacity, outline color/width and category-specific styles from actual fields.
- Boundary color, fill, selection color, opacity and line width; street or satellite basemap.

Example targeted patch:

```json
{
  "publication_id": "<existing publication UUID>",
  "style": {
    "markers": {"shape": "icon", "icon": "hospital", "color": "#087f8c", "radius": 12},
    "popup": {
      "title_field": "facility_name",
      "fields": [{"field": "facility_level", "label": "Level"}, {"field": "beds", "label": "Recorded beds", "format": "number"}]
    }
  }
}
```

Actual fields must come from inspection. Patches merge with existing map settings; `reset=true` resets map presentation only. Queries, coordinates, chart colors, dashboard layout and current filters are retained. New popup fields are projected from the permitted map source, keeping its saved filters. Popup content uses DOM text nodes; arbitrary HTML, executable scripts and external icon URLs are rejected. The marker legend follows customized category colors and size semantics. A publication update event refreshes the current custom viewer without a full-page reload.

## Validation

- Focused catalogue/composition/map styling and publication/geography regressions: 54 passed.
- Workspace/design review/prompt regressions: 47 passed using the appropriate REST-mocking setting.
- Browser check: both skins, desktop and phone widths, scoped filter changes/reset, custom icons/popups and matching legend; zero page errors.
- Live staging: catalogue and geometry APIs succeed, Kenya ADM1 contains 47 counties, catalogue page returns 200, anonymous API access returns 401.
- New Python modules lint clean; migration dry run has no pending schema changes. Frontend typecheck/build are also required after edits.

Phone-accessible visual previews: https://sandbox.lifehealth.app/lifedata-ai/dashboard-design-preview/ . All preview data is illustrative.

Commands: `scripts/check_adaptive_dashboard_ui.py` (mocked demonstration data), `scripts/verify_boundary_catalogue_live.py` (read-only with existing authorised session).

An exploratory broader `test_tools.py` run exposed older hard-coded registry expectations, REST tests run under MCP defaults, and PostgreSQL-only form tests incompatible with the temporary SQLite settings. Those failures are not reported as a green full suite; they need their own test-environment/baseline maintenance.
