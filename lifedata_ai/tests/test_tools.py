"""Tests for the tool layer: registry, audited execution, and every read tool.

Three properties carry the weight here, because each one fails silently rather
than loudly if it regresses:

* **The read-only guard is structural.** Phase 1 registers no write tools, and
  `execute()` refuses a write-mode tool even if one somehow reaches the
  registry. A test that only checked `build_registry()` would miss the second
  half.
* **Every tool call is audited.** The Phase 1 acceptance criterion is that all
  tool calls appear in `ToolCallLog` with the username — on the failure paths
  especially, since those are the ones worth reading afterwards.
* **Nothing row-level reaches the model.** `get_chart_data` caps, truncates and
  refuses; and a truncated result must never describe itself as complete.

Superset is mocked with respx throughout — these tests never touch the live
instance, and never reach api.anthropic.com at all.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import respx
import yaml
from django.conf import settings
from django.db import connection

from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import Conversation, ToolCallLog
from lifedata_ai.semantics.loader import REGISTRY_PATH, load_registry
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode
from lifedata_ai.tools.lifedata import (
    ExplainIndicatorTool,
    GetFormSchemaTool,
    ListFormsTool,
    RecommendDatasetTool,
)
from lifedata_ai.tools.registry import ToolRegistry, build_registry, execute
from lifedata_ai.tools.superset_read import (
    GetChartDataTool,
    GetChartTool,
    GetDashboardTool,
    GetDatasetTool,
    GetPageContextTool,
    ListDatasetsTool,
)

# --- Fixtures and helpers ------------------------------------------------

#: The read tools, in prompt order. Asserted exactly: an accidental addition
#: changes the prompt prefix (and so the prompt cache), and an accidental
#: removal silently narrows what the copilot can do.
EXPECTED_READ_TOOL_NAMES = [
    "list_databases",
    "list_datasets",
    "get_dataset",
    "list_charts",
    "get_chart",
    "list_dashboards",
    "get_dashboard",
    "get_chart_data",
    "get_page_context",
    "explain_indicator",
    "recommend_dataset",
    "review_dashboard_design",
    "ask_user",
    "list_forms",
    "get_form_schema",
]

#: The write tools. Each one PROPOSES a change and returns a signed envelope;
#: none of them touches Superset (spec §1). They are registered — the copilot is
#: no longer read-only — so the assertion is that they are all present, not that
#: they are absent.
EXPECTED_WRITE_TOOL_NAMES = [
    "create_chart",
    "update_chart",
    "rename_chart",
    "create_dashboard",
    "rename_dashboard",
    "add_chart_to_dashboard",
    "remove_chart_from_dashboard",
    "move_dashboard_component",
    "resize_dashboard_component",
    "add_native_filter",
]

#: Dropped, and asserted absent so they cannot creep back without a decision.
#:
#: `refresh_chart` permanently set `cache_timeout: 0` — a persistent
#: configuration change mislabelled as a refresh, which no executive asks for.
#: `apply_dashboard_theme` wrote `json_metadata["theme"]`, but Superset 6 exposes
#: theme *objects* at /api/v1/theme/ and nothing ever created the three registry
#: ids, so it was almost certainly a silent no-op.
DROPPED_TOOL_NAMES = ["refresh_chart", "apply_dashboard_theme"]

EXPECTED_TOOL_NAMES = EXPECTED_READ_TOOL_NAMES + EXPECTED_WRITE_TOOL_NAMES

#: rpt_lab_orders in semantics/registry.yaml — used wherever a test needs a
#: dataset the semantics registry actually knows about.
LAB_ORDERS_DATASET_ID = 3


def _identity(**overrides: Any) -> SupersetIdentity:
    fields: dict[str, Any] = {
        "username": "analyst",
        "user_id": 7,
        "first_name": "Ada",
        "last_name": "Analyst",
        "email": "analyst@example.test",
        "roles": ["Gamma"],
    }
    fields.update(overrides)
    return SupersetIdentity(**fields)


def _context(
    conversation: Conversation | None = None,
    page_context: dict[str, Any] | None = None,
    identity: SupersetIdentity | None = None,
) -> ToolContext:
    return ToolContext(
        superset_session_cookie="session-cookie-value",
        identity=identity or _identity(),
        conversation=conversation,
        page_context=page_context,
    )


def _conversation() -> Conversation:
    return Conversation.objects.create(superset_username="analyst", title="Test turn")


@contextmanager
def _superset():
    """respx router bound to whatever Superset URL this environment configures.

    `assert_all_called=False` because several tests deliberately prove that a
    route was *not* called — validation happening before the request is the
    point of those tests.
    """
    with respx.mock(
        base_url=settings.LIFEDATA_AI_SUPERSET_INTERNAL_URL,
        assert_all_called=False,
    ) as router:
        yield router


def _raw_registry() -> dict[str, Any]:
    """registry.yaml parsed straight from disk, bypassing the loader.

    Comparing tool output against this rather than against a copy pasted into
    the test is what makes governed-definition drift a test failure.
    """
    return yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))


def _raw_indicator(name: str) -> dict[str, Any]:
    return next(i for i in _raw_registry()["indicators"] if i["name"] == name)


def _raw_dataset(name: str) -> dict[str, Any]:
    return next(d for d in _raw_registry()["datasets"] if d["name"] == name)


# --- Dummy tools used to exercise execute()'s guard rails ----------------


class _EchoTool(Tool):
    """A trivially successful read tool."""

    name = "echo"
    description = "Echo a value back. Test fixture only."
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"value": {"type": "string", "description": "Anything."}},
        "required": ["value"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, value: str) -> dict[str, Any]:
        return {"echoed": value}


class _WriteTool(Tool):
    """A write-mode tool, standing in for the real proposal pipeline.

    It returns an envelope-shaped payload and touches nothing: that IS what a
    write tool does (spec §1). It exists here to prove `execute()` audits the
    call as `mode="write"`.
    """

    name = "dummy_write"
    description = "Proposes a change and returns an envelope. Test fixture only."
    mode = ToolMode.WRITE
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext) -> dict[str, Any]:
        return {"action_id": "dummy", "action_type": "dummy_write", "risk": "preview"}


class _ExplodingTool(Tool):
    """A tool with a bug in it, to prove the loop survives one."""

    name = "exploding"
    description = "Raises an unexpected exception. Test fixture only."
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext) -> dict[str, Any]:
        raise ZeroDivisionError("boom")


def _dummy_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    registry.register(_WriteTool())
    registry.register(_ExplodingTool())
    return registry


# =========================================================================
# Registry
# =========================================================================


def test_build_registry_registers_exactly_the_expected_tools():
    assert build_registry().names() == EXPECTED_TOOL_NAMES


def test_read_only_registry_still_excludes_every_write_tool():
    """A read-only deployment stays expressible, and the guard is structural.

    `build_registry(read_only=True)` is what a read-only build ships; if a write
    tool ever leaked into it the copilot could propose changes in a deployment
    that promised it would not.
    """
    registry = build_registry(read_only=True)

    assert registry.names() == EXPECTED_READ_TOOL_NAMES
    assert registry.filter_by_mode(ToolMode.WRITE) == []
    assert all(tool.mode is ToolMode.READ for tool in registry.all())


def test_write_tools_are_registered_and_declared_as_writes():
    """Registering a write tool does NOT give the orchestrator write access.

    Running one validates, snapshots and returns a *signed envelope*; the panel
    performs the HTTP write with the user's own session. The mode declaration is
    what routes it through that path, so it must be exact.
    """
    registry = build_registry()

    write_names = [tool.name for tool in registry.filter_by_mode(ToolMode.WRITE)]
    assert write_names == EXPECTED_WRITE_TOOL_NAMES


def test_every_read_spec_is_strict_and_well_formed():
    """Read tools must keep `strict: true`.

    It is what makes the API guarantee arguments validate before `run` is
    called, so a schema that quietly loses it degrades every call of that tool.
    """
    registry = build_registry()
    specs = {s["name"]: s for s in registry.to_anthropic_specs()}

    assert list(specs) == EXPECTED_TOOL_NAMES
    for name in EXPECTED_READ_TOOL_NAMES:
        spec = specs[name]
        assert spec.get("strict") is True, f"{name} lost strict mode"
        schema = spec["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(schema["required"])


def test_write_specs_are_well_formed_even_where_strict_cannot_apply():
    """Write schemas ARE the governed action schemas, so several have optional
    keys (`create_chart.dashboard_id`) and strict mode cannot be claimed.

    Nothing is lost: `validate_action` applies the full schema server-side, so
    the model-facing hint being looser never widens what is permitted. What must
    hold is that the export is still a closed object with a usable description.
    """
    specs = {s["name"]: s for s in build_registry().to_anthropic_specs()}

    for name in EXPECTED_WRITE_TOOL_NAMES:
        spec = specs[name]
        schema = spec["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        # A top-level union is refused by the API and must have been flattened.
        assert not {"oneOf", "anyOf", "allOf"} & set(schema)
        assert set(schema.get("required") or []) <= set(schema["properties"])


def test_every_spec_has_a_description_written_for_the_model():
    for spec in build_registry().to_anthropic_specs():
        assert spec["description"].strip(), f"{spec['name']} has no description"


def test_exported_spec_does_not_alias_the_tool_schema():
    tool = GetDatasetTool()
    spec = tool.to_anthropic_spec()

    spec["input_schema"]["properties"].clear()

    assert "dataset_id" in tool.input_schema["properties"]


def test_registry_refuses_duplicate_names():
    registry = ToolRegistry()
    registry.register(_EchoTool())

    with pytest.raises(ValueError, match="Duplicate tool name"):
        registry.register(_EchoTool())


# =========================================================================
# execute(): guard rails and audit
# =========================================================================


def test_unknown_tool_returns_an_error_result_and_never_raises():
    result = execute("no_such_tool", {}, _context(), registry=build_registry())

    assert result.ok is False
    assert result.content["error"] == "unknown_tool"
    # The available names are listed so the model can correct itself rather
    # than retrying the same invented name.
    assert "list_datasets" in result.content["message"]


def test_error_result_is_flagged_is_error_for_the_model():
    result = execute("no_such_tool", {}, _context(), registry=build_registry())
    block = result.to_tool_result_block("toolu_123")

    assert block["is_error"] is True
    assert json.loads(block["content"])["error"] == "unknown_tool"


def test_successful_result_carries_no_is_error_flag():
    result = execute("echo", {"value": "hello"}, _context(), registry=_dummy_registry())
    block = result.to_tool_result_block("toolu_123")

    assert result.ok is True
    assert "is_error" not in block
    assert json.loads(block["content"]) == {"echoed": "hello"}


@pytest.mark.django_db
def test_success_writes_an_audit_row():
    conversation = _conversation()

    execute("echo", {"value": "hello"}, _context(conversation), registry=_dummy_registry())

    log = ToolCallLog.objects.get()
    assert log.conversation_id == conversation.id
    assert log.superset_username == "analyst"
    assert log.tool_name == "echo"
    assert log.mode == "read"
    assert log.arguments == {"value": "hello"}
    assert log.ok is True
    assert log.error == ""
    assert log.duration_ms is not None


@pytest.mark.django_db
def test_failure_writes_an_audit_row_too():
    """The failure path is the one worth auditing; it must not be the one that skips it."""
    conversation = _conversation()

    execute("exploding", {}, _context(conversation), registry=_dummy_registry())

    log = ToolCallLog.objects.get()
    assert log.tool_name == "exploding"
    assert log.superset_username == "analyst"
    assert log.mode == "read"
    assert log.ok is False
    assert "boom" in log.error


@pytest.mark.django_db
def test_unknown_tool_is_audited_as_well():
    conversation = _conversation()

    execute("no_such_tool", {"a": 1}, _context(conversation), registry=build_registry())

    log = ToolCallLog.objects.get()
    assert log.tool_name == "no_such_tool"
    assert log.mode == "unknown"
    assert log.arguments == {"a": 1}
    assert log.ok is False


@pytest.mark.django_db
def test_a_write_tool_runs_but_is_audited_as_a_write():
    """A write tool DOES run here — and what it does is propose, never execute.

    `_WriteTool.run` returns an envelope-shaped dict; it performs no HTTP call.
    The invariant this test protects is the audit one: the row records
    `mode == "write"`, so a write action can never be mistaken for a read in the
    trail. The structural guarantee that no Superset write happens server-side
    lives in `superset_write.py`, which contains no POST/PUT/DELETE at all —
    `test_no_write_tool_performs_an_http_write` asserts that directly.
    """
    conversation = _conversation()
    registry = _dummy_registry()
    assert registry.get("dummy_write") is not None

    result = execute("dummy_write", {}, _context(conversation), registry=registry)

    assert result.ok is True
    assert result.content["action_type"] == "dummy_write"

    log = ToolCallLog.objects.get()
    assert log.mode == "write"
    assert log.ok is True


def test_no_write_tool_performs_an_http_write():
    """The security model as a source-level property (spec §1).

    `superset_write.py` may never issue a POST, PUT or DELETE against Superset:
    every change is handed to the panel as a signed envelope and executed with
    the user's own session. One `client.post(...)` would dismantle that, so the
    module's own source is asserted rather than trusted to review.

    Parsed rather than grepped. The module's docstring says *"if you reach for
    `client.post(...)`, stop"* — a substring search matches that warning and
    fails on a file that is entirely correct, which trains whoever hits it to
    weaken the check. Walking the AST sees calls and only calls.
    """
    import ast
    from pathlib import Path

    import lifedata_ai.tools.superset_write as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    forbidden = {"post", "put", "delete", "patch"}
    offenders = [
        f"line {node.lineno}: .{node.func.attr}(...)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
    ]
    assert not offenders, (
        "superset_write.py performs an HTTP write: "
        + "; ".join(offenders)
        + ". Write tools must only PROPOSE; the panel performs every Superset write."
    )


def test_audit_failure_outside_a_conversation_does_not_break_the_call():
    # ToolCallLog needs the conversation FK; a diagnostic call without one must
    # still return its result rather than 500.
    result = execute("echo", {"value": "hi"}, _context(None), registry=_dummy_registry())

    assert result.ok is True
    assert result.content == {"echoed": "hi"}


def test_invalid_arguments_come_back_as_a_correctable_error():
    result = execute("echo", {"value": 42}, _context(), registry=_dummy_registry())

    assert result.ok is False
    assert result.content["error"] == "invalid_arguments"
    assert "echo" in result.content["message"]
    # The path tells the model *which* argument to fix.
    assert result.content["details"]["path"] == ["value"]


def test_unexpected_arguments_are_rejected_by_the_schema():
    result = execute(
        "echo", {"value": "hi", "extra": True}, _context(), registry=_dummy_registry()
    )

    assert result.ok is False
    assert result.content["error"] == "invalid_arguments"


def test_missing_required_argument_is_rejected_before_run():
    result = execute("echo", {}, _context(), registry=_dummy_registry())

    assert result.ok is False
    assert result.content["error"] == "invalid_arguments"


def test_unexpected_exception_is_converted_not_propagated():
    result = execute("exploding", {}, _context(), registry=_dummy_registry())

    assert result.ok is False
    assert result.content["error"] == "tool_failed"
    assert "boom" in result.content["message"]


def test_none_arguments_are_treated_as_empty():
    result = execute("exploding", None, _context(), registry=_dummy_registry())

    assert result.ok is False  # ran, rather than blowing up on None


# =========================================================================
# Superset read tools: JSON-string field parsing
# =========================================================================


def test_list_datasets_annotates_registry_membership():
    with _superset() as router:
        router.route(method="GET", path="/api/v1/dataset/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": LAB_ORDERS_DATASET_ID,
                            "table_name": "rpt_lab_orders",
                            "schema": "reporting",
                            "kind": "virtual",
                        },
                        {
                            "id": 999,
                            "table_name": "scratch_table",
                            "schema": "public",
                            "kind": "physical",
                        },
                    ]
                },
            )
        )
        result = ListDatasetsTool().run(_context(), q="")

    known, unknown = result["datasets"]
    assert known["in_semantics_registry"] is True
    assert known["grain"] == "lab_order"
    assert unknown["in_semantics_registry"] is False
    assert unknown["grain"] is None
    assert result["count"] == 2
    assert result["truncated"] is False


def test_list_datasets_truncates_long_listings():
    rows = [{"id": i, "table_name": f"t{i}"} for i in range(120)]
    with _superset() as router:
        router.route(method="GET", path="/api/v1/dataset/").mock(
            return_value=httpx.Response(200, json={"result": rows})
        )
        result = ListDatasetsTool().run(_context(), q="")

    assert result["count"] == 50
    assert result["truncated"] is True


def test_get_dataset_summarises_columns_metrics_and_semantics():
    payload = {
        "result": {
            "id": LAB_ORDERS_DATASET_ID,
            "table_name": "rpt_lab_orders",
            "schema": "reporting",
            "kind": "virtual",
            "description": "One row per laboratory order.",
            "database": {"database_name": "lifehealth"},
            "main_dttm_col": "ordered_at",
            "owners": [{"first_name": "Ada", "last_name": "Analyst"}],
            "columns": [
                {
                    "column_name": "facility_name",
                    "type": "TEXT",
                    "is_dttm": False,
                    "groupby": True,
                    "filterable": True,
                },
                {
                    "column_name": "ordered_at",
                    "type": "TIMESTAMP",
                    "is_dttm": True,
                    "groupby": False,
                    "filterable": True,
                },
            ],
            "metrics": [
                {
                    "metric_name": "count",
                    "verbose_name": "Orders",
                    "expression": "COUNT(*)",
                    "d3format": ",d",
                }
            ],
        }
    }
    with _superset() as router:
        router.route(method="GET", path=f"/api/v1/dataset/{LAB_ORDERS_DATASET_ID}").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = GetDatasetTool().run(_context(), dataset_id=LAB_ORDERS_DATASET_ID)

    assert result["database"] == "lifehealth"
    assert result["main_datetime_column"] == "ordered_at"
    assert result["owners"] == ["Ada Analyst"]
    assert [c["name"] for c in result["columns"]] == ["facility_name", "ordered_at"]
    assert result["columns"][1]["is_temporal"] is True
    assert result["metrics"][0]["number_format"] == ",d"
    # The governed notes travel with the metadata, so the model has the grain in
    # front of it while it is choosing what to chart.
    assert result["semantics"]["grain"] == "lab_order"
    assert "Never treat an order as a result" in result["semantics"]["notes"]


def test_get_dataset_reports_a_missing_dataset_as_a_tool_error():
    with _superset() as router:
        router.route(method="GET", path="/api/v1/dataset/404").mock(
            return_value=httpx.Response(200, json={"result": {}})
        )
        with pytest.raises(ToolError) as exc:
            GetDatasetTool().run(_context(), dataset_id=404)

    assert exc.value.code == "not_found"


def test_get_chart_parses_the_params_json_string():
    params = {
        "viz_type": "echarts_timeseries_bar",
        "metrics": [{"label": "Orders", "expressionType": "SIMPLE"}],
        "groupby": ["facility_name"],
        "time_range": "Last 12 months",
        "row_limit": 10000,
        "adhoc_filters": [
            {
                "expressionType": "SIMPLE",
                "subject": "order_status",
                "operator": "==",
                "comparator": "verified",
            },
            {"expressionType": "SQL", "clause": "WHERE", "sqlExpression": "1=1"},
        ],
        "colour_noise": "ignored",
    }
    chart = {
        "result": {
            "id": 11,
            "slice_name": "Lab orders by facility",
            "viz_type": "echarts_timeseries_bar",
            "datasource_id": LAB_ORDERS_DATASET_ID,
            "datasource_name_text": "reporting.rpt_lab_orders",
            # Superset hands these back as JSON *strings*, not objects.
            "params": json.dumps(params),
            "query_context": json.dumps({"queries": [{}]}),
            "owners": [{"first_name": "Ada", "last_name": "Analyst"}],
            "dashboards": [{"id": 4, "dashboard_title": "Laboratory"}],
        }
    }
    with _superset() as router:
        router.route(method="GET", path="/api/v1/chart/11").mock(
            return_value=httpx.Response(200, json=chart)
        )
        result = GetChartTool().run(_context(), chart_id=11)

    form_data = result["form_data"]
    assert form_data["metrics"] == ["Orders"]
    assert form_data["group_by"] == ["facility_name"]
    assert form_data["row_limit"] == 10000
    assert result["on_dashboards"] == [{"id": 4, "title": "Laboratory"}]
    assert result["has_stored_query_context"] is True

    simple_filter, sql_filter = form_data["filters"]
    assert simple_filter == {
        "column": "order_status",
        "operator": "==",
        "value": "verified",
    }
    # A raw SQL filter on an existing chart is surfaced, not hidden — the model
    # must know it cannot reproduce that in anything it proposes (spec §6.3).
    assert sql_filter["type"] == "raw_sql_filter"


def test_get_chart_reports_unparseable_params_without_raising():
    chart = {
        "result": {
            "id": 12,
            "slice_name": "Broken",
            "params": "{not json",
            "query_context": None,
        }
    }
    with _superset() as router:
        router.route(method="GET", path="/api/v1/chart/12").mock(
            return_value=httpx.Response(200, json=chart)
        )
        result = GetChartTool().run(_context(), chart_id=12)

    assert result["form_data"] == {}
    assert "params" in result["warning"]
    assert result["has_stored_query_context"] is False


def _dashboard_routes(router, dashboard_id: int, dashboard: dict, charts: list) -> None:
    router.route(method="GET", path=f"/api/v1/dashboard/{dashboard_id}").mock(
        return_value=httpx.Response(200, json={"result": dashboard})
    )
    router.route(method="GET", path=f"/api/v1/dashboard/{dashboard_id}/charts").mock(
        return_value=httpx.Response(200, json={"result": charts})
    )


def test_get_dashboard_parses_position_json_and_json_metadata():
    position = {
        "ROOT_ID": {"type": "ROOT", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "children": ["ROW-1"]},
        "ROW-1": {"type": "ROW", "children": ["CHART-a", "CHART-b"]},
        "CHART-a": {
            "type": "CHART",
            "meta": {"chartId": 11, "width": 6, "height": 50, "sliceName": "Orders"},
        },
        "CHART-b": {
            "type": "CHART",
            "meta": {"chartId": 12, "width": 6, "height": 50, "sliceName": "Turnaround"},
        },
    }
    metadata = {
        "color_scheme": "supersetColors",
        "refresh_frequency": 0,
        "native_filter_configuration": [
            {
                "id": "NATIVE_FILTER-1",
                "name": "District",
                "filterType": "filter_select",
                "targets": [
                    {"datasetId": LAB_ORDERS_DATASET_ID, "column": {"name": "district_name"}}
                ],
            }
        ],
    }
    dashboard = {
        "id": 4,
        "dashboard_title": "Laboratory performance",
        "published": True,
        "position_json": json.dumps(position),
        "json_metadata": json.dumps(metadata),
        "owners": [{"id": 7, "first_name": "Ada", "last_name": "Analyst"}],
        "changed_on": "2026-07-01T09:00:00",
    }
    charts = [
        {"id": 11, "slice_name": "Orders", "form_data": {"viz_type": "bar", "datasource": "3__table"}},
        {"id": 12, "slice_name": "Turnaround", "form_data": {"viz_type": "table"}},
    ]

    with _superset() as router:
        _dashboard_routes(router, 4, dashboard, charts)
        result = GetDashboardTool().run(_context(), dashboard_id=4)

    assert result["colour_scheme"] == "supersetColors"
    assert result["chart_count"] == 2
    assert result["owned_by_you"] is True  # identity.user_id is 7

    layout = result["layout"]
    assert layout["has_layout"] is True
    assert layout["widget_count"] == 2
    assert layout["node_counts"]["CHART"] == 2
    row = layout["outline"][0]
    assert row["type"] == "ROW"
    assert [child["name"] for child in row["children"]] == ["Orders", "Turnaround"]
    assert [child["width"] for child in row["children"]] == [6, 6]

    native = result["native_filters"][0]
    assert native["name"] == "District"
    assert native["column"] == "district_name"
    assert "warnings" not in result


def test_get_dashboard_handles_null_position_and_metadata():
    """Verified finding §4: both fields are SQL NULL on a fresh dashboard."""
    dashboard = {
        "id": 9,
        "dashboard_title": "Brand new",
        "published": False,
        "position_json": None,
        "json_metadata": None,
        "owners": [],
    }

    with _superset() as router:
        _dashboard_routes(router, 9, dashboard, [])
        result = GetDashboardTool().run(_context(), dashboard_id=9)

    assert result["layout"] == {
        "has_layout": False,
        "note": "This dashboard has no saved layout yet.",
    }
    assert result["native_filters"] == []
    assert result["colour_scheme"] is None
    assert result["chart_count"] == 0
    assert result["owned_by_you"] is False
    # None is the normal state of a new dashboard, not a fault worth warning about.
    assert "warnings" not in result


def test_get_dashboard_survives_an_unavailable_membership_endpoint():
    dashboard = {"id": 9, "dashboard_title": "Restricted", "position_json": None}

    with _superset() as router:
        router.route(method="GET", path="/api/v1/dashboard/9").mock(
            return_value=httpx.Response(200, json={"result": dashboard})
        )
        router.route(method="GET", path="/api/v1/dashboard/9/charts").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )
        result = GetDashboardTool().run(_context(), dashboard_id=9)

    assert result["charts"] == []
    assert result["chart_count"] == 0


def test_get_dashboard_reports_unparseable_layout_as_a_warning():
    dashboard = {
        "id": 9,
        "dashboard_title": "Corrupt",
        "position_json": "{not json",
        "json_metadata": "{also not json",
    }

    with _superset() as router:
        _dashboard_routes(router, 9, dashboard, [])
        result = GetDashboardTool().run(_context(), dashboard_id=9)

    assert result["layout"]["has_layout"] is False
    assert len(result["warnings"]) == 2


# =========================================================================
# get_chart_data: the privacy boundary
# =========================================================================


def _query_context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "datasource": {"id": LAB_ORDERS_DATASET_ID, "type": "table"},
        "form_data": {"viz_type": "bar"},
        "queries": [
            {
                "metrics": ["count"],
                "columns": ["facility_name"],
                "row_limit": 50000,
                "filters": [],
            }
        ],
    }
    context.update(overrides)
    return context


def _chart(chart_id: int = 11, query_context: Any = None, **overrides: Any) -> dict[str, Any]:
    chart: dict[str, Any] = {
        "id": chart_id,
        "slice_name": "Orders by facility",
        "viz_type": "echarts_timeseries_bar",
        "datasource_id": LAB_ORDERS_DATASET_ID,
        "datasource_name_text": "reporting.rpt_lab_orders",
        "params": json.dumps({"viz_type": "echarts_timeseries_bar"}),
        "query_context": json.dumps(query_context if query_context is not None else _query_context()),
    }
    chart.update(overrides)
    return chart


def _chart_data_response(rows: list[dict], colnames: list[str] | None = None) -> dict[str, Any]:
    return {
        "result": [
            {
                "status": "success",
                "colnames": colnames if colnames is not None else list(rows[0]) if rows else [],
                "data": rows,
                "applied_filters": [],
                "rejected_filters": [],
                "is_cached": False,
                "queried_dttm": "2026-07-29T10:00:00",
            }
        ]
    }


@contextmanager
def _chart_data_call(chart: dict, response: dict, status: int = 200):
    with _superset() as router:
        router.route(method="GET", path=f"/api/v1/chart/{chart['id']}").mock(
            return_value=httpx.Response(200, json={"result": chart})
        )
        data_route = router.route(method="POST", path="/api/v1/chart/data").mock(
            return_value=httpx.Response(status, json=response)
        )
        yield data_route


def test_get_chart_data_caps_the_row_limit_before_sending():
    rows = [{"facility_name": f"F{i}", "count": i} for i in range(3)]
    with _chart_data_call(_chart(), _chart_data_response(rows)) as data_route:
        result = GetChartDataTool().run(_context(), chart_id=11)

    sent = json.loads(data_route.calls.last.request.content)
    # The chart is saved with row_limit 50000; nothing that large may be pulled
    # through the copilot.
    assert sent["queries"][0]["row_limit"] == settings.LIFEDATA_AI_MAX_QUERY_ROW_LIMIT == 200
    assert sent["force"] is False
    assert sent["result_format"] == "json"
    assert result["complete_result"] is True
    assert "note" not in result


def test_get_chart_data_keeps_a_row_limit_lower_than_the_cap():
    query_context = _query_context()
    query_context["queries"][0]["row_limit"] = 10
    rows = [{"facility_name": f"F{i}", "count": i} for i in range(3)]

    with _chart_data_call(_chart(query_context=query_context), _chart_data_response(rows)) as route:
        GetChartDataTool().run(_context(), chart_id=11)

    assert json.loads(route.calls.last.request.content)["queries"][0]["row_limit"] == 10


def test_get_chart_data_truncates_rows_and_says_how_many_there_were():
    rows = [{"facility_name": f"F{i}", "count": i} for i in range(80)]
    with _chart_data_call(_chart(), _chart_data_response(rows)):
        result = GetChartDataTool().run(_context(), chart_id=11)

    assert result["rows_shown"] == settings.LIFEDATA_AI_MAX_ROWS_TO_MODEL == 50
    assert len(result["rows"]) == 50
    assert result["rows_returned"] == 80
    assert result["truncated"]["rows"] is True
    assert result["complete_result"] is False
    # "50 of 80" — the total is known, because the query did not hit its cap.
    assert "Showing 50 of 80 rows" in result["note"]
    assert "at least" not in result["note"]


def test_get_chart_data_distinguishes_a_capped_query_from_a_known_total():
    rows = [{"facility_name": f"F{i}", "count": i} for i in range(200)]
    with _chart_data_call(_chart(), _chart_data_response(rows)):
        result = GetChartDataTool().run(_context(), chart_id=11)

    # The query came back exactly at its cap, so the true total is unknown:
    # "50 of 200" would imply 200 is the whole answer.
    assert "Showing 50 of at least 200 rows" in result["note"]
    assert "capped at 200 rows" in result["note"]
    assert result["complete_result"] is False


def test_get_chart_data_truncates_columns_and_never_claims_completeness():
    colnames = [f"col_{i}" for i in range(25)]
    rows = [{name: index for index, name in enumerate(colnames)} for _ in range(60)]

    with _chart_data_call(_chart(), _chart_data_response(rows, colnames)):
        result = GetChartDataTool().run(_context(), chart_id=11)

    assert len(result["columns"]) == settings.LIFEDATA_AI_MAX_COLUMNS_TO_MODEL == 20
    assert all(len(row) == 20 for row in result["rows"])
    assert result["truncated"] == {"rows": True, "columns": True, "row_limit_applied": 200}
    # A doubly truncated result must never be presentable as complete.
    assert result["complete_result"] is False
    assert "subset of columns" in result["note"]
    assert "not the complete result" in result["note"]


def test_get_chart_data_refuses_a_chart_saved_in_raw_query_mode():
    query_context = _query_context()
    query_context["form_data"] = {"viz_type": "table", "query_mode": "raw"}

    with _chart_data_call(_chart(query_context=query_context), {}) as data_route:
        with pytest.raises(ToolError) as exc:
            GetChartDataTool().run(_context(), chart_id=11)

    assert exc.value.code == "row_level_result_refused"
    # Refused before the request, so no row-level data is ever fetched at all.
    assert data_route.called is False


def test_get_chart_data_refuses_a_query_with_no_metrics_and_no_post_processing():
    query_context = _query_context()
    query_context["queries"][0] = {"columns": ["patient_id", "facility_name"], "row_limit": 100}

    with _chart_data_call(_chart(query_context=query_context), {}) as data_route:
        with pytest.raises(ToolError) as exc:
            GetChartDataTool().run(_context(), chart_id=11)

    assert exc.value.code == "row_level_result_refused"
    assert data_route.called is False


def test_get_chart_data_refuses_a_raw_chart_even_when_form_data_omits_query_mode():
    """The metrics check is the second line of defence behind the query_mode check."""
    query_context = _query_context()
    query_context["form_data"] = {"viz_type": "table"}
    query_context["queries"][0] = {"columns": ["patient_id"], "row_limit": 100}

    with _chart_data_call(_chart(query_context=query_context), {}) as data_route:
        with pytest.raises(ToolError) as exc:
            GetChartDataTool().run(_context(), chart_id=11)

    assert exc.value.code == "row_level_result_refused"
    assert data_route.called is False


def test_get_chart_data_accepts_post_processing_without_metrics():
    query_context = _query_context()
    query_context["queries"][0] = {
        "columns": ["facility_name"],
        "post_processing": [{"operation": "pivot"}],
        "row_limit": 100,
    }
    rows = [{"facility_name": "F1", "count": 3}]

    with _chart_data_call(_chart(query_context=query_context), _chart_data_response(rows)):
        result = GetChartDataTool().run(_context(), chart_id=11)

    assert result["rows_shown"] == 1


@pytest.mark.parametrize(
    "query_context",
    [
        None,
        {"queries": [], "datasource": {"id": 3}},
        {"queries": [{"metrics": ["count"]}]},  # no datasource
    ],
    ids=["missing", "empty_queries", "no_datasource"],
)
def test_get_chart_data_refuses_an_unusable_query_context_before_sending(query_context):
    chart = _chart(query_context=query_context)
    if query_context is None:
        chart["query_context"] = None

    with _chart_data_call(chart, {}) as data_route:
        with pytest.raises(ToolError) as exc:
            GetChartDataTool().run(_context(), chart_id=11)

    assert exc.value.code == "no_query_context"
    # Verified finding §3: sending a malformed body would come back as a 500,
    # which reads like a Superset outage. Validate first, always.
    assert data_route.called is False


def test_get_chart_data_reads_a_500_as_our_malformed_request():
    with _chart_data_call(_chart(), {"message": "Internal Server Error"}, status=500):
        with pytest.raises(ToolError) as exc:
            GetChartDataTool().run(_context(), chart_id=11)

    assert exc.value.code == "query_context_rejected"
    assert exc.value.details == {"status_code": 500}
    message = exc.value.message.lower()
    assert "query definition superset could not run" in message
    # The user must not be told Superset is broken when we sent something it
    # could not execute.
    for blame in ("superset is down", "unavailable", "try again later", "outage"):
        assert blame not in message


def test_get_chart_data_surfaces_a_failed_query_result():
    response = {"result": [{"status": "failed", "error": "column does not exist"}]}
    with _chart_data_call(_chart(), response):
        with pytest.raises(ToolError) as exc:
            GetChartDataTool().run(_context(), chart_id=11)

    assert exc.value.code == "query_failed"
    assert "column does not exist" in exc.value.message


@pytest.mark.django_db
def test_a_superset_403_is_surfaced_with_its_status():
    conversation = _conversation()
    with _superset() as router:
        router.route(method="GET", path="/api/v1/dataset/3").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )
        result = execute(
            "get_dataset",
            {"dataset_id": 3},
            _context(conversation),
            registry=build_registry(),
        )

    assert result.ok is False
    assert result.content["error"] == "superset_permission_denied"
    assert result.content["status_code"] == 403
    assert ToolCallLog.objects.get().ok is False


@pytest.mark.django_db
def test_an_expired_session_is_reported_as_needing_a_new_sign_in():
    conversation = _conversation()
    with _superset() as router:
        router.route(method="GET", path="/api/v1/dataset/3").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        result = execute(
            "get_dataset",
            {"dataset_id": 3},
            _context(conversation),
            registry=build_registry(),
        )

    assert result.content["error"] == "superset_unauthenticated"
    assert ToolCallLog.objects.get().tool_name == "get_dataset"


# =========================================================================
# get_page_context
# =========================================================================


def test_get_page_context_echoes_the_panel_payload():
    page = {"page_type": "dashboard", "dashboard_id": 4, "filters": []}
    result = GetPageContextTool().run(_context(page_context=page))

    assert result == {
        "available": True,
        "source": "panel",
        "user": "analyst",
        "page": page,
    }


def test_get_page_context_reports_when_nothing_is_known():
    result = GetPageContextTool().run(_context())

    assert result["available"] is False
    assert "list_dashboards" in result["note"]


@pytest.mark.django_db
def test_get_page_context_falls_back_to_where_the_conversation_started():
    conversation = Conversation.objects.create(
        superset_username="analyst", page_scope={"page_type": "chart", "chart_id": 11}
    )
    result = GetPageContextTool().run(_context(conversation))

    assert result["source"] == "conversation_start"
    assert result["page"]["chart_id"] == 11


# =========================================================================
# LifeData tools: governed definitions
# =========================================================================


def test_explain_indicator_returns_the_registry_definition_verbatim():
    raw = _raw_indicator("malaria_positivity")
    result = ExplainIndicatorTool().run(_context(), name="malaria_positivity")
    indicator = result["indicator"]

    assert indicator["definition"] == raw["definition"].strip()
    assert indicator["numerator"] == raw["numerator"]
    assert indicator["denominator"] == raw["denominator"]
    assert indicator["unit"] == raw["unit"] == "percent"
    # The distinction the registry exists to protect: a test is not a patient.
    assert indicator["counts"] == raw["counts"] == "tests"
    assert indicator["caveats"] == raw["caveats"].strip()
    assert "Counts tests, not patients" in indicator["caveats"]
    assert indicator["verbatim"] is True
    assert "do not paraphrase" in result["usage"]


def test_explain_indicator_attaches_the_owning_dataset():
    raw = _raw_dataset("rpt_lab_orders")
    result = ExplainIndicatorTool().run(_context(), name="malaria_positivity")

    assert result["dataset"]["name"] == "rpt_lab_orders"
    assert result["dataset"]["superset_dataset_id"] == raw["superset_dataset_id"]
    assert result["dataset"]["grain"] == raw["grain"]


def test_every_registry_indicator_is_explainable():
    registry = load_registry()
    tool = ExplainIndicatorTool()

    for name in registry.indicators:
        payload = tool.run(_context(), name=name)["indicator"]
        assert payload["definition"], f"{name} has no definition"
        assert payload["counts"], f"{name} does not say what it counts"


def test_explain_indicator_refuses_an_unknown_name_and_invents_nothing():
    with pytest.raises(ToolError) as exc:
        ExplainIndicatorTool().run(_context(), name="malaria_prevalence")

    error = exc.value
    assert error.code == "unknown_indicator"
    assert "do not invent one" in error.message
    assert error.details["known_indicators"] == sorted(load_registry().indicators)
    # A near miss gets a suggestion rather than a fabricated definition.
    assert "malaria_positivity" in error.details["did_you_mean"]


def test_unknown_indicator_reaches_the_model_as_a_structured_error():
    result = execute(
        "explain_indicator",
        {"name": "made_up_indicator"},
        _context(),
        registry=build_registry(),
    )

    assert result.ok is False
    assert result.content["error"] == "unknown_indicator"
    assert "known_indicators" in result.content["details"]


def test_recommend_dataset_returns_real_ids_and_interpretation_notes():
    raw = _raw_dataset("rpt_lab_orders")
    result = RecommendDatasetTool().run(
        _context(), analysis_goal="laboratory turnaround by facility"
    )

    assert result["matched_on_subject_areas"] is True
    top = result["datasets"][0]
    assert top["name"] == "rpt_lab_orders"
    assert top["superset_dataset_id"] == raw["superset_dataset_id"]
    assert top["rank"] == 1
    assert top["grain"] == "lab_order"
    # The notes carry the trap; dropping them is how the model starts calling an
    # order a result.
    assert "Never treat an order as a result" in top["notes"]
    assert "turnaround" in top["why"]


def test_recommend_dataset_lists_everything_when_nothing_matches():
    registry = load_registry()
    result = RecommendDatasetTool().run(_context(), analysis_goal="what happened up north")

    assert result["matched_on_subject_areas"] is False
    assert len(result["datasets"]) == len(registry.datasets)
    assert all("no subject-area keyword matched" in ds["why"] for ds in result["datasets"])


def test_recommend_dataset_states_the_patient_geography_policy():
    result = RecommendDatasetTool().run(_context(), analysis_goal="patient catchment geography")

    geography = next(
        ds for ds in result["datasets"] if ds["name"] == "rpt_patient_geography"
    )
    assert "district is the finest permitted grouping" in geography["geography_policy"]
    assert geography["small_cell_threshold"] == 5


# =========================================================================
# LifeData tools: dynamic forms
# =========================================================================
#
# The dynamic-form tables are created by `manage.py seed_lifehealth` (raw SQL,
# no Django models), so they do not exist in the pytest test database. Of the
# three options — create them here, run against the real database, or skip —
# creating them here is the only one that is both hermetic and honest: the
# tools' real SQL runs against real Postgres, the fixture rolls back with the
# test transaction, and nothing depends on seeding order or on a live database
# that CI would not have. The shapes below mirror sql/lifehealth_schema.sql;
# the `facility_id` foreign key is dropped because these tools never join to
# facility.


def _create_form_tables() -> None:
    """Minimal dynamic-form schema, mirroring sql/lifehealth_schema.sql."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DROP TABLE IF EXISTS public.form_submission CASCADE;
            DROP TABLE IF EXISTS public.form_question CASCADE;
            DROP TABLE IF EXISTS public.form_version CASCADE;
            DROP TABLE IF EXISTS public.dynamic_form CASCADE;

            CREATE TABLE public.dynamic_form (
                form_id   serial PRIMARY KEY,
                form_key  text NOT NULL UNIQUE,
                form_name text NOT NULL
            );
            CREATE TABLE public.form_version (
                form_version_id serial PRIMARY KEY,
                form_id         integer NOT NULL REFERENCES public.dynamic_form(form_id),
                version_number  integer NOT NULL,
                published_on    date NOT NULL
            );
            CREATE TABLE public.form_question (
                question_id     serial PRIMARY KEY,
                form_version_id integer NOT NULL REFERENCES public.form_version(form_version_id),
                question_key    text NOT NULL,
                question_label  text NOT NULL,
                question_type   text NOT NULL,
                unit            text,
                is_sensitive    boolean NOT NULL DEFAULT false,
                display_order   integer NOT NULL
            );
            CREATE TABLE public.form_submission (
                submission_id   serial PRIMARY KEY,
                form_version_id integer NOT NULL REFERENCES public.form_version(form_version_id),
                submitted_on    date NOT NULL
            );
            """
        )


def _seed_forms() -> None:
    """The seeded diabetes screening form, plus one form with no reporting view."""
    _create_form_tables()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO public.dynamic_form (form_key, form_name) VALUES
                ('diabetes_screening', 'Diabetes Screening'),
                ('community_outreach', 'Community Outreach');

            INSERT INTO public.form_version (form_id, version_number, published_on)
            SELECT form_id, 1, DATE '2025-01-15' FROM public.dynamic_form;

            INSERT INTO public.form_question
                (form_version_id, question_key, question_label, question_type,
                 unit, is_sensitive, display_order)
            SELECT v.form_version_id, q.key, q.label, q.type, q.unit, q.sensitive, q.ord
              FROM public.form_version v
              JOIN public.dynamic_form df USING (form_id)
              CROSS JOIN (VALUES
                  ('age',             'Age',                        'integer', 'years',  false, 1),
                  ('bmi',             'Body mass index',            'decimal', 'kg/m2',  false, 2),
                  ('fasting_glucose', 'Fasting glucose',            'decimal', 'mmol/L', false, 3),
                  ('family_history',  'Family history of diabetes', 'boolean', NULL,     false, 4),
                  ('risk_band',       'Risk band',                  'select',  NULL,     false, 5)
              ) AS q(key, label, type, unit, sensitive, ord)
             WHERE df.form_key = 'diabetes_screening';

            INSERT INTO public.form_submission (form_version_id, submitted_on)
            SELECT v.form_version_id, DATE '2026-02-01'
              FROM public.form_version v
              JOIN public.dynamic_form df USING (form_id)
              CROSS JOIN generate_series(1, 3)
             WHERE df.form_key = 'diabetes_screening';
            """
        )


