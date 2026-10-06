"""Criteria, permission boundaries and sequential query semantics."""

import json
from unittest.mock import Mock

import pytest
from django.test import RequestFactory

from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import DashboardPublication
from lifedata_ai.population import FIELDS, criteria_filters
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import ToolError
from lifedata_ai.tools.publications import PublishDashboardTool
from lifedata_ai.views import population

pytestmark = pytest.mark.django_db


def setup(monkeypatch):
    columns = ["person_id", "facility_name", "latitude", "longitude", "age_band", *FIELDS]
    publication = DashboardPublication.objects.create(
        superset_dashboard_id=48,
        owner_username="ana",
        title="Population",
        theme="light",
        layout=[],
        configuration={
            "preset": "population-discovery",
            "source_chart_id": 12,
            "boundary_chart_id": 13,
            "facility_chart_id": 14,
            "default_facility": "St. Charles",
        },
    )
    client = Mock()
    client.get_dashboard.return_value = {"id": 48}
    client.get_dashboard_charts.return_value = [{"id": 12}, {"id": 13}, {"id": 14}]
    client.get_chart.return_value = {
        "query_context": {
            "datasource": {"id": 250, "type": "table"},
            "queries": [{"columns": columns}],
        }
    }
    monkeypatch.setattr(population, "_client", lambda request: client)
    return publication, client


def request(body=None, method="POST"):
    req = RequestFactory().generic(
        method,
        "/lifedata-ai/api/publications/",
        json.dumps(body or {}),
        content_type="application/json",
    )
    req.superset_identity = SupersetIdentity(username="ana")
    req.superset_session_cookie = "fake"
    return req


@pytest.mark.parametrize(
    "criteria",
    [
        {"nin": ["secret"]},
        {"age": {"min": "18"}},
        {"age": {"min": float("nan")}},
        {"age": {"min": 65, "max": 18}},
        {"blood_group": []},
        {"blood_group": "O+"},
        {"age": {"operator": "SQL", "min": 1}},
    ],
)
def test_invalid_or_unpermitted_criteria_rejected(criteria):
    with pytest.raises(ToolError):
        criteria_filters(criteria, FIELDS)


def test_category_values_remain_structured_and_stage_specific():
    stages = criteria_filters(
        {"blood_group": ["O+'; DROP TABLE patient; --"], "age": {"min": 18, "max": 60}}, FIELDS
    )
    assert stages[2] == [{"col": "blood_group", "op": "IN", "val": ["O+'; DROP TABLE patient; --"]}]
    assert stages[4] == [
        {"col": "age", "op": ">=", "val": 18},
        {"col": "age", "op": "<=", "val": 60},
    ]


def test_funnel_adds_each_criterion_at_its_stage(monkeypatch):
    publication, client = setup(monkeypatch)

    def chart_data(context):
        return {
            "result": [{"data": [{"people": 299 - i}]} for i, _ in enumerate(context["queries"])]
        }

    client.chart_data.side_effect = chart_data
    response = population.discovery(
        request(
            {
                "facility": "St. Charles",
                "criteria": {
                    "blood_donor": ["Yes"],
                    "blood_group": ["O+"],
                    "hiv_status": ["NEGATIVE"],
                    "age": {"min": 18},
                    "village": ["LUGALA"],
                    "distance_km": {"max": 30},
                },
            }
        ),
        publication.id,
    )
    assert response.status_code == 200
    queries = client.chart_data.call_args.args[0]["queries"]
    names = [{f["col"] for f in q["filters"]} for q in queries[:6]]
    assert names == [
        {"facility_name"},
        {"facility_name", "blood_donor"},
        {"facility_name", "blood_donor", "blood_group"},
        {"facility_name", "blood_donor", "blood_group", "hiv_status"},
        {"facility_name", "blood_donor", "blood_group", "hiv_status", "age"},
        {
            "facility_name",
            "blood_donor",
            "blood_group",
            "hiv_status",
            "age",
            "village",
            "distance_km",
        },
    ]
    assert queries[-1]["filters"] == queries[5]["filters"]
    assert json.loads(response.content)["stages"][-1]["count"] == 294


