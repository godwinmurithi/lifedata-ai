"""Permission-scoped aggregate profiling; never returns individual records."""

import copy
import re
from datetime import UTC, datetime
from math import isclose

from lifedata_ai.tools.base import Tool, ToolError, ToolMode

EXPECTED_METRICS_SCHEMA = {"type": "array", "maxItems": 10, "items": {
    "type": "object", "additionalProperties": False, "properties": {
        "dataset_id": {"type": "integer", "minimum": 1},
        "field": {"type": ["string", "null"]},
        "aggregate": {"type": "string", "enum": ["COUNT", "COUNT_DISTINCT", "MIN", "MAX"]},
        "grain": {"type": "string", "maxLength": 200},
        "label": {"type": "string", "maxLength": 200},
        "expected_value": {"type": ["number", "string", "null"]},
        "filters": {"type": "array", "maxItems": 0, "items": {"type": "string"}},
        "time_range": {"type": "string", "enum": ["No filter"]}},
    "required": ["dataset_id", "field", "aggregate", "grain", "label", "expected_value", "filters", "time_range"]}}

METRIC_CHECKS_SCHEMA = {"type": "array", "maxItems": 10, "items": {
    "type": "object", "additionalProperties": False, "properties": {
        "chart_id": {"type": "integer", "minimum": 1},
        "result_column": {"type": "string", "minLength": 1, "maxLength": 300},
        "aggregate": {"type": "string", "enum": ["count", "count_distinct", "sum", "avg", "min", "max"]},
        "source_field": {"type": ["string", "null"]},
        "tolerance": {"type": "number", "minimum": 0, "maximum": 1}},
    "required": ["chart_id", "result_column", "aggregate"]}}


def identifier_field(name):
    return bool(re.search(r"(^|_)(id|uuid|code|key|identifier)(_|$)", name.lower())
                or re.search(r"[a-z]Id$", name))


def temporal_field(column):
    return bool(column.get("is_dttm") or any(t in str(column.get("type", "")).upper()
                                            for t in ("DATE", "TIME")))


def _range_value(value, column):
    # Superset serializes temporal aggregate results as Unix milliseconds.
    if temporal_field(column) and isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            return value
    return value


def numeric_field(column):
    return any(t in str(column.get("type", "")).upper() for t in
               ("INT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL"))


