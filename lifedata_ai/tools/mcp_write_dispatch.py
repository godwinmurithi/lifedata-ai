"""Executes a proposed write ActionRecord via native MCP.

Part of the 2026-09-25 full-MCP-authorization build (see AI_NATIVE_STATUS.md).
Covers exactly three action types -- the ones with a real MCP tool to call and
a payload shape this system can translate correctly:

    create_chart          -> MCP generate_chart (+ add_chart_to_existing_dashboard
                              if the chart was created onto an existing dashboard)
    create_dashboard      -> MCP generate_dashboard
    add_chart_to_dashboard -> MCP add_chart_to_existing_dashboard

Everything else (update_chart, rename_*, layout tools, native filters) has no
MCP equivalent yet or one this system can't populate correctly from a partial
payload -- `can_execute_via_mcp` returns False for those, and
views/execute_mcp_action.py never gets called for them; they keep using the
existing browser-executed REST path untouched.

Known behavior difference from the REST path, stated rather than hidden:
placing a new chart on a dashboard here goes through MCP's own dashboard
layout logic (generate_dashboard / add_chart_to_existing_dashboard), not this
project's layout compiler (validation/layout.py) -- so `placement` hints
("new_row", "kpi_row", "top") are not honored on this path. The REST path
still honors them.
"""

from __future__ import annotations

from typing import Any

from lifedata_ai.superset_client import SupersetError
from lifedata_ai.superset_mcp_client import SupersetMCPClient
from lifedata_ai.tools.mcp_chart_config import params_to_mcp_config


def can_execute_via_mcp(action_type: str, payload: dict[str, Any]) -> bool:
    """Whether views/execute_mcp_action.py should handle this action, vs. the
    existing browser-executed envelope. Called once, when WriteTool.run()
    builds the proposal, so the panel knows which path to take on confirm."""
    if action_type == "create_chart":
        return params_to_mcp_config(payload.get("params") or {}) is not None
    if action_type in ("create_dashboard", "add_chart_to_dashboard"):
        return True
    return False


def execute_via_mcp(
    mcp_client: SupersetMCPClient, action_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Perform the write. Returns {"superset_object_id": ..., "url": ...}.

    Raises SupersetError on failure -- the caller (views/execute_mcp_action.py)
    turns that into a FAILED ActionRecord, same as a browser-reported failure
    would.
    """
    if action_type == "create_chart":
        return _execute_create_chart(mcp_client, payload)
    if action_type == "create_dashboard":
        return _execute_create_dashboard(mcp_client, payload)
    if action_type == "add_chart_to_dashboard":
        return _execute_add_chart_to_dashboard(mcp_client, payload)
    raise SupersetError(f"No MCP execution path for action_type {action_type!r}")


def _execute_create_chart(mcp: SupersetMCPClient, payload: dict[str, Any]) -> dict[str, Any]:
    config = params_to_mcp_config(payload["params"])
    if config is None:
        # can_execute_via_mcp should have already refused this at proposal
        # time -- reaching here means the payload changed shape between
        # proposal and confirm, which should not happen but must not silently
        # half-execute.
        raise SupersetError("This chart's configuration is not supported on the MCP path.")

    result = mcp.generate_chart(
        dataset_id=payload["dataset_id"],
        config=config,
        chart_name=payload["chart_name"],
    )
    chart = result.get("chart") or {}
    chart_id = chart.get("id")
    url = chart.get("url") or result.get("explore_url")

    dashboard_id = payload.get("dashboard_id")
    if dashboard_id and chart_id:
        # See module docstring: this places the chart via MCP's own layout
        # logic, not this project's layout compiler -- `placement` is not
        # honored on this path.
        mcp.add_chart_to_existing_dashboard(int(dashboard_id), int(chart_id))

    return {"superset_object_id": chart_id, "url": url}


def _execute_create_dashboard(mcp: SupersetMCPClient, payload: dict[str, Any]) -> dict[str, Any]:
    chart_ids = [int(c) for c in (payload.get("chart_ids") or [])]
    result = mcp.generate_dashboard(chart_ids, payload["dashboard_title"])
    dashboard = result.get("dashboard") or {}
    return {
        "superset_object_id": dashboard.get("id"),
        "url": result.get("dashboard_url"),
    }


def _execute_add_chart_to_dashboard(mcp: SupersetMCPClient, payload: dict[str, Any]) -> dict[str, Any]:
    result = mcp.add_chart_to_existing_dashboard(
        int(payload["dashboard_id"]), int(payload["chart_id"])
    )
    dashboard = result.get("dashboard") or {}
    return {
        "superset_object_id": dashboard.get("id") or payload.get("dashboard_id"),
        "url": result.get("dashboard_url"),
    }
