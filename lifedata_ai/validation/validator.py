"""Policy validation for every write action the copilot proposes (spec §6).

This module is the gate between "the model said so" and "Superset was changed".
Spec §6 fixes both the rules and their order; the order matters because each
rule assumes the previous one has already held. Schema first, because no policy
rule can reason about a payload of unknown shape. Then the SQL ban and per-viz
coherence check, and finally risk classification — which is not a rejection
but a routing decision (execute now vs. show the user a preview card first).

The semantics-registry-backed geography policy and column/metric role checks
(per-dataset grain, sensitivity, and declared-column allowlisting) have been
removed along with the registry itself. This module no longer knows what any
dataset's rows mean or which of its columns are safe to group by — see
git history if that policy needs reconstructing.

The viz-type and colour-scheme allowlist rules have also been removed, by
explicit direction (2026-10-01): no restriction on viz type or colour scheme
for now, to be applied later. Every Superset viz type (including maps) and
any identifier-shaped colour scheme value now passes schema and validator
unconditionally — see validation/schemas/actions.py's "Allowlists (NOT
currently enforced)" section for where to re-wire this if restrictions come
back.

The first failing rule short-circuits. That is deliberate: a validation result
is fed back to the model as a tool result so it can self-correct (spec §5 step
1), and one precise reason produces a better correction than five cascading
ones. Every message therefore says what was wrong *and* what would be
acceptable.

Two properties make this module exhaustively testable, which spec §9 demands
("every validator rule gets a failing-case test"):

* **No network calls and no database access.** Dashboard ownership and component
  counts arrive as `ValidationContext` data gathered by the caller from
  `get_dashboard`; this module never fetches them.
* **Pure function of (action_type, payload, context).** Same inputs, same
  verdict, always.

Usage::

    result = validate_action("create_chart", payload, context=ValidationContext(
        identity_username=identity.username,
        dashboard_owners=["analyst"],          # from get_dashboard
    ))
    if not result.ok:
        return {"error": "validation_failed", "messages": result.errors}
    record.risk = result.risk
    record.payload = result.normalised_payload
"""

from __future__ import annotations

import copy
import functools
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from lifedata_ai.validation.schemas import (
    ALLOWED_ACTION_TYPES,
    UnknownActionTypeError,
    get_schema,
)

_log = logging.getLogger(__name__)

#: Rule identifiers, in the order spec §6 mandates. `ValidationResult.reasons`
#: is keyed by these so a caller (or a test) can see exactly which rule spoke.
#: `row_limit` and `risk` are normalisation/classification rather than gates,
#: which is why they sit last and never reject on their own.
#:
#: `geography` and `column_roles` were removed along with the semantics
#: registry (they enforced per-dataset grain/sensitivity/column-role policy
#: that no longer exists anywhere in this codebase).
#:
#: `viz_allowlist` and `color_scheme` were removed by explicit direction
#: (2026-10-01): no restriction on viz type or colour scheme for now, to be
#: applied later — see the "Allowlists (NOT currently enforced)" section in
#: validation/schemas/actions.py for where the values live if reintroduced.
RULE_ORDER: Final[tuple[str, ...]] = (
    "schema",
    "no_adhoc_sql",
    "viz_coherence",
    "row_limit",
    "risk",
)

_PASS: Final = "pass"
_NOT_APPLICABLE: Final = "not applicable"
_NOT_EVALUATED: Final = "not evaluated (an earlier rule failed)"

#: Fallback for `LIFEDATA_AI_MAX_QUERY_ROW_LIMIT` (spec §5 caps chart queries at
#: 200 rows). Duplicated as a constant so this module stays importable — and
#: testable — without a configured Django settings module.
DEFAULT_MAX_ROW_LIMIT: Final = 200


# --- Result and context ---------------------------------------------------