def _metric(label, aggregate, column=None):
    if column is None:
        return {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": label}
    return {"expressionType": "SIMPLE", "aggregate": aggregate,
            "column": column, "label": label}


def profile_dataset(context, dataset_id, fields=None, entity_field="", dataset=None):
    client = context.superset_client()
    dataset = dataset or client.get_dataset(dataset_id)
    columns = {c["column_name"]: c for c in dataset.get("columns", []) if c.get("column_name")}
    selected = list(dict.fromkeys(fields or list(columns)[:8]))
    if len(selected) > 12 or any(name not in columns for name in selected):
        raise ToolError("Choose at most 12 existing dataset fields.", code="invalid_fields")
    if entity_field and entity_field not in columns:
        raise ToolError("The entity field must exist in this dataset.", code="invalid_fields")
    if entity_field and entity_field not in selected:
        selected = [entity_field] + selected[:11]
    metrics = [_metric("profile_rows", "COUNT")]
    for index, name in enumerate(selected):
        column = columns[name]
        metrics.extend([_metric(f"p{index}_present", "COUNT", column),
                        _metric(f"p{index}_distinct", "COUNT_DISTINCT", column)])
        if temporal_field(column) or (numeric_field(column) and not identifier_field(name)):
            metrics.extend([_metric(f"p{index}_min", "MIN", column),
                            _metric(f"p{index}_max", "MAX", column)])
    query = {"datasource": {"id": dataset_id, "type": "table"}, "force": False,
             "queries": [{"columns": [], "metrics": metrics, "filters": [],
                          "time_range": "No filter", "row_limit": 1}],
             "result_format": "json", "result_type": "full"}
    payload = client.chart_data(query)
    results = payload.get("result") or []
    first = results[0] if results else {}
    if first.get("error") or first.get("status") == "failed":
        raise ToolError("Dataset aggregate profiling failed; do not infer totals from a sample.", code="profile_failed")
    rows = first.get("data") or []
    if not rows or not isinstance(rows[0], dict) or "profile_rows" not in rows[0]:
        raise ToolError("Dataset profiling returned no aggregate result.", code="profile_failed")
    row = rows[0]
    total = int(row["profile_rows"] or 0)
    output = []
    for index, name in enumerate(selected):
        present = int(row.get(f"p{index}_present") or 0)
        distinct = int(row.get(f"p{index}_distinct") or 0)
        output.append({"field": name, "non_null_count": present, "missing_count": total - present,
                       "missing_fraction": (total - present) / total if total else None,
                       "distinct_non_null_count": distinct,
                       "unique_non_null": bool(total and total == present == distinct),
                       "minimum": _range_value(row.get(f"p{index}_min"), columns[name]),
                       "maximum": _range_value(row.get(f"p{index}_max"), columns[name]),
                       "type": columns[name].get("type"), "temporal": temporal_field(columns[name]),
                       "identifier_candidate": identifier_field(name)})
    entity = next((f for f in output if f["field"] == entity_field), None)
    return {"dataset_id": dataset_id, "scope": "current_user_permissions_full_dataset",
            "row_count": total, "fields": output, "profiled_fields": selected,
            "unprofiled_field_count": len(columns) - len(selected),
            "entity_grain": {"field": entity_field or None,
                             "status": "unique" if entity and entity["unique_non_null"] else "requires_confirmation",
                             "repeated_non_null_rows": entity["non_null_count"] - entity["distinct_non_null_count"] if entity else None},
            "limitations": ["Uniqueness does not establish business meaning; confirm the entity and metric units.",
                            "Repeated entity keys can be legitimate events, not duplicate records.",
                            "No individual records or category values were returned."]}


class ProfileDatasetTool(Tool):
    name = "profile_dataset"
    mode = ToolMode.READ
    description = "Profile a permitted dataset using full server-side aggregates: row count, field cardinality, missingness, date/numeric ranges and entity-key repetition. No raw records. Select at most 12 fields; empty fields selects the first eight. Confirm business grain and units separately."
    input_schema = {"type": "object", "additionalProperties": False, "properties": {
        "dataset_id": {"type": "integer", "minimum": 1},
        "fields": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "entity_field": {"type": "string"}}, "required": ["dataset_id", "fields", "entity_field"]}

    def run(self, context, dataset_id, fields, entity_field):
        return profile_dataset(context, dataset_id, fields, entity_field)


def verify_expected_metrics(context, specifications):
    """Reconcile explicit full-scope claims independently of a saved chart query.

    Unsupported scopes remain unverified rather than comparing unlike totals.
    The caller must additionally bind these claims to the actual displayed chart.
    """
    if not isinstance(specifications, list) or len(specifications) > 10:
        raise ToolError("Provide at most ten expected aggregate claims.", code="invalid_metrics")
    outcomes = []
    profiles = {}
    for spec in specifications:
        if not isinstance(spec, dict):
            raise ToolError("Expected metric must be an object.", code="invalid_metrics")
        field = spec.get("field")
        aggregate = str(spec.get("aggregate", "")).upper()
        result = {"label": spec.get("label"), "dataset_id": spec.get("dataset_id"),
                  "field": field, "aggregate": aggregate, "expected": spec.get("expected_value")}
        if spec.get("filters") or spec.get("time_range", "No filter") != "No filter":
            outcomes.append({**result, "status": "unverified", "reason": "Filtered claim requires matching scoped reconciliation."})
            continue
        if aggregate not in {"COUNT", "COUNT_DISTINCT", "MIN", "MAX"} or (not field and aggregate != "COUNT"):
            outcomes.append({**result, "status": "unverified", "reason": "Unsupported independent aggregate."})
            continue
        key = (spec.get("dataset_id"), field)
        if key not in profiles:
            profiles[key] = profile_dataset(context, key[0], [field] if field else [], "")
        profile = profiles[key]
        if not field:
            actual = profile["row_count"]
        else:
            column = next(item for item in profile["fields"] if item["field"] == field)
            actual = column[{"COUNT": "non_null_count", "COUNT_DISTINCT": "distinct_non_null_count",
                             "MIN": "minimum", "MAX": "maximum"}[aggregate]]
        expected = spec.get("expected_value")
        equal = isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9) if (
            isinstance(actual, (int, float)) and isinstance(expected, (int, float))) else actual == expected
        if actual is None or "expected_value" not in spec:
            outcomes.append({**result, "actual": actual, "status": "unverified", "reason": "Missing aggregate or expected value."})
        else:
            outcomes.append({**result, "actual": actual, "status": "pass" if equal else "fail"})
    return {"scope": "independent_full_dataset_aggregate_claims", "metrics": outcomes,
            "status": "fail" if any(r["status"] == "fail" for r in outcomes) else
                      "pass" if outcomes and all(r["status"] == "pass" for r in outcomes) else "unverified",
            "limitation": "Claims must also be matched to displayed chart metrics, units and filters."}


