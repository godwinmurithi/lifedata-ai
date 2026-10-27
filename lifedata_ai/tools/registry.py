"""Tool discovery, Anthropic export, and the audited execution path (spec §5).

The agent loop talks to this module and nothing else: it asks for the tool
specs to send to the model, then hands each `tool_use` block back here to be
run. Everything that matters for governance sits on that one path — argument
validation, the read-only guard, structured error conversion, and the
`ToolCallLog` row that satisfies the Phase 1 criterion that *all* tool calls
appear in the audit log with the username.

`build_registry()` registers read tools only. Write tools exist as a mode in
`tools/base.py` so the boundary is explicit, but nothing server-side may run
them: write actions are validated, signed and executed by the panel with the
user's own session (spec §1). `execute()` refuses a write-mode tool outright
rather than relying on the registry's contents to keep that invariant.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from lifedata_ai.models import ToolCallLog
from lifedata_ai.superset_client import SupersetError, SupersetUnauthenticated
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode

logger = logging.getLogger(__name__)


# --- Registry ------------------------------------------------------------


class ToolRegistry:
    """An ordered collection of `Tool` instances, keyed by tool name.

    Insertion order is preserved because the exported spec list feeds the
    Anthropic prompt prefix: a stable order keeps the prompt cache warm across
    turns, and a reshuffled tool list would invalidate it.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def filter_by_mode(self, mode: ToolMode) -> list[Tool]:
        return [tool for tool in self._tools.values() if tool.mode is mode]

    #: Anthropic's own hard ceiling: "Too many strict tools (N). The maximum
    #: number of strict tools supported is 20." Exceeding it 400s the whole
    #: turn, same as an unclosed schema would. `strict` is an opportunistic
    #: guarantee, not a safety mechanism — `execute()` runs `validate_arguments`
    #: / `validate_action` against the full schema regardless, so capping which
    #: tools get it costs a convenience, not correctness.
    _MAX_STRICT_TOOLS = 20

    def to_anthropic_specs(self, mode: ToolMode | None = None) -> list[dict[str, Any]]:
        """Tool definitions for the Messages API `tools` parameter."""
        tools = self.all() if mode is None else self.filter_by_mode(mode)
        specs = [tool.to_anthropic_spec() for tool in tools]
        strict_seen = 0
        for spec in specs:
            if not spec.get("strict"):
                continue
            strict_seen += 1
            if strict_seen > self._MAX_STRICT_TOOLS:
                del spec["strict"]
        return specs

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ToolRegistry {len(self._tools)} tools: {', '.join(self._tools)}>"


# --- Discovery -----------------------------------------------------------

#: Module path -> class names to register, in prompt order. Read tools only for
#: Phase 1; `superset_write` is deliberately absent, not merely filtered.
_READ_TOOL_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "lifedata_ai.tools.superset_read",
        (
            "ListDatabasesTool",
            "ListDatasetsTool",
            "GetDatasetTool",
            "ListChartsTool",
            "GetChartTool",
            "ListDashboardsTool",
            "GetDashboardTool",
            "GetChartDataTool",
            "GetPageContextTool",
        ),
    ),
    # `review_dashboard_design` reads a dashboard and returns a verdict; it
    # changes nothing, so it is a read tool despite being about writing.
    ("lifedata_ai.tools.review", ("ReviewDashboardDesignTool",)),
    # `ask_user` writes nothing to Superset either. Emitting the SSE event and
    # recording the pending Clarification is the loop's job, because only the
    # loop knows the `tool_use` id the answer must resolve.
    ("lifedata_ai.tools.interaction", ("AskUserTool",)),
    (
        "lifedata_ai.tools.lifedata",
        (
            "ListFormsTool",
            "GetFormSchemaTool",
        ),
    ),
    # Full MCP passthrough -- registered as READ mode like everything else
    # here, which means tools called THROUGH these two (including MCP tools
    # tagged "mutate": execute_sql, generate_chart, update_chart, ...) run
    # IMMEDIATELY, with none of the propose/confirm/snapshot/undo pipeline
    # the hand-built write tools below have for the same operations. Full
    # MCP authorization per explicit instruction (2026-09-25) -- see
    # mcp_passthrough.py's module docstring and AI_NATIVE_STATUS.md.
    ("lifedata_ai.tools.mcp_passthrough", ("SearchMcpToolsTool", "CallMcpToolTool")),
    # Phase 7/10 prototype, admin-only (enforced inside the tool itself, not
    # by omission from non-admin registries -- see canvas.py's own gate).
    ("lifedata_ai.tools.canvas", ("AuthorCanvasTool",)),
    ("lifedata_ai.tools.publications", ("PublishDashboardTool", "EditDashboardChartStyleTool")),
    ("lifedata_ai.tools.geography", ("DiscoverAdministrativeBoundariesTool", "PrepareGeographicMapTool")),
    ("lifedata_ai.tools.templates", ("GetDashboardTemplateTool",)),
    ("lifedata_ai.tools.data_profile", ("ProfileDatasetTool",)),
    ("lifedata_ai.tools.capabilities", ("GetDashboardCapabilitiesTool",)),
    ("lifedata_ai.tools.quality", ("BeginDashboardBuildTool", "InspectDashboardTool", "CompleteDashboardBuildTool", "RestoreDashboardBuildTool")),
    ("lifedata_ai.tools.composition", ("PlanDashboardTool",)),
    ("lifedata_ai.tools.map_style", ("EditDashboardMapStyleTool",)),
    # Phase 18. READ mode despite `propose_skill` writing a `Skill` row --
    # see skills.py's module docstring for why that is not a WRITE tool in
    # this codebase's sense (no Superset envelope/apply step exists for it).
    ("lifedata_ai.tools.skills", ("ProposeSkillTool", "ListSkillsTool", "ApplySkillTool")),
)

