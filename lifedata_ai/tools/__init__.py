"""Copilot tools: the only surface the agent loop uses to touch Superset.

`base` defines the contract (see its docstring for a worked example); `registry`
handles discovery, Anthropic export, and the audited execution path. Import
from here rather than reaching into the submodules, so the interface can be
re-pointed at an MCP backend later without a repo-wide edit (spec §2).
"""

from __future__ import annotations

from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode
from lifedata_ai.tools.registry import (
    ToolRegistry,
    ToolResult,
    audit_refusal,
    build_registry,
    execute,
    get_registry,
)

__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolMode",
    "ToolRegistry",
    "ToolResult",
    "audit_refusal",
    "build_registry",
    "execute",
    "get_registry",
]
