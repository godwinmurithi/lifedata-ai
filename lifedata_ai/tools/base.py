"""The tool contract every copilot tool implements (spec §2, §5).

Spec §2 requires that tools sit behind a thin interface so a Superset MCP
backend can replace the direct REST calls later without the agent loop
changing. That is the whole reason this module exists: the loop only ever sees
`Tool.name`, `Tool.to_anthropic_spec()` and `Tool.run(...)`. Swapping the
transport means writing new `Tool` subclasses, not touching `agent/loop.py`.

Two invariants are enforced here rather than trusted to each tool author:

* **Writes never execute server-side.** `ToolMode.WRITE` exists so the registry
  can refuse to run such a tool at all — write actions are validated, signed and
  handed to the panel (spec §1). Phase 1 registers no write tools whatsoever.
* **Tool failures are conversational, not fatal.** Raise `ToolError` and the
  registry turns it into an `is_error` tool_result the model can self-correct
  from; anything else would abort the turn.

A minimal tool looks like this::

    from __future__ import annotations

    from typing import Any

    from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode


    class GetDatasetTool(Tool):
        \"\"\"Columns, types and saved metrics for one Superset dataset.\"\"\"

        name = "get_dataset"
        description = (
            "Return the columns, data types, descriptions and saved metrics of a "
            "Superset dataset. Use before proposing a chart so metrics are real."
        )
        mode = ToolMode.READ
        input_schema = {
            "type": "object",
            "properties": {
                "dataset_id": {
                    "type": "integer",
                    "description": "Superset dataset id, e.g. 2 for rpt_encounters.",
                },
            },
            "required": ["dataset_id"],
            "additionalProperties": False,
        }

        def run(self, context: ToolContext, dataset_id: int) -> dict[str, Any]:
            client = context.superset_client()
            payload = client.get_dataset(dataset_id)
            if not payload:
                raise ToolError(f"No dataset with id {dataset_id}.", code="not_found")
            return {"id": dataset_id, "columns": payload.get("columns", [])}

Note the schema shape: `additionalProperties: false` plus every property listed
in `required` is what lets `to_anthropic_spec()` add `"strict": true`, which
makes the API guarantee the arguments validate before `run` is ever called.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from lifedata_ai.superset_client import SupersetClient

if TYPE_CHECKING:  # Import-time cycles and app-registry ordering, not runtime needs.
    from lifedata_ai.middleware import SupersetIdentity
    from lifedata_ai.models import Conversation
    from lifedata_ai.semantics.loader import Registry


#: JSON Schema keywords the Anthropic tool API rejects.
#:
#: Discovered the hard way: a tool carrying `minimum` on an integer property is
#: refused with
#:     tools.15.custom: For 'integer' type, property 'minimum' is not supported
#: and that 400 kills the whole turn, not just the offending tool.
#:
#: Stripping them costs nothing in safety. The model-facing schema is only a
#: hint about shape; the authoritative check is `validate_arguments` here plus
#: `validate_action` in the validator, both of which use the FULL schema. So a
#: model that ignores the hint and sends row_limit=999999 is still refused —
#: just server-side rather than by the API.
_ANTHROPIC_UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "format",
        "$schema",
    }
)


def _sanitise_for_anthropic(schema: Any) -> Any:
    """Make a governed action schema acceptable as an Anthropic tool schema.

    Two transformations, both lossy in the same harmless direction: the
    model-facing schema becomes more permissive, while the authoritative
    server-side checks stay exactly as strict as before.
    """
    return _strip_keywords(_flatten_top_level_union(schema))


def _flatten_top_level_union(schema: Any) -> Any:
    """Collapse a top-level `oneOf` into one object schema.

    The API refuses a union at the top level:
        tools.21.custom.input_schema: input_schema does not support oneOf,
        allOf, or anyOf at the top level

    Our layout actions are genuinely unions — `move_after` needs a
    `target_chart_id`, `move_to_top` does not — so the schema is right and the
    export has to change. We merge the branches: every branch's properties
    become optional properties of one object, and only the fields required by
    *every* branch stay required. The `operation` discriminator keeps an enum of
    all its permitted values, which is the part the model actually needs.

    The union is not lost, only deferred: `validate_action` still applies the
    real `oneOf`, so a payload with `operation: "move_after"` and no
    `target_chart_id` is refused server-side with a message the model can
    correct from.
    """
    if not isinstance(schema, dict) or "oneOf" not in schema:
        return schema

    branches = [b for b in schema["oneOf"] if isinstance(b, dict)]
    if not branches:
        return schema

    merged_properties: dict[str, Any] = {}
    required_sets: list[set[str]] = []
    operations: list[Any] = []

    for branch in branches:
        required_sets.append(set(branch.get("required") or []))
        for name, definition in (branch.get("properties") or {}).items():
            if name == "operation" and isinstance(definition, dict):
                const = definition.get("const")
                if const is not None and const not in operations:
                    operations.append(const)
                continue
            merged_properties.setdefault(name, definition)

    if operations:
        merged_properties["operation"] = {
            "type": "string",
            "enum": operations,
            "description": "Which layout operation to perform.",
        }

    flattened = {
        key: value for key, value in schema.items() if key not in {"oneOf", "properties", "required"}
    }
    flattened["type"] = "object"
    flattened["properties"] = merged_properties
    flattened["required"] = sorted(set.intersection(*required_sets) if required_sets else set())
    # The union's branches are each closed, but the union ITSELF carries no
    # top-level `additionalProperties` — combining one with `oneOf` is the
    # classic JSON Schema trap, because it cannot see inside the branches and
    # rejects every branch-specific key. The flattened form has no branches, so
    # it can and must be closed: the merged property set is the union of every
    # branch's, so nothing legitimate is excluded, and without it the exported
    # schema silently invites keys `validate_action` will then refuse.
    if all(branch.get("additionalProperties") is False for branch in branches):
        flattened["additionalProperties"] = False
    return flattened


def _strip_keywords(schema: Any) -> Any:
    """Recursively drop keywords the Anthropic tool API will not accept."""
    if isinstance(schema, dict):
        return {
            key: _strip_keywords(value)
            for key, value in schema.items()
            if key not in _ANTHROPIC_UNSUPPORTED_KEYWORDS
        }
    if isinstance(schema, list):
        return [_strip_keywords(item) for item in schema]
    return schema


# --- Modes ---------------------------------------------------------------


class ToolMode(str, Enum):  # noqa: UP042 - see the note below on StrEnum
    """Whether a tool reads or writes.

    Read tools run server-side with the caller's forwarded Superset session
    cookie. Write tools are *never* executed here — see the module docstring.
    Subclasses `str` so the value serialises straight into `ToolCallLog.mode`
    and JSON payloads.

    Deliberately NOT `enum.StrEnum`, which ruff suggests. `StrEnum.__str__`
    returns the value, so `f"{ToolMode.WRITE}"` becomes `"write"` — but this
    class's `__str__` is inherited from `Enum` and returns `"ToolMode.WRITE"`.
    Switching would silently change the text of any log line or error message
    that interpolates a mode, and `mode.value` is used everywhere it matters.
    """

    READ = "read"
    WRITE = "write"


# --- Errors --------------------------------------------------------------


class ToolError(Exception):
    """A tool failed in a way the model should see and can react to.

    The registry converts this into a structured `is_error` tool_result rather
    than letting it escape and kill the turn. Use it for "dataset 99 does not
    exist" or "that chart has no stored query context" — anything the model can
    plausibly correct by calling a different tool or different arguments.

    Genuine bugs should raise normally; the registry still catches them for
    audit purposes but they are logged as unexpected.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_error",
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        """The JSON body handed back to the model as the error tool_result."""
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


