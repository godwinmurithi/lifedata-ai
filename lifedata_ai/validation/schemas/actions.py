"""Strict JSON Schemas for every write action the copilot may propose (spec §6.1).

Spec §6 makes this the *first* gate: "jsonschema for its action type (strict,
`additionalProperties: false`)". Nothing downstream — not the layout compiler,
not `superset_write.py` — is allowed to see a payload that has not passed the
schema for its `action_type`. The schemas are therefore written to make whole
classes of attack unrepresentable rather than merely detectable:

* **No adhoc SQL is expressible** (spec §6.3). A metric is either a saved-metric
  *name* matching an identifier pattern, or a closed aggregation object with no
  `expressionType` / `sqlExpression` key at all. Because every object here sets
  `additionalProperties: false`, `{"expressionType": "SQL", ...}` fails the
  schema — there is no key it could land in. Same for `adhoc_filters`: they are
  expressed as `{column, operator, value}`, never as Superset's raw clause form.
* **No raw `position_json` is expressible** (spec §6.7). `move_dashboard_component`
  and `resize_dashboard_component` accept *semantic operations only*; the layout
  compiler in `validation/layout.py` alone mutates `position_json`.
* **No arbitrary `params`.** Chart `params` (Superset `form_data`) is modelled as
  the small subset the copilot is permitted to set. Everything else Superset
  understands — `sqlExpression`, `query_mode`, `custom_sql`, `where`, `having`,
  `js_columns`, CSS keys — is rejected by `additionalProperties: false`.

Two allowlists used to be imported from the semantics registry at module import
time (spec §8's single-source argument); the semantics registry has been
removed, so they are plain constants here instead. Adding a viz type or layout
preset is now a code change to this file, not a `registry.yaml` edit.

Every schema is checked with `Draft202012Validator.check_schema` at import time,
so a malformed schema fails at Django startup instead of during a user's turn.

Downstream contract::

    from lifedata_ai.validation.schemas.actions import (
        ACTION_SCHEMAS, ALLOWED_ACTION_TYPES, get_schema, UnknownActionTypeError,
    )
"""

from __future__ import annotations

import copy
from typing import Any, Final

from jsonschema import Draft202012Validator

JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"


# --- Allowlists (NOT currently enforced — see below) ------------------------
#
# Formerly sourced from semantics/registry.yaml (removed). Explicit direction
# (2026-10-01): no restriction on viz type, colour scheme or layout preset for
# now — every Superset chart type (including maps), any colour scheme/value,
# and any layout shape should be available, "to be applied later on". None of
# these three lists are read by the schema or validator.py any more; they are
# kept only as (a) VIZ_ALLOWLIST's existing other use in
# tests_browser/test_viz_renders.py (the set of viz types that test suite has
# a rendering case for — a genuinely different, narrower purpose than "the
# allowlist the validator permits"), and (b) a ready-made starting point if
# limits are reintroduced later — re-wiring them back into the schema/rules
# below is the way to do that, not recreating the lists from scratch.

#: Layout preset ids this deployment used to restrict `create_dashboard` to.
#: Not enforced — see above.
LAYOUT_PRESET_IDS: Final[list[str]] = [
    "executive_overview",
    "facility_infrastructure",
]

#: Chart palettes Superset registers on this deployment. Not enforced — see
#: above; `color_scheme` currently accepts any identifier-shaped string so the
#: model can apply whatever palette a user asks for.
COLOR_SCHEMES: Final[list[str]] = [
    "supersetColors",
    "d3Category10",
    "googleCategory10c",
    "lyftColors",
    "bnbColors",
    "echarts4Colors",
]

#: Viz types this deployment had verified end-to-end (built, rendered via
#: Playwright, looked at — see tests_browser/test_viz_renders.py, which still
#: uses this list for that purpose). Not enforced as a restriction — see
#: above; `viz_type` currently accepts any identifier-shaped string, so an
#: unverified or new Superset viz type is schema-valid and only fails (loudly,
#: at save time) if Superset itself does not recognise it.
VIZ_ALLOWLIST: Final[list[str]] = [
    "big_number_total",
    "echarts_timeseries_line",
    "echarts_timeseries_bar",
    "pie",
    "table",
    "deck_polygon",
    "box_plot",
    "funnel",
    "histogram_v2",
    "treemap_v2",
    "waterfall",
    "sunburst_v2",
]