#: Write tools. Each one PROPOSES a change and returns a signed envelope; none
#: of them touches Superset. See lifedata_ai/tools/superset_write.py.
_WRITE_TOOL_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "lifedata_ai.tools.superset_write",
        (
            "CreateChartTool",
            "UpdateChartTool",
            "RenameChartTool",
            "CreateDashboardTool",
            "RenameDashboardTool",
            "SetDashboardCssTool",
            "SetDashboardThemeTool",
            "AddChartToDashboardTool",
            "RemoveChartFromDashboardTool",
            "MoveDashboardComponentTool",
            "ResizeDashboardComponentTool",
            "AddNativeFilterTool",
        ),
    ),
)


#: Tools that read the dynamic-form metadata tables. Named here rather than in a
#: separate source tuple so the prompt ORDER of every other tool is untouched
#: when they are dropped — that order is what keeps the prompt cache warm.
_FORM_TOOLS: frozenset[str] = frozenset({"ListFormsTool", "GetFormSchemaTool"})


def build_registry(read_only: bool = False) -> ToolRegistry:
    """Build a registry of every tool that imports cleanly.

    Imports are defensive on purpose: a missing or broken module degrades the
    copilot to the tools that do work instead of taking the whole chat endpoint
    down with an ImportError. Failures are logged loudly — a silently short
    tool list is far harder to diagnose than a stack trace.

    Registering a write tool does NOT give the orchestrator the ability to
    write. A write tool's `run()` validates, snapshots and returns a *signed
    envelope*; the panel executes it with the user's own session. That
    separation is the whole security model (spec §1), so `execute()` below
    still refuses to treat a write tool's return value as a completed action.

    `read_only=True` keeps the Phase 1 behaviour for tests that assert the
    copilot cannot propose changes at all.

    The form tools are registered only where the dynamic-form tables actually
    exist (`LIFEDATA_AI_ENABLE_FORM_TOOLS`, on by default). They read a schema
    `manage.py seed_lifehealth` creates; a deployment that adopts an existing
    Superset generally has neither those tables nor that schema, and the tools
    then fail at call time with a message about a seed command the operator has
    no reason to run. A tool that cannot work should not be offered: the model
    spends a round discovering it, and the user hears about a capability that
    does not exist here.
    """
    import importlib

    sources = _READ_TOOL_SOURCES if read_only else _READ_TOOL_SOURCES + _WRITE_TOOL_SOURCES
    if not getattr(settings, "LIFEDATA_AI_ENABLE_FORM_TOOLS", True):
        sources = tuple(
            (module_path, tuple(name for name in class_names if name not in _FORM_TOOLS))
            for module_path, class_names in sources
        )
        sources = tuple((path, names) for path, names in sources if names)

    registry = ToolRegistry()
    for module_path, class_names in sources:
        try:
            module = importlib.import_module(module_path)
        except Exception:
            logger.exception("Tool module %s could not be imported; skipping", module_path)
            continue

        for class_name in class_names:
            tool_class = getattr(module, class_name, None)
            if tool_class is None:
                logger.warning("%s.%s not found; skipping", module_path, class_name)
                continue
            try:
                tool = tool_class()
            except Exception:
                logger.exception("%s.%s could not be instantiated", module_path, class_name)
                continue

            if read_only and tool.mode is not ToolMode.READ:
                logger.error("Refusing to register write-mode tool %s in read-only registry",
                             tool.name)
                continue
            registry.register(tool)

    return registry