@dataclass
class ValidationResult:
    """The verdict on one proposed action.

    `errors` are written for the *model*, not for a log reader: each one names
    the offending value and the acceptable alternative, because the agent loop
    hands them straight back as a tool result to be corrected.

    `normalised_payload` is the payload as it should be persisted and executed —
    currently that means row limits clamped. It is `None` when `ok` is False, so
    a caller cannot accidentally execute a rejected action's payload.
    """

    ok: bool
    risk: str | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalised_payload: dict[str, Any] | None = None
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def message(self) -> str:
        """One human/model-readable sentence block summarising the verdict."""
        if self.ok:
            parts = [f"Action validated. Risk: {self.risk}."]
            parts.extend(self.warnings)
            return " ".join(parts)
        return " ".join(self.errors) or "Action rejected."

    def to_payload(self) -> dict[str, Any]:
        """JSON-serialisable form for `ActionRecord.validation_result` (spec §4)."""
        return {
            "ok": self.ok,
            "risk": self.risk,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "reasons": dict(self.reasons),
        }


@dataclass(frozen=True)
class ValidationContext:
    """Everything the validator is allowed to know beyond the payload itself.

    `dashboard_owners` comes from `get_dashboard` (spec §6.6) and is passed in
    rather than fetched, so this module needs no Superset client and no session
    cookie. `None` means "we did not look" — which fails closed to `preview`,
    never to `low`.

    `dashboard_component_count` is the number of components already on the
    target dashboard; it bounds how much a whole-dashboard layout operation
    touches. Unknown again means preview.
    """

    identity_username: str
    #: The caller's numeric Superset user id, when known. Needed because
    #: Superset's owner objects identify a person by `id` and carry no
    #: `username` — matching on the name alone found nothing, every time.
    identity_user_id: int | None = None
    dashboard_owners: list[Any] | None = None
    dashboard_component_count: int | None = None


# --- Rule 3: adhoc-SQL detection ------------------------------------------
#
# DEFENCE IN DEPTH, NOT THE PRIMARY CONTROL. The primary control is structural:
# `validation/schemas/actions.py` closes every object with
# `additionalProperties: false` and models metrics and filters in shapes that
# have no key an SQL expression could occupy, so `{"expressionType": "SQL"}` is
# *unrepresentable* and dies at rule 1. This scan exists for the day someone
# widens a schema, adds a passthrough key, or hands a payload to
# `superset_write.py` by a path that skipped rule 1. A heuristic string scan
# would be a poor primary defence — it is regex against attacker-chosen text —
# but it is a good second one, and it makes the intent explicit to anyone
# reading the schemas later: SQL is not merely inconvenient here, it is banned.

#: Keys that carry raw SQL in Superset's own form_data vocabulary. None of them
#: exist in our schemas; seeing one means the payload came from somewhere else.
_SQL_BEARING_KEYS: Final[frozenset[str]] = frozenset(
    {
        "expressiontype",
        "sqlexpression",
        "sqlexpressionorig",
        "custom_sql",
        "customsql",
        "where",
        "having",
        "having_filters",
        "sql",
        "js_columns",
        "extra_where",
    }
)

#: Free-text values shown to humans and never interpolated into a query. Scanned
#: with the unambiguous-injection patterns only, so a chart legitimately titled
#: "Deleted records over time" is not refused for containing the word DELETE.
_FREE_TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "chart_name",
        "new_name",
        "dashboard_title",
        "new_title",
        "name",
        "description",
        "label",
        "x_axis_title",
        "y_axis_title",
        "y_axis_format",
    }
)