def test_detail_reapplies_selected_cohort_and_does_not_return_contact_fields(monkeypatch):
    publication, client = setup(monkeypatch)
    client.chart_data.return_value = {"result": [{"data": []}]}
    response = population.discovery(
        request(
            {
                "facility": "St. Charles",
                "criteria": {"blood_group": ["O+"]},
                "person_id": "SBX-0001",
            }
        ),
        publication.id,
    )
    assert response.status_code == 200
    assert json.loads(response.content)["record"] is None
    q = client.chart_data.call_args.args[0]["queries"][0]
    assert {"col": "blood_group", "op": "IN", "val": ["O+"]} in q["filters"]
    assert {"col": "person_id", "op": "IN", "val": ["SBX-0001"]} in q["filters"]
    assert not {"nin", "personal_id", "name", "email", "phone"} & set(q["columns"])


def test_superset_denial_blocks_population_and_detail(monkeypatch):
    publication, client = setup(monkeypatch)
    client.get_dashboard.side_effect = SupersetError("Denied", status_code=403)
    assert population.discovery(request(), publication.id).status_code == 403
    client.chart_data.assert_not_called()


def test_source_chart_membership_is_required(monkeypatch):
    publication, client = setup(monkeypatch)
    client.get_dashboard_charts.return_value = [{"id": 12}]
    assert population.discovery(request(), publication.id).status_code == 403
    client.chart_data.assert_not_called()


def test_publication_tool_accepts_population_preset(monkeypatch):
    from lifedata_ai.tools.base import ToolContext

    publication, client = setup(monkeypatch)
    context = ToolContext(superset_session_cookie="fake", identity=SupersetIdentity(username="ana"))
    context._client = client
    result = PublishDashboardTool().run(
        context, 48, "Population", "light", configuration=publication.configuration
    )
    assert result["publication_id"] == str(publication.id)


def test_empty_population_keeps_all_six_zero_stages(monkeypatch):
    publication, client = setup(monkeypatch)
    client.chart_data.side_effect = lambda context: {
        "result": [{"data": []} for _ in context["queries"]]
    }
    response = population.discovery(request({"facility": "St. Charles"}), publication.id)
    body = json.loads(response.content)
    assert body["matches"] == 0
    assert [s["count"] for s in body["stages"]] == [0] * 6
    assert body["records"] == []


def test_all_facilities_does_not_impose_a_facility_filter(monkeypatch):
    publication, client = setup(monkeypatch)
    client.chart_data.return_value = {"result": [{"data": [{"people": 299}]}] * 20}
    response = population.discovery(request({"facility": "", "criteria": {}}), publication.pk)
    assert response.status_code == 200
    assert all(not q.get("filters") for q in client.chart_data.call_args.args[0]["queries"])


def test_summary_preserves_structured_criteria_and_sequential_counts(monkeypatch):
    publication, client = setup(monkeypatch)
    publication.configuration["summary_chart_id"] = 15
    publication.save()
    client.get_dashboard_charts.return_value.append({"id": 15})
    original = client.get_chart.return_value
    client.get_chart.side_effect = lambda chart: (
        original if chart == 12 else {"query_context": {"datasource": {"id": 251}}}
    )
    client.chart_data.side_effect = [
        {
            "result": [
                {
                    "data": [
                        {"panel": "stage", "label": str(i), "people": n}
                        for i, n in enumerate([299, 214, 64, 64, 64, 64])
                    ]
                }
            ]
        },
        {"result": [{"data": []}]},
    ]
    response = population.discovery(
        request({"facility": "", "criteria": {"blood_donor": ["Yes"], "blood_group": ["O+"]}}),
        publication.pk,
    )
    assert response.status_code == 200
    result = json.loads(response.content)
    assert [s["count"] for s in result["stages"]] == [299, 214, 64, 64, 64, 64]
    summary = client.chart_data.call_args_list[0].args[0]
    assert summary["queries"][0]["filters"] == [
        {"col": "blood_donor", "op": "IN", "val": ["Yes"]},
        {"col": "blood_group", "op": "IN", "val": ["O+"]},
    ]
    assert len(summary["form_data"]["adhoc_filters"]) == 2


@pytest.mark.parametrize("fields", [["nin"], ["age"], "sex", ["sex"] * 13])
def test_profile_fields_reject_unpermitted_or_numeric_fields(monkeypatch, fields):
    publication, client = setup(monkeypatch)
    response = population.discovery(request({"profile_fields": fields}), publication.pk)
    assert response.status_code == 400
    client.chart_data.assert_not_called()
