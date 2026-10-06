# Custom dashboards and chat attachments

The development sandbox now has a custom dashboard UI at
`https://sandbox.lifehealth.app/lifedata-ai/dashboards/`. Superset remains the
query and permissions engine; published pages render their own chart cards,
without a Superset dashboard iframe. Logged-out viewers use the shared Superset
login, and each data request forwards their session to Superset.

## Build and publish with the chatbot

Ask: **“Build a dashboard from this dataset, then publish it in the custom UI.”**
The chatbot reuses accessible raw data-source charts, or saves only the required
raw sources, then calls `publish_dashboard` directly. It creates no Superset
dashboard unless explicitly requested. The result is a clickable dashboard link
in the transcript, also restored when reopening the conversation.

`publish_dashboard` accepts a `title`, a `theme`, workspace `configuration`, an
optional `publication_id` for updates, and an optional source `dashboard_id`
for importing an existing native dashboard. The theme is
`light` or `dark`; an optional `layout` is also supported. Layout is an ordered list of
`{chart_id, span, height}` entries, one for each source chart. `span` is 4, 6 or
12 columns and `height` is 200–800 pixels. This lets the chatbot reproduce a
reference screenshot's card order and dimensions. Omitting layout preserves
manual edits when republishing. Custom-only pages are addressed by their
publication UUID; republishing with `publication_id` updates the existing URL.
Imported pages retain one publication per source dashboard and owner.

On the page, use **Edit layout** to drag cards with a mouse or touch. For keyboard
editing, focus a card's move handle, press Space, move with arrow keys, and press
Space again to drop (Escape cancels). Width and height controls resize cards;
changes save to LifeData and survive a reload. Only the publisher can edit a
publication; other viewers still require source dashboard/chart access.

Bar, line/area, pie/donut, scatter, KPI and table charts have custom renderers.
Other chart types show a labelled data-table fallback. This is a first custom UI
renderer, not full parity with every Superset plugin. Generic publications do
not mirror native Superset filters or dashboard text/tab components. The
facility preset below supplies its own map and cross-filtering. Republish after adding/removing source charts to refresh the
publication's chart layout. Data refresh reads the saved source chart query;
viewer data is capped at 10,000 rows, independently of model excerpt limits.

## Interactive facility dashboards

Ask the chatbot: **“Build and publish a facility dashboard from the complete HF
survey, with a map, shared PCN/Hub/Ownership/Level filters, and a Yes/No pie for
every service.”** The agent prompt and publishing tool expose this as a reusable
workflow. The tool uses the caller's existing Superset session and returns the
publication link in the chat transcript.

Save two raw source charts in one source dashboard: the complete survey
(`name_facility`, `hf_level`, `facility_type`, `ownership`, `lat`, `lon` and the
eleven service columns), and network links (`name_facility`, `pcn`, `hub`). If
the survey already has network columns, the same chart may supply both roles.
Extract geometry coordinates into a virtual SELECT dataset when necessary.
Then call `publish_dashboard` with:

```json
{
  "dashboard_id": 45,
  "title": "Trans Nzoia · Facility intelligence",
  "theme": "light",
  "configuration": {
    "preset": "facility-network",
    "source_chart_id": 433,
    "network_chart_id": 434,
    "region_label": "Trans Nzoia County",
    "map_bounds": [0.7, 34.4, 1.4, 35.4]
  }
}
```

IDs in this example belong to the development Trans Nzoia dashboard. Other
dashboards use their actual chart IDs and optional geographic configuration;
the preset has no hardcoded county label. Bounds are south, west, north, east.
Only charts belonging to the source dashboard are accepted. Republish to update
the existing URL rather than create a duplicate.

The preset shows a separate interactive Yes/No donut for each of the eleven
services. Missing values appear as **Not recorded**, never as No. Hover gives
counts and percentages; clicking a slice or its keyboard-accessible response
button filters every panel. Category, level and ownership bars also support
click-to-filter. Reset clears all filters. Map popups, search, zoom and fitting
all coordinates work alongside those filters. All 53 development survey records
remain available, including repeated names and coordinate outliers. PCN/hub
joins use exact normalised names and preserve missing assignments.

Dashboard pages send `Referrer-Policy: strict-origin-when-cross-origin`, allowing
OpenStreetMap tile requests to include the site origin without sending the
publication URL. This corrects the previous Django `same-origin` policy, which
omitted the browser referrer required by the [OSM tile policy](https://operations.osmfoundation.org/policies/tiles/).
Tile failures display a status message while facility markers remain usable.

## Attach images and files

Use the **+** button, drop files onto the composer, or paste a screenshot into
the message box. Image previews and removable file chips appear before sending.
Ask **“Use this screenshot as the layout reference, then publish the dashboard.”**
The model receives the image itself, rather than just its filename.

Supported inputs: PNG, JPEG, GIF, WebP, PDF, DOCX, XLSX, CSV, TSV, TXT, Markdown,
JSON, logs and YAML. Images and PDFs use native multimodal content blocks;
Office documents are converted to readable text and spreadsheet cells. Other
text files use UTF-8. Each turn accepts up to four files, each up to 5 MB and
15 MB in total. File text excerpts are capped at 100,000 characters and clearly
marked if truncated. Older visual attachments may be omitted from a model
request to fit its request-size budget; stored conversation content remains
unchanged. The model is told to request the file again when necessary.

Uploads are persisted with their chat message and replayed in later turns.
Spreadsheet upload provides the model with file contents; it does **not**
automatically import a new Superset dataset. The chatbot must use an actual
import tool before claiming the data is available as a dataset.

Native request formats follow the provider's [vision documentation](https://platform.claude.com/docs/en/build-with-claude/vision)
and [PDF documentation](https://platform.claude.com/docs/en/build-with-claude/pdf-support).

## Development and validation

The custom UI source is `lifedata_ai/frontend/`. Rebuild it with:

```sh
cd lifedata_ai/frontend
npm ci --ignore-scripts
npm run typecheck
npm run build
```

The generated JS/CSS in `lifedata_ai/static/dashboards/` are tracked so the Django
image does not need Node at runtime. Migration `0006_dashboardpublication` adds
publication settings in LifeData's schema, without editing Superset metadata.

Run the isolated affected backend suite in the development container:

```sh
docker exec \
  -e DJANGO_DB_ENGINE=django.db.backends.sqlite3 \
  -e POSTGRES_DB=:memory: -e DJANGO_DB_SCHEMA= \
  lifedata-ai4cti uv run pytest \
  lifedata_ai/tests/test_attachments.py \
  lifedata_ai/tests/test_publications.py \
  lifedata_ai/tests/test_views_chat.py \
  lifedata_ai/tests/test_views_conversations.py \
  lifedata_ai/tests/test_prompts.py \
  lifedata_ai/tests/test_development_records.py \
  lifedata_ai/tests/test_validation.py -q
```

`python3 scripts/check_custom_dashboard_ui.py` runs a Chromium smoke check with
mocked APIs and model output, without live credentials or billed model calls.
It checks custom rendering, keyboard dragging, saved resizing, screenshot
multipart upload, publication links and custom-page chatbot context. Chromium
and its runtime libraries must be available. PostgreSQL-specific legacy form
tests need their separate PostgreSQL test setup; REST tool tests must run with
`LIFEDATA_AI_USE_MCP_READS=false` because their mocked routes target REST.

The Superset frontend fork's final skipped tab-drop test was rewritten to use
pointer events and real registry hit-testing. The source remains on its local
`feat/phase8-react-dnd-to-dndkit` branch. The custom UI uses dnd-kit directly;
these changes do not replace the running Superset frontend image.
