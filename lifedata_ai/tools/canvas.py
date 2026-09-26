"""Sandboxed canvas — Phase 7/10 prototype: agent-authored bespoke visuals.

One proof, admin-only, behind a flag. Vima writes the actual rendering code
(a small HTML/JS snippet) itself; this tool's job is narrower than it looks:
fetch real data through the existing, already-governed chart-data path (same
registry gate and RLS every other read tool goes through -- this tool does
not open a new data-access door, only a new *rendering* one), do a minimal
defense-in-depth check that the snippet isn't trying to reach the network or
escape the sandbox, and hand both to the panel.

The actual isolation is the browser's own iframe sandbox
(`sandbox="allow-scripts"`, deliberately never `allow-same-origin`, no `src`
pointed at a real origin -- `srcdoc` gives it an opaque origin with no
access to cookies, storage, or the parent page), not this backend check.
Treat the check here as a second, weaker layer, not the actual boundary.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode
from lifedata_ai.tools.superset_read import GetChartDataTool

#: Substrings that would indicate an attempt to reach outside the sandbox.
#: The iframe sandbox (no allow-same-origin, srcdoc, no allow-popups) already
#: makes every one of these a no-op even if this check missed something --
#: this is a second layer, not the boundary itself.
_FORBIDDEN_SNIPPET_PATTERNS: tuple[str, ...] = (
    "fetch(", "xmlhttprequest", "websocket", "<script src", "<script\tsrc",
    "import(", "eval(", "document.cookie", "localstorage", "sessionstorage",
    "top.location", "parent.location", "window.open",
)

#: Generous but bounded -- this is a chart, not an app.
_MAX_SNIPPET_LENGTH = 20_000


class AuthorCanvasTool(Tool):
    name: ClassVar[str] = "author_canvas"
    mode: ClassVar[ToolMode] = ToolMode.READ
    description: ClassVar[str] = (
        "Render a bespoke, agent-authored visual that no built-in chart type "
        "covers, inside an isolated sandbox (no network, no access to the page "
        "or cookies). Admin-only prototype. Write self-contained HTML/CSS/JS "
        "(inline <style>/<script> only, no external resources, no fetch/"
        "XMLHttpRequest/WebSocket) that reads its data from a `data` variable "
        "the sandbox provides via postMessage -- do not try to fetch data "
        "yourself. Call get_chart_data or get_dataset first to know the real "
        "data shape before writing the snippet."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "chart_id": {
                "type": "integer",
                "description": "Chart whose data the canvas should visualise.",
            },
            "html_snippet": {
                "type": "string",
                "description": (
                    "Self-contained HTML/CSS/JS. Receives the chart's rows via "
                    "`window.addEventListener('message', e => { const data = "
                    "e.data.rows; ... })` -- do not fetch data another way."
                ),
                "maxLength": _MAX_SNIPPET_LENGTH,
            },
            "title": {"type": "string", "description": "Short title for the visual."},
        },
        "required": ["chart_id", "html_snippet", "title"],
        "additionalProperties": False,
    }

    def run(
        self, context: ToolContext, chart_id: int, html_snippet: str, title: str
    ) -> dict[str, Any]:
        if "Admin" not in (context.identity.roles or []):
            raise ToolError(
                "The sandboxed canvas is an admin-only prototype (Phase 10 opens it "
                "to other roles later, with a promotion/review flow -- not yet built).",
                code="admin_only",
            )

        lowered = html_snippet.lower()
        hit = next((p for p in _FORBIDDEN_SNIPPET_PATTERNS if p in lowered), None)
        if hit:
            raise ToolError(
                f"Refused: the snippet contains {hit!r}, which the sandbox already "
                "blocks structurally -- rewrite without it. The canvas has no network "
                "access and no path to fetch its own data; use the `data` postMessage "
                "payload instead.",
                code="forbidden_pattern",
            )

        # Reuses GetChartDataTool's own run() so this goes through the exact
        # same registry gate / RLS every other read of this chart's data
        # already goes through -- no new data-access path, only a new
        # rendering one.
        chart_data = GetChartDataTool().run(context, chart_id=chart_id)

        return {
            "type": "sandboxed_canvas",
            "title": title,
            "html_snippet": html_snippet,
            "rows": chart_data.get("rows", []),
            "columns": chart_data.get("columns", []),
            "note": (
                "Admin-only prototype (Phase 7/10). Rendered in an opaque-origin "
                "sandboxed iframe: no network, no cookies, no access to the parent "
                "page. Promotion to a reusable org chart type needs a human "
                "reviewer -- not built yet."
            ),
        }
