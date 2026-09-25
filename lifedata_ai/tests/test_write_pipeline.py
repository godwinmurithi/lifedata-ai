"""The write pipeline: undo, the state machine, and the calls that get emitted.

Every test here corresponds to a verified production defect. The write path was
entirely untested, which is how a `TypeError` came to live on the line after a
status change that had already been saved.

Superset is mocked with respx throughout: these tools READ from Superset (to
verify a chart's dataset, to fetch a layout) but must never write to it, and the
router asserts that by having no write routes registered at all.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from django.conf import settings
from django.test import Client
from httpx import Response

from lifedata_ai import middleware
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import ActionRecord, Conversation, Snapshot
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools import ToolContext, execute
from lifedata_ai.tools.superset_write import PLACEHOLDER_NEW_ID, CreateChartTool

pytestmark = pytest.mark.django_db

SESSION_PREFIX = "superset-session-for-"
LAB_ORDERS = 3

CHART_PARAMS = {
    "viz_type": "echarts_timeseries_bar",
    "datasource": {"id": LAB_ORDERS, "type": "table"},
    "metrics": ["orders"],
    "x_axis": "facility_name",
}


# --- Fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    def _verify(cookie: str) -> SupersetIdentity:
        if not cookie.startswith(SESSION_PREFIX):
            raise SupersetError("Superset rejected this session", status_code=401)
        # `user_id` matters: Superset identifies an owner by numeric id and
        # sends no `username`, so an identity without one can never be
        # recognised as an owner. 7 matches the `identity` fixture and the
        # owner ids in the Superset fixtures below.
        return SupersetIdentity(
            username=cookie[len(SESSION_PREFIX):],
            user_id=7,
            email=f"{cookie[len(SESSION_PREFIX):]}@example.test",
            roles=["Alpha"],
        )

    monkeypatch.setattr(middleware, "_cache", middleware._IdentityCache())
    monkeypatch.setattr(middleware, "_verify_with_superset", _verify)


@pytest.fixture
def as_analyst() -> Client:
    client = Client()
    client.cookies["session"] = f"{SESSION_PREFIX}analyst"
    return client


@pytest.fixture
def superset():
    """A Superset with READ routes only.

    The absence of write routes is the assertion: any POST/PUT/DELETE from
    server-side code raises `AllMockedAssertionError` rather than passing.

    `POST /api/v1/chart/data` is the one exception, and it is not a write: it
    runs a query and returns rows, taking the query context in the body only
    because it is too big for a URL. Every chart action now runs its query
    through it before signing (see `tools/compile_check.py`), and without a
    route here that check would raise into its own safety net on every test —
    passing the suite while exercising nothing.
    """
    with respx.mock(
        base_url=settings.LIFEDATA_AI_SUPERSET_INTERNAL_URL, assert_all_called=False
    ) as router:
        router.post("/api/v1/chart/data").mock(
            return_value=Response(200, json=HEALTHY_QUERY_RESULT)
        )
        yield router


#: What Superset returns for a query that runs and finds something. Shaped like
#: the real thing — `rowcount` and `data` are what `compile_check` reads.
HEALTHY_QUERY_RESULT = {
    "result": [{"status": "success", "error": None, "rowcount": 2,
                "data": [{"orders": 812}, {"orders": 655}]}]
}


def query_refused(router, message: str = "Columns missing in dataset: ['nope']"):
    """Make the compile check see a query Superset will not run."""
    return router.post("/api/v1/chart/data").mock(
        return_value=Response(400, json={"errors": [{"message": message}]})
    )


def query_returns_nothing(router):
    """A query that is valid but matches no rows."""
    return router.post("/api/v1/chart/data").mock(
        return_value=Response(200, json={"result": [{"status": "success", "rowcount": 0,
                                                     "data": []}]})
    )


def dashboard_route(router, dashboard_id: int, **overrides: Any):
    payload = {
        "id": dashboard_id,
        "dashboard_title": "Laboratory overview",
        "position_json": None,
        "json_metadata": None,
        "charts": [],
        # No `username` — see the note in `chart_route`.
        "owners": [{"id": 7, "first_name": "Ada", "last_name": "Analyst"}],
        "changed_on": "2026-01-01T00:00:00",
    }
    payload.update(overrides)
    return router.get(f"/api/v1/dashboard/{dashboard_id}").mock(
        return_value=Response(200, json={"id": dashboard_id, "result": payload})
    )


def chart_route(router, chart_id: int, **overrides: Any):
    payload = {
        "id": chart_id,
        "slice_name": "Orders by facility",
        "viz_type": "echarts_timeseries_bar",
        "datasource_id": LAB_ORDERS,
        "params": json.dumps(
            {
                "viz_type": "echarts_timeseries_bar",
                "datasource": f"{LAB_ORDERS}__table",
                "metrics": ["orders"],
                "x_axis": "facility_name",
                "adhoc_filters": [
                    {
                        "expressionType": "SIMPLE",
                        "subject": "order_status",
                        "operator": "==",
                        "comparator": "verified",
                        "clause": "WHERE",
                    }
                ],
                "time_range": "Last 12 months",
                "row_limit": 100,
            }
        ),
        "query_context": "{}",
        "uuid": f"uuid-{chart_id}",
        "dashboards": [],
        # SUPERSET'S REAL OWNER SHAPE, verified against 6.1.0:
        #
        #     [{"first_name": "Ada", "id": 7, "last_name": "Analyst"}]
        #
        # There is NO `username` key. The fixtures here used to invent one, the
        # ownership check matched on it, and both agreed with each other while
        # disagreeing with Superset — so the check refused every real object,
        # including ones the caller plainly owned, and every test passed.
        #
        # `id` is the identity that matters. Do not add `username` back.
        "owners": [{"id": 7, "first_name": "Ada", "last_name": "Analyst"}],
        "changed_on": "2026-01-01T00:00:00",
    }
    payload.update(overrides)
    return router.get(f"/api/v1/chart/{chart_id}").mock(
        return_value=Response(200, json={"id": chart_id, "result": payload})
    )


@pytest.fixture
def context(conversation: Conversation, identity: SupersetIdentity) -> ToolContext:
    return ToolContext(
        superset_session_cookie=f"{SESSION_PREFIX}analyst",
        identity=identity,
        conversation=conversation,
    )


def propose(name: str, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    result = execute(name, arguments, context)
    assert result.ok, result.content
    return result.content


# --- update_chart merges --------------------------------------------------


class TestUpdateChartMerges:
    """The schema has always said `params` is a PARTIAL update.

    `validation/schemas/actions.py` states it in terms: the write tool "must
    read the existing params, merge these keys over them, and write the result
    back — otherwise 'make it horizontal' silently discards the chart's
    filters". It replaced wholesale. "Make that a line chart instead" is the
    most common executive follow-up there is, and it was destructive.
    """

    def test_untouched_keys_survive(self, superset, context: ToolContext) -> None:
        chart_route(superset, 7)

        envelope = propose(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    "viz_type": "echarts_timeseries_bar",
                    # `datasource` is not optional even on a partial update: the
                    # validator has no network access, so the dataset it polices
                    # is the one the payload names. Omitting it fails closed.
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "orientation": "horizontal",
                },
            },
            context,
        )

        body = envelope["calls"][0]["body"]
        params = json.loads(body["params"])
        assert params["orientation"] == "horizontal"
        # The three things "make it horizontal" used to throw away.
        assert params["time_range"] == "Last 12 months"
        assert params["metrics"] == ["orders"]
        assert len(params["adhoc_filters"]) == 1

    def test_a_viz_change_drops_the_keys_coupled_to_the_old_one(
        self, superset, context: ToolContext
    ) -> None:
        """A `groupby` left over from a bar chart makes a tile unqueryable."""
        chart_route(
            superset,
            7,
            params=json.dumps(
                {
                    "viz_type": "echarts_timeseries_bar",
                    "groupby": ["test_type"],
                    "x_axis": "facility_name",
                    "metrics": ["orders"],
                    "time_range": "Last 12 months",
                }
            ),
        )

        envelope = propose(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    "viz_type": "big_number_total",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                },
            },
            context,
        )

        params = json.loads(envelope["calls"][0]["body"]["params"])
        assert params["viz_type"] == "big_number_total"
        assert "groupby" not in params
        assert "x_axis" not in params
        # A filter is not coupled to the viz type, so it survives.
        assert params["time_range"] == "Last 12 months"

    def test_the_datasource_is_recomputed_not_inherited(
        self, superset, context: ToolContext
    ) -> None:
        """It is derived from the dataset the chart REALLY belongs to.

        The validator has no network access, so it can only police the dataset a
        payload claims. Inheriting a claim out of stored params would reopen
        exactly that hole.
        """
        chart_route(superset, 7)

        envelope = propose(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    "viz_type": "table",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                },
            },
            context,
        )

        params = json.loads(envelope["calls"][0]["body"]["params"])
        assert params["datasource"] == f"{LAB_ORDERS}__table"

    def test_a_dataset_mismatch_is_still_refused(
        self, superset, context: ToolContext
    ) -> None:
        """The dataset decides which privacy policy applies."""
        chart_route(superset, 7, datasource_id=4)  # patient geography

        result = execute(
            "update_chart",
            {"chart_id": 7, "params": {**CHART_PARAMS, "viz_type": "table"}},
            context,
        )

        assert not result.ok
        assert result.content["error"] == "dataset_mismatch"


# --- mapping Uganda -------------------------------------------------------


class TestTheUgandaChoropleth:
    """"Map it by district" must not require knowing what an ISO code is.

    Superset's Country Map joins its Uganda boundaries on the ISO 3166-2 code —
    `UG-102`, not `Kampala`. Given names it draws all 112 districts and fills
    none, which looks like a broken map and is really a failed join. The model
    asks for a map the same way it asks for a bar chart; the translation happens
    here.
    """

    def _params(self, envelope: dict) -> dict:
        return json.loads(envelope["calls"][0]["body"]["params"])

    def _map(self, superset, context: ToolContext, dataset_id: int = 4) -> dict:
        chart_route(superset, 7)
        return self._params(
            propose(
                "create_chart",
                {
                    "chart_name": "Patients by district",
                    "dataset_id": dataset_id,
                    "params": {
                        "viz_type": "country_map",
                        "datasource": {"id": dataset_id, "type": "table"},
                        "metrics": ["patients"],
                        "groupby": ["district_name"],
                    },
                },
                context,
            )
        )

    def test_the_boundary_code_is_supplied_not_asked_for(
        self, superset, context: ToolContext
    ) -> None:
        params = self._map(superset, context)

        assert params["entity"] == "district_iso"
        # The grain now lives in `entity`. Leaving groupby behind would give the
        # chart two, which Superset answers with "Duplicate column/metric labels".
        assert "groupby" not in params

    def test_the_country_is_lowercase(self, superset, context: ToolContext) -> None:
        """Not cosmetic: `"Uganda"` leaves the Country control empty.

        Superset stores and matches this control as `"uganda"`. With the
        capitalised form the chart renders "Add required control values to
        preview chart" — read back out of a chart saved through Superset's own
        UI rather than guessed at.
        """
        assert self._map(superset, context)["select_country"] == "uganda"

    def test_it_carries_a_single_metric_and_a_sequential_scheme(
        self, superset, context: ToolContext
    ) -> None:
        params = self._map(superset, context)

        assert params["metric"] == "patients"
        assert params["linear_color_scheme"] == "superset_seq_1"

    def test_changing_away_from_a_map_drops_its_controls(
        self, superset, context: ToolContext
    ) -> None:
        chart_route(
            superset,
            7,
            params=json.dumps(
                {
                    "viz_type": "country_map",
                    "entity": "district_iso",
                    "select_country": "uganda",
                    "metric": "patients",
                    "metrics": ["patients"],
                }
            ),
        )

        envelope = propose(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    "viz_type": "echarts_timeseries_bar",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "x_axis": "facility_name",
                    "metrics": ["orders"],
                },
            },
            context,
        )

        params = self._params(envelope)
        assert "entity" not in params
        assert "select_country" not in params


class TestTheGeographyPolicySurvivesMaps:
    """Adding a map must not open the thing the maps ban exists for."""

    def _refused(self, params: dict, context: ToolContext, dataset_id: int = 4):
        result = execute(
            "create_chart",
            {
                "chart_name": "probe",
                "dataset_id": dataset_id,
                "params": {**params, "datasource": {"id": dataset_id, "type": "table"}},
            },
            context,
        )
        assert not result.ok, f"expected a refusal, got {result.content}"
        return result.content

    @pytest.mark.parametrize("viz", ["deck_scatter", "deck_heatmap", "deck_hex", "mapbox"])
    def test_point_and_heat_maps_are_still_refused(
        self, superset, context: ToolContext, viz: str
    ) -> None:
        self._refused(
            {"viz_type": viz, "metrics": ["patients"], "groupby": ["district_name"]},
            context,
        )

    def test_the_iso_code_cannot_become_a_chart_axis(
        self, superset, context: ToolContext
    ) -> None:
        """`district_iso` is `role: map_key` — a join code, not a dimension.

        Nobody should be able to build a bar chart labelled "UG-102, UG-113,
        UG-204". It is reachable only through a choropleth's entity, which the
        write path sets and the model never names.
        """
        self._refused(
            {
                "viz_type": "echarts_timeseries_bar",
                "metrics": ["patients"],
                "x_axis": "district_iso",
            },
            context,
        )
        self._refused(
            {"viz_type": "table", "metrics": ["patients"], "groupby": ["district_iso"]},
            context,
        )


# --- the period actually applying ----------------------------------------


class TestTheRequestedPeriodIsEnforced:
    """`time_range` alone does nothing, and Superset says nothing about it.

    Verified against 6.1.0 by building the same tile twice and reading the
    rendered figure: with only `time_range: "Last 12 months"` it showed 14,355 —
    the all-time total — and with a TEMPORAL_RANGE adhoc filter it showed 5,922.

    Every chart the copilot built before this announced a period it did not
    apply. A tile captioned "last 12 months" showing the all-time figure is
    worse than a blank one: it is wrong in a way that looks right.
    """

    def _params(self, envelope: dict) -> dict:
        return json.loads(envelope["calls"][0]["body"]["params"])

    def _temporal(self, envelope: dict) -> list[dict]:
        return [
            f
            for f in (self._params(envelope).get("adhoc_filters") or [])
            if f.get("operator") == "TEMPORAL_RANGE"
        ]

    def test_a_period_becomes_a_filter_on_the_datasets_time_column(
        self, superset, context: ToolContext
    ) -> None:
        envelope = propose(
            "create_chart",
            {
                "chart_name": "Laboratory orders",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "big_number_total",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                    "time_range": "Last 12 months",
                },
            },
            context,
        )

        filters = self._temporal(envelope)
        assert len(filters) == 1
        assert filters[0]["subject"] == "ordered_at"
        assert filters[0]["comparator"] == "Last 12 months"
        # Kept alongside: Superset's own UI writes both, and it records what was
        # asked for independently of how it is enforced.
        assert self._params(envelope)["time_range"] == "Last 12 months"

    def test_a_categorical_axis_is_not_mistaken_for_a_time_column(
        self, superset, context: ToolContext
    ) -> None:
        """The filter must land on `ordered_at`, never on `facility_name`.

        Filtering a period against a categorical column matches nothing, which
        turns a healthy chart into an empty one.
        """
        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders by facility",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "echarts_timeseries_bar",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "x_axis": "facility_name",
                    "metrics": ["orders"],
                    "time_range": "Last 12 months",
                },
            },
            context,
        )

        assert self._temporal(envelope)[0]["subject"] == "ordered_at"

    def test_a_temporal_axis_is_used_when_it_is_genuinely_temporal(
        self, superset, context: ToolContext
    ) -> None:
        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders per month",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "echarts_timeseries_line",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "x_axis": "ordered_at",
                    "metrics": ["orders"],
                    "time_range": "Last 12 months",
                },
            },
            context,
        )

        assert self._temporal(envelope)[0]["subject"] == "ordered_at"

    def test_no_filter_means_no_filter(self, superset, context: ToolContext) -> None:
        envelope = propose(
            "create_chart",
            {
                "chart_name": "Laboratory orders, all time",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "big_number_total",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                    "time_range": "No filter",
                },
            },
            context,
        )

        assert self._temporal(envelope) == []


# --- will it actually draw? ----------------------------------------------


class TestTheQueryIsRunBeforeSigning:
    """A chart whose query Superset refuses is refused here first.

    The API accepting a chart and the chart drawing something are different
    facts. This is the check that closes the gap — see `tools/compile_check.py`.
    """

    def test_a_query_superset_refuses_stops_the_action(
        self, superset, context: ToolContext
    ) -> None:
        query_refused(superset, "Columns missing in dataset: ['not_a_column']")

        result = execute(
            "create_chart",
            {
                "chart_name": "Broken",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "echarts_timeseries_bar",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "x_axis": "facility_name",
                    "metrics": ["orders"],
                },
            },
            context,
        )

        assert not result.ok
        assert result.content["error"] == "chart_would_not_render"
        # The model needs Superset's own words to fix it, not our wrapper's.
        assert "not_a_column" in result.content["message"]
        assert "Nothing has been created" in result.content["message"]

    def test_an_empty_result_warns_rather_than_refusing(
        self, superset, context: ToolContext
    ) -> None:
        """A quiet period is a real answer, so this is said, not enforced."""
        query_returns_nothing(superset)

        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders last week",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "big_number_total",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                },
            },
            context,
        )

        assert any("render empty" in w or "no rows" in w for w in envelope["warnings"])

    def test_the_note_does_not_leak_into_the_stored_payload(
        self, superset, context: ToolContext
    ) -> None:
        """`build_calls` passes it up through the payload; `run` lifts it out."""
        query_returns_nothing(superset)

        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders last week",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "big_number_total",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                },
            },
            context,
        )

        record = ActionRecord.objects.get(id=envelope["action_id"])
        assert "_verification" not in record.payload

    def test_superset_being_unwell_does_not_block_the_build(
        self, superset, context: ToolContext
    ) -> None:
        """A 502 is the stack having a bad afternoon, not a bad chart.

        Refusing here would make "can I build a chart?" depend on "is the
        warehouse fast right now?", which is not a trade worth making for a
        check that exists to catch our own mistakes.
        """
        superset.post("/api/v1/chart/data").mock(return_value=Response(502, json={}))

        envelope = propose(
            "create_chart",
            {
                "chart_name": "Laboratory orders",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "big_number_total",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                },
            },
            context,
        )

        assert envelope["calls"]


# --- the control names each viz actually reads ---------------------------


class TestSingleFigureTilesGetTheMetricKeyTheyRead:
    """A KPI tile must be saved with `metric`, not only `metrics`.

    Found by looking at an acceptance dashboard: three tiles across the top
    reading "No data" above charts that rendered perfectly, from the same
    dataset and the same saved metrics. Every automated check passed, the API
    accepted every write, and querying the tile's own definition by hand
    returned 14,355 rows.

    `big_number_total` builds its query from the SINGULAR `metric` control. The
    pipeline wrote `metrics: ["orders"]` and nothing else, so the plugin read an
    absent key and drew nothing — no error, anywhere, because the only thing
    wrong was the name.

    These assertions are on the form_data that reaches Superset rather than on
    the params the model sent, which is the whole point: the old tests checked
    the payload was well-formed and it always was.
    """

    def _params(self, envelope: dict) -> dict:
        return json.loads(envelope["calls"][0]["body"]["params"])

    def test_a_kpi_tile_is_saved_with_the_singular_metric(
        self, superset, context: ToolContext
    ) -> None:
        envelope = propose(
            "create_chart",
            {
                "chart_name": "Laboratory orders",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "big_number_total",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                },
            },
            context,
        )

        params = self._params(envelope)
        assert params["metric"] == "orders", (
            "big_number_total reads `metric`; without it the tile renders 'No data'"
        )
        # `metrics` stays too. Superset saves both for these charts, and dropping
        # the list would break anything that reads it.
        assert params["metrics"] == ["orders"]

    def test_other_viz_types_are_left_alone(self, superset, context: ToolContext) -> None:
        """The alias is per-viz, not a blanket rewrite.

        A bar chart with one metric must NOT gain `metric` — the key means
        nothing there, and writing keys a plugin does not read is how the
        original confusion started.
        """
        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders by test",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "echarts_timeseries_bar",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "x_axis": "test_type",
                    "metrics": ["orders"],
                },
            },
            context,
        )

        assert "metric" not in self._params(envelope)

    def test_a_pie_chart_needs_it_too(self, superset, context: ToolContext) -> None:
        """Verified by building one and looking at it.

        `pie` with only `metrics` renders an empty Metric control and "Add
        required control values to preview chart" — the same silent failure the
        KPI tiles had, on the other viz type an executive is most likely to ask
        for by name.
        """
        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders by test",
                "dataset_id": LAB_ORDERS,
                "params": {
                    "viz_type": "pie",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "groupby": ["test_type"],
                    "metrics": ["orders"],
                },
            },
            context,
        )

        assert self._params(envelope)["metric"] == "orders"

    def test_a_changed_metric_replaces_the_stale_one(
        self, superset, context: ToolContext
    ) -> None:
        """The failure mode worse than "No data": the wrong number, confidently.

        `metric` is derived, never preserved. A partial update carries the
        stored params forward, so a tile that already had `metric: orders` and
        is asked for the completion rate instead would keep displaying orders
        while `metrics` said otherwise — and nothing anywhere would look wrong.
        """
        chart_route(
            superset,
            7,
            params=json.dumps(
                {
                    "viz_type": "big_number_total",
                    "metric": "orders",
                    "metrics": ["orders"],
                }
            ),
        )

        envelope = propose(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    "viz_type": "big_number_total",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["lab_completion_rate"],
                },
            },
            context,
        )

        params = self._params(envelope)
        assert params["metric"] == "lab_completion_rate"
        assert params["metrics"] == ["lab_completion_rate"]

    def test_changing_viz_type_drops_the_stale_metric(
        self, superset, context: ToolContext
    ) -> None:
        """Turning a tile into a bar chart must not leave `metric` behind.

        `_VIZ_COUPLED_KEYS` exists for exactly this: a key that is meaningful
        for one viz and misleading on the next.
        """
        chart_route(
            superset,
            7,
            params=json.dumps(
                {
                    "viz_type": "big_number_total",
                    "metric": "orders",
                    "metrics": ["orders"],
                }
            ),
        )

        envelope = propose(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    "viz_type": "echarts_timeseries_bar",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "x_axis": "test_type",
                    "metrics": ["orders"],
                },
            },
            context,
        )

        assert "metric" not in self._params(envelope)


# --- create_chart onto a dashboard ---------------------------------------


class TestCreateChartOntoADashboard:
    """A chart needs BOTH the relation and a layout node.

    `create_chart(dashboard_id=…)` emitted only the POST with
    `dashboards: [id]`, which lists the chart on the dashboard and places it
    nowhere in the layout — so it renders nothing at all. This is likely a large
    part of why the first dashboards looked wrong.
    """

    def test_both_calls_are_emitted(self, superset, context: ToolContext) -> None:
        dashboard_route(superset, 9)

        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders by facility",
                "dataset_id": LAB_ORDERS,
                "dashboard_id": 9,
                "params": CHART_PARAMS,
            },
            context,
        )

        calls = envelope["calls"]
        assert [c["method"] for c in calls] == ["POST", "PUT"]
        assert calls[0]["path"] == "/api/v1/chart/"
        assert calls[0]["body"]["dashboards"] == [9]
        assert calls[1]["path"] == "/api/v1/dashboard/9"
        assert "position_json" in calls[1]["body"]

    def test_the_layout_carries_the_placeholder_as_a_value(
        self, superset, context: ToolContext
    ) -> None:
        """Not inside an already-serialised string.

        The chart's id does not exist until the POST returns, so the layout
        needs a placeholder. Putting one inside a JSON *string* would force the
        panel to do string surgery on content the orchestrator signed, which is
        not acceptable — so the value travels as a nested object and
        `json_encode` tells the panel to stringify it last.
        """
        dashboard_route(superset, 9)

        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders by facility",
                "dataset_id": LAB_ORDERS,
                "dashboard_id": 9,
                "params": CHART_PARAMS,
            },
            context,
        )

        layout_call = envelope["calls"][1]
        assert layout_call["json_encode"] == ["position_json"]
        position = layout_call["body"]["position_json"]
        assert isinstance(position, dict), "position_json must not be pre-serialised"
        chart_nodes = [
            node for node in position.values()
            if isinstance(node, dict) and node.get("type") == "CHART"
        ]
        assert len(chart_nodes) == 1
        assert chart_nodes[0]["meta"]["chartId"] == PLACEHOLDER_NEW_ID

    def test_a_chart_with_no_dashboard_is_still_one_call(
        self, context: ToolContext
    ) -> None:
        """The common path stays a single low-risk auto-applied write."""
        envelope = propose(
            "create_chart",
            {"chart_name": "Orders", "dataset_id": LAB_ORDERS, "params": CHART_PARAMS},
            context,
        )

        assert len(envelope["calls"]) == 1
        assert envelope["risk"] == "low"

    def test_the_summary_reads_as_a_receipt(self, context: ToolContext) -> None:
        """Not "create chart X", which tells the user nothing new.

        The card authorises effect; the user already stated intent. A receipt
        says what was actually built, in their own vocabulary.
        """
        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders by facility",
                "dataset_id": LAB_ORDERS,
                "params": {**CHART_PARAMS, "time_range": "Last 12 months"},
            },
            context,
        )

        assert envelope["summary"] == (
            "Orders, by facility, last 12 months → new chart “Orders by facility”"
        )


# --- The layout compiler is actually used --------------------------------


class TestTheLayoutCompilerIsWired:
    def test_create_dashboard_uses_the_compiler(self, superset, context: ToolContext) -> None:
        """`build_position_json` is gone; the compiler builds from nothing.

        `parse_position_json(None)` returns `empty_skeleton()`, so building a
        layout from scratch was always within the compiler's reach — the
        duplicate was the only reason its 59 tests had never run against a real
        dashboard build.
        """
        for chart_id in (1, 2, 3):
            chart_route(superset, chart_id)

        envelope = propose(
            "create_dashboard",
            {"dashboard_title": "Laboratory overview", "chart_ids": [1, 2, 3]},
            context,
        )

        body = envelope["calls"][0]["body"]
        position = json.loads(body["position_json"])
        assert position["DASHBOARD_VERSION_KEY"] == "v2"
        chart_ids = sorted(
            node["meta"]["chartId"]
            for node in position.values()
            if isinstance(node, dict) and node.get("type") == "CHART"
        )
        assert chart_ids == [1, 2, 3]
        # And the relation, without which every widget renders "There is no
        # chart definition associated with this component".
        assert [c["path"] for c in envelope["calls"][1:]] == [
            "/api/v1/chart/1", "/api/v1/chart/2", "/api/v1/chart/3"
        ]
        assert envelope["calls"][1]["body"]["dashboards"] == [PLACEHOLDER_NEW_ID]

    def test_cross_filtering_is_enabled_on_a_new_dashboard(
        self, superset, context: ToolContext
    ) -> None:
        """Clicking a facility bar to filter everything else is one key."""
        chart_route(superset, 1)

        envelope = propose(
            "create_dashboard",
            {"dashboard_title": "Laboratory overview", "chart_ids": [1]},
            context,
        )

        metadata = json.loads(envelope["calls"][0]["body"]["json_metadata"])
        assert metadata["cross_filters_enabled"] is True

    @pytest.mark.parametrize(
        ("placement", "expected_rows"),
        [
            # `append` joins the last row; `kpi_row` joins the FIRST row and
            # re-evens it, which is what "add this to the tiles at the top"
            # means. Both are one row here because the dashboard starts with one.
            ("append", 1),
            ("kpi_row", 1),
            ("new_row", 2),
            ("top", 2),
        ],
    )
    def test_add_chart_honours_placement(
        self, superset, context: ToolContext, placement: str, expected_rows: int
    ) -> None:
        """`placement` was declared in this action's schema and never read.

        The model could ask for a KPI row and be silently given a row of two.
        """
        existing = json.loads(
            _compile([{"operation": "add_chart", "chart_id": 1, "placement": "new_row"}])
        )
        dashboard_route(superset, 9, position_json=json.dumps(existing))
        chart_route(superset, 5)

        envelope = propose(
            "add_chart_to_dashboard",
            {"dashboard_id": 9, "chart_id": 5, "placement": placement},
            context,
        )

        position = json.loads(envelope["calls"][0]["body"]["position_json"])
        rows = [n for n in position.values() if isinstance(n, dict) and n.get("type") == "ROW"]
        assert len(rows) == expected_rows, f"{placement} placed the chart wrongly"

        if placement == "top":
            grid = position["GRID_ID"]["children"]
            first_row = position[grid[0]]
            assert position[first_row["children"][0]]["meta"]["chartId"] == 5

    def test_kpi_row_placement_evens_the_row_it_joins(
        self, superset, context: ToolContext
    ) -> None:
        """A tile added to the KPI row must not leave the row ragged."""
        existing = json.loads(
            _compile(
                [
                    {"operation": "add_chart", "chart_id": 1, "placement": "new_row"},
                    {"operation": "kpi_row", "chart_ids": [1]},
                ]
            )
        )
        dashboard_route(superset, 9, position_json=json.dumps(existing))
        chart_route(superset, 5)

        envelope = propose(
            "add_chart_to_dashboard",
            {"dashboard_id": 9, "chart_id": 5, "placement": "kpi_row"},
            context,
        )

        position = json.loads(envelope["calls"][0]["body"]["position_json"])
        widths = {
            node["meta"]["chartId"]: node["meta"]["width"]
            for node in position.values()
            if isinstance(node, dict) and node.get("type") == "CHART"
        }
        assert widths == {1: 6, 5: 6}

    def test_adding_a_chart_keeps_its_other_dashboards(
        self, superset, context: ToolContext
    ) -> None:
        """`PUT /api/v1/chart/{id}` REPLACES the dashboards collection.

        Sending only the new id silently removes the chart from every other
        dashboard it was on — so a chart reused across two dashboards vanishes
        from the first the moment it is added to the second, with nothing
        reported anywhere.
        """
        dashboard_route(superset, 9)
        chart_route(superset, 5, dashboards=[{"id": 4}, {"id": 6}])

        envelope = propose(
            "add_chart_to_dashboard", {"dashboard_id": 9, "chart_id": 5}, context
        )

        relation = envelope["calls"][1]
        assert relation["path"] == "/api/v1/chart/5"
        assert relation["body"]["dashboards"] == [4, 6, 9]

    def test_removing_a_chart_preserves_the_rest_of_the_layout(
        self, superset, context: ToolContext
    ) -> None:
        """`remove_chart`, not "rebuild the layout without it".

        Rebuilding threw away tabs, manual column layouts and every width the
        user had chosen — a removal that silently reformats the rest of the
        dashboard is a far bigger change than the one that was asked for.
        """
        existing = json.loads(
            _compile(
                [
                    {"operation": "add_chart", "chart_id": 1, "placement": "new_row"},
                    {"operation": "add_chart", "chart_id": 2, "placement": "append"},
                    {"operation": "add_chart", "chart_id": 3, "placement": "new_row"},
                    {"operation": "set_width", "chart_id": 3, "columns": 12},
                ]
            )
        )
        dashboard_route(superset, 9, position_json=json.dumps(existing))

        envelope = propose(
            "remove_chart_from_dashboard", {"dashboard_id": 9, "chart_id": 1}, context
        )

        position = json.loads(envelope["calls"][0]["body"]["position_json"])
        widths = {
            node["meta"]["chartId"]: node["meta"]["width"]
            for node in position.values()
            if isinstance(node, dict) and node.get("type") == "CHART"
        }
        assert set(widths) == {2, 3}
        assert widths[3] == 12, "the chosen full width was rebuilt away"


def _compile(operations: list[dict[str, Any]]) -> str:
    from lifedata_ai.validation.layout import compile_operations

    return compile_operations(None, operations)


# --- The audit record ----------------------------------------------------


def test_the_action_record_names_the_object_type(context: ToolContext) -> None:
    """Never set on any row ever written, and deletion-as-inverse needs it."""
    envelope = propose(
        "create_chart",
        {"chart_name": "Orders", "dataset_id": LAB_ORDERS, "params": CHART_PARAMS},
        context,
    )

    record = ActionRecord.objects.get(id=envelope["action_id"])
    assert record.superset_object_type == Snapshot.ObjectType.CHART


# --- Undo ----------------------------------------------------------------


class TestUndo:
    """Undo used to mark the original action UNDONE and then 500.

    Verified line ordering: `action.status = UNDONE`, `action.save(...)`, then
    `build_envelope(method=…, path=…, body=…)` — which takes `calls` and raised
    a TypeError. So EVERY `POST /api/undo/` recorded a change as reversed and
    then failed to reverse it. The record lied, which is worse than the crash.
    """

    @pytest.fixture
    def executed_action(self, conversation: Conversation) -> ActionRecord:
        action = ActionRecord.objects.create(
            conversation=conversation,
            action_type="update_chart",
            payload={"chart_id": 7},
            validation_result={"ok": True},
            risk=ActionRecord.Risk.LOW,
            status=ActionRecord.Status.EXECUTED,
            superset_object_type=Snapshot.ObjectType.CHART,
            superset_object_id="7",
        )
        Snapshot.objects.create(
            action=action,
            object_type=Snapshot.ObjectType.CHART,
            object_id="7",
            state={"slice_name": "Before", "params": "{}"},
            changed_on="2026-01-01T00:00:00",
        )
        return action

    def test_undo_returns_an_executable_envelope(
        self, superset, as_analyst: Client, executed_action: ActionRecord
    ) -> None:
        chart_route(superset, 7)

        response = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(executed_action.id)}),
            content_type="application/json",
        )

        assert response.status_code == 200
        body = response.json()
        # `calls`, not `preview`. The panel's executeAction reads `action.calls`
        # and returns immediately when it is empty, so the old `preview` key
        # meant undo silently did nothing even once the TypeError was fixed.
        assert body["calls"][0]["method"] == "PUT"
        assert body["calls"][0]["path"] == "/api/v1/chart/7"
        assert body["calls"][0]["body"]["slice_name"] == "Before"
        assert body["envelope"]

    def test_the_original_is_not_marked_undone_until_it_is(
        self, superset, as_analyst: Client, executed_action: ActionRecord
    ) -> None:
        """THE BUG. Proposing an undo changes nothing about the original."""
        chart_route(superset, 7)

        as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(executed_action.id)}),
            content_type="application/json",
        )

        executed_action.refresh_from_db()
        assert executed_action.status == ActionRecord.Status.EXECUTED

    def test_the_original_becomes_undone_when_the_browser_reports_success(
        self, superset, as_analyst: Client, executed_action: ActionRecord
    ) -> None:
        chart_route(superset, 7)
        undo_id = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(executed_action.id)}),
            content_type="application/json",
        ).json()["action_id"]

        response = as_analyst.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": undo_id, "status": "executed"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.json()["undone_action_id"] == str(executed_action.id)
        executed_action.refresh_from_db()
        assert executed_action.status == ActionRecord.Status.UNDONE

    def test_a_failed_undo_leaves_the_original_executed(
        self, superset, as_analyst: Client, executed_action: ActionRecord
    ) -> None:
        """Only the browser knows whether the restore actually happened."""
        chart_route(superset, 7)
        undo_id = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(executed_action.id)}),
            content_type="application/json",
        ).json()["action_id"]

        as_analyst.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": undo_id, "status": "failed"}),
            content_type="application/json",
        )

        executed_action.refresh_from_db()
        assert executed_action.status == ActionRecord.Status.EXECUTED

    def test_a_stale_snapshot_is_refused(
        self, superset, as_analyst: Client, executed_action: ActionRecord
    ) -> None:
        """Silently overwriting someone else's later edit is worse than declining."""
        chart_route(superset, 7, changed_on="2026-06-01T12:00:00")

        response = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(executed_action.id)}),
            content_type="application/json",
        )

        assert response.status_code == 409
        assert response.json()["error"] == "stale_snapshot"
        executed_action.refresh_from_db()
        assert executed_action.status == ActionRecord.Status.EXECUTED

    def test_restoring_a_creation_says_there_is_nothing_to_restore(
        self, as_analyst: Client, conversation: Conversation
    ) -> None:
        """Creating an object has no prior state — delete it instead."""
        created = ActionRecord.objects.create(
            conversation=conversation,
            action_type="create_chart",
            payload={},
            risk=ActionRecord.Risk.LOW,
            status=ActionRecord.Status.EXECUTED,
        )

        response = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(created.id), "mode": "restore"}),
            content_type="application/json",
        )

        assert response.status_code == 409
        assert response.json()["error"] == "not_undoable"
        assert "delete it instead" in response.json()["message"]


