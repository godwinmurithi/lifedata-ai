"""Write tools — they propose, they never execute (spec §1, §5).

Every tool here follows the same five steps:

    validate  ->  record  ->  snapshot  ->  build the Superset call  ->  sign

and returns a signed envelope. **Nothing in this module ever performs a
POST, PUT or DELETE against Superset.** The panel executes the envelope with
the user's own session cookie and CSRF token, which is what makes "the copilot
can never exceed your Superset permissions" a structural property rather than a
promise. If you are editing this file and reach for `client.post(...)`, stop:
that single line would dismantle the security model.

Two shape systems meet here and must not be confused:

* **Our schema shapes** — what the model emits and the validator polices.
  Filters are ``{column, operator, value}``; a datasource is ``{id, type}``.
* **Superset's shapes** — what the REST API expects. Filters are
  ``{expressionType: "SIMPLE", subject, operator, comparator, clause}``; a
  datasource is the string ``"4__table"``; ``params`` is a JSON *string*.

The translation lives in `_to_superset_form_data`, deliberately in one place.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from django.conf import settings
from django.db import transaction

from lifedata_ai.actions.envelopes import build_envelope, sign_envelope
from lifedata_ai.models import ActionRecord, Snapshot
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools import compile_check, mcp_write_dispatch
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode
from lifedata_ai.validation.layout import compile_operations
from lifedata_ai.validation.validator import ValidationContext, validate_action

# --- Translation to Superset's shapes --------------------------------------

#: Viz names that are not registered plugins in this Superset build, mapped to
#: the ones that are. Verified against 6.1.0.
_VIZ_ALIASES: dict[str, str] = {
    "bar": "echarts_timeseries_bar",
    "dist_bar": "echarts_timeseries_bar",
    "line": "echarts_timeseries_line",
}

#: Our operator vocabulary -> Superset's adhoc-filter operators.
_OPERATORS: dict[str, str] = {
    "==": "==",
    "!=": "!=",
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "IN": "IN",
    "NOT IN": "NOT IN",
    "LIKE": "LIKE",
    "IS NULL": "IS NULL",
    "IS NOT NULL": "IS NOT NULL",
}


def _to_superset_form_data(params: dict[str, Any], dataset_id: int) -> dict[str, Any]:
    """Render our validated params as Superset form_data."""
    form: dict[str, Any] = {k: v for k, v in params.items()
                            if k not in {"datasource", "adhoc_filters", "metrics"}}
    form["datasource"] = f"{dataset_id}__table"

    # Superset 6.1.0 does not register a plugin called `bar`; a chart saved with
    # it renders "Item with key \"bar\" is not registered" on the dashboard —
    # the API accepts the save happily, so the failure only shows up visually.
    # The allowlist no longer offers it, and this maps any that slip through.
    if form.get("viz_type") in _VIZ_ALIASES:
        form["viz_type"] = _VIZ_ALIASES[form["viz_type"]]

    # For a categorical bar chart the model reliably sets BOTH x_axis and
    # groupby to the same column — "orders by test_type" becomes
    # x_axis=test_type AND groupby=[test_type]. Superset then refuses the query
    # outright: "Duplicate column/metric labels: test_type", and the widget
    # shows a red Data error on the dashboard. Five of the first eight charts
    # the copilot built failed this way.
    #
    # x_axis is the category axis and groupby is the *series* split, so the
    # de-duplication is to keep the axis and drop the redundant split. Doing it
    # here rather than in the schema keeps the model's natural phrasing working
    # instead of bouncing it back with a validation error it cannot learn from.
    x_axis = form.get("x_axis")
    groupby = form.get("groupby")
    if x_axis and isinstance(groupby, list) and x_axis in groupby:
        remaining = [column for column in groupby if column != x_axis]
        if remaining:
            form["groupby"] = remaining
        else:
            form.pop("groupby", None)

    metrics = params.get("metrics")
    if metrics is not None:
        form["metrics"] = [_to_superset_metric(m) for m in metrics]

    filters = params.get("adhoc_filters")
    if filters:
        form["adhoc_filters"] = [_to_superset_filter(f) for f in filters]

    _apply_country_map(form, dataset_id)
    # Order matters: this derives form["metric"] from form["metrics"][0] for
    # every viz in _SINGLE_METRIC_VIZ, deck_polygon included -- it must run
    # BEFORE _apply_deck_polygon, which only reshapes that already-derived
    # value into deck.gl's object form. Reversed, _apply_single_metric_alias
    # would see a viz_type it does not special-case and pop "metric" straight
    # back out.
    _apply_single_metric_alias(form)
    _apply_deck_polygon(form, dataset_id)
    _apply_time_range(form, dataset_id)
    _apply_number_format(form)
    return form


#: The country whose boundaries this deployment maps. One value, because the
#: data is Ugandan; a multi-country deployment would move this into the registry
#: per dataset rather than making the model choose.
#:
#: **Lowercase, and that is not cosmetic.** Superset stores this control as
#: `"uganda"` and matches it exactly; `"Uganda"` leaves the Country control empty
#: and the chart renders "Add required control values to preview chart". Read
#: back out of a chart saved through Superset's own UI rather than guessed.
MAP_COUNTRY = "uganda"

#: A sequential scheme, because a choropleth encodes magnitude. Superset's own
#: default for this control; set explicitly so a map is not left to whatever the
#: default happens to become.
MAP_COLOR_SCHEME = "superset_seq_1"


def _apply_country_map(form: dict[str, Any], dataset_id: int) -> None:
    """Turn "map it by district" into the three controls a choropleth needs.

    The model asks for a map exactly as it asks for a bar chart — a viz type and
    `groupby: ["district_name"]`. Superset's Country Map wants something else
    entirely, and gets it here:

    * `select_country`, which the model never has to know;
    * `entity`, which must be the **ISO 3166-2 code** — `UG-102`, not `Kampala`.
      Given names, Superset draws all 112 Ugandan districts and fills none of
      them. Verified: the outline renders perfectly and every district stays
      white, which reads as a broken map rather than a failed join;
    * `metric`, singular, handled by `_apply_single_metric_alias` below.

    `district_iso` is `role: map_key` in the registry, so it is not groupable and
    cannot be a bar chart's axis. Nobody should ever see a chart of "UG-102,
    UG-113, UG-204" — the code exists to join boundaries and for nothing else,
    which is why the model never names it and never sees it.
    """
    if form.get("viz_type") != "country_map":
        return

    from lifedata_ai.semantics.loader import load_registry

    form.setdefault("select_country", MAP_COUNTRY)
    form.setdefault("linear_color_scheme", MAP_COLOR_SCHEME)

    dataset = load_registry().dataset_by_superset_id(int(dataset_id))
    map_key = next(
        (column.name for column in (dataset.columns if dataset else ()) if column.role == "map_key"),
        None,
    )
    if map_key:
        form["entity"] = map_key
    # `groupby` would add a second grain to a chart that already has one, and
    # Superset answers that with "Duplicate column/metric labels".
    form.pop("groupby", None)
    form.pop("x_axis", None)


#: Buikwe district's approximate center, and a zoom that fits the whole
#: district in one view. Hardcoded like MAP_COUNTRY above -- this deployment
#: maps one district, not an arbitrary one a registry field would need to
#: carry, and the geometry itself (not this default) is what actually
#: determines where each polygon is drawn.
BUIKWE_VIEWPORT: dict[str, Any] = {
    "longitude": 33.15,
    "latitude": 0.35,
    "zoom": 10,
    "bearing": 0,
    "pitch": 0,
}


def _apply_deck_polygon(form: dict[str, Any], dataset_id: int) -> None:
    """Turn "map it by subcounty" into deck.gl Polygon's controls.

    Verified against Superset's own example fixture
    (superset/examples/deckgl_demo/charts/Deck.gl_Polygons.yaml) for the exact
    control names this build's deck_polygon plugin reads -- undocumented
    anywhere else, and getting one wrong is the "renders blank, returns 2xx"
    failure mode the registry's viz_allowlist doctrine exists to prevent.

    The model asks for this exactly as it asks for a pie chart -- a viz_type
    and one metric to color by. Everything deck.gl-specific (which column
    holds the geometry, basemap tiles, viewport, color scheme) is filled in
    here so the model never has to know deck.gl's control names, the same
    shape as `_apply_country_map` above.
    """
    if form.get("viz_type") != "deck_polygon":
        return

    from lifedata_ai.semantics.loader import load_registry

    dataset = load_registry().dataset_by_superset_id(int(dataset_id))
    geometry_column = next(
        (column.name for column in (dataset.columns if dataset else ()) if column.role == "geometry"),
        None,
    )
    if not geometry_column:
        # No declared geometry column on this dataset -- leave viz_type as
        # requested and let Superset's own validation refuse it with ITS real
        # reason, rather than silently building an uncolorable, shapeless map.
        return

    form["line_column"] = geometry_column
    form["line_type"] = "json"
    # OpenStreetMap tiles, not Mapbox -- no API key configured or needed.
    form.setdefault("mapbox_style", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    form.setdefault("viewport", dict(BUIKWE_VIEWPORT))
    form.setdefault("linear_color_scheme", MAP_COLOR_SCHEME)
    form.setdefault("filled", True)
    form.setdefault("stroked", True)
    form.setdefault("extruded", False)
    form.setdefault("multiplier", 1)
    form.setdefault("line_width", 1)
    form.setdefault("line_width_unit", "meters")
    form.setdefault("legend_format", ",d")
    form.setdefault("legend_position", "tr")
    form.setdefault("js_columns", [])
    form.setdefault("js_data_mutator", "")
    form.setdefault("js_onclick_href", "")
    form.setdefault("js_tooltip", "")
    form.setdefault("reverse_long_lat", False)

    # `_apply_single_metric_alias` (which must run before this function --
    # see the call site) has already set form["metric"] = form["metrics"][0].
    # A saved-metric *name* (a bare string) can't be reshaped into deck.gl's
    # object form without inventing a SQL passthrough this module
    # deliberately doesn't have, so it's refused here rather than guessed at.
    metric = form.get("metric")
    if isinstance(metric, str):
        raise ToolError(
            f"deck_polygon needs an aggregation for its colour metric, not the saved "
            f"metric name {metric!r}. Pass params.metrics as "
            '[{"aggregate": "SUM", "column": {"column_name": "..."}}] instead.',
            code="deck_polygon_metric_unsupported",
        )
    # groupby/x_axis would add a second grain deck.gl doesn't use.
    form.pop("groupby", None)
    form.pop("x_axis", None)


def _apply_time_range(form: dict[str, Any], dataset_id: int) -> None:
    """Make the requested period actually restrict the query.

    **`time_range` on its own does nothing.** It is the legacy control, and the
    modern viz plugins take the period from an `adhoc_filters` entry whose
    operator is `TEMPORAL_RANGE`. Superset saves the chart either way and says
    nothing, so every chart built here has been announcing a period it did not
    apply.

    Verified against 6.1.0 by building the same tile twice and reading the
    rendered figure:

        time_range: "Last 12 months"                     ->  14,355   (all time)
        + adhoc TEMPORAL_RANGE on ordered_at             ->   5,922   (correct)

    14,355 is the total since the data begins. A tile captioned "last 12 months"
    showing the all-time figure is worse than a blank one: it is wrong in a way
    that looks right, and an executive has no way to tell.

    The column comes from the registry — the chart's own `x_axis` when that is
    genuinely temporal, otherwise the dataset's declared `time_column`. Without
    a temporal column there is nothing to filter on and the period is dropped
    rather than guessed at.

    The bare `time_range` key is kept alongside. Superset's own UI writes both,
    `_merge_form_data` and the design review read it, and it records what was
    asked for independently of how it is enforced.
    """
    time_range = form.get("time_range")
    if not time_range or str(time_range).strip().lower() in {"", "no filter", "no_filter"}:
        return

    filters = form.get("adhoc_filters") or []
    if any(
        isinstance(item, dict) and item.get("operator") == _TEMPORAL_RANGE
        for item in filters
    ):
        return

    column = _temporal_column(form, dataset_id)
    if not column:
        return
    form["adhoc_filters"] = [
        *filters,
        {
            "expressionType": "SIMPLE",
            "subject": column,
            "operator": _TEMPORAL_RANGE,
            "comparator": time_range,
            "clause": "WHERE",
        },
    ]


#: Superset's operator for "restrict this temporal column to a period".
_TEMPORAL_RANGE = "TEMPORAL_RANGE"


def _temporal_column(form: dict[str, Any], dataset_id: int) -> str | None:
    """Which column a period applies to, per the registry.

    The `x_axis` only when it is genuinely temporal: on a categorical bar chart
    the x_axis is something like `facility_name`, and a period filter against
    that matches nothing at all.
    """
    from lifedata_ai.semantics.loader import load_registry

    dataset = load_registry().dataset_by_superset_id(int(dataset_id))
    if dataset is None:
        return None
    x_axis = form.get("x_axis")
    if x_axis:
        declared = dataset.column(x_axis)
        if declared is not None and getattr(declared, "role", None) == "temporal":
            return str(x_axis)
    return dataset.time_column


#: Viz types whose query is built from the SINGULAR `metric` control.
#:
#: Superset's own form_data is not consistent about this: most plugins read
#: `metrics` (a list), while single-value ones read `metric`. The two are not
#: interchangeable and the unused one is ignored in silence.
#:
#: Both entries verified against 6.1.0 by building one chart of each shape and
#: looking at it — `pie` with only `metrics` shows an empty Metric control and
#: "Add required control values to preview chart". The other three allowlisted
#: types (`echarts_timeseries_line`, `echarts_timeseries_bar`, `table`) render
#: correctly from `metrics` and must NOT be added here.
_SINGLE_METRIC_VIZ: frozenset[str] = frozenset(
    {"big_number_total", "pie", "country_map", "deck_polygon"}
)


def _apply_single_metric_alias(form: dict[str, Any]) -> None:
    """Derive the `metric` key that single-value viz types actually read.

    Found by looking at an acceptance dashboard rather than by any test: three
    KPI tiles across the top reading **"No data"**, above charts that rendered
    perfectly — same dataset, same saved metrics, same time range. The API had
    accepted every write, `/api/v1/chart/data` returned 14,355 rows for the
    exact query, and every automated check passed.

    The tile was saved with `metrics: ["orders"]`. `big_number_total` builds its
    query from `metric` (singular). No metric, no query, no number — and no
    error anywhere, because nothing was wrong except that the plugin was reading
    a key nobody had set.

    **`metric` is derived here and nowhere else.** The action schema does not
    admit it, so the model cannot author one; this function is its only source,
    and it is rewritten from `metrics` on every write rather than preserved.
    That matters on a partial update: `_merge_form_data` carries the stored
    params forward, so a chart that already had `metric: orders` and is asked
    for the completion rate instead would otherwise keep the OLD metric while
    `metrics` showed the new one — a tile displaying the wrong number
    confidently, which is worse than one displaying nothing.

    `metrics` is written too. Superset itself saves both for these charts, and
    anything reading the list still finds it.

    This is the same failure as `_apply_number_format` below, one key over: a
    control name that differs per viz, silently ignored when missed. Worth
    stating plainly, because the next viz type added to the allowlist will have
    its own version of it — check it by building one and looking at it.
    """
    if form.get("viz_type") not in _SINGLE_METRIC_VIZ:
        # Meaningless for this viz, and a leftover from a previous shape would
        # be read by whatever the chart is turned into next.
        form.pop("metric", None)
        return
    metrics = form.get("metrics")
    if isinstance(metrics, list) and len(metrics) == 1:
        form["metric"] = metrics[0]
    else:
        form.pop("metric", None)


def _apply_number_format(form: dict[str, Any]) -> None:
    """Make one requested number format reach whichever key this viz honours.

    Superset is not consistent about the control name: `big_number_total` reads
    `y_axis_format` in some builds and `number_format` in others, and the wrong
    one is silently ignored — the tile renders `1234567.891` where an executive
    expected `1.2M`. Setting both is harmless (the viz reads one and ignores the
    other) and removes an entire class of "why is this number ugly" report.

    This is a backstop, not the mechanism. The saved metric's own `d3format`,
    projected onto Superset from registry.yaml, formats every chart that uses it
    with no model knowledge of d3 syntax whatsoever.
    """
    requested = form.pop("number_format", None) or form.get("y_axis_format")
    if not requested:
        return
    form["y_axis_format"] = requested
    if form.get("viz_type") == "big_number_total":
        form["number_format"] = requested


#: form_data keys that a *partial* update must recompute together rather than
#: merge key by key. Changing the viz type changes which of these are even
#: meaningful, and a leftover `groupby` from a bar chart turns a big-number tile
#: into a query Superset refuses.
_VIZ_COUPLED_KEYS: frozenset[str] = frozenset(
    # `metric` alongside `metrics`: it is set only for the single-figure viz
    # types, so changing away from one must not leave it behind to be picked up
    # by whatever the chart becomes next.
    # `entity` and `select_country` belong to a choropleth and to nothing else.
    # Left behind on a change to a bar chart they would be inert clutter; left
    # behind the other way they would be a second grain.
    {"groupby", "x_axis", "metrics", "metric", "orientation", "stack", "show_value",
     "show_labels", "entity", "select_country", "line_column", "line_type",
     "viewport", "filled", "stroked", "extruded"}
)

#: Keys that describe the chart's *identity* rather than its configuration.
#: Never carried over from the existing chart, because they are derived.
_DERIVED_KEYS: frozenset[str] = frozenset({"datasource", "viz_type"})


def _merge_form_data(
    existing: dict[str, Any], requested: dict[str, Any], raw_params: dict[str, Any]
) -> dict[str, Any]:
    """Lay a partial update over a chart's current form_data.

    Three rules, in order of how much trouble getting them wrong causes:

    1. **Keys the caller did not mention survive.** This is the whole point: the
       action schema promises `params` is partial, and Superset's PUT replaces
       wholesale, so without this "make it horizontal" discards every filter.
    2. **A viz-type change drops the keys coupled to the old viz type.** Turning
       a grouped bar chart into a `big_number_total` while keeping its `groupby`
       produces a chart Superset refuses to query. When the viz type changes, the
       coupled keys come only from the request.
    3. **`datasource` is always recomputed.** It is derived from the dataset the
       chart really belongs to, which `build_call` has just verified against
       Superset — never inherited from whatever the stored params claim.
    """
    merged = {k: v for k, v in existing.items() if k not in _DERIVED_KEYS}

    viz_changed = (
        "viz_type" in requested
        and existing.get("viz_type")
        and requested["viz_type"] != existing["viz_type"]
    )
    if viz_changed:
        for key in _VIZ_COUPLED_KEYS:
            merged.pop(key, None)

    merged.update(requested)

    # `_to_superset_form_data` only emits `metrics`/`adhoc_filters` when the
    # caller supplied them, so an untouched chart keeps its own — which is
    # exactly right. But an explicitly EMPTY list is a deliberate clear, and
    # `update()` above already honoured it. Nothing more to do; this comment
    # exists so the next reader does not "fix" it into dropping empties.
    if not merged.get("viz_type"):
        merged["viz_type"] = existing.get("viz_type") or raw_params.get("viz_type")
    return merged


def _to_superset_metric(metric: Any) -> Any:
    """A saved-metric name passes through; an aggregation becomes an adhoc metric.

    `expressionType` is hardcoded to SIMPLE and there is no branch that can emit
    SQL — that is the point. The schema already makes SQL unrepresentable; this
    keeps it unrepresentable on the way out too.
    """
    if isinstance(metric, str):
        return metric
    column_name = metric["column"]["column_name"]
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column_name},
        "aggregate": metric["aggregate"],
        "label": metric.get("label") or f"{metric['aggregate']}({column_name})",
    }


def _to_superset_filter(spec: dict[str, Any]) -> dict[str, Any]:
    operator = _OPERATORS.get(spec["operator"])
    if operator is None:
        raise ToolError(f"Unsupported filter operator {spec['operator']!r}.")
    return {
        "expressionType": "SIMPLE",
        "subject": spec["column"],
        "operator": operator,
        "comparator": spec.get("value"),
        "clause": "WHERE",
    }


# --- Base ------------------------------------------------------------------


class WriteTool(Tool):
    """Shared pipeline for every write action.

    Subclasses supply `action_type` and implement `build_call`, which returns
    ``(method, path, body)``. Everything else — validation, the audit record,
    the snapshot, signing — happens once, here, so a new action cannot
    accidentally skip a step.
    """

    mode: ClassVar[ToolMode] = ToolMode.WRITE
    action_type: ClassVar[str]

    #: What kind of Superset object this action creates or changes, recorded on
    #: the `ActionRecord`. Undo needs it to know that the inverse of "create a
    #: chart" is "delete that chart" rather than "restore a snapshot", and it was
    #: simply never set — every row carried an empty string.
    object_type: ClassVar[str] = ""

    # -- to be implemented by subclasses ---------------------------------

    def build_call(
        self, context: ToolContext, payload: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        """The single call this action performs. Override this, or `build_calls`."""
        raise NotImplementedError

    def build_calls(
        self, context: ToolContext, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """The ordered calls this action performs.

        Most actions are one call and implement `build_call`. Dashboard
        placement needs two — the layout and the chart-to-dashboard relation —
        and overrides this instead.
        """
        method, path, body = self.build_call(context, payload)
        return [{"method": method, "path": path, "body": body}]

    def snapshot_target(self, payload: dict[str, Any]) -> tuple[str, str] | None:
        """Return (object_type, object_id) whose prior state should be captured."""
        return None

    def ownership_target(self, payload: dict[str, Any]) -> tuple[str, str] | None:
        """Return (object_type, object_id) whose owner must be the caller.

        **Defaults to `snapshot_target`, and that is not a coincidence.** Any
        action with prior state worth capturing is by definition modifying an
        object that already exists — which is exactly the set that needs an
        ownership check. One default covers every tool but one.

        The exception is `create_chart(dashboard_id=…)`, which overrides this:
        the chart is new and has no prior state, but the dashboard whose layout
        it rewrites is not. See that class.
        """
        return self.snapshot_target(payload)

    #: Where `build_calls` leaves a note for `run` to lift out. Popped before the
    #: payload is persisted, so it never becomes part of the stored action.
    VERIFICATION_KEY: ClassVar[str] = "_verification"

    def _verify_it_runs(
        self,
        context: ToolContext,
        form_data: dict[str, Any],
        dataset_id: int,
        chart_name: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Refuse a chart whose query Superset will not run. See `compile_check`.

        Raises `ToolError` on a refusal, so the model gets a correctable message
        instead of the user getting a red widget. A query that runs but returns
        nothing is not refused — it is noted, and the note reaches the receipt so
        the copilot can say so rather than presenting a blank tile as a result.
        """
        verdict = compile_check.verify(context.superset_client(), form_data, dataset_id)
        if verdict.refused:
            raise ToolError(
                compile_check.refusal_message(verdict, chart_name),
                code="chart_would_not_render",
                details={"superset_error": verdict.message},
            )
        if verdict.empty and payload is not None:
            payload[self.VERIFICATION_KEY] = (
                f"“{chart_name}” is valid but returns no rows for the period and filters "
                "requested, so it will render empty. Say so, and offer to widen the period "
                "or relax the filter."
            )

    def _owned_by_caller(self, context: ToolContext) -> dict[str, Any]:
        """`{"owners": [<the caller's Superset user id>]}`, for a creation body.

        **Ownership enforcement is worthless if creations end up unowned**, and
        that is exactly what happened: `POST /api/v1/chart/` and
        `POST /api/v1/dashboard/` leave `owners` EMPTY unless the body names one,
        so the copilot built a six-chart dashboard and was then refused
        permission to lay it out or filter it — by its own ownership check,
        against an object it had just created.

        Verified in a browser: `owners=[None]` on every chart and dashboard the
        copilot had ever made, and the flagship "build me a dashboard" flow ended
        with two red failures and an apology.

        Returns an empty dict when the id is unknown, so the call still goes out
        rather than being refused — Superset's own default is no worse than
        before, and a missing id is an identity problem to fix elsewhere.
        """
        user_id = getattr(context.identity, "user_id", None)
        return {"owners": [int(user_id)]} if user_id else {}

    def target_object_id(self, payload: dict[str, Any]) -> Any:
        """The id of the object this action changes, or None when it creates one.

        A creation has no id until the browser performs it; `action_result` fills
        `superset_object_id` in from the response.
        """
        for key in ("chart_id", "dashboard_id"):
            if payload.get(key) is not None:
                return payload[key]
        return None

    # -- the pipeline ----------------------------------------------------

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        payload = dict(kwargs)

        result = validate_action(
            self.action_type, payload, context=self._validation_context(context, payload)
        )
        if not result.ok:
            # Returned as a ToolError so the loop feeds it back to the model as
            # a tool result it can correct from (spec §5 step 1).
            raise ToolError(
                "This action was refused: " + " ".join(result.errors),
                code="validation_failed",
                details={"errors": result.errors, "reasons": result.reasons},
            )

        approved = result.normalised_payload or payload
        self._require_ownership(context, approved)
        calls = self.build_calls(context, approved)
        # Lifted out before the payload is persisted: `build_calls` is the only
        # place with the rendered form_data, so it is where the compile check
        # runs, but the note it leaves belongs on the receipt rather than in the
        # stored action arguments.
        note = approved.pop(self.VERIFICATION_KEY, None)
        summary = self.summarise(approved)

        # Which path the panel takes on confirm: "mcp" means the server
        # performs the write itself via superset_mcp_client.py (see
        # views/execute_mcp_action.py); "browser" is the original path, where
        # the panel replays `calls` against Superset with the user's own
        # cookie. Decided per-action, not just per-flag: a create_chart whose
        # config mcp_chart_config.py can't translate still falls back to
        # "browser" even with the flag on, rather than failing at confirm
        # time. See tools/mcp_write_dispatch.py.
        execution_mode = (
            "mcp"
            if settings.LIFEDATA_AI_USE_MCP_WRITES
            and mcp_write_dispatch.can_execute_via_mcp(self.action_type, approved)
            else "browser"
        )

        verdict = result.to_payload() if hasattr(result, "to_payload") else {}
        if note:
            verdict["verification"] = note
        # The calls and the summary are recorded alongside the verdict so a
        # proposal survives a page reload. A preview card now outlives the turn
        # that produced it — it is waiting for a person — and a card that exists
        # on the server but not on screen is worse than one that never persisted.
        # `conversation_detail` re-signs these into a fresh envelope; the stored
        # signature is deliberately not kept, so an abandoned proposal cannot
        # stay executable in a stale tab.
        verdict["calls"] = calls
        verdict["summary"] = summary
        verdict["execution_mode"] = execution_mode

        with transaction.atomic():
            record = ActionRecord.objects.create(
                conversation=context.conversation,
                action_type=self.action_type,
                payload=approved,
                validation_result=verdict,
                risk=result.risk or ActionRecord.Risk.PREVIEW,
                status=ActionRecord.Status.PROPOSED,
                superset_object_type=self.object_type,
                superset_object_id=str(self.target_object_id(approved) or ""),
            )
            self._capture_snapshot(context, record, approved)

        envelope = build_envelope(
            action_id=str(record.id),
            action_type=self.action_type,
            calls=calls,
        )
        return {
            "action_id": str(record.id),
            "action_type": self.action_type,
            "risk": record.risk,
            "summary": summary,
            "execution_mode": execution_mode,
            # The signed token travels with the action and comes back on the
            # result report, where the server verifies the panel executed the
            # decision that was actually approved.
            "envelope": sign_envelope(envelope),
            # The calls the panel performs, against Superset, directly and in
            # order. It must send exactly these and nothing else — the
            # orchestrator never issues a Superset write itself (spec §1).
            # Only meaningful when execution_mode is "browser"; ignored by the
            # panel on the "mcp" path, which sends only the action_id.
            "calls": calls,
            # The compile-check note joins the warnings rather than getting its
            # own field: the model already knows to relay warnings, and one more
            # channel it might ignore helps nobody.
            "warnings": ([*result.warnings, note] if note else result.warnings),
        }

    def summarise(self, payload: dict[str, Any]) -> str:
        return self.action_type.replace("_", " ")

    # -- helpers ---------------------------------------------------------

    def _validation_context(
        self, context: ToolContext, payload: dict[str, Any]
    ) -> ValidationContext:
        """Supply ownership so the validator can classify risk.

        The validator makes no network calls by design, so ownership has to be
        fetched here. If it cannot be determined we pass None, and the validator
        fails closed to `preview`.
        """
        owners: list[str] | None = None
        dashboard_id = payload.get("dashboard_id")
        if dashboard_id:
            try:
                dashboard = context.superset_client().get_dashboard(dashboard_id)
                owners = [
                    o.get("username") or f"{o.get('first_name','')} {o.get('last_name','')}".strip()
                    for o in dashboard.get("owners") or []
                ]
            except SupersetError:
                owners = None
        return ValidationContext(
            identity_username=context.identity.username,
            identity_user_id=getattr(context.identity, "user_id", None),
            dashboard_owners=owners,
        )

    def _require_ownership(self, context: ToolContext, payload: dict[str, Any]) -> None:
        """Refuse outright if the caller does not own what this action changes.

        **The hard refusal lives here, not in the validator.** The validator is
        deliberately network-free and can only reason about what a payload
        claims; ownership has to be read from Superset. `UpdateChartTool` already
        set that precedent for the dataset check, with identical reasoning.

        The validator's ownership rule stays as a second layer: it routes a
        non-owner action to a preview card. That was the *only* control before
        this, which meant a non-owner change proceeded as soon as the user
        confirmed it — and `update_chart` had no ownership check at all.

        Superset's own permissions are a third layer and the authoritative one:
        the panel executes with the user's session, so Superset refuses what the
        user may not do. This check exists so the copilot does not *propose*
        something the user would have to be told, mid-flow, that they cannot do.

        **A caveat worth stating rather than hiding:** with one shared admin
        account this is a no-op, because everyone owns everything and Admin
        bypasses Superset's object permissions anyway. Per-executive non-Admin
        accounts are a prerequisite for the boundary to be real — see
        `manage.py create_pilot_users` and docs/runbook.md. Shipped regardless,
        because it still stops the copilot editing a colleague's chart the moment
        two people share a role.
        """
        target = self.ownership_target(payload)
        if not target:
            return
        object_type, object_id = target

        client = context.superset_client()
        try:
            if object_type == Snapshot.ObjectType.CHART:
                current = client.get_chart(int(object_id))
            else:
                current = client.get_dashboard(object_id)
        except SupersetError as exc:
            # FAIL CLOSED. If we cannot establish ownership we do not proceed:
            # the alternative is that the one case where the lookup is broken is
            # also the case where the check is skipped.
            raise ToolError(
                f"Could not read {object_type} {object_id} to check who owns it, so this "
                f"change is refused: {exc}",
                code="ownership_unknown",
            ) from exc

        owners = current.get("owners") or []
        if not (context.identity.username or "").strip():
            raise ToolError(
                "The acting user could not be identified, so ownership cannot be "
                "established and this change is refused.",
                code="identity_unknown",
            )
        if owns(owners, context.identity):
            return

        if not owners:
            # Nobody's, rather than someone else's. Worth its own message: the
            # fix is to claim it in Superset, not to ask a colleague. Seeded and
            # hand-built objects land here, and so did everything the copilot
            # created before `_owned_by_caller` existed.
            raise ToolError(
                f"This {object_type} has no owner in Superset, so the copilot cannot tell "
                "whether it is yours to change and will not touch it. Open it in Superset "
                "and add yourself as an owner (Edit properties -> Access), then ask again.",
                code="not_owner",
                details={"object_type": object_type, "object_id": str(object_id),
                         "reason": "unowned"},
            )

        labels = _owner_labels(owners)
        raise ToolError(
            f"This {object_type} belongs to {' and '.join(labels) or 'someone else'}, and "
            "this copilot only changes things you own. Ask them to make the change, or "
            "build your own copy instead.",
            code="not_owner",
            details={"object_type": object_type, "object_id": str(object_id),
                     "reason": "owned_by_someone_else", "owners": labels},
        )

    def _capture_snapshot(
        self, context: ToolContext, record: ActionRecord, payload: dict[str, Any]
    ) -> None:
        """Capture pre-change state so undo has something to restore (spec §4)."""
        target = self.snapshot_target(payload)
        if not target:
            return
        object_type, object_id = target
        client = context.superset_client()
        try:
            if object_type == Snapshot.ObjectType.CHART:
                current = client.get_chart(int(object_id))
                state = {
                    "slice_name": current.get("slice_name"),
                    "params": current.get("params"),
                    "query_context": current.get("query_context"),
                }
            else:
                current = client.get_dashboard(object_id)
                state = {
                    "dashboard_title": current.get("dashboard_title"),
                    "position_json": current.get("position_json"),
                    "json_metadata": current.get("json_metadata"),
                    "charts": current.get("charts"),
                }
        except SupersetError:
            # A missing snapshot must not block the action; it only means undo
            # will refuse later, which is the safe direction.
            return

        Snapshot.objects.create(
            action=record,
            object_type=object_type,
            object_id=str(object_id),
            state=state,
            changed_on=str(current.get("changed_on") or ""),
        )