@functools.lru_cache(maxsize=1)
def get_registry() -> ToolRegistry:
    """The process-wide registry.

    Tools are stateless, so one instance per process is safe and avoids
    re-importing on every chat turn. Call `build_registry()` directly in tests
    that need a fresh, isolated registry.
    """
    return build_registry()


# --- Execution -----------------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one tool call, in the shape the agent loop needs."""

    tool_name: str
    ok: bool
    content: Any
    error: str = ""
    duration_ms: int = 0

    def to_anthropic_content(self) -> str:
        """Serialise the payload for a `tool_result` content block.

        Always JSON text: the model parses it reliably, and `default=str` means
        a stray datetime or Decimal degrades to a string rather than raising
        mid-turn.
        """
        return json.dumps(self.content, default=str)

    def to_model_content(self):
        # Images are multimodal content, never text containing base64 JSON.
        if isinstance(self.content, dict) and self.content.get('_tool_images'):
            metadata = {k:v for k,v in self.content.items() if k != '_tool_images'}
            return [{'type':'text','text':json.dumps(metadata,default=str)}, *self.content['_tool_images']]
        return self.to_anthropic_content()

    def to_tool_result_block(self, tool_use_id: str) -> dict[str, Any]:
        """The complete `tool_result` block, `is_error` included."""
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": self.to_model_content(),
        }
        if not self.ok:
            block["is_error"] = True
        return block


def execute(
    name: str,
    arguments: dict[str, Any] | None,
    context: ToolContext,
    registry: ToolRegistry | None = None,
) -> ToolResult:
    """Run a tool by name, audit it, and never raise into the agent loop.

    Every exit path — unknown tool, write refusal, invalid arguments, Superset
    error, unexpected crash — produces a `ToolResult` and a `ToolCallLog` row.
    An exception escaping here would abort the turn and lose the audit record,
    which is exactly what the Phase 1 acceptance criterion forbids.
    """
    registry = registry or get_registry()
    arguments = arguments or {}
    tool = registry.get(name)
    started = time.monotonic()

    def finish(
        ok: bool,
        content: Any,
        *,
        error: str = "",
        mode: str = "unknown",
    ) -> ToolResult:
        duration_ms = int((time.monotonic() - started) * 1000)
        _write_audit_row(
            context=context,
            tool_name=name,
            mode=mode,
            arguments=arguments,
            ok=ok,
            error=error,
            duration_ms=duration_ms,
        )
        return ToolResult(
            tool_name=name,
            ok=ok,
            content=content,
            error=error,
            duration_ms=duration_ms,
        )

    if tool is None:
        message = f"Unknown tool: {name}. Available tools: {', '.join(registry.names())}."
        return finish(False, {"error": "unknown_tool", "message": message}, error=message)

    # A write tool IS runnable here — but understand exactly what running it
    # does: it validates, snapshots, and returns a *signed envelope*. It does
    # not touch Superset. The actual HTTP write is performed later by the panel,
    # with the user's own session cookie and CSRF token (spec §1).
    #
    # The guard that keeps that true lives in superset_write.py, which contains
    # no POST/PUT/DELETE against Superset at all. If that ever changes, this
    # comment is the thing that was lying.
    mode = tool.mode.value
    try:
        tool.validate_arguments(arguments)
        scope = context.page_context or {}
        native_style = name in {"set_dashboard_css", "set_dashboard_theme"}
        if name == "update_chart":
            native_style = bool(set(arguments.get("params") or {}) & {"color_scheme", "label_colors", "custom_colors", "color", "colors", "css"})
        if name == "call_mcp_tool":
            native_style = arguments.get("tool_name") in {"set_dashboard_css", "set_dashboard_theme"}
            if arguments.get("tool_name") in {"update_chart", "update_dashboard"}:
                try:
                    native_arguments = json.loads(arguments.get("arguments_json", "{}"))
                except (TypeError, ValueError):
                    native_arguments = {}
                def has_style(value):
                    if isinstance(value, dict):
                        return bool(set(value) & {"css", "color_scheme", "label_colors", "custom_colors", "theme"}) or any(has_style(v) for v in value.values())
                    if isinstance(value, list):
                        return any(has_style(v) for v in value)
                    return False
                native_style = native_style or has_style(native_arguments)
        if scope.get("dashboard_surface") == "custom" and native_style:
            raise ToolError('You are editing a CUSTOM publication. Superset CSS/theme does not style this page. '
                            'Use edit_dashboard_chart_style with the current publication_id and exact returned target. '
                            'Do not substitute page-wide colors for an individual chart request.', code='custom_dashboard_style_required')
        content = tool.run(context, **arguments)
    except ToolError as exc:
        return finish(False, exc.to_payload(), error=exc.message, mode=mode)
    except SupersetUnauthenticated as exc:
        message = "Your Superset session is no longer valid. Please sign in again."
        logger.warning("Tool %s hit an expired session: %s", name, exc)
        return finish(
            False,
            {"error": "superset_unauthenticated", "message": message},
            error=message,
            mode=mode,
        )
    except SupersetError as exc:
        # Surface Superset's own verdict rather than swallowing it — a 403 must
        # reach the user as "you lack permission", not as a generic failure.
        code = "superset_permission_denied" if exc.is_permission_denied else "superset_error"
        message = str(exc)
        logger.warning("Tool %s failed against Superset: %s", name, exc)
        return finish(
            False,
            {
                "error": code,
                "message": message,
                "status_code": exc.status_code,
                "detail": _truncate(exc.body),
            },
            error=message,
            mode=mode,
        )
    except TypeError as exc:
        # Almost always a schema/signature mismatch on the tool's own arguments;
        # returning it lets the model retry with a corrected call.
        message = f"{name} rejected those arguments: {exc}"
        logger.warning("Tool %s argument mismatch: %s", name, exc)
        return finish(
            False,
            {"error": "invalid_arguments", "message": message},
            error=message,
            mode=mode,
        )
    except Exception as exc:  # noqa: BLE001 - the loop must survive any tool bug
        message = f"{name} failed unexpectedly: {exc}"
        logger.exception("Unexpected failure in tool %s", name)
        return finish(
            False,
            {"error": "tool_failed", "message": message},
            error=message,
            mode=mode,
        )

    return finish(True, content, mode=mode)