class TestDeletionAsInverse:
    """"I didn't want that" for something the copilot just created.

    A DISTINCT affordance, never Undo quietly redefined. Undo restores state;
    deleting is destructive and is itself unundoable, and the chart may already
    be on a colleague's dashboard.

    **`delete_chart` and `delete_dashboard` stay out of the tool registry.** The
    model can never propose a deletion — strictly better than adding two
    destructive tools it might reach for. The human gets the affordance; the
    model does not get the capability.
    """

    @pytest.fixture
    def created_chart(self, conversation: Conversation) -> ActionRecord:
        return ActionRecord.objects.create(
            conversation=conversation,
            action_type="create_chart",
            payload={},
            risk=ActionRecord.Risk.LOW,
            status=ActionRecord.Status.EXECUTED,
            superset_object_type=Snapshot.ObjectType.CHART,
            superset_object_id="42",
        )

    def test_a_creation_defaults_to_delete(
        self, superset, as_analyst: Client, created_chart: ActionRecord
    ) -> None:
        """There is no other meaningful inverse, so no mode need be stated."""
        chart_route(superset, 42)

        response = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(created_chart.id)}),
            content_type="application/json",
        )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "delete"
        assert body["destructive"] is True
        assert body["calls"] == [
            {"method": "DELETE", "path": "/api/v1/chart/42", "body": {}}
        ]
        # The same shape a write tool returns, so the panel needs no special case.
        assert body["envelope"]

    def test_deletion_is_always_previewed(
        self, superset, as_analyst: Client, created_chart: ActionRecord
    ) -> None:
        """Unundoable, so it is confirmed however small it looks."""
        chart_route(superset, 42)

        body = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(created_chart.id)}),
            content_type="application/json",
        ).json()

        record = ActionRecord.objects.get(id=body["action_id"])
        assert record.risk == ActionRecord.Risk.PREVIEW

    def test_ownership_is_rechecked_at_deletion_time(
        self, superset, as_analyst: Client, created_chart: ActionRecord
    ) -> None:
        """The record may be hours old and the object may have changed hands."""
        chart_route(superset, 42, owners=[{"username": "someone_else"}])

        response = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(created_chart.id)}),
            content_type="application/json",
        )

        assert response.status_code == 403
        assert response.json()["error"] == "not_owner"

    def test_a_deleted_dashboard_says_its_charts_are_kept(
        self, superset, as_analyst: Client, conversation: Conversation
    ) -> None:
        """The one thing a user needs to know before pressing it."""
        created = ActionRecord.objects.create(
            conversation=conversation,
            action_type="create_dashboard",
            payload={},
            risk=ActionRecord.Risk.PREVIEW,
            status=ActionRecord.Status.EXECUTED,
            superset_object_type=Snapshot.ObjectType.DASHBOARD,
            superset_object_id="9",
        )
        dashboard_route(superset, 9)

        body = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(created.id)}),
            content_type="application/json",
        ).json()

        assert "charts on it are kept" in body["summary"]
        assert body["calls"][0]["path"] == "/api/v1/dashboard/9"

    def test_an_action_that_created_nothing_cannot_be_deleted(
        self, as_analyst: Client, conversation: Conversation
    ) -> None:
        """`superset_object_type` was empty on every row ever written before this."""
        rename = ActionRecord.objects.create(
            conversation=conversation,
            action_type="rename_chart",
            payload={},
            risk=ActionRecord.Risk.LOW,
            status=ActionRecord.Status.EXECUTED,
        )

        response = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(rename.id), "mode": "delete"}),
            content_type="application/json",
        )

        assert response.status_code == 409
        assert response.json()["error"] == "not_deletable"

    def test_an_unknown_mode_is_refused(
        self, as_analyst: Client, created_chart: ActionRecord
    ) -> None:
        response = as_analyst.post(
            "/lifedata-ai/api/undo/",
            data=json.dumps({"action_id": str(created_chart.id), "mode": "obliterate"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_mode"

    def test_the_model_cannot_propose_a_deletion(self) -> None:
        """The capability is the human's, not the model's.

        Adding `delete_chart`/`delete_dashboard` as tools would have been the
        obvious implementation and is strictly worse: the copilot would then be
        able to reach for deletion on its own reading of "get rid of that".
        """
        from lifedata_ai.tools import build_registry
        from lifedata_ai.validation.schemas import ALLOWED_ACTION_TYPES

        registry = build_registry()
        for name in ("delete_chart", "delete_dashboard"):
            assert registry.get(name) is None
            assert name not in ALLOWED_ACTION_TYPES


# --- The state machine ---------------------------------------------------


class TestTheStateMachine:
    """Statuses used to be assigned wherever a view felt like it."""

    @pytest.fixture
    def proposed(self, conversation: Conversation) -> ActionRecord:
        return ActionRecord.objects.create(
            conversation=conversation,
            action_type="create_chart",
            payload={},
            risk=ActionRecord.Risk.PREVIEW,
            status=ActionRecord.Status.PROPOSED,
        )

    def test_a_settled_record_refuses_a_late_report(
        self, as_analyst: Client, proposed: ActionRecord
    ) -> None:
        proposed.transition_to(ActionRecord.Status.CANCELLED)

        response = as_analyst.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": str(proposed.id), "status": "executed"}),
            content_type="application/json",
        )

        assert response.status_code == 409
        proposed.refresh_from_db()
        assert proposed.status == ActionRecord.Status.CANCELLED

    def test_a_failed_action_may_still_be_retried_into_success(
        self, as_analyst: Client, proposed: ActionRecord
    ) -> None:
        """The panel can genuinely retry, and the record must be able to say so."""
        proposed.transition_to(ActionRecord.Status.FAILED)

        response = as_analyst.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps(
                {"action_id": str(proposed.id), "status": "executed", "superset_object_id": 42}
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        proposed.refresh_from_db()
        assert proposed.status == ActionRecord.Status.EXECUTED
        assert proposed.superset_object_id == "42"

    def test_undone_is_terminal(self, proposed: ActionRecord) -> None:
        proposed.transition_to(ActionRecord.Status.EXECUTED)
        assert proposed.transition_to(ActionRecord.Status.UNDONE)
        assert not proposed.transition_to(ActionRecord.Status.EXECUTED)

    def test_undo_is_not_offered_for_an_undo(
        self, as_analyst: Client, conversation: Conversation
    ) -> None:
        """An undo of an undo is a loop nobody wants to reason about."""
        undo_record = ActionRecord.objects.create(
            conversation=conversation,
            action_type="undo_update_chart",
            payload={},
            risk=ActionRecord.Risk.LOW,
            status=ActionRecord.Status.PROPOSED,
        )
        Snapshot.objects.create(
            action=undo_record,
            object_type=Snapshot.ObjectType.CHART,
            object_id="7",
            state={},
        )

        response = as_analyst.post(
            "/lifedata-ai/api/action-result/",
            data=json.dumps({"action_id": str(undo_record.id), "status": "executed"}),
            content_type="application/json",
        )

        assert response.json()["undoable"] is False


# --- The in-flight guard -------------------------------------------------


def test_a_second_turn_on_one_conversation_gets_409(
    as_analyst: Client, conversation: Conversation
) -> None:
    """The panel's post-Apply continuation and a message the user typed a
    moment earlier can both reach `run_turn`, and both would try to resolve the
    same pending tool call. Making the interleaving impossible is cheaper than
    reasoning about it."""
    from lifedata_ai.agent import inflight

    assert inflight.claim(conversation)

    response = as_analyst.post(
        "/lifedata-ai/api/chat/",
        data=json.dumps({"message": "hello", "conversation_id": str(conversation.pk)}),
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["error"] == "turn_in_progress"


def test_a_stale_claim_does_not_wedge_the_conversation(
    conversation: Conversation, settings
) -> None:
    """A worker that dies mid-turn must not lock a conversation for ever."""
    import datetime as dt

    from django.utils import timezone

    from lifedata_ai.agent import inflight

    assert inflight.claim(conversation)
    Conversation.objects.filter(pk=conversation.pk).update(
        turn_started_at=timezone.now() - dt.timedelta(hours=2)
    )

    assert inflight.claim(conversation), "an abandoned claim was never released"


# --- Ownership -----------------------------------------------------------


class TestOwnership:
    """The copilot creates and modifies your own things, and nothing else.

    Before this the only control was a risk classification: a non-owner action
    was routed to a preview card and then proceeded as soon as the user
    confirmed it. `update_chart` had no ownership check at all.

    The hard refusal lives in the write tool, not the validator, for the reason
    `UpdateChartTool` already gives about the dataset check: the validator is
    deliberately network-free and can only police what a payload *claims*.
    """

    def test_someone_elses_chart_cannot_be_updated(
        self, superset, context: ToolContext
    ) -> None:
        chart_route(superset, 7, owners=[{"id": 99, "first_name": "Bwire", "last_name": "Okello"}])

        result = execute(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    "viz_type": "table",
                    "datasource": {"id": LAB_ORDERS, "type": "table"},
                    "metrics": ["orders"],
                },
            },
            context,
        )

        assert not result.ok
        assert result.content["error"] == "not_owner"
        # No record is written for an action that was never proposed.
        assert not ActionRecord.objects.exists()

    def test_someone_elses_dashboard_cannot_be_rearranged(
        self, superset, context: ToolContext
    ) -> None:
        dashboard_route(superset, 9, owners=[{"id": 99, "first_name": "Bwire", "last_name": "Okello"}])

        result = execute(
            "resize_dashboard_component",
            {"operation": "make_full_width", "dashboard_id": 9, "chart_id": 1},
            context,
        )

        assert not result.ok
        assert result.content["error"] == "not_owner"

    def test_ownership_fails_closed_when_superset_cannot_be_read(
        self, superset, context: ToolContext
    ) -> None:
        """The one case where the lookup breaks must not be the one where the
        check is skipped."""
        superset.get("/api/v1/chart/7").mock(return_value=Response(502, json={}))

        result = execute("rename_chart", {"chart_id": 7, "new_name": "New"}, context)

        assert not result.ok
        assert result.content["error"] == "ownership_unknown"

    def test_creating_a_chart_needs_no_owner_check(self, context: ToolContext) -> None:
        """There is nothing to own yet, and no Superset read is made."""
        result = execute(
            "create_chart",
            {"chart_name": "Orders", "dataset_id": LAB_ORDERS, "params": CHART_PARAMS},
            context,
        )

        assert result.ok

    def test_creating_a_chart_ONTO_a_dashboard_checks_that_dashboard(
        self, superset, context: ToolContext
    ) -> None:
        """The exception the default would otherwise miss.

        `create_chart` declares no `snapshot_target`, and `ownership_target`
        defaults to it — correctly, because the chart is new. But once this
        action also rewrites a dashboard's layout (§2.4's fix), it modifies an
        existing object someone else may own. Without the override it would
        bypass both the ownership check AND the dashboard snapshot: exactly the
        shape of hole a "creations need no check" rule leaves behind.
        """
        dashboard_route(superset, 9, owners=[{"id": 99, "first_name": "Bwire", "last_name": "Okello"}])

        result = execute(
            "create_chart",
            {
                "chart_name": "Orders",
                "dataset_id": LAB_ORDERS,
                "dashboard_id": 9,
                "params": CHART_PARAMS,
            },
            context,
        )

        assert not result.ok
        assert result.content["error"] == "not_owner"
        assert "dashboard" in result.content["details"]["object_type"]

    def test_that_case_also_snapshots_the_dashboard(
        self, superset, context: ToolContext
    ) -> None:
        """Undoing "put a new chart on my dashboard" restores the LAYOUT.

        The chart has no prior state; the dashboard's layout does. Without this
        the undo would find no snapshot and refuse.
        """
        # `position_json: None` is the NORMAL state of a freshly created
        # dashboard in Superset 6.1.0, and `parse_position_json` treats it as a
        # first-class case rather than an error.
        dashboard_route(superset, 9)

        envelope = propose(
            "create_chart",
            {
                "chart_name": "Orders",
                "dataset_id": LAB_ORDERS,
                "dashboard_id": 9,
                "params": CHART_PARAMS,
            },
            context,
        )

        snapshot = Snapshot.objects.get(action_id=envelope["action_id"])
        assert snapshot.object_type == Snapshot.ObjectType.DASHBOARD
        assert snapshot.object_id == "9"


def test_every_write_tool_declares_its_targets_deliberately():
    """§10.2's structural check: a new tool fails this until someone thinks.

    The table is the point. `ownership_target` defaults to `snapshot_target`,
    which is right for every tool that modifies an existing object — but a tool
    author who does not think about it gets the default silently. Listing the
    expected answer per tool means adding tool #11 breaks this test rather than
    quietly shipping without a check.
    """
    from lifedata_ai.tools.superset_write import WRITE_TOOL_CLASSES

    #: tool name -> (snapshot target type, ownership target type), or None.
    #:
    #: `create_chart` appears twice on purpose. Without a `dashboard_id` it
    #: creates something that does not exist yet: nothing to own, nothing to
    #: snapshot. WITH one it also rewrites an existing dashboard's layout — so
    #: both targets become that dashboard, and that override is the single
    #: exception to "ownership defaults to snapshot".
    expected = {
        "create_chart": ("dashboard", "dashboard"),
        "update_chart": ("chart", "chart"),
        "rename_chart": ("chart", "chart"),
        "create_dashboard": (None, None),
        "rename_dashboard": ("dashboard", "dashboard"),
        "add_chart_to_dashboard": ("dashboard", "dashboard"),
        "remove_chart_from_dashboard": ("dashboard", "dashboard"),
        "move_dashboard_component": ("dashboard", "dashboard"),
        "resize_dashboard_component": ("dashboard", "dashboard"),
        "add_native_filter": ("dashboard", "dashboard"),
    }
    payload = {"chart_id": 1, "dashboard_id": 2}

    covered = set()
    for tool_class in WRITE_TOOL_CLASSES:
        tool = tool_class()
        covered.add(tool.name)
        assert tool.name in expected, (
            f"{tool.name} is not in the coverage table. Decide what it owns and "
            "snapshots, then add its row."
        )
        want_snapshot, want_ownership = expected[tool.name]
        snapshot = tool.snapshot_target(payload)
        ownership = tool.ownership_target(payload)
        assert (snapshot[0] if snapshot else None) == want_snapshot, tool.name
        assert (ownership[0] if ownership else None) == want_ownership, tool.name

    assert covered == set(expected), f"stale rows: {set(expected) - covered}"

    # The other half of the create_chart row: no dashboard, no targets.
    standalone = CreateChartTool()
    assert standalone.snapshot_target({"dataset_id": 3}) is None
    assert standalone.ownership_target({"dataset_id": 3}) is None


class TestCreationsAreOwned:
    """Ownership enforcement is worthless if creations end up unowned.

    `POST /api/v1/chart/` and `POST /api/v1/dashboard/` leave `owners` EMPTY
    unless the body names one — so the copilot built a six-chart dashboard and
    was then refused permission to lay it out or filter it, by its own ownership
    check, against an object it had just created.

    Found by running the acceptance script in a browser: `owners=[None]` on
    everything the copilot had ever made, and the flagship "build me a
    dashboard" flow ending in two red failures and an apology.
    """

    def test_a_created_chart_names_its_owner(self, context: ToolContext) -> None:
        envelope = propose(
            "create_chart",
            {"chart_name": "Orders", "dataset_id": LAB_ORDERS, "params": CHART_PARAMS},
            context,
        )

        assert envelope["calls"][0]["body"]["owners"] == [context.identity.user_id]

    def test_a_created_dashboard_names_its_owner(
        self, superset, context: ToolContext
    ) -> None:
        chart_route(superset, 1)

        envelope = propose(
            "create_dashboard",
            {"dashboard_title": "Laboratory overview", "chart_ids": [1]},
            context,
        )

        assert envelope["calls"][0]["body"]["owners"] == [context.identity.user_id]

    def test_the_owner_is_the_caller_not_a_service_account(
        self, conversation: Conversation
    ) -> None:
        """Whoever is asking. The panel executes with their session, so anything
        else would be a lie the audit trail then repeats."""
        other = SupersetIdentity(username="bwire", user_id=42, roles=["Alpha"])
        context = ToolContext(
            superset_session_cookie=f"{SESSION_PREFIX}bwire",
            identity=other,
            conversation=conversation,
        )

        envelope = propose(
            "create_chart",
            {"chart_name": "Orders", "dataset_id": LAB_ORDERS, "params": CHART_PARAMS},
            context,
        )

        assert envelope["calls"][0]["body"]["owners"] == [42]

    def test_an_unknown_user_id_omits_owners_rather_than_failing(
        self, conversation: Conversation
    ) -> None:
        """Superset's own default is no worse than before this existed.

        A missing user id is an identity problem to fix in the middleware, not a
        reason to refuse a chart the user asked for.
        """
        anonymous = SupersetIdentity(username="analyst", user_id=None, roles=["Alpha"])
        context = ToolContext(
            superset_session_cookie=f"{SESSION_PREFIX}analyst",
            identity=anonymous,
            conversation=conversation,
        )

        envelope = propose(
            "create_chart",
            {"chart_name": "Orders", "dataset_id": LAB_ORDERS, "params": CHART_PARAMS},
            context,
        )

        assert "owners" not in envelope["calls"][0]["body"]

    def test_an_unowned_object_is_refused_with_its_own_message(
        self, superset, context: ToolContext
    ) -> None:
        """Nobody's is a different problem from someone else's.

        The fix for an unowned object is to claim it, not to go and find a
        colleague — so the two refusals must not read the same. Seeded and
        hand-built objects land here, as does everything the copilot created
        before creations were owned.
        """
        chart_route(superset, 7, owners=[])

        result = execute("rename_chart", {"chart_id": 7, "new_name": "New"}, context)

        assert not result.ok
        assert result.content["details"]["reason"] == "unowned"
        assert "add yourself as an owner" in result.content["message"]


class TestTheOwnerShape:
    """Superset identifies an owner by numeric id and sends NO username.

    Verified against 6.1.0. `GET /api/v1/dashboard/{id}` returns

        [{"first_name": "Life", "id": 1, "last_name": "Admin"}]

    and the chart serialiser adds `email` but still no username.

    Every ownership comparison in this codebase matched on `owner["username"]`,
    found nothing, and refused EVERY object — including ones the caller plainly
    owned. The copilot built a dashboard and was then refused permission to put
    a filter on it, by its own check.

    The tests did not catch it because the FIXTURES invented `{"username":
    "analyst"}`, a shape Superset does not produce. Asserting against a made-up
    fixture only proves the code agrees with the fixture. These cases pin the
    real shapes.
    """

    @pytest.mark.parametrize(
        ("owners", "expected", "why"),
        [
            ([{"id": 7, "first_name": "Ada", "last_name": "Analyst"}], True,
             "the dashboard serialiser: id only"),
            ([{"id": 7, "first_name": "Ada", "last_name": "Analyst",
               "email": "analyst@example.test"}], True,
             "the chart serialiser: id and email"),
            ([{"id": 99, "first_name": "Bwire", "last_name": "Okello"}], False,
             "someone else's id"),
            ([], False, "unowned"),
            (None, False, "absent"),
            # Tolerated shapes: a deployment with a different auth backend, and
            # the plain-string form `ValidationContext` documents.
            ([{"username": "analyst"}], True, "a serialiser that does send username"),
            ([{"email": "analyst@example.test"}], True, "email only"),
            (["analyst"], True, "a plain username string"),
        ],
    )
    def test_ownership_is_recognised_from_what_superset_actually_sends(
        self, identity: SupersetIdentity, owners, expected: bool, why: str
    ) -> None:
        from lifedata_ai.tools.superset_write import owns

        assert owns(owners, identity) is expected, why

    def test_an_identity_with_no_user_id_falls_back_to_name_and_email(self) -> None:
        """Not every deployment populates the numeric id."""
        from lifedata_ai.tools.superset_write import owns

        nameless = SupersetIdentity(
            username="analyst", user_id=None, email="analyst@example.test"
        )

        assert owns([{"username": "analyst"}], nameless)
        assert owns([{"email": "analyst@example.test"}], nameless)
        # And with only an id to go on, it cannot claim ownership — fail closed.
        assert not owns([{"id": 7}], nameless)

    def test_the_risk_rule_agrees_with_the_hard_refusal(
        self, identity: SupersetIdentity
    ) -> None:
        """Two layers that disagree about who owns something is worse than one.

        The validator classifies risk and the write tool refuses outright. If
        they read owners differently, the preview card and the refusal tell the
        user different stories.
        """
        from lifedata_ai.validation.validator import RISK_LOW, ValidationContext, validate_action

        superset_shape = [{"id": 7, "first_name": "Ada", "last_name": "Analyst"}]
        result = validate_action(
            "add_chart_to_dashboard",
            {"dashboard_id": 9, "chart_id": 5},
            context=ValidationContext(
                identity_username=identity.username,
                identity_user_id=identity.user_id,
                dashboard_owners=superset_shape,
            ),
        )

        assert result.risk == RISK_LOW, result.reasons["risk"]