# --- Chart actions ---------------------------------------------------------


class CreateChartTool(WriteTool):
    name: ClassVar[str] = "create_chart"
    action_type: ClassVar[str] = "create_chart"
    object_type: ClassVar[str] = Snapshot.ObjectType.CHART
    description: ClassVar[str] = (
        "Create a new Superset chart and save it. Use this whenever the user asks for a "
        "chart — build it, do not describe how one could be built. Pass params.viz_type, "
        "params.datasource, and the metrics and groupby it should use; prefer the saved "
        "metric names from the catalogue in your instructions. Pass dashboard_id to place "
        "it on an existing dashboard at the same time."
    )
    input_schema: ClassVar[dict[str, Any]] = {}  # set below from the action schema

    def snapshot_target(self, payload):
        """The DASHBOARD, when there is one — never the chart.

        The chart is new and has no prior state to restore. The dashboard's
        layout, which this action rewrites, does. Without this an undo of
        "put a new chart on my dashboard" would have nothing to restore.
        """
        dashboard_id = payload.get("dashboard_id")
        if dashboard_id is None:
            return None
        return (Snapshot.ObjectType.DASHBOARD, str(dashboard_id))

    def build_calls(self, context, payload):
        dataset_id = payload["dataset_id"]
        form_data = _to_superset_form_data(payload["params"], dataset_id)
        self._verify_it_runs(context, form_data, dataset_id, payload["chart_name"], payload)
        body = {
            "slice_name": payload["chart_name"],
            "datasource_id": dataset_id,
            "datasource_type": "table",
            "viz_type": form_data["viz_type"],
            # Superset stores form_data as a JSON string, not an object.
            "params": json.dumps(form_data),
            # Stored so the chart can be READ BACK. Without it the chart renders
            # fine but `POST /api/v1/chart/data` has nothing to run, so
            # `get_chart_data` refuses and the copilot cannot quote a figure from
            # a chart it built one turn earlier. See `saved_query_context_for`.
            "query_context": json.dumps(
                compile_check.saved_query_context_for(form_data, dataset_id)
            ),
            # Without this the chart is created UNOWNED, and the copilot's own
            # ownership check then refuses to touch what it just made.
            **self._owned_by_caller(context),
        }
        if payload.get("description"):
            body["description"] = payload["description"]

        dashboard_id = payload.get("dashboard_id")
        if not dashboard_id:
            return [{"method": "POST", "path": "/api/v1/chart/", "body": body}]

        # A chart needs BOTH the dashboard relation and a position_json CHART
        # node. This action used to emit only the POST with `dashboards: [id]`,
        # which puts the chart in the dashboard's chart list and nowhere in its
        # layout — so it renders nothing at all. (The mirror-image mistake,
        # layout without the relation, renders "There is no chart definition
        # associated with this component". Both were seen on real dashboards.)
        body["dashboards"] = [int(dashboard_id)]
        current = context.superset_client().get_dashboard(dashboard_id)
        position = compile_operations(
            current.get("position_json"),
            [{
                "operation": "add_chart",
                # The chart does not exist yet, so its id does not either. The
                # placeholder travels as a *value* inside a nested object and is
                # substituted by the panel after the POST returns — see
                # `json_encode` below for why it cannot be a string already.
                "chart_id": PLACEHOLDER_NEW_ID,
                "placement": payload.get("placement", "append"),
                "slice_name": payload["chart_name"],
            }],
            allow_placeholder_chart_id=True,
        )
        return [
            {"method": "POST", "path": "/api/v1/chart/", "body": body},
            {
                "method": "PUT",
                "path": f"/api/v1/dashboard/{dashboard_id}",
                # Carried as a nested OBJECT, not a JSON string, so the panel can
                # substitute the new chart id as a value. Putting a placeholder
                # inside an already-serialised string would force string surgery
                # on signed content, which is not acceptable — the panel would be
                # editing bytes the orchestrator signed.
                "body": {"position_json": json.loads(position)},
                "json_encode": ["position_json"],
            },
        ]

    def summarise(self, payload):
        return _describe_chart(payload)