def _create_reporting_view() -> None:
    """A stand-in for the hand-written flattened view, so column reads are exercised."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE SCHEMA IF NOT EXISTS reporting;
            DROP VIEW IF EXISTS reporting.rpt_diabetes_screening CASCADE;
            CREATE VIEW reporting.rpt_diabetes_screening AS
            SELECT 1::integer  AS submission_id,
                   DATE '2026-02-01' AS submitted_on,
                   4.9::numeric AS fasting_glucose_mmol_l;
            COMMENT ON VIEW reporting.rpt_diabetes_screening IS
                'One row per screening submission. A screening result is not a diagnosis.';
            """
        )


@pytest.mark.django_db
def test_list_forms_returns_the_seeded_diabetes_screening_form():
    _seed_forms()

    result = ListFormsTool().run(_context())

    forms = {form["form_key"]: form for form in result["forms"]}
    diabetes = forms["diabetes_screening"]
    assert diabetes["form_name"] == "Diabetes Screening"
    assert diabetes["wired"] is True
    assert diabetes["reporting_view"] == "reporting.rpt_diabetes_screening"
    assert diabetes["superset_dataset_id"] == 5

    version = diabetes["versions"][0]
    assert version["version_number"] == 1
    assert version["submission_count"] == 3
    assert [q["question_key"] for q in version["questions"]] == [
        "age",
        "bmi",
        "fasting_glucose",
        "family_history",
        "risk_band",
    ]
    assert version["questions"][2]["unit"] == "mmol/L"

    # A form with no registry entry is reported as unchartable rather than
    # quietly presented as analysable.
    assert forms["community_outreach"]["wired"] is False
    assert forms["community_outreach"]["reporting_view"] is None
    assert "Only forms with a reporting_view can be charted" in result["usage"]


