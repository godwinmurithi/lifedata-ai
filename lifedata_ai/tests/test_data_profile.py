from unittest.mock import Mock

import pytest

from lifedata_ai.tools.base import ToolError
from lifedata_ai.tools.composition import PlanDashboardTool
from lifedata_ai.tools.data_profile import (
    ProfileDatasetTool,
    verify_expected_metrics,
    verify_metric_checks,
)


def fixture_context():
    client = Mock()
    client.get_dataset.return_value = {"columns": [
        {"column_name": "person_id", "type": "INTEGER"},
        {"column_name": "reported_at", "type": "TIMESTAMP", "is_dttm": True},
        {"column_name": "district", "type": "VARCHAR"},
        {"column_name": "weight", "type": "FLOAT"},
    ]}
    client.chart_data.return_value = {"result": [{"data": [{"profile_rows": 100,
        "p0_present": 98, "p0_distinct": 80, "p1_present": 90, "p1_distinct": 15,
        "p1_min": "2025-01-01", "p1_max": "2025-12-31"}]}]}
    context = Mock()
    context.superset_client.return_value = client
    return context, client


def test_profile_uses_full_aggregates_and_reports_grain_missingness():
    context, client = fixture_context()
    result = ProfileDatasetTool().run(context, 7, ["person_id", "reported_at"], "person_id")
    assert result["row_count"] == 100
    assert result["fields"][0]["missing_count"] == 2
    assert result["entity_grain"]["repeated_non_null_rows"] == 18
    assert result["entity_grain"]["status"] == "requires_confirmation"
    assert result["fields"][1]["minimum"] == "2025-01-01"
    query = client.chart_data.call_args.args[0]["queries"][0]
    assert query["columns"] == [] and query["row_limit"] == 1
    assert query["metrics"][0]["sqlExpression"] == "COUNT(*)"
    assert all(metric.get("aggregate") not in {"SUM", "AVG"} for metric in query["metrics"])


def test_profile_rejects_unknown_fields_before_querying():
    context, client = fixture_context()
    with pytest.raises(ToolError, match="existing"):
        ProfileDatasetTool().run(context, 7, ["made_up"], "")
    client.chart_data.assert_not_called()


def test_profile_rejects_query_errors_instead_of_inventing_zero():
    context, client = fixture_context()
    client.chart_data.return_value = {"result": [{"status": "failed", "error": "denied"}]}
    with pytest.raises(ToolError, match="failed"):
        ProfileDatasetTool().run(context, 7, [], "")


def test_plan_excludes_numeric_identifiers_and_preserves_unknown_units():
    context, _ = fixture_context()
    result = PlanDashboardTool().run(context, 7, "monitoring")
    assert result["numeric_fields"] == ["weight"]
    assert result["identifier_fields"] == ["person_id"]
    assert result["analytical_spec"]["headline"]["unit"] == "records"
    assert "SUM" in result["analytical_spec"]["measure_policy"]
    assert result["profile"]["row_count"] == 100


def test_reconciliation_distinguishes_failure_and_unsupported_scope():
    context, client = fixture_context()
    result = verify_expected_metrics(context, [
        {"dataset_id": 7, "aggregate": "COUNT", "expected_value": 100},
        {"dataset_id": 7, "aggregate": "COUNT", "expected_value": 90},
        {"dataset_id": 7, "aggregate": "COUNT", "expected_value": 90, "filters": [{"col": "district"}]},
    ])
    assert [item["status"] for item in result["metrics"]] == ["pass", "fail", "unverified"]
    assert result["status"] == "fail"
    assert client.chart_data.call_count == 1


def test_chart_reconciliation_preserves_saved_filter_and_time_scope(monkeypatch):
    context, client = fixture_context()
    monkeypatch.setattr("lifedata_ai.publication_sources.publication_charts", lambda *_: [{"id": 4}])
    original = {"columns": [], "metrics": ["count"], "filters": [{"col": "district", "op": "==", "val": "East"}],
                "time_range": "2025-01-01 : 2026-01-01", "row_limit": 1000}
    client.get_chart.return_value = {"id": 4, "query_context": {"datasource": {"id": 7, "type": "table"}, "queries": [original]}}
    client.chart_data.side_effect = [{"result": [{"data": [{"Total": 20}]}]},
                                     {"result": [{"data": [{"vima_independent_total": 20}]}]}]
    result = verify_metric_checks(context, Mock(), [{"chart_id": 4, "result_column": "Total", "aggregate": "count"}])
    assert result["status"] == "pass"
    independent = client.chart_data.call_args_list[1].args[0]["queries"][0]
    assert independent["filters"] == original["filters"]
    assert independent["time_range"] == original["time_range"]
    assert independent["metrics"][0]["sqlExpression"] == "COUNT(*)"


def test_grouped_chart_is_unverified_instead_of_summing_capped_results(monkeypatch):
    context, client = fixture_context()
    monkeypatch.setattr("lifedata_ai.publication_sources.publication_charts", lambda *_: [{"id": 4}])
    client.get_chart.return_value = {"id": 4, "query_context": {"datasource": {"id": 7, "type": "table"},
        "queries": [{"columns": ["district"], "metrics": ["count"]}]}}
    result = verify_metric_checks(context, Mock(), [{"chart_id": 4, "result_column": "Total", "aggregate": "count"}])
    assert result["status"] == "unverified"
    client.chart_data.assert_not_called()