def verify_metric_checks(context, publication, checks):
    """Compare a saved chart's single aggregate with an independent scoped query.

    Preserves Superset's saved WHERE/time scope and caller permissions. Grouped,
    raw, postprocessed or multiquery results require a different reconciliation;
    these are explicitly unverified and must not certify a dashboard.
    """
    from lifedata_ai.publication_sources import publication_charts
    from lifedata_ai.superset_client import SupersetError
    from lifedata_ai.tools.superset_read import _parse_json_field, _prepare_query_context

    if not isinstance(checks, list) or len(checks) > 10:
        raise ToolError("Provide at most ten chart metric checks.", code="invalid_metrics")
    client = context.superset_client()
    allowed = {chart["id"] for chart in publication_charts(client, publication)}
    outcomes = []

    def query_row(query_context):
        response = client.chart_data(query_context)
        results = response.get("result") or []
        result = results[0] if len(results) == 1 else {}
        if result.get("error") or result.get("status") == "failed":
            raise ToolError("Aggregate reconciliation query failed.", code="metric_query_failed")
        rows = result.get("data") or []
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise ToolError("Reconciliation requires exactly one aggregate result row.", code="metric_shape")
        return rows[0]

    for spec in checks:
        result = {"chart_id": spec.get("chart_id"), "result_column": spec.get("result_column"),
                  "aggregate": spec.get("aggregate"), "source_field": spec.get("source_field")}
        try:
            if spec.get("chart_id") not in allowed:
                raise ToolError("Metric check chart is not a source in this publication.", code="invalid_metrics")
            aggregate = str(spec.get("aggregate", "")).upper()
            if aggregate not in {"COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX"}:
                raise ToolError("Unsupported aggregate.", code="invalid_metrics")
            tolerance = spec.get("tolerance", 1e-6)
            if not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 1:
                raise ToolError("Tolerance must be between zero and one.", code="invalid_metrics")
            chart = client.get_chart(spec["chart_id"])
            parsed, note = _parse_json_field(chart.get("query_context"), "query_context")
            prepared = _prepare_query_context(parsed, chart, note, row_limit_cap=2)
            if len(prepared["queries"]) != 1:
                raise ToolError("Multiple chart queries cannot be reconciled as one metric.", code="metric_shape")
            original = prepared["queries"][0]
            if original.get("columns") or original.get("post_processing") or original.get("is_timeseries") or original.get("having") or (original.get("extras") or {}).get("having"):
                outcomes.append({**result, "status": "unverified", "reason": "Grouped, time-series or postprocessed chart requires matching per-group reconciliation."})
                continue
            field = spec.get("source_field")
            datasource = prepared["datasource"]
            if not isinstance(datasource, dict) or datasource.get("type") != "table":
                raise ToolError("Reconciliation requires a known table datasource.", code="invalid_metrics")
            dataset = client.get_dataset(datasource["id"])
            columns = {c["column_name"]: c for c in dataset.get("columns", [])}
            if field and field not in columns:
                raise ToolError("Metric source field does not exist.", code="invalid_fields")
            if not field and aggregate != "COUNT":
                raise ToolError("This aggregate requires a source field.", code="invalid_fields")
            if field and aggregate in {"SUM", "AVG"} and (not numeric_field(columns[field]) or identifier_field(field)):
                raise ToolError("SUM/AVG requires a numeric measure, not an identifier.", code="invalid_fields")
            displayed = query_row(prepared)
            if spec.get("result_column") not in displayed:
                raise ToolError("The requested result column is absent from this chart.", code="invalid_fields")
            independent = copy.deepcopy(prepared)
            independent_query = independent["queries"][0]
            independent_query["metrics"] = [_metric("vima_independent_total", aggregate, columns[field] if field else None)]
            independent_query["columns"] = []
            independent_query["orderby"] = []
            independent_query["row_limit"] = 1
            # These controls affect grouped ranking, not the saved WHERE/time scope.
            for key in ("series_columns", "series_limit", "series_limit_metric", "order_desc"):
                independent_query.pop(key, None)
            independently_queried = query_row(independent)
            actual = independently_queried.get("vima_independent_total")
            shown = displayed[spec["result_column"]]
            if actual is None or shown is None:
                outcomes.append({**result, "status": "unverified", "reason": "Null aggregate requires an explicit missing-data policy."})
                continue
            equal = isclose(actual, shown, rel_tol=1e-9, abs_tol=tolerance) if (
                isinstance(actual, (int, float)) and isinstance(shown, (int, float))) else actual == shown
            outcomes.append({**result, "status": "pass" if equal else "fail", "displayed": shown,
                             "independent": actual, "tolerance": tolerance,
                             "scope": {"filters": original.get("filters", []), "time_range": original.get("time_range")}})
        except (ToolError, SupersetError) as exc:
            outcomes.append({**result, "status": "fail", "reason": str(exc)})
    return {"scope": "saved_chart_aggregate_with_matching_filters_and_time_range", "metrics": outcomes,
            "status": "fail" if any(r["status"] == "fail" for r in outcomes) else
                      "pass" if outcomes and all(r["status"] == "pass" for r in outcomes) else "unverified"}