@pytest.mark.django_db
def test_list_forms_never_returns_answers():
    _seed_forms()

    result = ListFormsTool().run(_context())

    # Only the data is inspected: `usage` is prose that legitimately mentions
    # answers in order to say they are unavailable.
    payload = json.dumps(result["forms"])
    for leak in ("form_answer", "value_text", "value_number", "value_bool", "answer"):
        assert leak not in payload


@pytest.mark.django_db
def test_get_form_schema_by_key_includes_the_reporting_view_columns():
    _seed_forms()
    _create_reporting_view()

    result = GetFormSchemaTool().run(_context(), form_key="diabetes_screening", form_id=None)

    assert result["form_key"] == "diabetes_screening"
    assert result["wired"] is True
    assert result["versions"][0]["published_on"] == "2025-01-15"
    columns = [column["column"] for column in result["reporting_view_columns"]]
    # The point of returning columns at all: the view is hand-written, so
    # 'fasting_glucose' is not the column name.
    assert "fasting_glucose_mmol_l" in columns
    assert "fasting_glucose" not in columns
    assert "not a diagnosis" in result["reporting_view_description"]
    assert "column names are not derived from" in result["usage"]


@pytest.mark.django_db
def test_get_form_schema_by_id_matches_lookup_by_key():
    _seed_forms()

    by_key = GetFormSchemaTool().run(_context(), form_key="diabetes_screening", form_id=None)
    by_id = GetFormSchemaTool().run(_context(), form_key=None, form_id=by_key["form_id"])

    assert by_id["form_key"] == "diabetes_screening"
    assert by_id["versions"] == by_key["versions"]


