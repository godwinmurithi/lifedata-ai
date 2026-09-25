"""Superset native-MCP client, as an alternative to superset_client.py's REST wrapper.

Same identity invariant as the REST client, different mechanism: instead of
forwarding the user's own session cookie, this mints a short-lived HS256 JWT
carrying `sub=<their Superset username>` and sends it as a Bearer token on
every MCP call. superset-staging/mcp_auth_bridge.py resolves that token back
into a real Superset user (g.user) on the Superset side, so RBAC still runs
as that user -- never a shared service account. See that file's docstring for
why the bridge is needed at all (two real gaps in Superset 6.1.0's shipped
MCP+JWT integration, not a design choice).

Methods here return dicts shaped like the REST client's, so tools/superset_read.py
does not need to change to consume either backend -- see tools/base.py's own
docstring: the whole point of the Tool abstraction is that a transport swap
does not touch tool bodies. Where Superset's MCP tool genuinely can't produce
an equivalent (get_dashboard's position_json layout -- not a field MCP's
DashboardInfo exposes), the REST client is used for that one piece instead of
faking it.

get_chart_data deliberately has NO client-side method here mirroring the REST
client's `chart_data(query_context)` -- see tools/superset_read.py's
GetChartDataTool, which now calls this module's `get_chart_data_by_id`
directly. That tool used to gate reads through the semantics registry
allowlist before running a chart's stored query; this MCP path does not, per
the explicit 2026-09-25 instruction to defer that gate to a production pass
(see AI_NATIVE_STATUS.md). Flagged here because it is the single highest-risk
line in this file, not because it is hidden.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import jwt
from django.conf import settings

from lifedata_ai.superset_client import SupersetError

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 120


class SupersetMCPClient:
    """Superset MCP client bound to one user's identity (never a service account)."""

    def __init__(self, username: str):
        self.username = username
        self.mcp_url = settings.LIFEDATA_AI_MCP_URL

    # -- plumbing -----------------------------------------------------------

    def _mint_token(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": settings.MCP_JWT_ISSUER,
                "aud": settings.MCP_JWT_AUDIENCE,
                "sub": self.username,
                "iat": now,
                "exp": now + _TOKEN_TTL_SECONDS,
            },
            settings.MCP_JWT_SECRET,
            algorithm="HS256",
        )

    def _call(self, tool_name: str, request: dict[str, Any]) -> dict[str, Any]:
        """Call one MCP tool via the search/call_tool dispatcher and return its JSON body.

        Synchronous wrapper around the async `mcp` client SDK -- every call
        gets its own short-lived event loop, matching this codebase's rule
        that tools stay synchronous (agent/loop.py is a plain Django view).
        """
        try:
            return asyncio.run(self._call_async(tool_name, request))
        except Exception as exc:  # noqa: BLE001 - normalised to SupersetError below
            raise SupersetError(f"MCP call to {tool_name} failed: {exc}") from exc

    async def _call_async(self, tool_name: str, request: dict[str, Any]) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self._mint_token()}"}
        async with streamablehttp_client(self.mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "call_tool", {"name": tool_name, "arguments": {"request": request}}
                )
                if result.isError:
                    message = _first_text(result) or f"{tool_name} failed"
                    raise SupersetError(message)
                text = _first_text(result)
                if text is None:
                    return {}
                try:
                    return json.loads(text)
                except ValueError:
                    return {"raw": text}

    # -- listings, REST-shaped ------------------------------------------------

    def list_databases(self, q: str | None = None) -> list[dict]:
        payload = self._call("list_databases", _search_request(q))
        return [_database_to_rest(d) for d in payload.get("databases", [])]

    def list_datasets(self, q: str | None = None) -> list[dict]:
        payload = self._call("list_datasets", _search_request(q))
        return [_dataset_to_rest(d) for d in payload.get("datasets", [])]

    def list_charts(self, q: str | None = None) -> list[dict]:
        payload = self._call("list_charts", _search_request(q))
        return [_chart_to_rest(c) for c in payload.get("charts", [])]

    def list_dashboards(self, q: str | None = None) -> list[dict]:
        payload = self._call("list_dashboards", _search_request(q))
        return [_dashboard_to_rest(d) for d in payload.get("dashboards", [])]

    # -- single-object gets, REST-shaped --------------------------------------

    def get_dataset(self, dataset_id: int) -> dict:
        payload = self._call("get_dataset_info", {"identifier": dataset_id})
        if payload.get("error") or "id" not in payload:
            return {}
        return _dataset_to_rest(payload, full=True)

    def get_chart(self, chart_id: int) -> dict:
        payload = self._call("get_chart_info", {"identifier": chart_id})
        if payload.get("error") or "id" not in payload:
            return {}
        return _chart_to_rest(payload, full=True)

    # -- chart data (see module docstring: no registry gate here) ------------

    def get_chart_data_by_id(self, chart_id: int, row_limit: int) -> dict:
        """MCP's get_chart_data: takes the chart id directly, not a query_context.

        Superset builds and runs the query server-side; row_limit caps it the
        same way tools/superset_read.py capped the REST query_context.
        """
        payload = self._call(
            "get_chart_data", {"identifier": chart_id, "limit": row_limit}
        )
        if payload.get("error"):
            raise SupersetError(payload.get("message", "get_chart_data failed"))
        return payload