# --- Primitive building blocks -------------------------------------------

#: Column, metric and dataset identifiers. The pattern is the SQL-injection
#: floor for every place a name reaches a query: no spaces, quotes, parentheses,
#: commas or semicolons means `COUNT(*) FROM x --` cannot be smuggled in as a
#: "metric name".
_IDENTIFIER: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,62}$",
}

_SCALAR: Final[dict[str, Any]] = {
    "type": ["string", "number", "boolean", "null"],
    "maxLength": 256,
}

_SUPERSET_ID: Final[dict[str, Any]] = {"type": "integer", "minimum": 1}

_TITLE: Final[dict[str, Any]] = {"type": "string", "minLength": 1, "maxLength": 120}

#: Free text shown to humans only — never interpolated into a query. Capped so a
#: model cannot stuff a payload with prompt-injection bait for the next turn.
_DESCRIPTION: Final[dict[str, Any]] = {"type": "string", "maxLength": 1000}


# --- Metrics (spec §6.3: no adhoc SQL) -----------------------------------

#: A simple aggregation over one declared column. Note what is *absent*:
#: `expressionType`, `sqlExpression`, `hasCustomLabel` — with
#: `additionalProperties: false` there is no key an SQL expression could occupy,
#: so `{"expressionType": "SQL"}` is unrepresentable rather than merely refused.
_SIMPLE_AGGREGATION: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["aggregate", "column"],
    "properties": {
        "aggregate": {
            "enum": ["COUNT", "SUM", "AVG", "MIN", "MAX", "COUNT_DISTINCT"],
        },
        "column": {
            "type": "object",
            "additionalProperties": False,
            "required": ["column_name"],
            "properties": {"column_name": _IDENTIFIER},
        },
        "label": {"type": "string", "minLength": 1, "maxLength": 128},
    },
}

#: Either a saved-metric reference by name, or the closed aggregation above.
#: `COUNT(*)` has no column, so it cannot be expressed as an aggregation here —
#: use the `count` saved metric that every Superset dataset exposes.
_METRIC: Final[dict[str, Any]] = {
    "oneOf": [_IDENTIFIER, _SIMPLE_AGGREGATION],
}

_METRIC_LIST: Final[dict[str, Any]] = {
    "type": "array",
    "items": _METRIC,
    "minItems": 1,
    "maxItems": 10,
}


# --- Filters (spec §6.3: no raw SQL clauses) ------------------------------

#: Deliberately *not* Superset's `adhoc_filter` shape. Superset accepts
#: `{"expressionType": "SQL", "sqlExpression": "..."}`; modelling the filter as
#: `{column, operator, value}` means `superset_write.py` must render the Superset
#: form itself, and a raw clause has nowhere to enter. Never pass a Superset-shaped
#: filter through this schema unchanged.
_ADHOC_FILTER: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["column", "operator"],
    "properties": {
        "column": _IDENTIFIER,
        "operator": {
            "enum": [
                "==",
                "!=",
                ">",
                "<",
                ">=",
                "<=",
                "IN",
                "NOT IN",
                "LIKE",
                "IS NULL",
                "IS NOT NULL",
            ],
        },
        "value": {
            "oneOf": [
                _SCALAR,
                {"type": "array", "items": _SCALAR, "maxItems": 100},
            ],
        },
    },
}


# --- Chart params (the permitted slice of Superset form_data) -------------

#: Superset's `datasource` is the string `"4__table"`. It is modelled as an
#: object so the copilot cannot hand-craft that string (and with it a datasource
#: the validator never checked); `superset_write.py` renders `f"{id}__{type}"`.
#: When present it must agree with the action's `dataset_id` — a cross-field
#: check that belongs in `validator.py`, not in the schema.
_DATASOURCE: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "type"],
    "properties": {
        "id": _SUPERSET_ID,
        "type": {"enum": ["table"]},
    },
}

#: Superset time-range grammar covers presets ("Last 12 months", "No filter")
#: and explicit ranges ("2024-01-01 : 2024-12-31"). The pattern admits those and
#: nothing with quotes, parentheses or semicolons, because this value does reach
#: the query builder.
_TIME_RANGE: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    "pattern": r"^[A-Za-z0-9 :/,.+\-]+$",
}

