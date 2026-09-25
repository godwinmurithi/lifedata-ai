"""`review_dashboard_design` — the checks that replace a layout recipe.

With recipes deliberately off the table, this tool is what stops a free-form
build producing a dashboard that is *valid* and reads badly. Every test here is
a dashboard an executive would have complained about.

`review()` is a pure function of the layout graph and the chart records, so
none of this needs a Superset: the fixtures below build `position_json` the way
the layout compiler does.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lifedata_ai.tools.review import MAX_PIE_SLICES, MAX_TITLE_LENGTH, review
from lifedata_ai.validation.layout import compile_operations


def layout(*rows: list[int]) -> dict[str, Any]:
    """A dashboard layout with the given chart ids, one list per row.

    Built through the real compiler rather than by hand, so a change to the
    node shape breaks these tests instead of letting them assert against a
    layout Superset would no longer recognise.
    """
    operations: list[dict[str, Any]] = []
    for row in rows:
        for index, chart_id in enumerate(row):
            operations.append(
                {
                    "operation": "add_chart",
                    "chart_id": chart_id,
                    "placement": "append" if index else "new_row",
                }
            )
    return json.loads(compile_operations(None, operations))


def kpi_layout(tile_ids: list[int], *rows: list[int]) -> dict[str, Any]:
    """A layout whose first row is a real KPI row.

    Built by applying the compiler's own `kpi_row` operation, because that is
    what sets the tile widths — five charts placed with `append` sum past twelve
    columns and the compiler reflows them into two rows, which is correct
    behaviour and not what these tests are about.
    """
    operations: list[dict[str, Any]] = []
    for row in (list(tile_ids), *rows):
        for index, chart_id in enumerate(row):
            operations.append(
                {
                    "operation": "add_chart",
                    "chart_id": chart_id,
                    "placement": "append" if index else "new_row",
                }
            )
    operations.append({"operation": "kpi_row", "chart_ids": tile_ids})
    return json.loads(compile_operations(None, operations))


def chart(
    chart_id: int,
    viz_type: str = "echarts_timeseries_bar",
    name: str = "A chart",
    **params: Any,
) -> dict[str, Any]:
    return {
        "id": chart_id,
        "slice_name": name,
        "viz_type": viz_type,
        "params": json.dumps({"viz_type": viz_type, **params}),
    }


def tile(chart_id: int, name: str = "Unique patients", **params: Any) -> dict[str, Any]:
    defaults = {"metrics": ["unique_patients"], "number_format": ",d"}
    defaults.update(params)
    return chart(chart_id, "big_number_total", name, **defaults)


def codes(findings: list[dict[str, Any]]) -> set[str]:
    return {finding["code"] for finding in findings}


def find(findings: list[dict[str, Any]], code: str) -> dict[str, Any]:
    matches = [f for f in findings if f["code"] == code]
    assert matches, f"expected a {code!r} finding, got {sorted(codes(findings))}"
    return matches[0]


# --- The shape of an executive dashboard ---------------------------------


class TestTheKpiRow:
    """The one finding that changes what a dashboard IS, not how it looks."""

    def test_a_dashboard_with_no_single_figure_row_is_reported(self) -> None:
        findings = review(layout([1, 2], [3]), [chart(1), chart(2), chart(3)])

        finding = find(findings, "no_kpi_row")
        assert finding["severity"] == "high"
        # A finding the copilot cannot act on is a complaint, so every one names
        # the operation that resolves it.
        assert "big_number_total" in finding["fix"]

    def test_the_fix_names_the_tiles_that_already_exist(self) -> None:
        """Different advice when the tiles are built but scattered.

        Two `big_number_total` charts sitting in rows beside other things is a
        half-finished build, and the useful instruction is "gather these",
        not "build some".
        """
        findings = review(
            layout([1, 7], [2, 8]),
            [tile(1), chart(7), tile(2, name="Encounters"), chart(8)],
        )

        finding = find(findings, "no_kpi_row")
        assert "kpi_row" in finding["fix"]
        assert "1" in finding["fix"] and "2" in finding["fix"]

    def test_a_correct_dashboard_reports_nothing(self) -> None:
        """Tiles first, a breakdown below, and a date filter — the standard.

        The filter is part of it: `_filter_findings` reports its absence, so a
        dashboard without one is not "correct" however good its layout.
        """
        findings = review(
            layout([1, 2], [3]),
            [tile(1), tile(2, name="Encounters"), chart(3, name="Orders by facility")],
            {"native_filter_configuration": [{"filterType": "filter_time"}]},
        )

        assert findings == []

    def test_a_tile_row_below_the_fold_is_reported(self) -> None:
        findings = review(
            layout([3], [1, 2]),
            [chart(3), tile(1), tile(2, name="Encounters")],
        )

        finding = find(findings, "kpi_row_not_first")
        assert "kpi_row" in finding["fix"]

    def test_too_many_tiles_stop_reading_at_a_glance(self) -> None:
        charts = [tile(n, name=f"Metric {n}") for n in range(1, 6)] + [chart(9)]
        findings = review(kpi_layout([1, 2, 3, 4, 5], [9]), charts)

        assert "too_many_kpi_tiles" in codes(findings)

    def test_a_single_tile_reads_as_an_orphan(self) -> None:
        findings = review(layout([1], [2]), [tile(1), chart(2)])

        assert "too_few_kpi_tiles" in codes(findings)

    def test_tiles_split_across_two_rows_are_reported(self) -> None:
        """The shape an acceptance run actually produced.

        Widening one tile past the room left in its row makes the layout
        compiler reflow the overflow onto a new row — deliberately, because
        Superset squashes a row that sums past 12. The result is two rows that
        are each entirely tiles, and the review used to look only at the first
        one: a tidy pair, nothing to report. The third tile sat orphaned
        underneath, and the copilot told the user the design check was clean
        about a layout they could see was not.
        """
        findings = review(
            layout([1, 2], [3], [9]),
            [tile(1), tile(2, name="Encounters"), tile(3, name="Turnaround"), chart(9)],
        )

        finding = find(findings, "kpi_row_split")
        assert finding["severity"] == "high"
        assert "kpi_row" in finding["fix"]
        # Every id, so the copilot can act without going back to look.
        for chart_id in ("1", "2", "3"):
            assert chart_id in finding["fix"]

    def test_split_tiles_are_counted_together(self) -> None:
        """Five tiles are too many however they are spread.

        The count used to come from the first tile row alone, so splitting them
        hid the problem as well as creating one.
        """
        charts = [tile(n, name=f"Metric {n}") for n in range(1, 6)] + [chart(9)]
        findings = review(layout([1, 2, 3], [4, 5], [9]), charts)

        assert "too_many_kpi_tiles" in codes(findings)
        assert "kpi_row_split" in codes(findings)


class TestRowGeometry:
    def test_mixed_heights_in_one_row_are_reported(self) -> None:
        """Superset does not equalise them, so the row renders ragged."""
        position = layout([1, 2], [3])
        for node in position.values():
            if isinstance(node, dict) and node.get("meta", {}).get("chartId") == 2:
                node["meta"]["height"] = 90

        findings = review(position, [tile(3), chart(1), chart(2)])

        finding = find(findings, "mixed_row_heights")
        assert "equalize_row" in finding["fix"]

    def test_a_small_difference_is_left_alone(self) -> None:
        position = layout([1, 2], [3])
        for node in position.values():
            if isinstance(node, dict) and node.get("meta", {}).get("chartId") == 2:
                node["meta"]["height"] = 52

        findings = review(position, [tile(3), chart(1), chart(2)])

        assert "mixed_row_heights" not in codes(findings)

    def test_the_same_chart_placed_twice_is_reported(self) -> None:
        """Also ambiguous to the layout operations, which refuse to move it."""
        position = layout([1, 2])
        # `add_chart` refuses a duplicate, so the second node is grafted on the
        # way a mid-build mistake would produce it.
        duplicate = dict(
            (k, v) for k, v in position.items() if isinstance(v, dict) and v.get("type") == "CHART"
        )
        node_id, node = next(
            (k, v) for k, v in duplicate.items() if v["meta"]["chartId"] == 1
        )
        clone_id = node_id + "-clone"
        position[clone_id] = {**node, "id": clone_id}
        row_id = next(
            k for k, v in position.items()
            if isinstance(v, dict) and v.get("type") == "ROW" and node_id in v["children"]
        )
        position[row_id]["children"].append(clone_id)

        findings = review(position, [tile(1), tile(2, name="Encounters")])

        finding = find(findings, "duplicate_chart")
        assert "remove_chart_from_dashboard" in finding["fix"]


# --- Per-chart polish -----------------------------------------------------


class TestChartFindings:
    def test_a_title_that_is_a_query_expression_is_reported(self) -> None:
        """An executive should never be shown one, and they leak in easily."""
        findings = review(
            layout([1], [2]),
            [tile(1), tile(2, name="COUNT_DISTINCT(patient_id)")],
        )

        finding = find(findings, "title_is_an_expression")
        assert finding["severity"] == "high"
        assert "rename_chart" in finding["fix"]

    def test_an_over_long_title_is_reported(self) -> None:
        long_title = "Unique patients by facility level for the last twelve months, monthly"
        assert len(long_title) > MAX_TITLE_LENGTH

        findings = review(layout([1], [2]), [tile(1), chart(2, name=long_title)])

        assert "title_too_long" in codes(findings)

    def test_a_pie_with_no_slice_limit_is_reported(self) -> None:
        findings = review(
            layout([1], [2]),
            [tile(1), chart(2, "pie", name="Orders by test")],
        )

        finding = find(findings, "pie_too_many_slices")
        assert str(MAX_PIE_SLICES) in finding["fix"]

    def test_a_bounded_pie_is_left_alone(self) -> None:
        findings = review(
            layout([1], [2]),
            [tile(1), chart(2, "pie", name="Orders by test", row_limit=5)],
        )

        assert "pie_too_many_slices" not in codes(findings)

    def test_an_unformatted_tile_is_reported(self) -> None:
        """Otherwise the tile renders 1234567.891 to an executive."""
        findings = review(
            layout([1, 2]),
            [tile(1), chart(2, "big_number_total", name="Encounters", metrics=["encounters"])],
        )

        finding = find(findings, "no_number_format")
        assert "number_format" in finding["fix"]

    @pytest.mark.parametrize("key", ["number_format", "y_axis_format"])
    def test_either_format_spelling_counts(self, key: str) -> None:
        """Superset reads one in some builds and the other in others."""
        formatted = chart(
            2, "big_number_total", name="Encounters", metrics=["encounters"], **{key: ",d"}
        )
        findings = review(layout([1, 2]), [tile(1), formatted])

        assert "no_number_format" not in codes(findings)

    def test_a_legend_on_a_single_series_chart_is_reported(self) -> None:
        findings = review(
            layout([1], [2]),
            [tile(1), chart(2, name="Orders", metrics=["orders"], show_legend=True)],
        )

        finding = find(findings, "redundant_legend")
        assert finding["severity"] == "low"

    def test_a_legend_on_a_grouped_chart_is_left_alone(self) -> None:
        findings = review(
            layout([1], [2]),
            [
                tile(1),
                chart(
                    2,
                    name="Orders",
                    metrics=["orders"],
                    groupby=["test_type"],
                    show_legend=True,
                ),
            ],
        )

        assert "redundant_legend" not in codes(findings)


# --- Degenerate input -----------------------------------------------------


def test_an_empty_dashboard_reports_nothing_to_fix() -> None:
    """A dashboard with no charts is mid-build, not badly designed."""
    assert review({}, []) == []


def test_every_finding_names_an_operation_the_copilot_has() -> None:
    """A finding the model cannot act on is a complaint, not a review.

    Checked across a deliberately terrible dashboard so every branch fires.
    """
    findings = review(
        layout([1, 2], [3]),
        [
            chart(1, "pie", name="COUNT(*) by facility_name"),
            chart(2, name="Orders", metrics=["orders"], show_legend=True),
            chart(3, "big_number_total", name="Encounters", metrics=["encounters"]),
        ],
    )

    assert findings, "the terrible dashboard produced no findings"
    actions = (
        "create_chart", "update_chart", "rename_chart", "kpi_row", "equalize_row",
        "remove_chart_from_dashboard", "big_number_total", "echarts_timeseries_bar",
        "move_dashboard_component", "add_native_filter",
    )
    for finding in findings:
        assert any(action in finding["fix"] for action in actions), (
            f"{finding['code']} names no action the copilot can take: {finding['fix']}"
        )


# --- Filters --------------------------------------------------------------


DATE_FILTER = {"native_filter_configuration": [{"filterType": "filter_time"}]}


class TestFilters:
    """The layout standard says every dashboard gets a date filter.

    This tool checked everything EXCEPT that — the one rule actually written
    down — so a dashboard built without one was reported as "clean". Observed
    exactly that way in a browser: six charts, no filter, "the design check came
    back clean". A design check that misses its own standard is worse than none,
    because it is believed.
    """

    def test_a_dashboard_with_no_filters_is_reported(self) -> None:
        findings = review(layout([1, 2], [3]), [tile(1), tile(2, name="Orders"), chart(3)])

        finding = find(findings, "no_date_filter")
        assert finding["severity"] == "high"
        assert "add_native_filter" in finding["fix"]
        assert "time_range" in finding["fix"]

    def test_a_dashboard_with_only_a_value_filter_is_still_reported(self) -> None:
        """A facility picker is not the one an executive reaches for first."""
        findings = review(
            layout([1, 2], [3]),
            [tile(1), tile(2, name="Orders"), chart(3)],
            {"native_filter_configuration": [{"filterType": "filter_select"}]},
        )

        assert "no_date_filter" in codes(findings)

    def test_a_dashboard_with_a_date_filter_passes(self) -> None:
        findings = review(
            layout([1, 2], [3]),
            [tile(1), tile(2, name="Orders"), chart(3)],
            DATE_FILTER,
        )

        assert "no_date_filter" not in codes(findings)

    def test_disabled_cross_filtering_is_reported(self) -> None:
        findings = review(
            layout([1, 2], [3]),
            [tile(1), tile(2, name="Orders"), chart(3)],
            {**DATE_FILTER, "cross_filters_enabled": False},
        )

        assert "cross_filters_disabled" in codes(findings)

    def test_an_empty_dashboard_is_not_nagged_about_filters(self) -> None:
        """Nothing to filter yet — it is mid-build, not badly designed."""
        assert review({}, [], {}) == []
