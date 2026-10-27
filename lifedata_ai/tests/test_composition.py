import copy
from unittest.mock import Mock

import pytest

from lifedata_ai.composition import apply_criteria, automatic_layout
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import DashboardPublication
from lifedata_ai.tools.base import ToolContext, ToolError
from lifedata_ai.tools.publications import PublishDashboardTool
from lifedata_ai.tools.review import ReviewDashboardDesignTool

pytestmark = pytest.mark.django_db


def sources():
    client = Mock()
    charts = {
        12: {"id": 12, "viz_type": "table", "slice_name": "Detail", "datasource_id": 8},
        13: {
            "id": 13,
            "viz_type": "big_number_total",
            "slice_name": "Total records",
            "datasource_id": 8,
        },
        14: {
            "id": 14,
            "viz_type": "echarts_timeseries_line",
            "slice_name": "Monthly trend",
            "datasource_id": 8,
        },
    }
    client.get_chart.side_effect = lambda identifier: copy.deepcopy(charts[identifier])
    client.get_dataset.return_value = {"columns": [{"column_name": "country"}]}
    context = ToolContext(superset_session_cookie="fake", identity=SupersetIdentity(username="ana"))
    context._client = client
    config = {
        "preset": "composed-dashboard",
        "source_chart_id": 12,
        "chart_ids": [12, 13, 14],
        "composition": {
            "purpose": "monitoring",
            "filters": [{"field": "country", "label": "Country", "chart_ids": [12, 13, 14]}],
        },
    }
    return client, context, config


def test_composed_custom_publish_uses_aggregate_charts_and_preserves_manual_edits():
    client, context, config = sources()
    result = PublishDashboardTool().run(context, title="Monitoring", configuration=config)
    publication = DashboardPublication.objects.get(pk=result["publication_id"])
    assert publication.superset_dashboard_id is None
    assert publication.layout[0]["chart_id"] == 13 and publication.layout[0]["height"] == 200
    assert publication.configuration["design"]["baseline"] == "vima-adaptive-v1"
    client.get_dashboard.assert_not_called()
    publication.layout.reverse()
    publication.save()
    edited = copy.deepcopy(publication.layout)
    PublishDashboardTool().run(context, publication_id=str(publication.pk), title="Updated")
    publication.refresh_from_db()
    assert publication.layout == edited
    verdict = ReviewDashboardDesignTool().run(context, publication_id=str(publication.pk))
    assert verdict["verdict"] == "ok" and "Browser appearance" in verdict["review_scope"]


def test_filter_field_and_section_membership_are_validated():
    client, context, config = sources()
    config["composition"]["filters"][0]["field"] = "foreign"
    with pytest.raises(ToolError, match="Filter fields"):
        PublishDashboardTool().run(context, title="Bad", configuration=config)
    config["composition"]["filters"] = []
    config["composition"]["sections"] = [{"title": "Overview", "chart_ids": [12, 13]}]
    with pytest.raises(ToolError, match="every source chart"):
        PublishDashboardTool().run(context, title="Bad", configuration=config)
    assert DashboardPublication.objects.count() == 0


def test_viewer_filters_append_saved_query_filters_and_reject_arbitrary_columns():
    _, _, config = sources()
    prepared = {"queries": [{"filters": [{"col": "tenant", "op": "==", "val": "current"}]}]}
    apply_criteria(prepared, config, 12, {"country": ["Uganda"]})
    assert prepared["queries"][0]["filters"] == [
        {"col": "tenant", "op": "==", "val": "current"},
        {"col": "country", "op": "IN", "val": ["Uganda"]},
    ]
    with pytest.raises(ToolError):
        apply_criteria(prepared, config, 12, {"tenant": ["other"]})
    with pytest.raises(ToolError):
        apply_criteria(prepared, config, 12, {"country": "Uganda"})


def test_no_fake_kpis_or_empty_map_slots_are_added():
    result = automatic_layout([{"id": 1, "viz_type": "table"}])
    assert result == [{"chart_id": 1, "span": 12, "height": 480}]


def test_new_tools_are_registered_with_valid_schemas():
    import jsonschema

    from lifedata_ai.tools.registry import build_registry

    registry = build_registry()
    for name in [
        "plan_dashboard",
        "edit_dashboard_map_style",
        "discover_administrative_boundaries",
        "review_dashboard_design",
    ]:
        tool = registry.get(name)
        assert tool is not None
        jsonschema.Draft7Validator.check_schema(tool.input_schema)


def test_planning_does_not_mistake_population_for_latitude():
    from lifedata_ai.tools.composition import PlanDashboardTool

    client, context, _ = sources()
    client.chart_data.return_value = {"result": [{"data": [{"profile_rows": 10}]}]}
    client.get_dataset.return_value = {
        "columns": [
            {"column_name": "population", "type": "INTEGER"},
            {"column_name": "latitude", "type": "DOUBLE"},
            {"column_name": "country_code", "type": "VARCHAR"},
            {"column_name": "reported_at", "type": "TIMESTAMP", "is_dttm": True},
        ]
    }
    result = PlanDashboardTool().run(context, 8, "overview")
    assert result["geographic_candidates"] == ["latitude", "country_code"]
    assert result["time_fields"] == ["reported_at"]