@pytest.mark.django_db
def test_get_form_schema_says_an_unwired_form_cannot_be_charted():
    _seed_forms()

    result = GetFormSchemaTool().run(_context(), form_key="community_outreach", form_id=None)

    assert result["wired"] is False
    assert "reporting_view_columns" not in result
    assert "cannot be charted" in result["usage"]


@pytest.mark.django_db
def test_get_form_schema_refuses_an_unknown_form_and_lists_the_real_ones():
    _seed_forms()

    with pytest.raises(ToolError) as exc:
        GetFormSchemaTool().run(_context(), form_key="malaria_screening", form_id=None)

    assert exc.value.code == "unknown_form"
    assert exc.value.details["known_form_keys"] == ["community_outreach", "diabetes_screening"]


def test_get_form_schema_requires_one_identifier():
    with pytest.raises(ToolError) as exc:
        GetFormSchemaTool().run(_context(), form_key=None, form_id=None)

    assert exc.value.code == "invalid_arguments"
    assert "list_forms" in exc.value.message


@pytest.mark.django_db
def test_missing_form_tables_degrade_to_a_named_operator_fix():
    """An unseeded database must produce an intelligible refusal, not a 500."""
    with pytest.raises(ToolError) as exc:
        ListFormsTool().run(_context())

    assert exc.value.code == "forms_unavailable"
    assert "seed_lifehealth" in exc.value.message