# --- Context -------------------------------------------------------------


@dataclass
class ToolContext:
    """Everything a tool is allowed to know about the caller.

    Deliberately narrow: a tool gets the verified identity, the session cookie
    to act *as that user*, the conversation for audit, and the page context the
    panel sent. It never gets the raw `HttpRequest`, so a tool cannot reach for
    anything the agent loop did not decide to hand over.
    """

    #: The end user's Superset `session` cookie. Forwarded on every read call so
    #: Superset applies that user's own permissions (spec §1).
    superset_session_cookie: str
    identity: SupersetIdentity
    #: None only outside a persisted chat turn (diagnostics, tests). When None,
    #: `ToolCallLog` cannot be written — the audit row needs the FK.
    conversation: Conversation | None = None
    #: Normalised page context supplied by the panel: page_type, dashboard_id,
    #: chart_id, dataset_id, active filters, selected widget. `get_page_context`
    #: echoes this back; other tools may read it as a hint, never as authority.
    page_context: dict[str, Any] | None = None

    _client: SupersetClient | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def username(self) -> str:
        return self.identity.username

    def superset_client(self) -> SupersetClient:
        """A Superset client bound to this user's session, built once per turn.

        Cached on the context so a multi-tool turn does not rebuild the client
        for every call. The client itself opens a fresh httpx connection per
        request, so there is no shared connection state to go stale.
        """
        if self._client is None:
            self._client = SupersetClient(session_cookie=self.superset_session_cookie)
        return self._client

    def registry(self) -> Registry:
        """The semantics registry (spec §8).

        Exposed here so the LifeData tools do not each import the loader; it is
        `lru_cache`d upstream, so repeated calls are free.
        """
        from lifedata_ai.semantics.loader import load_registry

        return load_registry()