#: Included despite not being a label toggle: "a monthly chart of encounters" is
#: a Phase 2 acceptance criterion and is unexpressible without it.
_TIME_GRAIN: Final[dict[str, Any]] = {
    "enum": [
        "PT1S",
        "PT1M",
        "PT1H",
        "P1D",
        "P1W",
        "P1M",
        "P3M",
        "P1Y",
    ],
}

CHART_PARAMS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["viz_type"],
    "properties": {
        # -- what and where --
        # No allowlist — see the "Allowlists (NOT currently enforced)" section
        # above. `_IDENTIFIER`'s pattern is a safety floor, not a policy
        # restriction: it blocks injection (no spaces/quotes/parens/commas),
        # not any particular viz type, so every Superset viz type (including
        # maps) is schema-valid, known or not.
        "viz_type": _IDENTIFIER,
        "datasource": _DATASOURCE,
        # -- the query --
        "query_mode": {"enum": ["aggregate", "raw"]},
        "all_columns": {"type": "array", "items": _IDENTIFIER, "minItems": 1},
        "metrics": _METRIC_LIST,
        "groupby": {
            "type": "array",
            "items": _IDENTIFIER,
            "maxItems": 5,
        },
        "x_axis": _IDENTIFIER,
        "adhoc_filters": {
            "type": "array",
            "items": _ADHOC_FILTER,
            "maxItems": 20,
        },
        "time_range": _TIME_RANGE,
        "time_grain_sqla": _TIME_GRAIN,
        # `row_limit` bounds the *saved chart*, not the aggregate excerpt sent to
        # Claude — that is capped separately at 200/50 rows by the read tools.
        "row_limit": {"type": "integer", "minimum": 1, "maximum": 10000},
        "sort": {
            "type": "object",
            "additionalProperties": False,
            "required": ["descending"],
            "properties": {
                "metric": _IDENTIFIER,
                "descending": {"type": "boolean"},
            },
        },
        # -- presentation --
        "orientation": {"enum": ["vertical", "horizontal"]},
        "color_scheme": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": r"^[A-Za-z0-9_\-]+$",
        },
        # Phase 16: per-category colour overrides ("make Iganga orange
        # everywhere"). Superset's own form_data field name and shape --
        # {category_label: "#RRGGBB"} -- not a new mechanism, just newly
        # allowed through this schema. Hex-only: Superset's own control
        # accepts named CSS colours too, but validating that safely would
        # need the same allowlist Superset's own colour picker has, which
        # isn't exposed anywhere this project can read -- hex is the subset
        # that's unambiguous to validate.
        "label_colors": {
            "type": "object",
            "maxProperties": 50,
            "propertyNames": {"maxLength": 128},
            "additionalProperties": {
                "type": "string",
                "pattern": r"^#[0-9A-Fa-f]{6}$",
            },
        },
        "stack": {"type": "boolean"},
        "show_legend": {"type": "boolean"},
        "legend_orientation": {"enum": ["top", "bottom", "left", "right"]},
        "show_value": {"type": "boolean"},
        "show_labels": {"type": "boolean"},
        "rich_tooltip": {"type": "boolean"},
        "y_axis_format": {"type": "string", "maxLength": 32},
        # `big_number_total` reads `y_axis_format` in some Superset builds and
        # `number_format` in others, and getting it wrong shows an executive
        # `1234567.891` where they expected `1.2M`. Both are accepted and
        # `superset_write.py` sets whichever the viz type wants. The real
        # backstop is the saved metric's own `d3format`, projected from
        # registry.yaml, which formats correctly with no model involvement at all.
        "number_format": {"type": "string", "maxLength": 32},
        "x_axis_title": {"type": "string", "maxLength": 80},
        "y_axis_title": {"type": "string", "maxLength": 80},
    },
}

# Which params a given viz type actually *needs* differs per viz (`big_number_total`
# takes one metric and no groupby; `pie` wants a bounded row_limit). Encoding
# that as JSON Schema branches would
# be brittle and would have to change every time the allowlist does, so those
# per-viz requirements are policy checks in `validator.py`, not schema.


# --- Chart actions --------------------------------------------------------