def _search_request(q: str | None) -> dict[str, Any]:
    # page_size matches the REST client's _listing() (superset_client.py) --
    # MCP's own default is 10, which would silently under-report results
    # (verified: a deployment with 17 datasets returned only 10 before this
    # was added). The real cap the model sees is MAX_LISTING_ITEMS in
    # tools/superset_read.py; this just keeps MCP from truncating first.
    request: dict[str, Any] = {"page_size": 100}
    if q:
        request["search"] = q
    return request


def _database_to_rest(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": d.get("id"),
        "database_name": d.get("database_name"),
        "backend": d.get("backend"),
    }


def _dataset_to_rest(d: dict[str, Any], full: bool = False) -> dict[str, Any]:
    out = {
        "id": d.get("id"),
        "table_name": d.get("table_name"),
        "schema": d.get("schema_name"),
        "kind": "virtual" if d.get("is_virtual") else "physical",
    }
    if full:
        out.update(
            {
                "description": d.get("description"),
                "database": {"database_name": d.get("database_name")},
                "main_dttm_col": d.get("main_dttm_col"),
                "owners": d.get("owners") or [],
                "columns": d.get("columns") or [],
                "metrics": d.get("metrics") or [],
            }
        )
    return out


def _chart_to_rest(c: dict[str, Any], full: bool = False) -> dict[str, Any]:
    out = {
        "id": c.get("id"),
        "slice_name": c.get("slice_name"),
        "viz_type": c.get("viz_type"),
    }
    if full:
        form_data = c.get("form_data") if isinstance(c.get("form_data"), dict) else {}
        out.update(
            {
                "description": c.get("description"),
                # ChartInfo carries no numeric dataset id field (verified against
                # 6.1.0's schema) -- but form_data.datasource is Superset's classic
                # "{id}__{type}" string (e.g. "1__table") and every chart has one,
                # so this is not a fallback, it's the actual source of truth.
                "datasource_id": _parse_datasource_id(form_data.get("datasource")),
                "datasource_name_text": c.get("datasource_name"),
                "owners": c.get("owners") or [],
                "dashboards": [],  # MCP ChartInfo does not carry membership; see get_dashboard_info instead.
                "params": json.dumps(form_data) if form_data else None,
                # MCP's ChartInfo carries no stored query_context blob -- GetChartDataTool's
                # MCP path calls Superset's own get_chart_data tool by chart id instead of
                # replaying one. But GetChartTool.run() derives the model-facing
                # `has_stored_query_context` field as `bool(query_context)`, and that
                # signal needs to mean "get_chart_data will work for this chart" whichever
                # path is active. In MCP mode that's true whenever form_data exists (MCP
                # builds the query fresh from it), so alias to form_data rather than
                # leaving this None -- a None here would make the tool tell the model
                # get_chart_data can't run when it actually can.
                "query_context": form_data or None,
            }
        )
    return out


def _dashboard_to_rest(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": d.get("id"),
        "dashboard_title": d.get("dashboard_title"),
        "status": "published" if d.get("published") else "draft",
    }


def _parse_datasource_id(datasource: Any) -> int | None:
    """Parse Superset's "{id}__{type}" form_data.datasource string, e.g. "1__table"."""
    if not isinstance(datasource, str) or "__" not in datasource:
        return None
    head = datasource.split("__", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _first_text(result: Any) -> str | None:
    for item in result.content:
        text = getattr(item, "text", None)
        if text is not None:
            return text
    return None
