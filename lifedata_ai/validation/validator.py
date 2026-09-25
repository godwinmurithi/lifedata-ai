"""Policy validation for every write action the copilot proposes (spec §6).

This module is the gate between "the model said so" and "Superset was changed".
Spec §6 fixes both the rules and their order; the order matters because each
rule assumes the previous one has already held. Schema first, because no policy
rule can reason about a payload of unknown shape. Viz allowlist next, because
the viz type decides which other params are even meaningful. Then the SQL ban,
the geography policy, the column allowlist, and finally risk classification —
which is not a rejection but a routing decision (execute now vs. show the user a
preview card first).

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

from lifedata_ai.semantics.loader import Dataset, Registry, load_registry
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
RULE_ORDER: Final[tuple[str, ...]] = (
    "schema",
    "viz_allowlist",
    "no_adhoc_sql",
    "geography",
    "column_roles",
    "color_scheme",
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
    registry: Registry = field(default_factory=load_registry)


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


# --- Rule 4: geography policy ---------------------------------------------

#: The only columns a `geography: patient` dataset may be grouped by (spec §6.4:
#: "reject group-bys below district"). Region is coarser than district, so it is
#: permitted too. Anything else is refused rather than inspected, because on a
#: patient-grain dataset an unrecognised grouping column is exactly the case
#: where guessing wrong discloses something.
_PATIENT_GEOGRAPHY_GROUPBY_ALLOWED: Final[frozenset[str]] = frozenset(
    {"district_name", "district", "region", "region_name"}
)

#: Name fragments that mark a column as finer than district, or as a coordinate.
#: Matched against `_` / non-alphanumeric separated tokens with a prefix/suffix
#: test — substring matching would flag "population" for containing "lat".
#:
#: This list is a DENYLIST and therefore fails open on vocabulary nobody thought
#: of: an early version knew "subcounty" but not "sub_county" (two tokens), and
#: knew Ugandan units but not Rwandan ones ("sector", "umudugudu"), so those
#: columns sailed through. Grouping columns no longer depend on this list at all
#: — see `_is_permitted_patient_axis`, which is an allowlist — but filter columns
#: still do, so keep the vocabulary generous. When in doubt add the word: the
#: cost of a false positive is one refusal message naming district_name.
_FINER_THAN_DISTRICT_TOKENS: Final[tuple[str, ...]] = (
    # sub-district administrative units, several countries' vocabularies
    "sub",  # sub_county, sub_district, sub_ward — the two-token spellings
    "subcounty",
    "subdistrict",
    "parish",
    "village",
    "ward",
    "cell",
    "sector",
    "zone",
    "locality",
    "settlement",
    "hamlet",
    "neighbourhood",
    "neighborhood",
    "suburb",
    "commune",
    "kebele",
    "barangay",
    "umudugudu",
    "lc1",
    "estate",
    "camp",
    "catchment",
    # premises and individuals
    "facility",
    "patient",
    "household",
    "home",
    "house",
    "dwelling",
    "compound",
    "resid",  # residence, resident, residential
    "street",
    "road",
    "plot",
    "site",
    "address",
    "postcode",
    # coordinates
    "gps",
    "lat",
    "lon",
    "lng",
    "point",
    "geom",
    "coord",
)

#: Column names that read as a date or period. Used to keep time axes and date
#: filters legal on a patient-geography dataset: "district counts by month" is
#: not a disclosure question, and refusing it would make the dataset useless.
#: Matched on whole tokens only — a prefix test would make `last_update`
#: temporal because "update" ends with "date", and a permissive test on this
#: list widens what is allowed, so it must not guess.
_TEMPORAL_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "date",
        "dates",
        "datetime",
        "time",
        "timestamp",
        "ts",
        "dt",
        "epoch",
        "hour",
        "minute",
        "day",
        "daily",
        "week",
        "weekly",
        "month",
        "monthly",
        "quarter",
        "year",
        "yearly",
        "period",
    }
)

#: Tokens naming a human subject, and tokens naming an identifier. A column is
#: treated as a person identifier when it carries one of each — so `patient_id`
#: and `client_uuid` are refused while `patient_count` and `facility_name` are
#: untouched. See `_is_person_identifier` for why this check exists at all.
_PERSON_ENTITY_TOKENS: Final[frozenset[str]] = frozenset(
    {"patient", "person", "client", "individual", "subject", "beneficiary", "member"}
)

_IDENTIFIER_TOKENS: Final[frozenset[str]] = frozenset(
    {"id", "ids", "uuid", "guid", "identifier", "number", "no", "code", "name", "hash"}
)

#: Tokens that mark a column as an aggregate measure rather than a place. They
#: exempt a filter column from the sub-district denylist, because
#: `patient_count` — the only measure `rpt_patient_geography` has — otherwise
#: trips the "patient" marker, making "districts with at least fifty patients"
#: unaskable. Coordinates below are exempt from the exemption.
_MEASURE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "count",
        "counts",
        "total",
        "totals",
        "sum",
        "avg",
        "average",
        "mean",
        "median",
        "rate",
        "ratio",
        "percent",
        "percentage",
        "pct",
        "num",
        "volume",
    }
)

#: Coordinate markers. Never exempted by `_MEASURE_TOKENS`: an "average
#: latitude" is still a location.
_COORDINATE_TOKENS: Final[tuple[str, ...]] = (
    "gps",
    "lat",
    "lon",
    "lng",
    "point",
    "geom",
    "coord",
)

#: Identifiers that need no entity token because they name a person by
#: themselves.
_STANDALONE_PERSON_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {"mrn", "nid", "nin", "national_id", "nhis_number", "phone", "phone_number", "msisdn", "email"}
)

#: Viz-name tokens that mean "renders individual points". Kept separate from the
#: registry's explicit `patient_geography_forbidden_viz` list so a newly added
#: point-map type is refused on patient data from the moment it appears, rather
#: than from the moment someone remembers to add it to the list.
_POINT_RENDERING_VIZ_TOKENS: Final[frozenset[str]] = frozenset(
    {"scatter", "point", "heat", "heatmap", "hex", "geojson", "path", "mapbox", "cluster", "marker", "dot"}
)


# --- Rule 6: risk classification ------------------------------------------

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


def _tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", name.lower()) if token]


def _suggests_finer_than_district(column: str) -> bool:
    """Whether a column name reads as sub-district or as a coordinate."""
    for token in _tokens(column):
        for marker in _FINER_THAN_DISTRICT_TOKENS:
            if token == marker or token.startswith(marker) or token.endswith(marker):
                return True
    return False


def _is_temporal(column: str) -> bool:
    """Whether a column name reads as a date, timestamp or reporting period."""
    return bool(_TEMPORAL_TOKENS.intersection(_tokens(column)))


def _is_permitted_patient_axis(column: str) -> bool:
    """Whether `column` may be a chart axis on a `geography: patient` dataset.

    An ALLOWLIST, not a denylist, and that asymmetry is the whole point.
    `groupby` has always been allowlisted; `x_axis` used to be screened with
    `_suggests_finer_than_district` instead, which meant any sub-district word
    missing from that vocabulary (`sub_county`, `sector`, `locality`,
    `umudugudu`) became a legal category axis — a per-sub-district breakdown by
    another name, since on a bar or timeseries chart the axis *is* a grouping.
    An axis is now permitted only if it is a district/region column or a date,
    so an unrecognised column is refused rather than waved through.
    """
    return column.strip().lower() in _PATIENT_GEOGRAPHY_GROUPBY_ALLOWED or _is_temporal(column)


def _is_permitted_patient_filter_column(column: str) -> bool:
    """Whether `column` may be filtered on a `geography: patient` dataset.

    Deliberately weaker than `_is_permitted_patient_axis`: a filter narrows the
    population, it does not set the output grain, and the output stays district
    aggregated with small cells suppressed in the reporting view itself (spec
    §1). Measure filters ("districts with at least 50 patients") therefore stay
    available, at the price of failing open on a place word this module has
    never heard of. Add unknown place words to `_FINER_THAN_DISTRICT_TOKENS`.
    """
    if _is_temporal(column):
        return True
    tokens = set(_tokens(column))
    if any(
        token == marker or token.startswith(marker) or token.endswith(marker)
        for token in tokens
        for marker in _COORDINATE_TOKENS
    ):
        return False
    if tokens & _MEASURE_TOKENS:
        return True
    return not _suggests_finer_than_district(column)


def _is_person_identifier(column: str) -> bool:
    """Whether a column identifies an individual human being.

    Not a geography question, and not one of spec §6's six numbered rules —
    but grouping *any* dataset by `patient_id` turns an aggregate chart into a
    row-per-patient listing saved on a shared dashboard, which is the disclosure
    spec §1's privacy boundary exists to prevent. The registry's `geography`
    flag does not catch it, because a dataset can be `geography: facility` and
    still carry a patient key (`rpt_encounters` does).

    Requires an entity token *and* an identifier token so that `patient_count`,
    `unique_patients` and `facility_name` are untouched. Delete the two calls in
    `_rule_geography` to remove this control; nothing else depends on it.
    """
    normalised = column.strip().lower()
    if normalised in _STANDALONE_PERSON_IDENTIFIERS:
        return True
    tokens = set(_tokens(column))
    return bool(tokens & _PERSON_ENTITY_TOKENS) and bool(tokens & _IDENTIFIER_TOKENS)


def _renders_points(viz_type: str) -> bool:
    return bool(_POINT_RENDERING_VIZ_TOKENS.intersection(_tokens(viz_type)))


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
    #: Resolved by the geography rule and reused by risk classification.
    dataset: Dataset | None = None


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


def _rule_viz_allowlist(check: _Check) -> tuple[list[str], str]:
    """Rule 2 — the viz type must be on the registry allowlist (spec §6.2)."""
    params = _params(check.payload)
    if params is None:
        return [], f"{_NOT_APPLICABLE} — {check.action_type} carries no chart params"

    viz_type = params.get("viz_type")
    if not isinstance(viz_type, str):
        return [], f"{_NOT_APPLICABLE} — no viz_type declared"

    registry = check.context.registry
    if registry.is_viz_allowed(viz_type):
        return [], f"{_PASS} — viz_type {viz_type}"

    message = (
        f"Viz type {viz_type!r} is not approved for this deployment. "
        f"Permitted viz types: {_quote_list(registry.viz_allowlist)}."
    )
    # The geography policy is stated here as well as in rule 4, because rule 2
    # runs first and would otherwise refuse a point map with a bland "not on the
    # list" — hiding the actual reason (spec §6.4, Phase 4 acceptance criterion).
    if viz_type in registry.patient_geography_forbidden_viz or _renders_points(viz_type):
        message += (
            " Point, scatter, cluster and heat maps are never permitted: they plot "
            "individual locations, and on patient data the policy is district-level "
            "aggregation only. Use echarts_timeseries_bar or table to rank districts — "
            "this deployment has no map viz type, because rpt_patient_geography carries "
            "district centroids (points) and a choropleth needs polygon boundaries."
        )
    return [message], f"fail — viz_type {viz_type} not on the allowlist"


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
        for key, value in node.items():
            lowered = key.lower()
            if lowered in _SQL_BEARING_KEYS:
                errors.append(
                    f"Raw SQL is not permitted: {path}.{key} carries a SQL expression. "
                    "Express metrics as a saved-metric name or "
                    "{aggregate, column: {column_name}}, and filters as "
                    "{column, operator, value}."
                )
            elif lowered == "query_mode" and str(value).lower() != "aggregate":
                errors.append(
                    f"query_mode must be 'aggregate'; {value!r} was requested at {path}. "
                    "Raw-records mode would return row-level data and is never permitted."
                )
    if errors:
        return errors, "fail — SQL-bearing key or non-aggregate query_mode present"

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


def _rule_geography(check: _Check) -> tuple[list[str], str]:
    """Rule 4 — the patient-geography policy (spec §6.4).

    A `geography: patient` dataset may be grouped no finer than district and may
    never be drawn as points. `geography: facility` datasets are institutions,
    not people, so exact coordinates are fine.

    One control here goes beyond spec §6.4: `_person_identifier_errors` refuses a
    grouping by a patient key on *every* dataset, because the registry's
    geography flag does not cover a facility-geography dataset that happens to
    carry `patient_id`. See that function for how to back it out.

    FAIL CLOSED on an unresolvable dataset. An action whose dataset cannot be
    identified is treated exactly as if it were patient geography, because the
    alternative — assuming a benign dataset — means the one case where the
    lookup is broken or the id is unknown is also the case where the policy is
    silently skipped. The refusal names the fix (`params.datasource`), so the
    model can correct it in one turn.
    """
    registry = check.context.registry
    params = _params(check.payload)

    if params is None:
        return _geography_for_filter(check)

    declared_id = check.payload.get("dataset_id")
    datasource = params.get("datasource")
    datasource_id = datasource.get("id") if isinstance(datasource, dict) else None

    # Cross-field check: the policy is evaluated against one dataset, so the
    # payload must not name two (a chart pointed at a dataset the validator never
    # inspected is precisely the hole this closes).
    if declared_id is not None and datasource_id is not None and declared_id != datasource_id:
        return (
            [
                f"params.datasource.id ({datasource_id}) does not match dataset_id "
                f"({declared_id}). Both must name the same dataset."
            ],
            "fail — dataset_id and params.datasource.id disagree",
        )

    dataset_id = declared_id if declared_id is not None else datasource_id
    if dataset_id is None:
        return (
            [
                "The dataset for this chart could not be identified, so the geography "
                "policy cannot be applied and the action is refused. Include "
                'params.datasource as {"id": <dataset id>, "type": "table"} — call '
                "get_chart to find the dataset an existing chart uses."
            ],
            "fail — dataset unresolved (failed closed)",
        )

    dataset = registry.dataset_by_superset_id(dataset_id)
    check.dataset = dataset
    if dataset is None:
        return (
            [
                f"Dataset id {dataset_id} is not in the LifeData semantics registry, so "
                "its privacy classification is unknown and the action is refused. Use "
                "recommend_dataset or list_datasets to choose a registered dataset."
            ],
            f"fail — dataset {dataset_id} unregistered (failed closed)",
        )

    # Row-level grain is refused on every dataset, not only patient-geography
    # ones: `rpt_encounters` is `geography: facility` and still carries
    # `patient_id`, so the geography flag alone would let a per-patient table
    # onto a shared dashboard.
    identifier_errors = _person_identifier_errors(params, dataset)
    if identifier_errors:
        return identifier_errors, f"fail — row-level grouping on {dataset.name}"

    if not dataset.is_patient_geography:
        return (
            [],
            f"{_NOT_APPLICABLE} — {dataset.name} has geography={dataset.geography!r}",
        )

    errors: list[str] = []
    viz_type = params.get("viz_type")
    if isinstance(viz_type, str) and (
        viz_type in registry.patient_geography_forbidden_viz or _renders_points(viz_type)
    ):
        errors.append(
            f"{viz_type!r} plots individual points and {dataset.name} holds patient "
            "geography, which may only be shown aggregated to district. Use "
            "echarts_timeseries_bar or table for a district ranking; this deployment "
            "has no map viz type."
        )

    for column in params.get("groupby") or []:
        if str(column).lower() not in _PATIENT_GEOGRAPHY_GROUPBY_ALLOWED:
            errors.append(
                f"{column!r} may not be used to group {dataset.name}: patient geography "
                "is aggregated to district, so the only permitted grouping columns are "
                f"{_quote_list(_PATIENT_GEOGRAPHY_GROUPBY_ALLOWED)}."
            )

    # On a bar or timeseries chart the x axis IS a grouping, so it is allowlisted
    # exactly like `groupby` — plus dates, because "district counts by month" is
    # not a privacy question. Screening it with the sub-district word list
    # instead was the hole that let `x_axis: "sub_county"` through.
    x_axis = params.get("x_axis")
    if isinstance(x_axis, str) and not _is_permitted_patient_axis(x_axis):
        errors.append(
            f"{x_axis!r} cannot be the axis of a chart on {dataset.name}: patient "
            "geography is aggregated to district, so the axis must be one of "
            f"{_quote_list(_PATIENT_GEOGRAPHY_GROUPBY_ALLOWED)} or a date/period column."
        )

    for index, filter_spec in enumerate(params.get("adhoc_filters") or []):
        column = filter_spec.get("column") if isinstance(filter_spec, dict) else None
        if isinstance(column, str) and not _is_permitted_patient_filter_column(column):
            errors.append(
                f"adhoc_filters[{index}] filters {dataset.name} on {column!r}, which is "
                "finer than district. Filter on district_name or region instead."
            )

    if errors:
        return errors, f"fail — patient-geography policy on {dataset.name}"

    if dataset.small_cell_threshold:
        check.warnings.append(
            f"{dataset.name} suppresses districts with fewer than "
            f"{dataset.small_cell_threshold} patients in the view itself; describe gaps "
            "as suppressed small counts, not as absence of patients."
        )
    return [], f"{_PASS} — district-level use of {dataset.name}"


def _person_identifier_errors(params: dict[str, Any], dataset: Dataset) -> list[str]:
    """Refuse groupings that would make one chart row equal one person.

    Only `groupby` and `x_axis` are inspected: those set the output grain. A
    *metric* over a patient key is the opposite — `COUNT_DISTINCT(patient_id)`
    is the governed way to count people (see the `unique_patients` indicator) —
    and a *filter* on one narrows the population without listing it.
    """
    errors: list[str] = []
    candidates: list[str] = [str(column) for column in params.get("groupby") or []]
    x_axis = params.get("x_axis")
    if isinstance(x_axis, str):
        candidates.append(x_axis)

    for column in candidates:
        if _is_person_identifier(column):
            errors.append(
                f"{column!r} identifies individual people, so grouping {dataset.name} by "
                "it would put one person per row on a saved chart. Aggregate instead: "
                "count people with a COUNT_DISTINCT metric over that column and group by "
                "facility, district or period."
            )
    return errors


def _geography_for_filter(check: _Check) -> tuple[list[str], str]:
    """Geography policy for actions with no chart params.

    Only `add_native_filter` can name a dataset and column outside `params`. The
    fail-closed rule does not extend to actions that carry no dataset reference
    at all (renames, layout moves, theme changes): they cannot express a
    grouping or a viz type, so there is no geography decision to fail closed on.
    """
    if check.action_type != "add_native_filter":
        return [], f"{_NOT_APPLICABLE} — {check.action_type} expresses no query"

    column = check.payload.get("column")
    filter_type = check.payload.get("filter_type")
    needs_dataset = filter_type in {"value", "numerical_range"} or column is not None

    dataset_id = check.payload.get("dataset_id")
    if dataset_id is None:
        # FAIL CLOSED. `dataset_id` is optional in the schema because a
        # `time_range` filter needs none, and that optionality was a hole: a
        # filter naming `column: "village_name"` and no dataset skipped the
        # geography policy entirely, because there was no dataset to classify.
        if needs_dataset:
            return (
                [
                    f"A {filter_type!r} filter on {column!r} must name the dataset it "
                    "filters, because the privacy policy that applies depends on it. "
                    "Include dataset_id — use list_datasets or get_dashboard to find it."
                ],
                "fail — filter names a column but no dataset (failed closed)",
            )
        return [], f"{_NOT_APPLICABLE} — filter names no dataset"

    dataset = check.context.registry.dataset_by_superset_id(dataset_id)
    check.dataset = dataset
    if dataset is None:
        return (
            [
                f"Dataset id {dataset_id} is not in the LifeData semantics registry, so "
                "its privacy classification is unknown and the filter is refused."
            ],
            f"fail — dataset {dataset_id} unregistered (failed closed)",
        )
    if not dataset.is_patient_geography:
        return [], f"{_NOT_APPLICABLE} — {dataset.name} is not patient geography"

    if isinstance(column, str) and not _is_permitted_patient_filter_column(column):
        return (
            [
                f"A filter on {column!r} would expose {dataset.name} below district "
                "level. Filter on district_name or region instead."
            ],
            f"fail — sub-district filter column on {dataset.name}",
        )
    return [], f"{_PASS} — district-level filter on {dataset.name}"


def _rule_column_roles(check: _Check) -> tuple[list[str], str]:
    """Only declared dimension or temporal columns may set a chart's grain.

    An ALLOWLIST, deliberately, and the reasoning is worth keeping. The obvious
    alternative is to forbid `role: key` — refuse `facility_id`, allow the rest.
    Three reasons this is better:

    * It is one rule, not a list of forbidden roles that grows every time the
      registry learns a new one.
    * It matches what the catalogue in the prompt actually claims: *"complete;
      nothing else exists"*. A denylist contradicts that header.
    * It closes the case a denylist leaves open. A column the registry has never
      heard of — a new column on a reporting view, a dataset added without
      curation — would silently become groupable under a denylist. Here it is
      refused, which is the same fail-closed posture the geography rule takes.

    The refusal names the alternatives, so the model corrects in one turn rather
    than guessing: *"facility_id is an internal key; group by facility_name"*.

    Filters are NOT policed here. A filter narrows the population without setting
    the output grain, and refusing a filter on `turnaround_hours` would make
    "orders that took over 48 hours" unaskable for no privacy gain. The
    patient-geography rule above still polices filter columns on the one dataset
    where they matter.
    """
    params = _params(check.payload)
    if params is None:
        return [], f"{_NOT_APPLICABLE} — {check.action_type} carries no chart params"
    dataset = check.dataset
    if dataset is None:
        # Rule 4 fails closed on an unresolvable dataset, so reaching here with
        # none means the action legitimately names no dataset at all.
        return [], f"{_NOT_APPLICABLE} — no dataset resolved for {check.action_type}"
    if not dataset.columns:
        # A dataset in the registry that declares no columns yet. Refusing every
        # grouping on it would break it entirely; the geography and
        # person-identifier rules still apply.
        return (
            [],
            f"{_NOT_APPLICABLE} — {dataset.name} declares no columns in the registry",
        )

    candidates: list[tuple[str, str]] = [
        ("groupby", str(column)) for column in params.get("groupby") or []
    ]
    x_axis = params.get("x_axis")
    if isinstance(x_axis, str):
        # On a bar or timeseries chart the axis IS a grouping, so it goes
        # through the same gate rather than a looser one.
        candidates.append(("x_axis", x_axis))
    # A choropleth's `entity` is NOT checked here, because the model cannot set
    # it: `entity` is absent from CHART_PARAMS_SCHEMA. A map is requested the
    # same way as a bar chart — `groupby: [district_name]` — and the write path
    # translates that to the boundary code Superset joins on. So the gate below
    # is the only gate, and `district_iso` is reachable only through it.

    errors: list[str] = []
    for where, name in candidates:
        column = dataset.column(name)
        if column is not None and column.is_groupable:
            continue
        errors.append(_column_role_message(where, name, column, dataset))

    errors.extend(_metric_errors(params, dataset))

    if errors:
        return errors, f"fail — ungroupable column or unknown metric on {dataset.name}"
    checked = ", ".join(name for _where, name in candidates) or "nothing to check"
    return [], f"{_PASS} — {checked}"


#: Metrics Superset creates on every dataset without anyone declaring them, so a
#: chart may legitimately name them even though the registry does not.
_SUPERSET_BUILTIN_METRICS: Final[frozenset[str]] = frozenset({"count"})


def _metric_errors(params: dict[str, Any], dataset: Dataset) -> list[str]:
    """Refuse a saved-metric name that does not exist on this dataset.

    A bare string in `params.metrics` is a *saved metric reference*, and Superset
    fails an unknown one at query time — the chart saves happily and then renders
    a red Data error on the dashboard, which the executive discovers and the
    copilot never learns about. Refusing here turns that into a tool error the
    model corrects in the same turn.

    Aggregation objects are untouched: they name a column, and the column rules
    above already govern what may be charted.
    """
    if not dataset.metrics:
        return []

    available = ", ".join(f"{m.name} ({m.display_label})" for m in dataset.metrics)
    errors: list[str] = []
    for metric in params.get("metrics") or []:
        if not isinstance(metric, str) or metric in _SUPERSET_BUILTIN_METRICS:
            continue
        if dataset.metric(metric) is not None:
            continue
        errors.append(
            f"{metric!r} is not a saved metric on {dataset.name}. Use one of: "
            f"{available}; or express the figure as "
            "{aggregate, column: {column_name}}."
        )
    return errors


def _column_role_message(where: str, name: str, column: Any, dataset: Dataset) -> str:
    """A refusal that names the fix, because the model reads this and retries."""
    alternatives = ", ".join(
        f"{c.name} ({c.display_label})" for c in dataset.dimensions()[:6]
    ) or "none are declared"

    if column is None:
        return (
            f"{name!r} is not a column of {dataset.name}, so it cannot be used as "
            f"params.{where}. The catalogue in your instructions lists every column "
            f"that exists. Break this dataset down by: {alternatives}."
        )
    if column.role == "key":
        return (
            f"{name!r} is an internal key on {dataset.name}, so grouping by it would "
            f"put one row per record on a saved chart. Group by "
            f"{dataset.default_dimension or (dataset.dimensions()[0].name if dataset.dimensions() else 'a dimension')} "
            f"instead. Available: {alternatives}."
        )
    if column.role == "measure_source":
        return (
            f"{name!r} is a number to aggregate on {dataset.name}, not a category to "
            f"break down by. Put it in params.metrics as an aggregation, and set "
            f"params.{where} to one of: {alternatives}."
        )
    if column.role == "internal":
        note = f" {column.note}" if column.note else ""
        return (
            f"{name!r} is an internal helper column on {dataset.name} and is not "
            f"available for charting.{note} Break down by: {alternatives}."
        )
    return (
        f"{name!r} has role {column.role!r} on {dataset.name} and may not set a "
        f"chart's grain. Break down by: {alternatives}."
    )


def _rule_color_scheme(check: _Check) -> tuple[list[str], str]:
    """A chart's palette must be one Superset actually registers.

    Small, and worth keeping after `apply_dashboard_theme` was dropped. An
    unvetted scheme name does not error — Superset silently falls back to its
    default — so the chart renders in the wrong palette and nobody is told. A
    consistent palette across a dashboard is exactly the kind of coherence an
    executive notices without being able to name.

    The schema's own identifier pattern already makes CSS unrepresentable here;
    this rule is about correctness, not injection.
    """
    params = _params(check.payload) or {}
    scheme = params.get("color_scheme")
    if not isinstance(scheme, str):
        return [], f"{_NOT_APPLICABLE} — no colour scheme requested"

    approved = check.context.registry.color_schemes
    if scheme in approved:
        return [], f"{_PASS} — color_scheme {scheme}"
    return (
        [
            f"Colour scheme {scheme!r} is not one this Superset build registers, so the "
            f"chart would silently fall back to the default. Approved values: "
            f"{_quote_list(approved)}. Omit color_scheme to inherit the dashboard's "
            "palette, which is the usual choice."
        ],
        f"fail — unapproved colour scheme {scheme}",
    )


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
    ("viz_allowlist", _rule_viz_allowlist),
    ("no_adhoc_sql", _rule_no_adhoc_sql),
    ("geography", _rule_geography),
    ("column_roles", _rule_column_roles),
    ("color_scheme", _rule_color_scheme),
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
