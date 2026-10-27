"""Validated, data-independent composition for custom dashboards."""

import json

from lifedata_ai.tools.base import ToolError

ENCODING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "dimension": {"type": "string", "minLength": 1},
        "metrics": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "series": {"type": "string", "minLength": 1},
        "time": {"type": "boolean"}, "stack": {"type": "boolean"},
        "number_format": {"type": "string", "maxLength": 20},
        "unit": {"type": "string", "maxLength": 40},
        "aggregation": {"enum": ["sum", "mean"]},
        "chart_type": {"enum": ["bar", "line", "area", "pie", "scatter", "table", "kpi", "map"]},
        **{field: {"type": "string", "minLength": 1} for field in ("latitude", "longitude", "geometry_field", "name_field", "category_field")},
    },
}

RENDERER_CAPABILITIES = {
    "version": "vima-query-renderer-v2",
    "charts": ["bar", "line", "area", "pie", "scatter", "table", "kpi", "map"],
    "encodings": ENCODING_SCHEMA,
    "supports": ["multiple measures", "categorical series pivot", "time axis", "stacked series", "null gaps", "number formats", "units", "shared chart tabs"],
    "rules": ["Encoding fields name actual saved-query result columns, including metric aliases.",
              "Duplicate dimension/series cells require an explicit justified sum or mean aggregation; never silently sum rates.",
              "Percent formatting assumes a fraction (0.25 means 25%). Unsupported types show an explicit data table fallback.",
              "Use shared chart groups and purpose-led pages to fit readable charts in a viewport; never shrink the entire dashboard."],
}

COMPOSITION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "encodings": {"type": "object", "patternProperties": {"^[1-9][0-9]*$": ENCODING_SCHEMA}, "additionalProperties": False},
        "layout_mode": {"enum": ["flow", "viewport"]},
        "purpose": {"enum": ["overview", "monitoring", "comparison", "geographic", "exploration"]},
        "subtitle": {"type": "string", "maxLength": 240},
        "filters": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field": {"type": "string", "minLength": 1},
                    "label": {"type": "string", "minLength": 1, "maxLength": 60},
                    "chart_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "integer", "minimum": 1},
                    },
                },
                "required": ["field", "label", "chart_ids"],
            },
        },
        "sections": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 80},
                    "chart_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "integer", "minimum": 1},
                    },
                },
                "required": ["title", "chart_ids"],
            },
        },
        "skin": {"enum": ["navy-teal", "warm"]},
    },
    "required": ["purpose"],
}


def params(chart):
    value = chart.get("params") or chart.get("form_data") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return value if isinstance(value, dict) else {}


def viz(chart):
    return chart.get("viz_type") or params(chart).get("viz_type") or "table"


def automatic_layout(charts):
    """Content controls prominence; nothing is added to fill an empty slot."""
    kpis = [c for c in charts if viz(c) in {"big_number", "big_number_total"}]
    others = [c for c in charts if c not in kpis]
    # Small multiples stay aligned. Trends/maps/tables get room for labels.
    # Pie/donut charts get extra height: a cramped 360px card is exactly what
    # clips their legend labels (seen live on "Marital status"). The custom-page
    # grid only supports span 4/6/12, so width stays at the standard half-width.
    def dimensions(c):
        if c in kpis:
            return (12 if len(kpis) == 1 else 6 if len(kpis) == 2 else 4), 200
        if any(t in viz(c) for t in ("map", "deck", "table")):
            return 12, 480
        if "line" in viz(c):
            return 12, 360
        if any(t in viz(c) for t in ("pie", "donut")):
            return 6, 420
        return 6, 360
    return [
        {"chart_id": int(c["id"]), "span": dimensions(c)[0], "height": dimensions(c)[1]}
        for c in kpis + others
    ]


def validate_composition(config, charts, client):
    import jsonschema

    try:
        jsonschema.validate(config.get("composition"), COMPOSITION_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ToolError(
            "Invalid dashboard composition: " + exc.message, code="invalid_configuration"
        ) from exc
    ids = {int(c["id"]) for c in charts}
    if set(config.get("chart_ids", [])) != ids:
        raise ToolError(
            "Composition chart_ids must match its accessible source charts.",
            code="invalid_configuration",
        )
    composition = config["composition"]
    if not {int(key) for key in composition.get("encodings", {})}.issubset(ids):
        raise ToolError("Chart encodings must target accessible publication charts.", code="invalid_configuration")
    sections = composition.get("sections", [])
    ordered = [i for section in sections for i in section["chart_ids"]]
    if sections and (len(ordered) != len(set(ordered)) or set(ordered) != ids):
        raise ToolError(
            "Sections must include every source chart exactly once.", code="invalid_configuration"
        )
    filters = composition.get("filters", [])
    if len({f["field"] for f in filters}) != len(filters):
        raise ToolError("Dashboard filters need distinct fields.", code="invalid_configuration")
    by_id = {int(c["id"]): c for c in charts}
    datasets = {}
    for item in filters:
        if not set(item["chart_ids"]).issubset(ids):
            raise ToolError(
                "Filter targets must belong to the publication.", code="invalid_configuration"
            )
        for identifier in item["chart_ids"]:
            chart = by_id[identifier]
            p = params(chart)
            datasource = (
                chart.get("datasource_id")
                or chart.get("table_id")
                or str(p.get("datasource", "")).split("__")[0]
            )
            try:
                datasource = int(datasource)
            except (ValueError, TypeError):
                raise ToolError("Filter sources need a valid dataset.", code="invalid_source") from None
            if datasource not in datasets:
                datasets[datasource] = client.get_dataset(datasource)
            if item["field"] not in {
                c["column_name"] for c in datasets[datasource].get("columns", [])
            }:
                raise ToolError(
                    "Filter fields must exist in every targeted dataset.", code="invalid_source"
                )


def apply_criteria(prepared, configuration, chart_id, criteria):
    """Append approved viewer filters; never replace saved filters or source queries."""
    allowed = {f["field"]: f for f in configuration.get("composition", {}).get("filters", [])}
    if not isinstance(criteria, dict) or set(criteria) - set(allowed):
        raise ToolError("Unknown dashboard filter.", code="invalid_criteria")
    for field, values in criteria.items():
        if (
            not isinstance(values, list)
            or len(values) > 100
            or any(not isinstance(v, str) or len(v) > 500 for v in values)
        ):
            raise ToolError("Invalid filter values.", code="invalid_criteria")
        if values and chart_id in allowed[field]["chart_ids"]:
            for query in prepared["queries"]:
                query.setdefault("filters", []).append({"col": field, "op": "IN", "val": values})


def review_composition(publication, charts):
    findings = []

    def add(code, problem, fix):
        findings.append({"severity": "medium", "code": code, "problem": problem, "fix": fix})

    if not charts:
        add("empty", "No accessible charts.", "Add accessible source charts.")
    for chart in charts:
        if len(chart.get("slice_name", "")) > 80:
            add(
                "long_title",
                "A chart title is too long.",
                "Rename it to a concise question or measure.",
            )
        p = params(chart)
        if viz(chart) in {"pie", "donut"} and int(p.get("row_limit") or 0) > 6:
            add(
                "crowded_pie",
                "A pie permits more than six categories.",
                "Use a horizontal bar chart or group categories explicitly.",
            )
    if len(charts) > 9 and not publication.configuration.get("composition", {}).get("sections"):
        add(
            "density", "Many charts compete in one view.", "Group charts into purpose-led sections."
        )
    return findings