class UpdateChartTool(WriteTool):
    name: ClassVar[str] = "update_chart"
    action_type: ClassVar[str] = "update_chart"
    object_type: ClassVar[str] = Snapshot.ObjectType.CHART
    description: ClassVar[str] = (
        "Change an existing chart. Send only the keys you want to change — the chart's "
        "existing configuration is read and your keys are merged over it, so a request to "
        "make a chart horizontal keeps its filters, time range and metrics. Use this for "
        "any follow-up on a chart the user is looking at."
    )
    input_schema: ClassVar[dict[str, Any]] = {}

    def snapshot_target(self, payload):
        return (Snapshot.ObjectType.CHART, str(payload["chart_id"]))

    def build_call(self, context, payload):
        chart_id = payload["chart_id"]
        claimed = (payload.get("params") or {}).get("datasource") or {}
        claimed_id = claimed.get("id")

        # SECURITY: the validator has no network access, so it can only police
        # the dataset the payload *claims*. A payload naming dataset 1 while the
        # chart really belongs to dataset 4 (patient geography) would be checked
        # against the wrong policy. Close that here, where we can look.
        try:
            current = context.superset_client().get_chart(chart_id)
        except SupersetError as exc:
            raise ToolError(
                f"Could not read chart {chart_id} to verify its dataset: {exc}",
                code="chart_unreadable",
            ) from exc

        real_id = _chart_dataset_id(current)
        if real_id is not None and claimed_id is not None and int(claimed_id) != int(real_id):
            raise ToolError(
                f"Chart {chart_id} belongs to dataset {real_id}, but the request claims "
                f"dataset {claimed_id}. The dataset determines which privacy policy applies, "
                "so this mismatch is refused. Call get_chart and use its real dataset.",
                code="dataset_mismatch",
            )

        if real_id is None and claimed_id is None:
            raise ToolError(
                f"Chart {chart_id} does not report which dataset it uses, so the privacy "
                "policy that applies cannot be established. Include params.datasource.",
                code="dataset_unknown",
            )
        dataset_id = int(real_id if real_id is not None else claimed_id)

        # MERGE, do not replace. `PUT /api/v1/chart/{id}` overwrites `params`
        # wholesale, and the action schema documents `params` as a *partial*
        # update — so replacing here silently discarded the chart's filters,
        # time range and metrics. "Make that a line chart instead" is the single
        # most common executive follow-up, and it was destructive.
        existing = _load_json(current.get("params")) or {}
        requested = _to_superset_form_data(payload["params"], dataset_id)
        form_data = _merge_form_data(existing, requested, payload["params"])

        # After the merge, not before: an edit is only safe if the chart still
        # runs once the requested keys are laid over the existing ones. "Make it
        # a bar chart by facility" can produce a query the chart never had.
        self._verify_it_runs(
            context,
            form_data,
            dataset_id,
            current.get("slice_name") or f"chart {chart_id}",
            payload,
        )

        body = {
            "params": json.dumps(form_data),
            "viz_type": form_data["viz_type"],
            # Rewritten, not left alone. `params` has just changed, so a
            # query_context stored from the previous definition now describes a
            # chart that no longer exists — and `get_chart_data` would answer
            # confidently with the OLD query's numbers, which is worse than
            # refusing. Superset's own Explore rewrites it on every save for the
            # same reason.
            "query_context": json.dumps(
                compile_check.saved_query_context_for(form_data, dataset_id)
            ),
        }
        if payload.get("description"):
            body["description"] = payload["description"]
        return "PUT", f"/api/v1/chart/{chart_id}", body

    def summarise(self, payload):
        changed = ", ".join(sorted(k for k in (payload.get("params") or {}) if k != "datasource"))
        return f"update chart {payload['chart_id']} ({changed or 'no changes'})"