#: C0 and C1 control characters, excluding tab, newline and carriage return.
#: NUL is the one that actually breaks things — Postgres `text` cannot hold it,
#: so it fails far downstream as an opaque 500 rather than here as a clear
#: refusal. The rest are log- and terminal-injection vectors.
_CONTROL_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: Constructs that are injection attempts wherever they appear, including in a
#: chart title. Multi-token forms are used in preference to bare keywords to
#: keep the false-positive rate low enough that the rule can stay a hard reject.
_SQL_PATTERNS_ANYWHERE: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\bSELECT\b[\s\S]{0,200}?\bFROM\b", re.I), "a SELECT ... FROM statement"),
    (re.compile(r"\bUNION\s+(ALL\s+)?SELECT\b", re.I), "a UNION SELECT"),
    (
        re.compile(
            r";\s*\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|GRANT|CREATE|TRUNCATE|EXEC)\b",
            re.I,
        ),
        "a statement separator followed by a SQL keyword",
    ),
    (re.compile(r"--"), "an SQL line comment (--)"),
    (re.compile(r"/\*"), "an SQL block comment (/*)"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW|INDEX)\b", re.I), "a DROP statement"),
    (re.compile(r"\bDELETE\s+FROM\b", re.I), "a DELETE statement"),
    (re.compile(r"\bINSERT\s+INTO\b", re.I), "an INSERT statement"),
    (re.compile(r"\bUPDATE\b[\s\S]{0,80}?\bSET\b", re.I), "an UPDATE statement"),
    (re.compile(r"\bALTER\s+(TABLE|VIEW|DATABASE|SCHEMA)\b", re.I), "an ALTER statement"),
    (re.compile(r"\bGRANT\s+\w", re.I), "a GRANT statement"),
    (re.compile(r"\bpg_sleep\b", re.I), "a pg_sleep() timing probe"),
    (re.compile(r"\binformation_schema\b", re.I), "an information_schema probe"),
    (re.compile(r"\bxp_\w", re.I), "an xp_ extended stored procedure"),
)

#: Additional patterns for values that reach the query builder — column names,
#: metric names, filter values, time ranges. A bare statement keyword has no
#: business in any of those, so the bar is lower here than for free text.
_SQL_PATTERNS_QUERY_VALUES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|GRANT|TRUNCATE|EXEC|EXECUTE)\b", re.I),
        "a bare SQL statement keyword",
    ),
    (re.compile(r"\bOR\s+['\"]?\d+['\"]?\s*=\s*['\"]?\d+", re.I), "a tautology such as OR 1=1"),
    (re.compile(r"\bCASE\s+WHEN\b", re.I), "an inline CASE expression"),
)



#: Actions that are `preview` no matter how small they look. Removal is
#: destructive, so it is confirmed however few components it touches (spec §6.6).
_ALWAYS_PREVIEW: Final[frozenset[str]] = frozenset({"remove_chart_from_dashboard"})

#: Layout operations that restructure rows rather than moving one component.
_LAYOUT_REBUILD_OPERATIONS: Final[frozenset[str]] = frozenset({"kpi_row", "equalize_row"})

#: `preview` above this many components touched (spec §6.6: "> 2 components",
#: "dashboard creation with > 2 charts").
MAX_LOW_RISK_COMPONENTS: Final = 2

#: Actions exempt from the ownership rule. Empty: `refresh_chart` was the only
#: entry and it has been dropped — it permanently set `cache_timeout: 0`, a
#: persistent configuration change mislabelled as a refresh. Kept as a named
#: constant so the exemption stays an explicit, reviewable decision.
_OWNERSHIP_EXEMPT: Final[frozenset[str]] = frozenset()

RISK_LOW: Final = "low"
RISK_PREVIEW: Final = "preview"


# --- Helpers ---------------------------------------------------------------


@functools.cache
def _validator_for(action_type: str) -> Draft202012Validator:
    """A compiled validator per action type.

    Compiled once because `get_schema` deep-copies on every call and the agent
    loop validates on every write tool call. Raises `UnknownActionTypeError` for
    an invented action type — exceptions are not cached, so the raise repeats.
    """
    return Draft202012Validator(get_schema(action_type))