# --- Tool ----------------------------------------------------------------


class Tool(ABC):
    """Base class for every copilot tool.

    Subclasses declare four class attributes and implement `run`. Keep `run`
    synchronous — the agent loop is a plain Django view, not async.

    `run` must return something JSON-serialisable and must never return
    row-level patient records (spec §1 privacy boundary); aggregate, row-capped
    results only.
    """

    #: Snake-case, stable. This is the string the model emits, so renaming one
    #: invalidates any prompt or conversation history that referenced it.
    name: ClassVar[str]
    #: Written for the model, not for developers. Say when to call it, not just
    #: what it does — that measurably improves tool selection.
    description: ClassVar[str]
    mode: ClassVar[ToolMode]
    #: JSON Schema for the arguments. Prefer `additionalProperties: false` with
    #: every property in `required` so strict mode can be enabled.
    input_schema: ClassVar[dict[str, Any]]

    @abstractmethod
    def run(self, context: ToolContext, **kwargs: Any) -> Any:
        """Execute the tool and return a JSON-serialisable result.

        Raise `ToolError` for failures the model should see and retry from.
        """

    # -- export -----------------------------------------------------------

    def to_anthropic_spec(self) -> dict[str, Any]:
        """The tool definition passed to the Anthropic Messages API.

        Adds `"strict": true` when the schema supports it. Verified against the
        current API: `strict` is a top-level field on the tool definition
        (alongside name/description/input_schema) — *not* part of `tool_choice`
        — and requires `additionalProperties: false` plus a complete `required`
        list. With it set, the API guarantees the arguments validate, so `run`
        never sees a malformed payload.
        """
        spec: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": _sanitise_for_anthropic(self.input_schema),
        }
        if self.supports_strict():
            spec["strict"] = True
        return spec

    @staticmethod
    def _sanitise_for_anthropic(schema: dict[str, Any]) -> dict[str, Any]:
        """Exposed for tests; see the module-level function."""
        return _sanitise_for_anthropic(schema)

    def supports_strict(self) -> bool:
        """Whether the input schema meets the strict-tool-use requirements."""
        schema = self.input_schema
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return False
        if schema.get("additionalProperties") is not False:
            return False
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        return set(properties) == set(required)

    # -- validation -------------------------------------------------------

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        """Check model-supplied arguments against `input_schema`.

        Belt and braces behind strict mode: the registry calls this before
        `run`, so a tool body can trust its keyword arguments even when strict
        could not be enabled or the caller is a test.

        Raises `ToolError` so the failure reaches the model as a correctable
        tool_result rather than a 500.
        """
        try:
            import jsonschema
        except ImportError:  # pragma: no cover - jsonschema is a hard dependency
            return

        try:
            jsonschema.validate(instance=arguments, schema=self.input_schema)
        except jsonschema.ValidationError as exc:
            raise ToolError(
                f"Invalid arguments for {self.name}: {exc.message}",
                code="invalid_arguments",
                details={"path": list(exc.absolute_path)},
            ) from exc

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name} ({self.mode.value})>"