class RenameChartTool(WriteTool):
    name: ClassVar[str] = "rename_chart"
    action_type: ClassVar[str] = "rename_chart"
    object_type: ClassVar[str] = Snapshot.ObjectType.CHART
    description: ClassVar[str] = "Rename an existing chart."
    input_schema: ClassVar[dict[str, Any]] = {}

    def snapshot_target(self, payload):
        return (Snapshot.ObjectType.CHART, str(payload["chart_id"]))

    def build_call(self, context, payload):
        return ("PUT", f"/api/v1/chart/{payload['chart_id']}",
                {"slice_name": payload["new_name"]})

    def summarise(self, payload):
        return f"rename chart {payload['chart_id']} to “{payload['new_name']}”"


# --- Dashboard actions -----------------------------------------------------


class CreateDashboardTool(WriteTool):
    name: ClassVar[str] = "create_dashboard"
    action_type: ClassVar[str] = "create_dashboard"
    object_type: ClassVar[str] = Snapshot.ObjectType.DASHBOARD
    description: ClassVar[str] = (
        "Create a dashboard and place existing charts on it. Build the charts first with "
        "create_chart, then pass their ids as chart_ids in reading order — single-figure "
        "tiles first, then the trend, then breakdowns, then any table. The layout is "
        "produced by the layout compiler, never by you. Cross-filtering is enabled, so "
        "clicking a bar filters the rest of the dashboard."
    )
    input_schema: ClassVar[dict[str, Any]] = {}

    def build_calls(self, context, payload):
        body: dict[str, Any] = {
            "dashboard_title": payload["dashboard_title"],
            # See `_owned_by_caller`. A dashboard created without this cannot
            # afterwards be laid out or filtered by the copilot that built it.
            **self._owned_by_caller(context),
        }
        chart_ids = [int(c) for c in (payload.get("chart_ids") or [])]

        if chart_ids:
            # THE LAYOUT COMPILER, not a bespoke arrangement. `build_position_json`
            # used to do this: hardcoded rows of two, fixed height 50, no
            # placement semantics — a weaker duplicate of `_op_add_chart`, and
            # the only reason the compiler's 59 tests had never run end to end.
            #
            # `parse_position_json(None)` returns `empty_skeleton()`, so building
            # a layout from nothing is the compiler's normal first call, not a
            # special case.
            uuids = _chart_uuids(context, chart_ids)
            body["position_json"] = compile_operations(
                None,
                [
                    {
                        "operation": "add_chart",
                        "chart_id": chart_id,
                        # The first chart opens a row; the rest fill it and the
                        # compiler reflows past 12 columns on its own.
                        "placement": "append" if index else "new_row",
                        **_layout_meta(uuids.get(chart_id)),
                    }
                    for index, chart_id in enumerate(chart_ids)
                ],
            )

        metadata: dict[str, Any] = {
            # Clicking a facility bar to filter everything else is a
            # disproportionately large perceived-quality win for one key, and it
            # is off by default on a dashboard created through the API.
            "cross_filters_enabled": True,
        }
        body["json_metadata"] = json.dumps(metadata)

        calls = [{"method": "POST", "path": "/api/v1/dashboard/", "body": body}]
        # Writing the layout is not enough. Superset also needs the
        # chart-to-dashboard relation, or every widget renders "There is no
        # chart definition associated with this component". Verified: a
        # dashboard with a correct position_json but no relation shows three
        # empty boxes; adding the relation makes the same layout render.
        #
        # The dashboard's id does not exist until the first call runs, so the
        # panel substitutes it — see PLACEHOLDER_NEW_ID.
        for chart_id in chart_ids:
            calls.append({
                "method": "PUT",
                "path": f"/api/v1/chart/{chart_id}",
                "body": {"dashboards": [PLACEHOLDER_NEW_ID]},
            })
        return calls

    def summarise(self, payload):
        charts = len(payload.get("chart_ids") or [])
        suffix = f" with {charts} chart{'s' if charts != 1 else ''}" if charts else ""
        return f"new dashboard “{payload['dashboard_title']}”{suffix}"