_CREATE_CHART: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["chart_name", "dataset_id", "params"],
    "properties": {
        "chart_name": _TITLE,
        "dataset_id": _SUPERSET_ID,
        "params": CHART_PARAMS_SCHEMA,
        "description": _DESCRIPTION,
        # Superset's chart POST accepts `dashboards`, so a chart can be created
        # straight onto a dashboard. It is NOT enough on its own: the dashboard's
        # position_json needs a CHART node too, or the widget never renders. The
        # write tool emits both calls — see CreateChartTool.build_calls.
        "dashboard_id": _SUPERSET_ID,
        # Where on that dashboard. Semantic only; the compiler decides geometry.
        "placement": {"enum": ["append", "new_row", "kpi_row", "top"]},
    },
}

#: `params` here is a *partial* update. Superset's `PUT /api/v1/chart/{id}`
#: replaces `params` wholesale, so `superset_write.py` must read the existing
#: params, merge these keys over them, and write the result back — otherwise
#: "make it horizontal" silently discards the chart's filters.
_UPDATE_CHART: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["chart_id", "params"],
    "properties": {
        "chart_id": _SUPERSET_ID,
        "params": CHART_PARAMS_SCHEMA,
        "description": _DESCRIPTION,
    },
}

_RENAME_CHART: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["chart_id", "new_name"],
    "properties": {
        "chart_id": _SUPERSET_ID,
        "new_name": _TITLE,
    },
}

# --- Dashboard actions ----------------------------------------------------

_CREATE_DASHBOARD: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dashboard_title"],
    "properties": {
        "dashboard_title": _TITLE,
        "description": _DESCRIPTION,
        # Charts to place immediately. More than two makes this a `preview`
        # action under spec §6.6 — a risk decision, not a schema one.
        "chart_ids": {
            "type": "array",
            "items": _SUPERSET_ID,
            "maxItems": 20,
            "uniqueItems": True,
        },
        # No allowlist — see the "Allowlists (NOT currently enforced)" section
        # above; the compiler decides the actual geometry regardless, this is
        # only ever a hint.
        "layout_preset": _IDENTIFIER,
    },
}

_RENAME_DASHBOARD: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dashboard_id", "new_title"],
    "properties": {
        "dashboard_id": _SUPERSET_ID,
        "new_title": _TITLE,
    },
}

#: Bounded well short of "arbitrary stylesheet" -- long enough for real
#: layout/typography/colour overrides on one dashboard, short of anything
#: that reads as smuggled payload. Superset stores this as plain CSS text
#: with no sanitisation of its own (it is injected into the dashboard's
#: style tag, not rendered as HTML), so the cap is this schema's only
#: control -- there is deliberately no attempt to parse or validate CSS
#: syntax here, matching this module's rule of refusing what it cannot
#: safely represent rather than trying to safely represent everything.
_DASHBOARD_CSS: Final[dict[str, Any]] = {"type": "string", "maxLength": 8000}

_SET_DASHBOARD_CSS: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dashboard_id", "css"],
    "properties": {
        "dashboard_id": _SUPERSET_ID,
        "css": _DASHBOARD_CSS,
    },
}

#: Superset 6.1's per-dashboard theming: a dashboard points at a row in the
#: `themes` table (Ant Design token overrides) via `theme_id`, separately
#: from its own `css`. Only the two shipped system themes are offered here
#: -- "default" (id 1, THEME_DEFAULT) and "dark" (id 2, THEME_DARK) -- not
#: theme creation with arbitrary token overrides, which is a materially
#: bigger surface (a full REST resource, /api/v1/theme/) not attempted in
#: this pass. "reset" clears theme_id so the dashboard falls back to
#: whatever THEME_DEFAULT/THEME_DARK Superset resolves globally.
_SET_DASHBOARD_THEME: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dashboard_id", "theme"],
    "properties": {
        "dashboard_id": _SUPERSET_ID,
        "theme": {"enum": ["default", "dark", "reset"]},
    },
}

_ADD_CHART_TO_DASHBOARD: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dashboard_id", "chart_id"],
    "properties": {
        "dashboard_id": _SUPERSET_ID,
        "chart_id": _SUPERSET_ID,
        # Semantic placement hint for the layout compiler. Still no geometry:
        # the compiler decides columns and rows (spec §6.7).
        "placement": {"enum": ["append", "new_row", "kpi_row", "top"]},
    },
}