def _max_row_limit() -> int:
    """`LIFEDATA_AI_MAX_QUERY_ROW_LIMIT`, or the documented default.

    Read lazily and defensively so importing this module never requires Django
    settings to be configured — the point of a pure validator is that a plain
    `pytest` process can exercise it.
    """
    try:
        from django.conf import settings

        return int(getattr(settings, "LIFEDATA_AI_MAX_QUERY_ROW_LIMIT", DEFAULT_MAX_ROW_LIMIT))
    except Exception:  # pragma: no cover - settings unconfigured or misconfigured
        return DEFAULT_MAX_ROW_LIMIT


def _iter_nodes(node: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    """Walk every node of a JSON-ish structure, yielding (json_path, value)."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_nodes(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_nodes(value, f"{path}[{index}]")


def _leaf_key(path: str) -> str:
    """The owning key of a JSON path, with any list index stripped.

    `$.params.adhoc_filters[0].value` -> `value`; `$.params.metrics[1]` ->
    `metrics`. Used to decide whether a string is free text or a query value.
    """
    segment = path.rsplit(".", 1)[-1]
    return segment.split("[", 1)[0]


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _owner_identifiers(owners: list[Any] | None) -> set[str] | None:
    """Every string an owner can be recognised by, casefolded.

    **Superset's owner objects carry no `username`.** Verified against 6.1.0:
    `GET /api/v1/dashboard/{id}` returns
    `[{"first_name": "Life", "id": 1, "last_name": "Admin"}]`, and the chart
    serialiser adds `email` but still no username. This function used to read
    `owner["username"]` and therefore returned an empty set for every real
    dashboard — so the risk rule saw "you are not an owner" every time, and
    silently classified every dashboard action as needing confirmation.

    That is the benign half of the same defect: `superset_write.py` matched the
    same way and REFUSED outright, which is how it was found.

    The comparison is stringly-typed because `ValidationContext.dashboard_owners`
    is documented as accepting plain usernames too, and callers (including the
    tests) pass those. The id is stringified so a numeric owner id can match a
    numeric identity.
    """
    if owners is None:
        return None
    identifiers: set[str] = set()
    for owner in owners:
        if isinstance(owner, str):
            if owner.strip():
                identifiers.add(owner.strip().casefold())
        elif isinstance(owner, dict):
            for key in ("username", "email", "id"):
                value = str(owner.get(key) or "").strip()
                if value:
                    identifiers.add(value.casefold())
    return identifiers


def _params(payload: dict[str, Any]) -> dict[str, Any] | None:
    params = payload.get("params")
    return params if isinstance(params, dict) else None


def _quote_list(values: Any) -> str:
    return ", ".join(sorted(str(value) for value in values))


# --- Rules -----------------------------------------------------------------
#
# Each rule takes the mutable check state and returns (errors, verdict). An
# empty error list means the rule held; the verdict is recorded either way so
# `reasons` explains a pass as well as a failure.


@dataclass
class _Check:
    """Working state threaded through the rules of one validation."""

    action_type: str
    payload: dict[str, Any]
    context: ValidationContext
    warnings: list[str] = field(default_factory=list)


def _rule_schema(check: _Check) -> tuple[list[str], str]:
    """Rule 1 — strict jsonschema for the action type (spec §6.1)."""
    # `_validator_for` is `lru_cache`d, so an unhashable `action_type` (a dict or
    # list arriving from a malformed tool call) raised TypeError out of the cache
    # lookup before any rule ran — an exception, not a verdict, which a caller
    # could misread as "nothing to refuse". Reject it as a normal schema failure.
    if not isinstance(check.action_type, str):
        return (
            [
                f"action_type must be a string, not {type(check.action_type).__name__}. "
                f"Permitted actions: {_quote_list(ALLOWED_ACTION_TYPES)}."
            ],
            f"fail — action_type is a {type(check.action_type).__name__}, not a string",
        )

    try:
        validator = _validator_for(check.action_type)
    except UnknownActionTypeError as exc:
        return [str(exc)], f"fail — unknown action_type {check.action_type!r}"

    if not isinstance(check.payload, dict):
        return (
            [f"The payload for {check.action_type} must be a JSON object."],
            "fail — payload is not an object",
        )

    error = best_match(validator.iter_errors(check.payload))
    if error is None:
        return [], f"{_PASS} — matches the {check.action_type} schema"

    # `oneOf` reports the useless "is not valid under any of the given schemas"
    # at the top level, so `best_match` is used to descend to the branch that got
    # furthest, and the permitted operations are spelled out for the model.
    message = (
        f"Payload does not match the {check.action_type} schema at "
        f"{_json_path(error.absolute_path)}: {error.message}."
    )
    operations = _schema_operations(check.action_type)
    if operations:
        message += f" Permitted operations for {check.action_type}: {_quote_list(operations)}."
    return [message], f"fail — schema violation at {_json_path(error.absolute_path)}"


def _schema_operations(action_type: str) -> list[str]:
    """The `operation` consts of a discriminated-union action schema, if any."""
    schema = get_schema(action_type)
    operations: list[str] = []
    for branch in schema.get("oneOf", []):
        const = branch.get("properties", {}).get("operation", {}).get("const")
        if const:
            operations.append(const)
    return operations


def _rule_no_adhoc_sql(check: _Check) -> tuple[list[str], str]:
    """Rule 3 — no adhoc SQL anywhere in the payload (spec §6.3).

    Three distinct checks: SQL-bearing keys, `query_mode`, and a heuristic scan
    of every nested string. See the module section comment above for why the
    scan is defence in depth rather than the primary control.

    Note for tests: the first two checks are *unreachable* through
    `validate_action`, because rule 1's `additionalProperties: false` rejects
    `sqlExpression` and `query_mode` before this rule runs. Exercise them by
    calling this function with a `_Check` directly — that they cannot currently
    fire is the point, not a reason to delete them.
    """
    errors: list[str] = []

    for path, node in _iter_nodes(check.payload):
        if not isinstance(node, dict):
            continue
        for key in node:
            lowered = key.lower()
            if lowered in _SQL_BEARING_KEYS:
                errors.append(
                    f"Raw SQL is not permitted: {path}.{key} carries a SQL expression. "
                    "Express metrics as a saved-metric name or "
                    "{aggregate, column: {column_name}}, and filters as "
                    "{column, operator, value}."
                )
    if errors:
        return errors, "fail — SQL-bearing key present"

    for path, node in _iter_nodes(check.payload):
        if not isinstance(node, str):
            continue
        if (found := _CONTROL_CHARACTERS.search(node)) is not None:
            # A NUL cannot be stored in a Postgres `text` column at all, so this
            # would otherwise surface as an opaque Superset 500 long after the
            # validator said yes. The other C0/C1 codes are log- and
            # terminal-injection vectors, and no legitimate chart name needs one.
            # Tab, newline and carriage return stay legal — descriptions use them.
            return (
                [
                    f"The value at {path} contains a control character "
                    f"(U+{ord(found.group()):04X}), which is not permitted. Use plain "
                    "text for names, labels and descriptions."
                ],
                f"fail — control character U+{ord(found.group()):04X} at {path}",
            )
        is_free_text = _leaf_key(path) in _FREE_TEXT_KEYS
        patterns = _SQL_PATTERNS_ANYWHERE
        if not is_free_text:
            patterns = _SQL_PATTERNS_ANYWHERE + _SQL_PATTERNS_QUERY_VALUES
        for pattern, description in patterns:
            if pattern.search(node):
                return (
                    [
                        f"The value at {path} looks like injected SQL — it contains "
                        f"{description}. This copilot cannot write SQL. Use column and "
                        "metric names from the dataset, and express conditions as "
                        "{column, operator, value} filters."
                    ],
                    f"fail — {description} at {path}",
                )

    return [], f"{_PASS} — no SQL constructs found"


def _rule_viz_coherence(check: _Check) -> tuple[list[str], str]:
    """Per-viz requirements the JSON Schema deliberately does not encode.

    Not one of the six numbered rules in spec §6; it runs after them and before
    risk classification. Encoding "big_number_total takes exactly one metric" as
    schema branches would make the schemas change every time the allowlist does,
    so it lives here — where the refusal can also say what to send instead.
    """
    params = _params(check.payload)
    if params is None:
        return [], f"{_NOT_APPLICABLE} — {check.action_type} carries no chart params"

    viz_type = params.get("viz_type")
    metrics = params.get("metrics") or []
    groupby = params.get("groupby") or []
    errors: list[str] = []

    if viz_type == "big_number_total":
        if len(metrics) != 1:
            errors.append(
                "big_number_total shows a single figure, so params.metrics must contain "
                f"exactly one metric ({len(metrics)} supplied). Use a bar or table chart "
                "to compare several metrics."
            )
        if groupby:
            errors.append(
                "big_number_total cannot be grouped; drop params.groupby, or use "
                "echarts_timeseries_bar or bar to break the figure down."
            )

    if errors:
        return errors, f"fail — params incompatible with {viz_type}"
    return [], f"{_PASS} — params compatible with {viz_type}"


def _rule_row_limit(check: _Check) -> tuple[list[str], str]:
    """Clamp `params.row_limit` to the POC maximum (spec §17 / settings).

    Clamps rather than rejects: an over-large limit is an honest mistake about
    scale, not an attempt at anything, and refusing the whole chart over it would
    cost the user a turn for no safety gain. The clamp is reported so neither the
    user nor the model believes they got the limit they asked for.
    """
    params = _params(check.payload)
    if params is None or "row_limit" not in params:
        return [], f"{_NOT_APPLICABLE} — no row_limit requested"

    requested = params.get("row_limit")
    maximum = _max_row_limit()
    if not isinstance(requested, int) or requested <= maximum:
        return [], f"{_PASS} — row_limit {requested} within the maximum {maximum}"

    params["row_limit"] = maximum
    check.warnings.append(
        f"row_limit {requested} exceeds the maximum of {maximum} rows and was clamped "
        f"to {maximum}."
    )
    return [], f"{_PASS} — row_limit clamped from {requested} to {maximum}"


# --- Risk classification (rule 6) ------------------------------------------


def _components_touched(check: _Check) -> int | None:
    """How many dashboard components the action changes, or None if unknowable."""
    action_type = check.action_type
    payload = check.payload

    if action_type == "create_dashboard":
        return len(payload.get("chart_ids") or [])

    operation = payload.get("operation")
    if operation == "kpi_row":
        return len(payload.get("chart_ids") or [])
    if operation == "equalize_row":
        # The row's membership is not in the payload, so the dashboard's own
        # component count is the only available bound — unknown means unknown.
        return check.context.dashboard_component_count

    return 1


def _classify_risk(check: _Check) -> tuple[str, str]:
    """Rule 6 — `low` (execute immediately) or `preview` (confirm first), §6.6."""
    action_type = check.action_type
    payload = check.payload

    if action_type in _ALWAYS_PREVIEW:
        return RISK_PREVIEW, "preview — removal is destructive"

    dashboard_id = payload.get("dashboard_id")
    if dashboard_id is not None and action_type not in _OWNERSHIP_EXEMPT:
        owners = _owner_identifiers(check.context.dashboard_owners)
        if owners is None:
            return (
                RISK_PREVIEW,
                f"preview — ownership of dashboard {dashboard_id} unknown (failed closed)",
            )
        username = check.context.identity_username
        if not isinstance(username, str) or not username.strip():
            return (
                RISK_PREVIEW,
                f"preview — the acting identity is unknown, so ownership of dashboard "
                f"{dashboard_id} cannot be established (failed closed)",
            )
        # The user id as well as the name, because Superset's owner objects
        # carry an id and no username — see `_owner_identifiers`.
        mine = {username.strip().casefold()}
        if check.context.identity_user_id is not None:
            mine.add(str(check.context.identity_user_id).casefold())
        if not (mine & owners):
            return (
                RISK_PREVIEW,
                f"preview — {username} does not own dashboard {dashboard_id}",
            )

    touched = _components_touched(check)
    if touched is None:
        return RISK_PREVIEW, "preview — number of components affected is unknown (failed closed)"
    if touched > MAX_LOW_RISK_COMPONENTS:
        return RISK_PREVIEW, f"preview — touches {touched} components (limit {MAX_LOW_RISK_COMPONENTS})"

    operation = payload.get("operation")
    if operation in _LAYOUT_REBUILD_OPERATIONS:
        return RISK_PREVIEW, f"preview — {operation} restructures a row rather than one component"

    plural = "component" if touched == 1 else "components"
    return RISK_LOW, f"low — small change ({touched} {plural})"


# --- Entry point -----------------------------------------------------------

_RULES: Final[tuple[tuple[str, Any], ...]] = (
    ("schema", _rule_schema),
    ("no_adhoc_sql", _rule_no_adhoc_sql),
    ("viz_coherence", _rule_viz_coherence),
    ("row_limit", _rule_row_limit),
)


def validate_action(
    action_type: str,
    payload: dict[str, Any],
    *,
    context: ValidationContext,
) -> ValidationResult:
    """Validate one proposed write action and classify its risk (spec §6).

    Rules run in the order spec §6 fixes and the first failure short-circuits, so
    `errors` normally holds the reasons from exactly one rule. `reasons` records
    every rule's verdict — including "not evaluated" for the rules after a
    failure — which is what makes the audit row explain itself later.

    The payload is never mutated: normalisation (row-limit clamping) happens on a
    deep copy exposed as `normalised_payload`. Execute that copy, not the input.

    **This function never raises.** An exception escaping a security gate is
    worse than a rejection: the caller sees no verdict and may treat that as
    nothing to refuse, and the shapes that caused it were reachable from a
    model-authored payload (a several-thousand-deep object exhausted the stack
    inside `copy.deepcopy`, before rule 1 could reject it). Anything unexpected
    is logged and becomes a refusal.
    """
    try:
        return _validate_action(action_type, payload, context=context)
    except Exception as exc:  # noqa: BLE001 - deliberate fail-closed boundary
        _log.exception(
            "validator raised on action_type=%r; failing closed", action_type
        )
        reasons = {rule: _NOT_EVALUATED for rule in RULE_ORDER}
        reasons["schema"] = f"fail — validator raised {type(exc).__name__} (failed closed)"
        return ValidationResult(
            ok=False,
            risk=None,
            errors=[
                "This action could not be validated because it is malformed beyond "
                f"inspection ({type(exc).__name__}), so it is refused. Send a simpler, "
                "well-formed payload for this action type."
            ],
            warnings=[],
            normalised_payload=None,
            reasons=reasons,
        )


def _validate_action(
    action_type: str,
    payload: dict[str, Any],
    *,
    context: ValidationContext,
) -> ValidationResult:
    """The real validation pass. Only `validate_action` should call this."""
    check = _Check(
        action_type=action_type,
        payload=copy.deepcopy(payload) if isinstance(payload, dict) else payload,
        context=context,
    )
    reasons: dict[str, str] = {rule: _NOT_EVALUATED for rule in RULE_ORDER}

    for rule_name, rule in _RULES:
        errors, verdict = rule(check)
        reasons[rule_name] = verdict
        if errors:
            return ValidationResult(
                ok=False,
                risk=None,
                errors=errors,
                warnings=list(check.warnings),
                normalised_payload=None,
                reasons=reasons,
            )

    risk, risk_reason = _classify_risk(check)
    reasons["risk"] = risk_reason
    return ValidationResult(
        ok=True,
        risk=risk,
        errors=[],
        warnings=list(check.warnings),
        normalised_payload=check.payload,
        reasons=reasons,
    )