class RenameDashboardTool(WriteTool):
    name: ClassVar[str] = "rename_dashboard"
    action_type: ClassVar[str] = "rename_dashboard"
    object_type: ClassVar[str] = Snapshot.ObjectType.DASHBOARD
    description: ClassVar[str] = "Rename an existing dashboard."
    input_schema: ClassVar[dict[str, Any]] = {}

    def snapshot_target(self, payload):
        return (Snapshot.ObjectType.DASHBOARD, str(payload["dashboard_id"]))

    def build_call(self, context, payload):
        return ("PUT", f"/api/v1/dashboard/{payload['dashboard_id']}",
                {"dashboard_title": payload["new_title"]})

    def summarise(self, payload):
        return f"rename dashboard {payload['dashboard_id']} to “{payload['new_title']}”"


class AddChartToDashboardTool(WriteTool):
    name: ClassVar[str] = "add_chart_to_dashboard"
    action_type: ClassVar[str] = "add_chart_to_dashboard"
    object_type: ClassVar[str] = Snapshot.ObjectType.DASHBOARD
    description: ClassVar[str] = (
        "Place an existing chart onto an existing dashboard. Say where with placement: "
        "'append' adds it to the last row, 'new_row' gives it a row of its own, 'top' puts "
        "it above everything, 'kpi_row' adds it to the row of single-figure tiles at the "
        "top and re-evens that row."
    )
    input_schema: ClassVar[dict[str, Any]] = {}

    def snapshot_target(self, payload):
        return (Snapshot.ObjectType.DASHBOARD, str(payload["dashboard_id"]))

    def build_calls(self, context, payload):
        dashboard_id = payload["dashboard_id"]
        new_chart = int(payload["chart_id"])
        current = context.superset_client().get_dashboard(dashboard_id)

        # `placement` has been declared in this action's schema since it was
        # written and was never read: the model could ask for a KPI row and be
        # silently given a row of two. The compiler has understood all four
        # values all along.
        uuids = _chart_uuids(context, [new_chart])
        position = compile_operations(
            current.get("position_json"),
            [{
                "operation": "add_chart",
                "chart_id": new_chart,
                "placement": payload.get("placement", "append"),
                **_layout_meta(uuids.get(new_chart)),
            }],
        )
        # Layout, then the relation — both are required (see CreateDashboardTool).
        return [
            {"method": "PUT", "path": f"/api/v1/dashboard/{dashboard_id}",
             "body": {"position_json": position}},
            {"method": "PUT", "path": f"/api/v1/chart/{new_chart}",
             "body": {"dashboards": _dashboards_including(context, new_chart, dashboard_id)}},
        ]

    def summarise(self, payload):
        placement = payload.get("placement", "append")
        return (
            f"put chart {payload['chart_id']} on dashboard {payload['dashboard_id']} "
            f"({placement.replace('_', ' ')})"
        )