_REMOVE_CHART_FROM_DASHBOARD: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dashboard_id", "chart_id"],
    "properties": {
        "dashboard_id": _SUPERSET_ID,
        "chart_id": _SUPERSET_ID,
    },
}

_ADD_NATIVE_FILTER: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dashboard_id", "filter_type", "name"],
    "properties": {
        "dashboard_id": _SUPERSET_ID,
        "filter_type": {
            "enum": ["value", "time_range", "time_grain", "numerical_range"],
        },
        "name": _TITLE,
        # Required in practice for `value`/`numerical_range` filters, which need a
        # column to filter on; that conditional requirement is a validator policy
        # check so the refusal message can name the filter type.
        "dataset_id": _SUPERSET_ID,
        "column": _IDENTIFIER,
        "default_value": {
            "oneOf": [
                _SCALAR,
                {"type": "array", "items": _SCALAR, "maxItems": 100},
            ],
        },
        "multiple": {"type": "boolean"},
        # Which charts the filter drives. Absent means "all charts on the
        # dashboard", which is Superset's own default.
        "target_chart_ids": {
            "type": "array",
            "items": _SUPERSET_ID,
            "maxItems": 50,
            "uniqueItems": True,
        },
    },
}


# --- Layout actions (spec §6.7: semantic only, never position_json) -------
#
# Modelled as a discriminated union on `operation`. Each branch is closed, so an
# argument belonging to a different operation (`target_chart_id` on
# `move_to_top`, say) is a schema error rather than a silently ignored key that
# the compiler would then have to guess about.
#
# The top level carries no `properties`/`additionalProperties` of its own —
# combining those with `oneOf` is the classic JSON Schema trap, because the
# top-level `additionalProperties` cannot see inside the branches and rejects
# every branch-specific key.


