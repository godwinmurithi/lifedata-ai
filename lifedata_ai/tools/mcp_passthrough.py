"""Full, unmediated access to Superset's native MCP tool catalogue.

Every other tool in this codebase wraps ONE specific MCP capability (or one
REST endpoint) behind a checked, REST-shaped interface -- see
tools/superset_read.py, tools/superset_write.py, superset_mcp_client.py.
This module is the opposite: it exposes the entire MCP tool surface
directly, by name, per explicit instruction (2026-09-25) to give the
chatbot full MCP capability rather than the hand-wired subset built so far.

WHAT THIS ACTUALLY MEANS, STATED PLAINLY:

Registered as READ-mode tools (tools/registry.py), meaning they execute
IMMEDIATELY, server-side, the moment the model calls them -- there is no
propose/confirm/snapshot/undo step. That is fine for genuinely read-only MCP
tools (list_databases, get_chart_preview, get_schema, ...). It is NOT fine
in the same way for MCP tools tagged "mutate" in their own metadata:
execute_sql, generate_chart, update_chart, create_virtual_dataset,
save_sql_query, generate_dashboard, add_chart_to_existing_dashboard. Calling
one of those through call_mcp_tool performs that write immediately, with
none of the ownership check, snapshot-for-undo, or human confirmation step
that tools/superset_write.py's hand-built CreateChartTool/UpdateChartTool
etc. have for the exact same underlying operations.

execute_sql specifically is the largest capability this unlocks: raw SQL
Lab access to any database connection the calling user's Superset role can
reach -- not scoped by semantics/registry.yaml's dataset allowlist at all,
which only ever gated dataset/chart-shaped access. This was deliberately
NOT wired in during the earlier MCP work for exactly this reason. It is
wired in now because the instruction was explicit and repeated: staging,
full authorization, the user's own call to make. Recorded here, not to
relitigate it, but so the next person reading this code knows it was a
decision, not an oversight -- see AI_NATIVE_STATUS.md for the full context.
"""

from __future__ import annotations

import json
from typing import Any

from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode


class SearchMcpToolsTool(Tool):
    """Discover what native MCP tools exist on this Superset, by keyword."""

    name = "search_mcp_tools"
    description = (
        "Search Superset's full native MCP tool catalogue by keyword (e.g. 'sql', "
        "'dataset', 'chart', 'dashboard'). Returns tool names, descriptions and whether "
        "each one reads or mutates. Call this to discover a capability beyond this "
        "copilot's other named tools -- e.g. running arbitrary SQL, previewing a chart "
        "before saving, or listing database connections -- then call it with "
        "call_mcp_tool."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword to search the MCP tool catalogue for.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, query: str) -> dict[str, Any]:
        mcp = context.superset_mcp_client()
        try:
            tools = mcp.search_tools(query)
        except SupersetError as exc:
            raise ToolError(f"MCP tool search failed: {exc}", code="mcp_search_failed") from exc
        return {"query": query, "tools": tools, "count": len(tools)}


class CallMcpToolTool(Tool):
    """Call any native MCP tool directly, by name, with its own arguments."""

    name = "call_mcp_tool"
    description = (
        "Call any tool in Superset's native MCP catalogue directly, by name, with its "
        "own arguments object -- including tools not otherwise exposed by this copilot, "
        "such as execute_sql for arbitrary SQL Lab queries, get_chart_preview, "
        "create_virtual_dataset, or save_sql_query. Use search_mcp_tools first if you "
        "are not sure of the exact tool name or its argument shape. "
        "IMPORTANT: tools that mutate Superset (execute_sql, generate_chart, "
        "update_chart, generate_dashboard, create_virtual_dataset, save_sql_query, "
        "add_chart_to_existing_dashboard, ...) run IMMEDIATELY when called here -- there "
        "is no preview or confirmation step the way this copilot's own create_chart/"
        "update_chart tools have. Tell the user plainly what you are about to do before "
        "calling a mutating tool this way, since they cannot see or approve it first."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Exact MCP tool name, e.g. 'execute_sql' or 'get_chart_preview'.",
            },
            # A JSON *string*, not a nested object. Anthropic's tool schema
            # rejects any object-type (sub)schema that doesn't itself declare
            # additionalProperties -- verified: "tools.N.custom: For 'object'
            # type, 'additionalProperties' must be explicitly set to false".
            # Every MCP tool has a different argument shape, so this field
            # can never be that closed -- a JSON string sidesteps the
            # constraint entirely and is parsed in run() below.
            "arguments_json": {
                "type": "string",
                "description": (
                    "The tool's own arguments, as a JSON object string -- e.g. "
                    '\'{"database_id": 1, "sql": "SELECT 1"}\'.'
                ),
            },
        },
        "required": ["tool_name", "arguments_json"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, tool_name: str, arguments_json: str) -> dict[str, Any]:
        try:
            arguments = json.loads(arguments_json)
        except ValueError as exc:
            raise ToolError(
                f"arguments_json is not valid JSON: {exc}", code="invalid_arguments_json"
            ) from exc
        if not isinstance(arguments, dict):
            raise ToolError(
                "arguments_json must decode to a JSON object.", code="invalid_arguments_json"
            )

        mcp = context.superset_mcp_client()
        try:
            result = mcp.call_any_tool(tool_name, arguments)
        except SupersetError as exc:
            raise ToolError(
                f"MCP tool {tool_name!r} failed: {exc}", code="mcp_call_failed"
            ) from exc
        return {"tool_name": tool_name, "result": result}