class RemoveChartFromDashboardTool(WriteTool):
    name: ClassVar[str] = "remove_chart_from_dashboard"
    action_type: ClassVar[str] = "remove_chart_from_dashboard"
    object_type: ClassVar[str] = Snapshot.ObjectType.DASHBOARD
    description: ClassVar[str] = "Remove a chart from a dashboard. Always requires confirmation."
    input_schema: ClassVar[dict[str, Any]] = {}

    def snapshot_target(self, payload):
        return (Snapshot.ObjectType.DASHBOARD, str(payload["dashboard_id"]))

    def build_call(self, context, payload):
        dashboard_id = payload["dashboard_id"]
        current = context.superset_client().get_dashboard(dashboard_id)
        # `remove_chart`, not "rebuild the layout without it". Rebuilding threw
        # away tabs, manual column layouts and every width the user had chosen —
        # a removal that silently reformats the rest of the dashboard is a much
        # bigger change than the one that was asked for. The compiler detaches
        # one node and tidies away the row only if it is left empty.
        position = compile_operations(
            current.get("position_json"),
            [{"operation": "remove_chart", "chart_id": int(payload["chart_id"])}],
        )
        return "PUT", f"/api/v1/dashboard/{dashboard_id}", {"position_json": position}

    def summarise(self, payload):
        return f"remove chart {payload['chart_id']} from dashboard {payload['dashboard_id']}"


class _LayoutTool(WriteTool):
    """Shared body for the two semantic layout actions.

    The model expresses intent (`make_full_width`, `move_after`, …) and the
    compiler alone produces position_json — spec §6.7. Nothing here accepts raw
    layout JSON.
    """

    object_type: ClassVar[str] = Snapshot.ObjectType.DASHBOARD
    input_schema: ClassVar[dict[str, Any]] = {}

    def snapshot_target(self, payload):
        return (Snapshot.ObjectType.DASHBOARD, str(payload["dashboard_id"]))

    def build_call(self, context, payload):
        dashboard_id = payload["dashboard_id"]
        current = context.superset_client().get_dashboard(dashboard_id)
        operations = [{k: v for k, v in payload.items() if k != "dashboard_id"}]
        position = compile_operations(current.get("position_json"), operations)
        return "PUT", f"/api/v1/dashboard/{dashboard_id}", {"position_json": position}

    def summarise(self, payload):
        return f"{payload['operation'].replace('_', ' ')} on dashboard {payload['dashboard_id']}"