def _layout_branch(
    operation: str, required: list[str], properties: dict[str, Any]
) -> dict[str, Any]:
    """One closed branch of a layout discriminated union.

    `operation` is a `const`, which is what guarantees at most one branch can
    match and keeps `oneOf` (rather than `anyOf`) honest.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", *required],
        "properties": {"operation": {"const": operation}, **properties},
    }


_MOVE_DASHBOARD_COMPONENT: Final[dict[str, Any]] = {
    "type": "object",
    "oneOf": [
        _layout_branch(
            "move_after",
            ["dashboard_id", "chart_id", "target_chart_id"],
            {
                "dashboard_id": _SUPERSET_ID,
                "chart_id": _SUPERSET_ID,
                "target_chart_id": _SUPERSET_ID,
            },
        ),
        _layout_branch(
            "move_before",
            ["dashboard_id", "chart_id", "target_chart_id"],
            {
                "dashboard_id": _SUPERSET_ID,
                "chart_id": _SUPERSET_ID,
                "target_chart_id": _SUPERSET_ID,
            },
        ),
        _layout_branch(
            "move_to_top",
            ["dashboard_id", "chart_id"],
            {"dashboard_id": _SUPERSET_ID, "chart_id": _SUPERSET_ID},
        ),
        _layout_branch(
            "move_to_bottom",
            ["dashboard_id", "chart_id"],
            {"dashboard_id": _SUPERSET_ID, "chart_id": _SUPERSET_ID},
        ),
        # "Put the KPIs across the top": gather these charts into one row at the
        # head of the dashboard. Capped at 6 because a 12-column grid cannot
        # render more than that legibly.
        _layout_branch(
            "kpi_row",
            ["dashboard_id", "chart_ids"],
            {
                "dashboard_id": _SUPERSET_ID,
                "chart_ids": {
                    "type": "array",
                    "items": _SUPERSET_ID,
                    "minItems": 1,
                    "maxItems": 6,
                    "uniqueItems": True,
                },
            },
        ),
    ],
}

_RESIZE_DASHBOARD_COMPONENT: Final[dict[str, Any]] = {
    "type": "object",
    "oneOf": [
        _layout_branch(
            "make_full_width",
            ["dashboard_id", "chart_id"],
            {"dashboard_id": _SUPERSET_ID, "chart_id": _SUPERSET_ID},
        ),
        _layout_branch(
            "make_half_width",
            ["dashboard_id", "chart_id"],
            {"dashboard_id": _SUPERSET_ID, "chart_id": _SUPERSET_ID},
        ),
        # `columns` is grid columns out of Superset's 12, not pixels — the model
        # never expresses pixel geometry.
        _layout_branch(
            "set_width",
            ["dashboard_id", "chart_id", "columns"],
            {
                "dashboard_id": _SUPERSET_ID,
                "chart_id": _SUPERSET_ID,
                "columns": {"type": "integer", "minimum": 1, "maximum": 12},
            },
        ),
        _layout_branch(
            "set_height",
            ["dashboard_id", "chart_id", "height_units"],
            {
                "dashboard_id": _SUPERSET_ID,
                "chart_id": _SUPERSET_ID,
                "height_units": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        ),
        # Even out the mixed widget heights `review_dashboard_design` reports.
        # The row is identified by a chart inside it — there are no stable row
        # ids the model could have learnt.
        _layout_branch(
            "equalize_row",
            ["dashboard_id", "chart_id"],
            {"dashboard_id": _SUPERSET_ID, "chart_id": _SUPERSET_ID},
        ),
    ],
}


# --- Public registry ------------------------------------------------------

ACTION_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "create_chart": _CREATE_CHART,
    "update_chart": _UPDATE_CHART,
    "rename_chart": _RENAME_CHART,
    "create_dashboard": _CREATE_DASHBOARD,
    "rename_dashboard": _RENAME_DASHBOARD,
    "set_dashboard_css": _SET_DASHBOARD_CSS,
    "set_dashboard_theme": _SET_DASHBOARD_THEME,
    "add_chart_to_dashboard": _ADD_CHART_TO_DASHBOARD,
    "remove_chart_from_dashboard": _REMOVE_CHART_FROM_DASHBOARD,
    "move_dashboard_component": _MOVE_DASHBOARD_COMPONENT,
    "resize_dashboard_component": _RESIZE_DASHBOARD_COMPONENT,
    "add_native_filter": _ADD_NATIVE_FILTER,
}

for _action_type, _schema in ACTION_SCHEMAS.items():
    _schema["$schema"] = JSON_SCHEMA_DIALECT
    _schema["title"] = _action_type

ALLOWED_ACTION_TYPES: Final[frozenset[str]] = frozenset(ACTION_SCHEMAS)


class UnknownActionTypeError(KeyError):
    """`get_schema` was asked for an action type that does not exist.

    Subclasses `KeyError` so callers already writing `except KeyError` keep
    working, but `str()` gives the plain sentence instead of `KeyError`'s
    quoted-repr, because this message is surfaced to the model as a tool error
    it is expected to self-correct from.
    """

    def __init__(self, action_type: str) -> None:
        known = ", ".join(sorted(ALLOWED_ACTION_TYPES))
        self.action_type = action_type
        self.message = f"Unknown action_type {action_type!r}. Permitted actions: {known}."
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


def get_schema(action_type: str) -> dict[str, Any]:
    """Return the JSON Schema for `action_type`.

    A deep copy, so a caller that decorates the schema (adding a description for
    the Anthropic tool export, say) cannot corrupt the shared definition for
    every later request in the process.

    Raises `UnknownActionTypeError` for anything not in `ALLOWED_ACTION_TYPES` —
    which is itself part of the policy: an action type the copilot invents has
    no schema, so it can never pass validation.
    """
    try:
        schema = ACTION_SCHEMAS[action_type]
    except KeyError:
        raise UnknownActionTypeError(action_type) from None
    return copy.deepcopy(schema)


# --- Import-time self-check ----------------------------------------------
#
# A schema with a typo (`"require"`, a bad `pattern`) is not an error jsonschema
# reports when *validating* — it silently ignores the unknown keyword and lets
# the payload through. That failure mode is exactly the one this package exists
# to prevent, so every schema is checked at import: a broken schema takes the
# process down at startup rather than quietly waving an action through.

for _action_type, _schema in ACTION_SCHEMAS.items():
    try:
        Draft202012Validator.check_schema(_schema)
    except Exception as exc:  # pragma: no cover - guards a developer mistake
        raise RuntimeError(
            f"Action schema {_action_type!r} is not a valid JSON Schema: {exc}"
        ) from exc

del _action_type, _schema