@pytest.mark.django_db
def test_a_failing_form_tool_is_still_audited():
    """A failed statement must not poison the transaction the audit row needs.

    PostgreSQL aborts the whole transaction on a failed statement, so a
    swallowed `DatabaseError` here would take the `ToolCallLog` INSERT down with
    it — losing the audit row on exactly the calls worth auditing.
    """
    conversation = _conversation()

    result = execute("list_forms", {}, _context(conversation), registry=build_registry())

    assert result.ok is False
    assert result.content["error"] == "forms_unavailable"

    log = ToolCallLog.objects.get()
    assert log.tool_name == "list_forms"
    assert log.ok is False
    # A surviving-but-blank row would satisfy the assertions above while telling
    # an auditor nothing. The row has to carry why it failed and who provoked it.
    assert "seed_lifehealth" in log.error
    assert log.conversation_id == conversation.id
    assert log.superset_username == _context(conversation).username
    assert log.mode == "read"
    assert log.duration_ms is not None


@pytest.mark.django_db
def test_a_failing_form_tool_leaves_the_connection_usable():
    """The next tool in the same turn must still be able to query."""
    with pytest.raises(ToolError):
        ListFormsTool().run(_context())  # tables absent: a real DatabaseError

    # Everything after this would fail on an aborted transaction if the failed
    # statement had not been contained in a savepoint.
    _seed_forms()
    assert ListFormsTool().run(_context())["forms"]


def test_the_dropped_tools_stay_dropped():
    """§12: fewer, better-verified options make the model more reliable.

    Asserted rather than assumed, because a dropped tool is exactly the kind of
    thing that comes back in a merge. Each removal was a decision with a reason
    recorded beside `DROPPED_TOOL_NAMES`.
    """
    registry = build_registry()

    for name in DROPPED_TOOL_NAMES:
        assert name not in registry.names()
        assert registry.get(name) is None