class MoveDashboardComponentTool(_LayoutTool):
    name: ClassVar[str] = "move_dashboard_component"
    action_type: ClassVar[str] = "move_dashboard_component"
    description: ClassVar[str] = (
        "Reposition a chart on a dashboard. Express the intent — move_after, move_before, "
        "move_to_top, move_to_bottom, kpi_row — never raw layout JSON."
    )


class ResizeDashboardComponentTool(_LayoutTool):
    name: ClassVar[str] = "resize_dashboard_component"
    action_type: ClassVar[str] = "resize_dashboard_component"
    description: ClassVar[str] = (
        "Resize a chart on a dashboard: make_full_width, make_half_width, set_width, "
        "set_height or equalize_row."
    )


class AddNativeFilterTool(WriteTool):
    name: ClassVar[str] = "add_native_filter"
    action_type: ClassVar[str] = "add_native_filter"
    object_type: ClassVar[str] = Snapshot.ObjectType.DASHBOARD
    description: ClassVar[str] = (
        "Add a native dashboard filter (a date range, or a value filter on a column)."
    )
    input_schema: ClassVar[dict[str, Any]] = {}

    def snapshot_target(self, payload):
        return (Snapshot.ObjectType.DASHBOARD, str(payload["dashboard_id"]))

    def build_call(self, context, payload):
        dashboard_id = payload["dashboard_id"]
        current = context.superset_client().get_dashboard(dashboard_id)
        metadata = _load_json(current.get("json_metadata")) or {}
        filters = list(metadata.get("native_filter_configuration") or [])

        config = _native_filter_config(
            payload,
            len(filters),
            chart_ids=_layout_chart_ids(current),
            tab_ids=_layout_tab_ids(current),
        )
        # Replace a filter of the same name rather than stacking a second copy
        # beside it. "Add a date filter" said twice should leave one date filter.
        existing = next(
            (i for i, f in enumerate(filters) if f.get("name") == config["name"]), None
        )
        if existing is None:
            filters.append(config)
        else:
            config["id"] = filters[existing].get("id", config["id"])
            filters[existing] = config

        metadata["native_filter_configuration"] = filters
        # Filters and cross-filtering are the same feature to a user: click a bar,
        # everything else narrows. Enabling it alongside is one key.
        metadata.setdefault("cross_filters_enabled", True)
        return ("PUT", f"/api/v1/dashboard/{dashboard_id}",
                {"json_metadata": json.dumps(metadata)})

    def summarise(self, payload):
        kind = {
            "time_range": "date range",
            "time_grain": "time grain",
            "numerical_range": "numeric range",
        }.get(payload["filter_type"], "value")
        return (
            f"add a {kind} filter “{payload['name']}” to dashboard "
            f"{payload['dashboard_id']}"
        )


def owns(owners: Any, identity: Any) -> bool:
    """Whether `identity` is among `owners`, as Superset really reports them.

    **Superset's owner objects carry no `username` field.** Verified against
    6.1.0 — `GET /api/v1/dashboard/{id}` returns

        [{"first_name": "Life", "id": 1, "last_name": "Admin"}]

    and the chart and list serialisers add `email` but still no username. Code
    that matched on `owner["username"]` therefore found nothing, every time, and
    the ownership check refused EVERY object including ones the caller plainly
    owned. The copilot built a dashboard and was then refused permission to put a
    filter on it.

    That went unnoticed because the unit fixtures invented `{"username":
    "analyst"}` — a shape Superset does not produce. Asserting against a made-up
    fixture proves the code agrees with the fixture.

    So the id is the primary key here, because it is the field Superset always
    sends. Email and username are accepted as fallbacks: they appear in some
    serialisers, and a deployment with a different auth backend may populate them
    when the numeric id is absent.
    """
    user_id = getattr(identity, "user_id", None)
    email = (getattr(identity, "email", "") or "").strip().casefold()
    username = (getattr(identity, "username", "") or "").strip().casefold()

    for owner in owners or []:
        if isinstance(owner, str):
            if owner.strip().casefold() in {username, email} - {""}:
                return True
            continue
        if not isinstance(owner, dict):
            continue
        if user_id is not None and owner.get("id") is not None:
            if int(owner["id"]) == int(user_id):
                return True
        for key in ("username", "email"):
            value = str(owner.get(key) or "").strip().casefold()
            if value and value in {username, email} - {""}:
                return True
    return False


def _owner_labels(owners: Any) -> list[str]:
    """Owner names for a refusal message, from whatever fields are present."""
    labels: list[str] = []
    for owner in owners or []:
        if isinstance(owner, str):
            labels.append(owner)
        elif isinstance(owner, dict):
            name = " ".join(
                str(owner.get(part) or "") for part in ("first_name", "last_name")
            ).strip()
            labels.append(
                name or str(owner.get("username") or owner.get("email") or owner.get("id"))
            )
    return [label for label in labels if label]


# --- Summaries -------------------------------------------------------------
#
# The preview card must read as a RECEIPT, not as a second question. The user has
# already said what they want — `ask_user` gathered intent — and the card
# authorises effect. `create chart "X"` tells them nothing they did not already
# know, so it reads as being asked the same thing twice.
#
# "Unique patients by facility, last 12 months → new chart “Patients by
# facility”" tells them what was actually built, in their own vocabulary.


def _describe_query(params: dict[str, Any], registry: Any = None) -> str:
    """What this chart measures, how it is broken down, and over what period."""
    parts: list[str] = []

    metrics = params.get("metrics") or []
    labels = [_metric_label(m, registry) for m in metrics]
    if labels:
        parts.append(" and ".join(labels[:2]) + ("…" if len(labels) > 2 else ""))

    dimensions = [d for d in (params.get("groupby") or []) if d]
    x_axis = params.get("x_axis")
    if x_axis and x_axis not in dimensions:
        dimensions.insert(0, x_axis)
    if dimensions:
        parts.append("by " + ", ".join(_column_label(d, registry) for d in dimensions[:2]))

    time_range = params.get("time_range")
    if time_range and time_range.lower() != "no filter":
        parts.append(time_range.lower())

    filters = params.get("adhoc_filters") or []
    if filters:
        parts.append(f"({len(filters)} filter{'s' if len(filters) != 1 else ''})")

    return ", ".join(parts)


def _metric_label(metric: Any, registry: Any = None) -> str:
    """A metric's plain-English label, falling back to something readable.

    A bare string is a *saved metric* name. The registry declares a label for
    every one it projects onto Superset ("Malaria positivity"), which is what an
    executive should see on the receipt card rather than `malaria_positivity`.
    """
    if isinstance(metric, str):
        declared = registry.metric(metric) if registry is not None else None
        if declared is not None and declared.label:
            return declared.label
        return metric.replace("_", " ")
    aggregate = metric.get("aggregate", "")
    column = (metric.get("column") or {}).get("column_name", "")
    if metric.get("label"):
        return str(metric["label"])
    if aggregate == "COUNT_DISTINCT":
        return f"unique {column.replace('_', ' ')}"
    return f"{aggregate.lower()} {column.replace('_', ' ')}".strip()


def _column_label(column: str, registry: Any = None) -> str:
    if registry is not None:
        declared = registry.column(column)
        if declared is not None and declared.label:
            return declared.label.lower()
    return column.replace("_", " ")


def _describe_chart(payload: dict[str, Any]) -> str:
    from lifedata_ai.semantics.loader import load_registry

    registry = load_registry()
    query = _describe_query(payload.get("params") or {}, registry)
    name = payload.get("chart_name") or "chart"
    lead = f"{query} → " if query else ""
    return f"{lead}new chart “{name}”"


# --- helpers ---------------------------------------------------------------


#: Our filter vocabulary -> Superset's `filterType`.
#:
#: `time_grain` used to fall through to the default and produce a *value select*
#: on no column at all, which renders as an empty dropdown the user cannot use.
#: Superset's own name for it is `filter_timegrain`.
_FILTER_TYPES: dict[str, str] = {
    "value": "filter_select",
    "time_range": "filter_time",
    "time_grain": "filter_timegrain",
    "numerical_range": "filter_range",
}

#: Filter types that drive the whole dashboard's time range rather than one
#: column, and so carry no `targets` entry with a column in it.
_TIME_FILTER_TYPES: frozenset[str] = frozenset({"time_range", "time_grain"})


