"""Inspect schema and propose an editable dashboard plan before saving sources."""

import re

from lifedata_ai.tools.base import Tool, ToolMode
from lifedata_ai.tools.data_profile import identifier_field, profile_dataset


class PlanDashboardTool(Tool):
    name = "plan_dashboard"
    mode = ToolMode.READ
    description = (
        "Inspect the CURRENT permitted dataset and recommend a dashboard composition without a supplied template. "
        "Call before creating a new general dashboard. Returns actual field names, chart options, layout and quality requirements. "
        "Does not create charts or invent metrics. Use publish_dashboard preset composed-dashboard for flexible dashboards; "
        "use domain workspace presets when their interactions fit. SINCEP-specific layouts are not universal defaults."
    )
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "dataset_id": {"type": "integer", "minimum": 1},
            "purpose": {
                "enum": ["overview", "monitoring", "comparison", "geographic", "exploration"]
            },
        },
        "required": ["dataset_id", "purpose"],
    }

    def run(self, context, dataset_id, purpose):
        dataset = context.superset_client().get_dataset(dataset_id)
        columns = dataset.get("columns", [])
        dates = [
            c["column_name"]
            for c in columns
            if c.get("is_dttm")
            or any(t in str(c.get("type", "")).upper() for t in ("DATE", "TIME"))
        ]
        numeric = [
            c["column_name"]
            for c in columns
            if any(
                t in str(c.get("type", "")).upper()
                for t in ("INT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC")
            )
        ]
        identifiers = [c["column_name"] for c in columns if identifier_field(c["column_name"])]
        numeric = [name for name in numeric if name not in identifiers]
        categories = [c["column_name"] for c in columns if c["column_name"] not in dates + numeric + identifiers]
        geographic = [
            c["column_name"]
            for c in columns
            if re.search(
                r"(^|_)(country|district|county|subcounty|ward|parish|village|latitude|longitude|lat|lng|lon|geom|geometry|adm[0-5])(_|$)",
                c["column_name"].lower(),
            )
        ]
        selected = list(dict.fromkeys(identifiers[:1] + dates[:2] + categories[:4] + numeric[:3]))[:12]
        profile = profile_dataset(context, dataset_id, selected, identifiers[0] if identifiers else "", dataset)
        from lifedata_ai.composition import RENDERER_CAPABILITIES
        return {
            "renderer_capabilities": RENDERER_CAPABILITIES,
            "dataset_id": dataset_id,
            "purpose": purpose,
            "time_fields": dates,
            "numeric_fields": numeric,
            "identifier_fields": identifiers,
            "profile": profile,
            "analytical_spec": {
                "grain": profile["entity_grain"],
                "expected_metrics": [{"dataset_id": dataset_id, "field": None,
                                      "aggregate": "COUNT", "grain": "record",
                                      "label": "Records", "expected_value": profile["row_count"],
                                      "filters": [], "time_range": "No filter"}],
                "headline": {"viz_type": "big_number_total", "dataset_id": dataset_id,
                             "metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "Records"},
                             "unit": "records", "denominator": "all permission-scoped records; not people unless grain is confirmed"},
                "distribution_candidates": [{"viz_type": "echarts_timeseries_bar", "dataset_id": dataset_id,
                                             "groupby": [field],
                                             "metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "Records"}, "row_limit": 25,
                                             "missing_policy": "display missing separately",
                                             "coverage_policy": "label top 25 when cardinality exceeds 25"}
                                            for field in categories[:4]],
                "measure_policy": "Use saved metrics or explicitly confirmed units and aggregations; numeric type alone does not justify SUM.",
                "time_policy": "Select a meaningful event date, confirm period and grain, then use a supported time series.",
                "renderer_policy": "Check get_dashboard_capabilities before creating chart specifications; do not silently substitute tables.",
            },
            "categorical_fields": categories,
            "geographic_candidates": geographic,
            "saved_metrics": [m.get("metric_name") for m in dataset.get("metrics", [])],
            "plan": {
                "headline": "Choose 1–3 meaningful source-backed aggregates; counts use the actual entity grain.",
                "primary": "Use a genuine time series for monitoring; comparable bars for comparison. "
                           + ("A map is default here, not optional: geographic_candidates is non-empty, so call "
                              "discover_administrative_boundaries and include the map in this publish."
                              if geographic else "No geographic_candidates were found, so no map is expected."),
                "supporting": "Choose a small number of distributions and an authorized detail table. Omit unsupported sections.",
                "publish": {
                    "preset": "composed-dashboard",
                    "source_chart_id": "first actual saved chart ID",
                    "chart_ids": "all actual saved chart IDs",
                    "composition": {"purpose": purpose, "skin": "navy-teal", "layout_mode": "viewport"},
                },
                "filters": "Configure composition.filters with field, label, chart_ids; only target datasets containing that field.",
            },
            "requirements": [
                "Confirm units, entity grain, period and denominators; numeric IDs are not measures.",
                "Use server-side aggregate charts for full totals; never sum a capped raw sample.",
                "Keep missing separate from No or zero; multiple-answer percentages may exceed 100%.",
                "Do not invent dates, targets, workflow steps, coordinates, alert rules or cluster statistics.",
                "Discover exact-country catalogue releases for geography. Preserve reference dates and attribution.",
                "After publication call review_dashboard_design(publication_id=...) and fix findings.",
                "Preview at desktop and mobile widths when screenshot tools are available; do not claim visual verification without one.",
            ],
        }