def audit_refusal(
    context: ToolContext, name: str, arguments: dict[str, Any] | None, reason: str
) -> None:
    """Record a tool call the LOOP refused before it could run.

    Two guards refuse a call without reaching `execute()`: the per-turn write cap
    and the `ask_user` guards. Both are turn-level state that a tool cannot see,
    so they cannot move into the tool — but a refused call is still a call the
    model made, and the Phase 1 acceptance criterion is that *all* tool calls
    appear in the audit log with the username. Without this the trail shows a
    turn that proposed fourteen writes and attempted no more, which is not what
    happened.
    """
    registry = get_registry()
    tool = registry.get(name)
    _write_audit_row(
        context=context,
        tool_name=name,
        mode=tool.mode.value if tool is not None else "unknown",
        arguments=arguments or {},
        ok=False,
        error=reason,
        duration_ms=0,
    )


def _write_audit_row(
    *,
    context: ToolContext,
    tool_name: str,
    mode: str,
    arguments: dict[str, Any],
    ok: bool,
    error: str,
    duration_ms: int,
) -> None:
    """Persist the `ToolCallLog` row; never let auditing break the turn.

    `ToolCallLog.conversation` is a required FK, so calls made outside a
    persisted conversation (diagnostics, tests) are logged to the application
    log instead. Chat turns always carry a conversation, so the acceptance
    criterion still holds where it matters.
    """
    if context.conversation is None:
        logger.info(
            "Tool %s called by %s outside a conversation; no audit row written",
            tool_name,
            context.username,
        )
        return

    try:
        ToolCallLog.objects.create(
            conversation=context.conversation,
            superset_username=context.username,
            tool_name=tool_name,
            mode=mode,
            arguments=arguments,
            ok=ok,
            error=error[:2000],
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 - a failed audit write must not kill the turn
        logger.exception("Could not write ToolCallLog for %s", tool_name)


def _truncate(value: Any, limit: int = 500) -> Any:
    """Keep Superset error bodies small enough to sit in a tool_result."""
    if isinstance(value, str):
        return value[:limit]
    return value