def _native_filter_config(
    payload: dict[str, Any],
    index: int,
    chart_ids: list[int],
    tab_ids: list[str],
) -> dict[str, Any]:
    """One entry of `json_metadata.native_filter_configuration`.

    The shape was verified against a filter created by hand in the Superset 6.1.0
    UI and diffed against what this function emitted. Four things were wrong and
    each one made the filter silently useless rather than erroring:

    * `filter_type: "time_grain"` fell through to a value select on no column.
    * There was no `type: "NATIVE_FILTER"` key, which is what tells Superset this
      entry is a filter rather than a divider.
    * `chartsInScope` and `tabsInScope` were absent. Superset computes scope from
      them, and an absent list is an empty one — so the filter drove nothing.
    * `target_chart_ids` was declared in the action schema and never read, so
      scope was always "everything" in intent and "nothing" in effect.
    """
    filter_type = payload["filter_type"]
    requested_targets = [int(c) for c in (payload.get("target_chart_ids") or [])]
    in_scope = [c for c in chart_ids if not requested_targets or c in requested_targets]

    config: dict[str, Any] = {
        # Stable and derived from the name, so re-adding the same filter updates
        # it rather than stacking a second copy beside it.
        "id": f"NATIVE_FILTER-lifedata-{index}",
        # Without this Superset treats the entry as a layout divider and renders
        # nothing at all.
        "type": "NATIVE_FILTER",
        "name": payload["name"],
        "filterType": _FILTER_TYPES.get(filter_type, "filter_select"),
        "targets": [],
        "controlValues": {
            "multiSelect": bool(payload.get("multiple", True)),
            "enableEmptyFilter": False,
            "searchAllOptions": False,
        },
        "defaultDataMask": {"filterState": {}, "extraFormData": {}, "ownState": {}},
        "cascadeParentIds": [],
        "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
        # Superset reads scope from these two, and treats an absent list as
        # empty — which is why the filter drove nothing.
        "chartsInScope": in_scope,
        "tabsInScope": tab_ids,
        "description": "",
    }

    if filter_type in _TIME_FILTER_TYPES:
        # A time filter targets the dashboard, not a column. Superset still wants
        # the key present, with the dataset so it knows which time column to use.
        config["targets"] = [{"datasetId": payload["dataset_id"]}] if payload.get("dataset_id") else [{}]
    elif payload.get("dataset_id") and payload.get("column"):
        config["targets"] = [
            {"datasetId": payload["dataset_id"], "column": {"name": payload["column"]}}
        ]

    if payload.get("default_value") is not None:
        config["defaultDataMask"] = {
            "filterState": {"value": payload["default_value"]},
            "extraFormData": {},
            "ownState": {},
        }
    return config


def _layout_chart_ids(dashboard: dict[str, Any]) -> list[int]:
    """Chart ids on the dashboard, for a filter's `chartsInScope`."""
    return _existing_chart_ids(dashboard)


def _layout_tab_ids(dashboard: dict[str, Any]) -> list[str]:
    """TAB node ids, for a filter's `tabsInScope`.

    An untabbed dashboard has none, and an empty list is correct there — Superset
    reads `chartsInScope` for the widgets either way.
    """
    position = _load_json(dashboard.get("position_json")) or {}
    return sorted(
        node_id
        for node_id, node in position.items()
        if isinstance(node, dict) and node.get("type") == "TAB"
    )


#: Stands in for an id that does not exist until an earlier call in the same
#: envelope creates it. The panel replaces it with the `id` returned by the
#: first call in the sequence.
PLACEHOLDER_NEW_ID = "__NEW_ID__"


def _chart_uuids(context: ToolContext, chart_ids: list[int]) -> dict[int, dict[str, str]]:
    """Fetch uuid and name per chart, for the layout nodes.

    Superset's v2 layout resolves a widget by `meta.uuid`; without it the
    dashboard renders but the widgets are anonymous.
    """
    out: dict[int, dict[str, str]] = {}
    client = context.superset_client()
    for chart_id in chart_ids:
        try:
            chart = client.get_chart(int(chart_id))
        except SupersetError:
            continue
        out[int(chart_id)] = {
            "uuid": chart.get("uuid") or "",
            "sliceName": chart.get("slice_name") or "",
        }
    return out


# `build_position_json` used to live here: a hand-rolled layout builder that
# produced rows of two at a fixed height with no placement semantics. It was
# deleted, not moved. `validation/layout.py` already had `_op_add_chart`, which
# handles all four placements, sizes tiles and reflows the row — and
# `parse_position_json(None)` returns `empty_skeleton()`, so building a layout
# from nothing was always within its reach. The duplicate was the only reason the
# compiler's 59 tests had never run against a real dashboard build.


def _layout_meta(chart: dict[str, str] | None) -> dict[str, Any]:
    """The optional `slice_name` / `chart_uuid` an add_chart operation accepts.

    Superset's v2 layout resolves a widget by `meta.uuid`. It is not fatal to
    omit — the chart-to-dashboard relation carries membership — but without it
    the widget renders anonymously until the dashboard is next saved.
    """
    if not chart:
        return {}
    meta: dict[str, Any] = {}
    if chart.get("sliceName"):
        meta["slice_name"] = chart["sliceName"]
    if chart.get("uuid"):
        meta["chart_uuid"] = chart["uuid"]
    return meta


def _dashboards_including(
    context: ToolContext, chart_id: int, dashboard_id: Any
) -> list[int]:
    """The chart's dashboard memberships, with this dashboard added.

    `PUT /api/v1/chart/{id}` REPLACES the `dashboards` collection outright, so
    sending only the new id silently removes the chart from every other dashboard
    it was on. A chart reused across two dashboards would vanish from the first
    the moment it was added to the second, and nothing would report it.
    """
    ids = {int(dashboard_id)}
    try:
        chart = context.superset_client().get_chart(int(chart_id))
    except SupersetError:
        # Fail towards the smaller change: without the read we know only the
        # dashboard being added to, and refusing the whole action over a
        # transient read failure would be worse.
        return sorted(ids)
    for entry in chart.get("dashboards") or []:
        if isinstance(entry, dict) and entry.get("id") is not None:
            ids.add(int(entry["id"]))
        elif isinstance(entry, int):
            ids.add(entry)
    return sorted(ids)


def _existing_chart_ids(dashboard: dict[str, Any]) -> list[int]:
    """Chart ids already on a dashboard, read from its layout then its charts."""
    position = _load_json(dashboard.get("position_json")) or {}
    ids = [
        node["meta"]["chartId"]
        for node in position.values()
        if isinstance(node, dict)
        and node.get("type") == "CHART"
        and isinstance(node.get("meta"), dict)
        and node["meta"].get("chartId") is not None
    ]
    if ids:
        return ids
    return [c["id"] for c in (dashboard.get("charts") or []) if isinstance(c, dict) and c.get("id")]


def _load_json(raw: Any) -> dict[str, Any] | None:
    """Superset returns these as JSON strings, or NULL on a fresh object."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _chart_dataset_id(chart: dict[str, Any]) -> int | None:
    """Dig the real dataset id out of a chart record.

    Superset exposes this in more than one place depending on the endpoint, so
    check each rather than assuming one shape.
    """
    for key in ("datasource_id",):
        if chart.get(key) is not None:
            return int(chart[key])
    params = _load_json(chart.get("params")) or {}
    datasource = params.get("datasource")
    if isinstance(datasource, str) and "__" in datasource:
        return int(datasource.split("__", 1)[0])
    return None


#: Wire each tool's input schema to the governed action schema, so the model
#: cannot be offered a shape the validator would then reject.
def _bind_schemas() -> None:
    from lifedata_ai.validation.schemas import get_schema

    for tool_class in WRITE_TOOL_CLASSES:
        tool_class.input_schema = get_schema(tool_class.action_type)


WRITE_TOOL_CLASSES: list[type[WriteTool]] = [
    CreateChartTool,
    UpdateChartTool,
    RenameChartTool,
    CreateDashboardTool,
    RenameDashboardTool,
    AddChartToDashboardTool,
    RemoveChartFromDashboardTool,
    MoveDashboardComponentTool,
    ResizeDashboardComponentTool,
    AddNativeFilterTool,
]

_bind_schemas()

MAX_WRITES_PER_TURN = settings.LIFEDATA_AI_MAX_WRITES_PER_TURN
