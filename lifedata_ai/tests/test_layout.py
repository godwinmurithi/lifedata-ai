"""Tests for the semantic layout compiler (spec §6 rule 7).

Three of these earn their keep more than the rest:

* `test_round_trip_no_operations_preserves_semantics` is the property test the
  spec asks for by name — parse then compile with no operations must give back
  the same graph, or every other operation is building on sand.
* `test_null_position_json_*` cover the verified Superset 6.1.0 behaviour that
  a freshly created dashboard returns `position_json` as JSON `null`
  (`docs/superset-integration-findings.md` item 4). This is the first call the
  copilot ever makes against a dashboard it created.
* `test_chart_node_ids_survive_*` guards the one silent-corruption mode:
  regenerating a `CHART-xxxx` id detaches the chart from the dashboard without
  raising anything.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lifedata_ai.validation.layout import (
    GRID_COLUMN_COUNT,
    GRID_ID,
    KPI_ROW_HEIGHT,
    ROOT_ID,
    VERSION_KEY,
    LayoutError,
    LayoutTree,
    apply_operation,
    compile_operations,
    parse_position_json,
    serialise,
)

# --- Fixtures -------------------------------------------------------------
#
# Shaped after real `position_json` from Superset 6.1.0: a flat mapping in
# which `DASHBOARD_VERSION_KEY` is a bare string sitting beside the node dicts,
# and `HEADER_ID` is a node that is *not* reachable from `ROOT_ID`.


def _chart(node_id: str, chart_id: int, width: int = 4, height: int = 50) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "CHART",
        "children": [],
        "parents": [ROOT_ID, GRID_ID],
        "meta": {
            "chartId": chart_id,
            "width": width,
            "height": height,
            "sliceName": f"Chart {chart_id}",
        },
    }


def three_chart_dashboard() -> dict[str, Any]:
    """One row of three equal charts, plus a second row with one chart."""
    return {
        VERSION_KEY: "v2",
        ROOT_ID: {"id": ROOT_ID, "type": "ROOT", "children": [GRID_ID]},
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": "Laboratory"}},
        GRID_ID: {
            "id": GRID_ID,
            "type": "GRID",
            "children": ["ROW-top", "ROW-bottom"],
            "parents": [ROOT_ID],
            "meta": {},
        },
        "ROW-top": {
            "id": "ROW-top",
            "type": "ROW",
            "children": ["CHART-aaa", "CHART-bbb", "CHART-ccc"],
            "parents": [ROOT_ID, GRID_ID],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        },
        "ROW-bottom": {
            "id": "ROW-bottom",
            "type": "ROW",
            "children": ["CHART-ddd"],
            "parents": [ROOT_ID, GRID_ID],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        },
        "CHART-aaa": _chart("CHART-aaa", 101),
        "CHART-bbb": _chart("CHART-bbb", 102),
        "CHART-ccc": _chart("CHART-ccc", 103),
        "CHART-ddd": _chart("CHART-ddd", 104, width=12, height=60),
    }


def tabbed_dashboard() -> dict[str, Any]:
    """Charts nested under TABS/TAB — the shape that breaks naive parent logic."""
    return {
        VERSION_KEY: "v2",
        ROOT_ID: {"id": ROOT_ID, "type": "ROOT", "children": ["TABS-1"]},
        "TABS-1": {
            "id": "TABS-1",
            "type": "TABS",
            "children": ["TAB-1"],
            "parents": [ROOT_ID],
            "meta": {},
        },
        "TAB-1": {
            "id": "TAB-1",
            "type": "TAB",
            "children": ["ROW-t1"],
            "parents": [ROOT_ID, "TABS-1"],
            "meta": {"text": "Turnaround"},
        },
        "ROW-t1": {
            "id": "ROW-t1",
            "type": "ROW",
            "children": ["CHART-t1", "CHART-t2"],
            "parents": [ROOT_ID, "TABS-1", "TAB-1"],
            "meta": {},
        },
        "CHART-t1": _chart("CHART-t1", 201),
        "CHART-t2": _chart("CHART-t2", 202),
        GRID_ID: {"id": GRID_ID, "type": "GRID", "children": [], "parents": [ROOT_ID], "meta": {}},
    }


def overflowing_dashboard() -> dict[str, Any]:
    """A row already summing past 12 columns.

    Superset lets this happen. A no-op compile must leave it exactly as found —
    reflow only ever fires on rows an operation touched.
    """
    layout = three_chart_dashboard()
    layout["ROW-top"]["children"].append("CHART-eee")
    layout["CHART-eee"] = _chart("CHART-eee", 105, width=8)
    return layout


ALL_FIXTURES = [three_chart_dashboard, tabbed_dashboard, overflowing_dashboard]


# --- Semantic comparison --------------------------------------------------


def _semantics(layout: dict[str, Any]) -> dict[str, Any]:
    """The parts of a layout that must survive a round trip.

    Key *order* deliberately is not one of them; child order is, because child
    order is the layout.
    """
    nodes = {
        key: value
        for key, value in layout.items()
        if isinstance(value, dict) and isinstance(value.get("type"), str)
    }
    return {
        "extras": {k: v for k, v in layout.items() if k not in nodes},
        "types": {k: v["type"] for k, v in nodes.items()},
        "children": {k: list(v.get("children", [])) for k, v in nodes.items()},
        "parents": {k: list(v.get("parents", [])) for k, v in nodes.items()},
        "charts": {
            k: v["meta"]["chartId"] for k, v in nodes.items() if v["type"] == "CHART"
        },
        "meta": {k: v.get("meta", {}) for k, v in nodes.items()},
    }


# --- The round-trip property (spec §6 rule 7) -----------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda f: f.__name__)
def test_round_trip_no_operations_preserves_semantics(fixture) -> None:
    layout = fixture()
    result = json.loads(compile_operations(json.dumps(layout), []))
    assert _semantics(result) == _semantics(layout)


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda f: f.__name__)
def test_round_trip_accepts_a_dict_as_well_as_a_string(fixture) -> None:
    layout = fixture()
    assert compile_operations(layout, []) == compile_operations(json.dumps(layout), [])


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda f: f.__name__)
def test_round_trip_is_idempotent(fixture) -> None:
    once = compile_operations(json.dumps(fixture()), [])
    assert compile_operations(once, []) == once


def test_serialisation_is_byte_identical_across_runs() -> None:
    operations = [
        {"operation": "make_full_width", "chart_id": 102},
        {"operation": "move_to_top", "chart_id": 104},
    ]
    first = compile_operations(json.dumps(three_chart_dashboard()), operations)
    second = compile_operations(json.dumps(three_chart_dashboard()), operations)
    assert first == second


def test_key_order_of_the_input_does_not_change_the_output() -> None:
    layout = three_chart_dashboard()
    reversed_layout = dict(reversed(list(layout.items())))
    assert compile_operations(layout, []) == compile_operations(reversed_layout, [])


# --- The NULL position_json case (integration finding 4) ------------------


@pytest.mark.parametrize("raw", [None, "", "   ", "null", "{}", {}, "{ }"])
def test_null_position_json_yields_a_valid_skeleton(raw) -> None:
    tree = parse_position_json(raw)
    assert set(tree.nodes) == {ROOT_ID, GRID_ID}
    assert tree.nodes[ROOT_ID]["children"] == [GRID_ID]
    assert tree.nodes[GRID_ID]["type"] == "GRID"
    assert tree.nodes[GRID_ID]["parents"] == [ROOT_ID]
    assert tree.extras[VERSION_KEY] == "v2"


def test_null_position_json_compiles_without_operations() -> None:
    skeleton = json.loads(compile_operations(None, []))
    assert skeleton[ROOT_ID]["children"] == [GRID_ID]
    assert skeleton[GRID_ID]["children"] == []


def test_first_chart_on_a_freshly_created_dashboard() -> None:
    result = json.loads(
        compile_operations(None, [{"operation": "add_chart", "chart_id": 7, "slice_name": "Orders"}])
    )
    grid_children = result[GRID_ID]["children"]
    assert len(grid_children) == 1
    row = result[grid_children[0]]
    assert row["type"] == "ROW"
    chart = result[row["children"][0]]
    assert chart["type"] == "CHART"
    assert chart["meta"]["chartId"] == 7
    assert chart["meta"]["sliceName"] == "Orders"
    assert chart["parents"] == [ROOT_ID, GRID_ID, row["id"]]


# --- make_full_width ------------------------------------------------------


def test_make_full_width_sets_twelve_and_keeps_the_node_id() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "make_full_width", "chart_id": 102}],
        )
    )
    assert "CHART-bbb" in result
    assert result["CHART-bbb"]["id"] == "CHART-bbb"
    assert result["CHART-bbb"]["meta"]["width"] == GRID_COLUMN_COUNT
    assert result["CHART-bbb"]["meta"]["chartId"] == 102


def test_make_full_width_pushes_neighbours_down_preserving_order() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "make_full_width", "chart_id": 102}],
        )
    )
    order = [
        chart
        for row_id in result[GRID_ID]["children"]
        for chart in result[row_id]["children"]
    ]
    assert order == ["CHART-aaa", "CHART-bbb", "CHART-ccc", "CHART-ddd"]
    rows = result[GRID_ID]["children"]
    widths_per_row = [
        sum(result[c]["meta"]["width"] for c in result[r]["children"]) for r in rows
    ]
    assert all(total <= GRID_COLUMN_COUNT for total in widths_per_row)


def test_make_full_width_inside_a_tab_stays_in_the_tab() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(tabbed_dashboard()),
            [{"operation": "make_full_width", "chart_id": 201}],
        )
    )
    assert result["CHART-t1"]["parents"][:3] == [ROOT_ID, "TABS-1", "TAB-1"]
    assert result["CHART-t2"]["parents"][:3] == [ROOT_ID, "TABS-1", "TAB-1"]


def test_make_half_width() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "make_half_width", "chart_id": 104}],
        )
    )
    assert result["CHART-ddd"]["meta"]["width"] == 6


# --- Copy on write --------------------------------------------------------


def test_apply_operation_does_not_mutate_the_input_tree() -> None:
    layout = three_chart_dashboard()
    tree = parse_position_json(json.dumps(layout))
    before = serialise(tree)
    apply_operation(tree, {"operation": "make_full_width", "chart_id": 102})
    assert serialise(tree) == before


def test_a_failing_operation_leaves_the_tree_untouched() -> None:
    tree = parse_position_json(json.dumps(three_chart_dashboard()))
    before = serialise(tree)
    with pytest.raises(LayoutError):
        apply_operation(tree, {"operation": "make_full_width", "chart_id": 999})
    assert serialise(tree) == before


# --- Errors the model has to be able to act on ----------------------------


def test_unknown_operation_lists_the_supported_ones() -> None:
    tree = parse_position_json(None)
    with pytest.raises(LayoutError) as excinfo:
        apply_operation(tree, {"operation": "rotate_widget", "chart_id": 1})
    message = str(excinfo.value)
    assert "rotate_widget" in message
    assert "make_full_width" in message


def test_unknown_chart_names_the_charts_that_are_present() -> None:
    tree = parse_position_json(json.dumps(three_chart_dashboard()))
    with pytest.raises(LayoutError) as excinfo:
        apply_operation(tree, {"operation": "make_full_width", "chart_id": 999})
    message = str(excinfo.value)
    assert "999" in message
    assert "101" in message


def test_a_chart_present_twice_is_refused_as_ambiguous() -> None:
    layout = three_chart_dashboard()
    layout["CHART-dup"] = _chart("CHART-dup", 101)
    layout["ROW-bottom"]["children"].append("CHART-dup")
    tree = parse_position_json(layout)
    with pytest.raises(LayoutError, match="ambiguous"):
        apply_operation(tree, {"operation": "make_full_width", "chart_id": 101})


def test_moving_a_chart_relative_to_itself_is_refused() -> None:
    tree = parse_position_json(json.dumps(three_chart_dashboard()))
    with pytest.raises(LayoutError, match="itself"):
        apply_operation(
            tree, {"operation": "move_after", "chart_id": 101, "target_chart_id": 101}
        )


def test_malformed_json_is_reported_as_such() -> None:
    with pytest.raises(LayoutError, match="not valid JSON"):
        parse_position_json("{not json")


def test_a_dangling_child_reference_is_refused() -> None:
    layout = three_chart_dashboard()
    layout["ROW-top"]["children"].append("CHART-missing")
    with pytest.raises(LayoutError, match="does not"):
        parse_position_json(layout)


def test_a_layout_without_a_root_is_refused() -> None:
    with pytest.raises(LayoutError, match="ROOT_ID"):
        parse_position_json({"CHART-x": _chart("CHART-x", 1)})


def test_a_node_whose_id_disagrees_with_its_key_is_refused() -> None:
    layout = three_chart_dashboard()
    layout["CHART-aaa"]["id"] = "CHART-elsewhere"
    with pytest.raises(LayoutError, match="inconsistent"):
        parse_position_json(layout)


# --- move -----------------------------------------------------------------


def test_move_after_reorders_within_a_row() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "move_after", "chart_id": 101, "target_chart_id": 103}],
        )
    )
    assert result["ROW-top"]["children"] == ["CHART-bbb", "CHART-ccc", "CHART-aaa"]


def test_move_after_across_rows_prunes_the_emptied_row() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "move_after", "chart_id": 104, "target_chart_id": 101}],
        )
    )
    assert "ROW-bottom" not in result
    # 4 + 12 + 4 + 4 overflows, so the row is line-broken rather than squashed,
    # and reading order is preserved across the new rows.
    order = [
        chart for row_id in result[GRID_ID]["children"] for chart in result[row_id]["children"]
    ]
    assert order == ["CHART-aaa", "CHART-ddd", "CHART-bbb", "CHART-ccc"]
    assert result["ROW-top"]["children"] == ["CHART-aaa"]


def test_move_before() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "move_before", "chart_id": 103, "target_chart_id": 101}],
        )
    )
    assert result["ROW-top"]["children"] == ["CHART-ccc", "CHART-aaa", "CHART-bbb"]


def test_move_to_top_puts_the_chart_in_its_own_first_row() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "move_to_top", "chart_id": 103}],
        )
    )
    first_row = result[result[GRID_ID]["children"][0]]
    assert first_row["children"] == ["CHART-ccc"]
    assert result["ROW-top"]["children"] == ["CHART-aaa", "CHART-bbb"]


def test_move_to_top_is_a_no_op_when_already_at_the_top() -> None:
    layout = three_chart_dashboard()
    once = compile_operations(json.dumps(layout), [{"operation": "move_to_top", "chart_id": 103}])
    twice = compile_operations(once, [{"operation": "move_to_top", "chart_id": 103}])
    assert twice == once


def test_move_to_bottom() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "move_to_bottom", "chart_id": 101}],
        )
    )
    last_row = result[result[GRID_ID]["children"][-1]]
    assert last_row["children"] == ["CHART-aaa"]


# --- kpi_row --------------------------------------------------------------


def test_kpi_row_gathers_charts_into_one_row_at_the_top() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "kpi_row", "chart_ids": [104, 101, 102]}],
        )
    )
    top_row_id = result[GRID_ID]["children"][0]
    assert result[top_row_id]["children"] == ["CHART-ddd", "CHART-aaa", "CHART-bbb"]
    widths = [result[c]["meta"]["width"] for c in result[top_row_id]["children"]]
    assert widths == [4, 4, 4]
    assert all(result[c]["meta"]["height"] == KPI_ROW_HEIGHT for c in result[top_row_id]["children"])
    assert "ROW-bottom" not in result
    assert result["ROW-top"]["children"] == ["CHART-ccc"]


def test_kpi_row_fills_the_grid_exactly_with_five_tiles() -> None:
    layout = three_chart_dashboard()
    layout["CHART-eee"] = _chart("CHART-eee", 105)
    layout["ROW-bottom"]["children"].append("CHART-eee")
    result = json.loads(
        compile_operations(layout, [{"operation": "kpi_row", "chart_ids": [101, 102, 103, 104, 105]}])
    )
    top_row_id = result[GRID_ID]["children"][0]
    widths = [result[c]["meta"]["width"] for c in result[top_row_id]["children"]]
    assert widths == [3, 3, 2, 2, 2]
    assert sum(widths) == GRID_COLUMN_COUNT


def test_kpi_row_refuses_a_repeated_chart() -> None:
    tree = parse_position_json(three_chart_dashboard())
    with pytest.raises(LayoutError, match="more than once"):
        apply_operation(tree, {"operation": "kpi_row", "chart_ids": [101, 101]})


# --- equalize_row ---------------------------------------------------------


def test_equalize_row_gives_equal_widths_and_the_tallest_height() -> None:
    layout = three_chart_dashboard()
    layout["CHART-bbb"]["meta"]["height"] = 80
    layout["CHART-ccc"]["meta"]["width"] = 2
    result = json.loads(
        compile_operations(layout, [{"operation": "equalize_row", "chart_id": 101}])
    )
    for node_id in ("CHART-aaa", "CHART-bbb", "CHART-ccc"):
        assert result[node_id]["meta"]["width"] == 4
        assert result[node_id]["meta"]["height"] == 80


def test_equalize_row_can_be_targeted_by_row_id() -> None:
    tree = parse_position_json(three_chart_dashboard())
    result = apply_operation(tree, {"operation": "equalize_row", "chart_id": "ROW-top"})
    assert result.nodes["CHART-ccc"]["meta"]["width"] == 4


def test_equalize_row_refuses_a_row_it_cannot_fit() -> None:
    layout = three_chart_dashboard()
    for index in range(13):
        node_id = f"CHART-x{index}"
        layout[node_id] = _chart(node_id, 300 + index, width=1)
        layout["ROW-bottom"]["children"].append(node_id)
    tree = parse_position_json(layout)
    with pytest.raises(LayoutError, match="cannot each get"):
        apply_operation(tree, {"operation": "equalize_row", "chart_id": 104})


# --- add / remove ---------------------------------------------------------


def test_add_chart_appends_into_the_last_row_when_it_fits() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "add_chart", "chart_id": 500, "placement": "append", "width": 4}],
        )
    )
    # ROW-bottom already holds a 12-wide chart, so the newcomer is line-broken
    # into a fresh row rather than overflowing it.
    added = [
        node_id
        for node_id, node in result.items()
        if isinstance(node, dict) and node.get("meta", {}).get("chartId") == 500
    ]
    assert len(added) == 1
    assert added[0].startswith("CHART-")
    last_row = result[result[GRID_ID]["children"][-1]]
    assert last_row["children"] == added
    assert result["ROW-bottom"]["children"] == ["CHART-ddd"]


def test_add_chart_refuses_a_chart_already_present() -> None:
    tree = parse_position_json(three_chart_dashboard())
    with pytest.raises(LayoutError, match="already on this dashboard"):
        apply_operation(tree, {"operation": "add_chart", "chart_id": 101})


def test_remove_chart_prunes_the_row_it_emptied() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "remove_chart", "chart_id": 104}],
        )
    )
    assert "CHART-ddd" not in result
    assert "ROW-bottom" not in result
    assert result[GRID_ID]["children"] == ["ROW-top"]


def test_remove_chart_keeps_a_row_that_still_has_children() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "remove_chart", "chart_id": 101}],
        )
    )
    assert result["ROW-top"]["children"] == ["CHART-bbb", "CHART-ccc"]


# --- Invariants across sequences ------------------------------------------


def test_chart_node_ids_survive_a_sequence_of_operations() -> None:
    layout = three_chart_dashboard()
    original_ids = {
        node_id for node_id, node in layout.items()
        if isinstance(node, dict) and node.get("type") == "CHART"
    }
    result = json.loads(
        compile_operations(
            json.dumps(layout),
            [
                {"operation": "kpi_row", "chart_ids": [101, 102]},
                {"operation": "make_full_width", "chart_id": 104},
                {"operation": "move_after", "chart_id": 103, "target_chart_id": 104},
                {"operation": "set_height", "chart_id": 103, "height_units": 40},
            ],
        )
    )
    surviving = {n for n, v in result.items() if isinstance(v, dict) and v.get("type") == "CHART"}
    assert surviving == original_ids


def test_every_chart_stays_reachable_from_root() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [
                {"operation": "kpi_row", "chart_ids": [101, 104]},
                {"operation": "move_to_bottom", "chart_id": 102},
            ],
        )
    )
    seen: set[str] = set()
    stack = [ROOT_ID]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        stack.extend(result[node_id].get("children", []))
    charts = {n for n, v in result.items() if isinstance(v, dict) and v.get("type") == "CHART"}
    assert charts <= seen


def test_parents_are_recomputed_after_a_move() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "move_to_top", "chart_id": 103}],
        )
    )
    new_row_id = result[GRID_ID]["children"][0]
    assert result["CHART-ccc"]["parents"] == [ROOT_ID, GRID_ID, new_row_id]


def test_the_unreachable_header_node_is_left_alone() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "make_full_width", "chart_id": 101}],
        )
    )
    assert result["HEADER_ID"] == three_chart_dashboard()["HEADER_ID"]


def test_the_version_key_survives_compilation() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "make_full_width", "chart_id": 101}],
        )
    )
    assert result[VERSION_KEY] == "v2"


def test_tree_reports_its_chart_ids() -> None:
    tree: LayoutTree = parse_position_json(three_chart_dashboard())
    assert tree.chart_ids() == [101, 102, 103, 104]


# --- Bounds ---------------------------------------------------------------


def test_set_width_out_of_range_is_refused() -> None:
    tree = parse_position_json(three_chart_dashboard())
    with pytest.raises(LayoutError, match="outside the dashboard grid"):
        apply_operation(tree, {"operation": "set_width", "chart_id": 101, "columns": 13})


def test_set_height_out_of_range_is_refused() -> None:
    tree = parse_position_json(three_chart_dashboard())
    with pytest.raises(LayoutError, match="out of range"):
        apply_operation(tree, {"operation": "set_height", "chart_id": 101, "height_units": 500})


def test_set_width_within_range() -> None:
    result = json.loads(
        compile_operations(
            json.dumps(three_chart_dashboard()),
            [{"operation": "set_width", "chart_id": 101, "columns": 3}],
        )
    )
    assert result["CHART-aaa"]["meta"]["width"] == 3
