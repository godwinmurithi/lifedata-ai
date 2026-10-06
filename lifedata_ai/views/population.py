"""Server-side population discovery; Superset owns counts, filters and access."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from lifedata_ai.models import DashboardPublication
from lifedata_ai.population import (
    COUNT,
    FIELDS,
    PERSON_COLUMNS,
    STAGES,
    criteria_filters,
    query,
    run_queries,
)
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import ToolError
from lifedata_ai.tools.superset_read import _parse_json_field
from lifedata_ai.views.dashboards import _client, _error


def _source(request, pk):
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication or publication.configuration.get("preset") != "population-discovery":
        raise SupersetError("Population dashboard not found.", status_code=404)
    client = _client(request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        permission = pool.submit(client.get_dashboard, publication.superset_dashboard_id)
        membership = pool.submit(client.get_dashboard_charts, publication.superset_dashboard_id)
        permission.result()
        members = {int(c["id"]) for c in membership.result()}
    config = publication.configuration
    source_keys = ["source_chart_id", "boundary_chart_id", "facility_chart_id"]
    if config.get("summary_chart_id"):
        source_keys.append("summary_chart_id")
    for key in source_keys:
        if config.get(key) not in members:
            raise SupersetError("Discovery source is not in this dashboard.", status_code=403)
    with ThreadPoolExecutor(max_workers=2) as pool:
        source_query = pool.submit(client.get_chart, config["source_chart_id"])
        summary_query = (
            pool.submit(client.get_chart, config["summary_chart_id"])
            if config.get("summary_chart_id")
            else None
        )
        source = source_query.result()
        summary = summary_query.result() if summary_query else None
    context, _ = _parse_json_field(source.get("query_context"), "query_context")
    dataset = context.get("datasource", {}).get("id")
    columns = context.get("queries", [{}])[0].get("columns", [])
    if type(dataset) is not int or "person_id" not in columns or "facility_name" not in columns:
        raise ToolError(
            "Discovery needs a saved raw population source chart.", code="invalid_source"
        )
    summary_dataset = None
    if config.get("summary_chart_id"):
        summary_context, _ = _parse_json_field(summary.get("query_context"), "query_context")
        summary_dataset = summary_context.get("datasource", {}).get("id")
        if type(summary_dataset) is not int:
            raise ToolError("Invalid Superset summary source.", code="invalid_source")
    return SimpleNamespace(
        publication=publication,
        client=client,
        config=config,
        dataset=dataset,
        columns=columns,
        summary_dataset=summary_dataset,
    )


def _chart_rows(source, chart):
    # _source already checked all reference chart membership for this request.
    saved = source.client.get_chart(chart)
    context, _ = _parse_json_field(saved.get("query_context"), "query_context")
    if not context.get("datasource") or not context.get("queries"):
        raise ToolError("Missing geographic source query.", code="invalid_source")
    return run_queries(
        source.client,
        context["datasource"]["id"],
        context["queries"],
        form_data=context.get("form_data"),
    )[0]


@csrf_exempt
def discovery(request, pk):
    if request.method not in ("GET", "POST"):
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    try:
        source = _source(request, pk)
        if request.method == "GET":
            default = source.config.get("default_facility", "")
            available = [f for f in FIELDS if f in source.columns]
            with ThreadPoolExecutor(max_workers=3) as pool:
                facility_query = pool.submit(
                    _chart_rows, source, source.config["facility_chart_id"]
                )
                boundary_query = pool.submit(
                    _chart_rows, source, source.config["boundary_chart_id"]
                )
                inventory_query = pool.submit(
                    run_queries,
                    source.client,
                    source.dataset,
                    [
                        query(
                            columns=available,
                            filters=[{"col": "facility_name", "op": "IN", "val": [default]}]
                            if default
                            else [],
                        )
                    ],
                )
                facilities = facility_query.result()
                boundaries = boundary_query.result()
                rows = inventory_query.result()[0]
            names = {str(r["facility_name"]) for r in facilities}
            if default and default not in names:
                default = ""
            if not names:
                raise ToolError("No facility locations are available.", code="missing_facility")
            # Options are inventories, never client-side population filtering.
            catalog = []
            for field in available:
                entry = dict(FIELDS[field])
                values = [r.get(field) for r in rows if r.get(field) is not None]
                if entry["kind"] == "category":
                    entry["options"] = sorted({str(v) for v in values})
                else:
                    entry["extent"] = [min(values), max(values)] if values else None
                catalog.append(entry)
            return JsonResponse(
                {
                    "catalog": catalog,
                    "facilities": facilities,
                    "default_facility": default,
                    "boundaries": boundaries,
                    "notes": [
                        "Donor willingness is recorded; separate contact consent is not available.",
                        "Matches selected discovery criteria; clinical review is required for donation.",
                        "Donation history and last donation date are not available in this source.",
                        "Age and screening values are the recorded survey snapshot; distances are straight-line.",
                    ],
                }
            )
        body = json.loads(request.body)
        if not isinstance(body, dict) or set(body) - {
            "criteria",
            "facility",
            "person_id",
            "profile_fields",
        }:
            raise ToolError("Invalid discovery request.", code="invalid_criteria")
        by_stage = criteria_filters(body.get("criteria", {}), source.columns)
        facility = body.get("facility", source.config.get("default_facility", ""))
        if not isinstance(facility, str) or len(facility) > 250:
            raise ToolError("Select a facility.", code="invalid_criteria")
        active = [{"col": "facility_name", "op": "IN", "val": [facility]}] if facility else []
        stage_queries = []
        for stage in range(6):
            active = [*active, *by_stage[stage]]
            stage_queries.append(query(filters=list(active), metrics=[COUNT], row_limit=1))
        if "person_id" in body:
            person = body["person_id"]
            if not isinstance(person, str) or len(person) > 80:
                raise ToolError("Invalid person identifier.", code="invalid_criteria")
            # Reapply all criteria; detail cannot be used to bypass the matched cohort.
            rows = run_queries(
                source.client,
                source.dataset,
                [
                    query(
                        columns=["person_id", *[f for f in FIELDS if f in source.columns]],
                        filters=[*active, {"col": "person_id", "op": "IN", "val": [person]}],
                        row_limit=1,
                    )
                ],
            )[0]
            return JsonResponse({"record": rows[0] if rows else None})
        breakdowns = [
            f
            for f in (
                "age_band",
                "sex",
                "blood_group",
                "blood_donor",
                "subcounty",
                "parish",
                "village",
                "hiv_status",
                "s_hbsag",
                "s_hcv",
                "s_syphillis",
                "bp_results",
                "sickle_cells_disease",
            )
            if f in source.columns
        ]
        profile_fields = body.get("profile_fields", [])
        if (
            not isinstance(profile_fields, list)
            or len(profile_fields) > 12
            or any(
                not isinstance(f, str)
                or f not in FIELDS
                or f not in source.columns
                or FIELDS[f]["kind"] != "category"
                for f in profile_fields
            )
        ):
            raise ToolError("Invalid profile fields.", code="invalid_criteria")
        breakdowns += list(dict.fromkeys(f for f in profile_fields if f not in breakdowns))
        queries = [
            *stage_queries,
            *[
                query(columns=[f], filters=active, metrics=[COUNT], row_limit=1000)
                for f in breakdowns
            ],
            query(columns=[f for f in PERSON_COLUMNS if f in source.columns], filters=active),
        ]
        if source.summary_dataset is not None:
            form_data = {
                "adhoc_filters": [
                    {
                        "expressionType": "SIMPLE",
                        "subject": f["col"],
                        "operator": f["op"],
                        "comparator": f["val"],
                        "clause": "WHERE",
                    }
                    for f in active
                ]
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                summary_query = pool.submit(
                    run_queries,
                    source.client,
                    source.summary_dataset,
                    [query(columns=["panel", "label", "people"], filters=active)],
                    form_data=form_data,
                )
                records_query = pool.submit(
                    run_queries, source.client, source.dataset, [queries[-1]]
                )
                summary_rows = summary_query.result()[0]
                matched_records = records_query.result()
            stage_rows = sorted(
                [r for r in summary_rows if r["panel"] == "stage"], key=lambda r: r["label"]
            )
            if len(stage_rows) != 6:
                raise ToolError(
                    "Superset did not return all six discovery stages.", code="query_failed"
                )
            results = [[{"people": r["people"]}] for r in stage_rows]
            results += [
                [{f: r["label"], "people": r["people"]} for r in summary_rows if r["panel"] == f]
                for f in breakdowns
            ]
            results += matched_records
        else:
            results = run_queries(source.client, source.dataset, queries)
        stages = [
            {"label": label, "count": int(rows[0].get("people", 0)) if rows else 0}
            for label, rows in zip(STAGES, results[:6], strict=True)
        ]
        records = results[-1]
        return JsonResponse(
            {
                "stages": stages,
                "matches": stages[-1]["count"],
                "breakdowns": {f: rows for f, rows in zip(breakdowns, results[6:-1], strict=True)},
                "records": records,
                "records_truncated": len(records) < stages[-1]["count"],
                "mapped": sum(
                    type(r.get("latitude")) in (int, float)
                    and type(r.get("longitude")) in (int, float)
                    and -90 <= r["latitude"] <= 90
                    and -180 <= r["longitude"] <= 180
                    for r in records
                ),
            }
        )
    except (SupersetError, ToolError) as exc:
        return _error(exc)
    except (ValueError, TypeError, KeyError):
        return JsonResponse(
            {"error": "Invalid population request or source configuration."}, status=400
        )
