"""Translates our validated chart params into Superset MCP's chart-config schema.

Our schema (validation/schemas/actions.py, CHART_PARAMS_SCHEMA) is close to
Superset's own form_data vocabulary (metrics, groupby, adhoc_filters). MCP's
`generate_chart`/`update_chart` tools take a different, typed, chart-type-
specific `config` object instead (superset/mcp_service/chart/schemas.py:
XYChartConfig, PieChartConfig, TableChartConfig, BigNumberChartConfig, ...).
This module is the one-way bridge, covering exactly the 5 viz types
semantics/registry.yaml's `viz_allowlist` permits -- the only ones this
system's validator lets the model create in the first place:

    big_number_total, echarts_timeseries_bar, echarts_timeseries_line,
    pie, table

Returns None when a payload can't be translated (a required field the target
MCP config needs isn't present) rather than guessing at a default -- callers
fall back to the existing, already-correct REST/browser path. See
CreateChartTool in tools/superset_write.py.
"""

from __future__ import annotations

from typing import Any

#: Our adhoc_filter operator vocabulary -> MCP's FilterConfig.op. Everything
#: not listed here (>, <, >=, <=, !=, IN, NOT IN) is spelled identically in
#: both, verified against superset/mcp_service/chart/schemas.py FilterConfig.
_FILTER_OP_MAP: dict[str, str] = {"==": "="}


def _to_mcp_filters(adhoc_filters: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not adhoc_filters:
        return None
    return [
        {
            "column": f["column"],
            "op": _FILTER_OP_MAP.get(f["operator"], f["operator"]),
            "value": f["value"],
        }
        for f in adhoc_filters
    ]


def _to_column_ref(metric: Any) -> dict[str, Any]:
    """Our `_METRIC` is `oneOf(saved-metric name string, {aggregate, column, label})`;
    MCP's `ColumnRef` covers both shapes as one type (`saved_metric` flag)."""
    if isinstance(metric, str):
        return {"name": metric, "saved_metric": True}
    ref: dict[str, Any] = {
        "name": metric["column"]["column_name"],
        "aggregate": metric["aggregate"],
    }
    if metric.get("label"):
        ref["label"] = metric["label"]
    return ref


def params_to_mcp_config(params: dict[str, Any]) -> dict[str, Any] | None:
    """Our CHART_PARAMS_SCHEMA dict -> MCP's discriminated chart config, or
    None if this specific payload can't be translated (fall back to REST)."""
    viz_type = params.get("viz_type")
    metrics = params.get("metrics") or []
    groupby = params.get("groupby") or []
    filters = _to_mcp_filters(params.get("adhoc_filters"))
    row_limit = params.get("row_limit")

    if viz_type == "big_number_total":
        # Validator's own policy rule already requires exactly one metric and
        # no groupby for this viz type (validation/validator.py) -- checked
        # again here since this function must not assume it was called after
        # that rule ran.
        if len(metrics) != 1:
            return None
        config: dict[str, Any] = {
            "chart_type": "big_number",
            "metric": _to_column_ref(metrics[0]),
        }
        if filters:
            config["filters"] = filters
        return config

    if viz_type in ("echarts_timeseries_bar", "echarts_timeseries_line"):
        x_col = params.get("x_axis") or (groupby[0] if groupby else None)
        if not x_col or not metrics:
            return None
        config = {
            "chart_type": "xy",
            "x": {"name": x_col},
            "y": [_to_column_ref(m) for m in metrics],
            "kind": "bar" if viz_type == "echarts_timeseries_bar" else "line",
        }
        if params.get("orientation"):
            config["orientation"] = params["orientation"]
        if params.get("stack") is not None:
            config["stacked"] = bool(params["stack"])
        if filters:
            config["filters"] = filters
        if row_limit:
            config["row_limit"] = row_limit
        return config

    if viz_type == "pie":
        if not groupby or not metrics:
            return None
        config = {
            "chart_type": "pie",
            "dimension": {"name": groupby[0]},
            "metric": _to_column_ref(metrics[0]),
        }
        if params.get("show_legend") is not None:
            config["show_legend"] = bool(params["show_legend"])
        if params.get("show_labels") is not None:
            config["show_labels"] = bool(params["show_labels"])
        if filters:
            config["filters"] = filters
        if row_limit:
            config["row_limit"] = row_limit
        return config

    if viz_type == "table":
        columns = [{"name": g} for g in groupby] + [_to_column_ref(m) for m in metrics]
        if not columns:
            return None
        config = {"chart_type": "table", "columns": columns}
        if filters:
            config["filters"] = filters
        if row_limit:
            config["row_limit"] = row_limit
        return config

    # Outside the 5-type viz_allowlist -- the validator should have already
    # refused this payload before it got here, but fail closed rather than
    # guess at an unfamiliar chart type's config shape.
    return None
