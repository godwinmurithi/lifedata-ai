"""`review_dashboard_design` — the copilot checking its own work (plan §6.4).

With layout recipes deliberately off the table, this is what replaces a recipe's
determinism. A free-form build can produce a dashboard that is *valid* and still
reads badly: no single-figure row, a pie with eleven slices, three widgets of
different heights in one row, a title that says `COUNT_DISTINCT(patient_id)`.
None of those is an error anywhere in the stack, and every one of them is the
first thing an executive notices.

Three properties make this worth having rather than being a second opinion:

* **It is a pure function** of `position_json` plus the chart list. No judgement,
  no model call, fully unit-testable, and the same dashboard always gets the same
  verdict.
* **Every finding maps onto an action the model already has.** A finding the
  copilot cannot act on is a complaint. `make_full_width`, `kpi_row`,
  `equalize_row`, `rename_chart` and `update_chart` cover all of them, and each
  finding names the one to use.
* **It runs before the copilot claims to be finished.** The system prompt says so
  explicitly. "I built it and checked it" is a different product from "I built
  it".

It was already a dead entry in the panel's label table — declared, never
implemented — so building it also retires that inconsistency.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode

#: A title longer than this stops being a label and starts being a sentence. The
#: dashboard grid truncates it anyway, so the user sees "Unique patients by
#: facility level for the last twel…".
MAX_TITLE_LENGTH = 60

#: Above this, a pie is a colour-matching exercise. Six is the same bound the
#: chart-type policy in the system prompt states.
MAX_PIE_SLICES = 6

#: A dashboard whose first row is not single-figure tiles opens on detail rather
#: than on the headline. Between two and four tiles reads as a banner.
MIN_KPI_TILES = 2
MAX_KPI_TILES = 4

#: Heights within one row that differ by more than this leave visible ragged
#: whitespace. Superset does not equalise them.
HEIGHT_TOLERANCE = 5


class ReviewDashboardDesignTool(Tool):
    """Report what is wrong with a dashboard's design, and how to fix it."""

    name: ClassVar[str] = "review_dashboard_design"
    description: ClassVar[str] = (
        "Check a dashboard against the LifeData executive layout standard and return a list "
        "of concrete problems, each naming the operation that fixes it. Call this after "
        "building or rearranging a dashboard and FIX WHAT IT REPORTS before telling the user "
        "you are done. It checks for a missing, misplaced or split row of single-figure "
        "tiles, mismatched widget heights in a row, overcrowded pie charts, missing number "
        "formats, duplicated charts, over-long titles, and legends on single-series charts."
    )
    mode: ClassVar[ToolMode] = ToolMode.READ
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["dashboard_id"],
        "properties": {
            "dashboard_id": {
                "type": "integer",
                "description": "The dashboard to review.",
            }
        },
    }

    def run(self, context: ToolContext, dashboard_id: int) -> dict[str, Any]:
        client = context.superset_client()
        try:
            dashboard = client.get_dashboard(dashboard_id)
        except SupersetError as exc:
            raise ToolError(
                f"Could not read dashboard {dashboard_id}: {exc}", code="dashboard_unreadable"
            ) from exc

        charts = _charts_of(client, dashboard, dashboard_id)
        layout = _load(dashboard.get("position_json")) or {}
        metadata = _load(dashboard.get("json_metadata")) or {}
        findings = review(layout, charts, metadata)

        return {
            "dashboard_id": dashboard_id,
            "dashboard_title": dashboard.get("dashboard_title"),
            "chart_count": len(charts),
            "findings": findings,
            "verdict": "ok" if not findings else f"{len(findings)} to fix",
            "usage": (
                "Fix these before telling the user the dashboard is ready. Each finding names "
                "the operation that resolves it. If you judge a finding not worth acting on, "
                "say so to the user in one clause rather than ignoring it silently."
            ),
        }


# --- The pure part --------------------------------------------------------


def review(
    layout: dict[str, Any],
    charts: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Every design problem in this dashboard, worst first.

    A pure function of the layout graph, the chart records and the dashboard's
    `json_metadata`, which is what makes it exhaustively testable without a
    Superset anywhere near it.
    """
    by_id = {int(c["id"]): c for c in charts if c.get("id") is not None}
    rows = _rows(layout)

    findings: list[dict[str, Any]] = []
    findings.extend(_kpi_row_findings(rows, by_id))
    findings.extend(_filter_findings(metadata or {}, bool(rows)))
    findings.extend(_row_height_findings(rows, by_id))
    findings.extend(_duplicate_findings(rows, by_id))
    for chart in charts:
        findings.extend(_chart_findings(chart))
    return findings


def _filter_findings(metadata: dict[str, Any], has_charts: bool) -> list[dict[str, Any]]:
    """Every dashboard gets a date filter, and the review must say so.

    The layout standard in the system prompt states it plainly, and this tool
    checked everything EXCEPT it — so a dashboard built without one was reported
    as "clean" while failing the one rule that is written down. A design check
    that misses the standard it exists to enforce is worse than none, because it
    is believed.

    Observed exactly that way: six charts, no filter, "the design check came back
    clean".
    """
    if not has_charts:
        return []

    findings: list[dict[str, Any]] = []
    filters = metadata.get("native_filter_configuration") or []
    if not filters:
        findings.append(
            {
                "severity": "high",
                "code": "no_date_filter",
                "problem": (
                    "This dashboard has no filters, so an executive cannot change the "
                    "period without editing every chart."
                ),
                "fix": (
                    "add_native_filter with filter_type 'time_range' — every dashboard "
                    "gets a date filter."
                ),
            }
        )
    elif not any(f.get("filterType") == "filter_time" for f in filters):
        findings.append(
            {
                "severity": "medium",
                "code": "no_date_filter",
                "problem": (
                    f"This dashboard has {len(filters)} filter(s) but none of them is a "
                    "date range, which is the one an executive reaches for first."
                ),
                "fix": "add_native_filter with filter_type 'time_range'.",
            }
        )

    if metadata.get("cross_filters_enabled") is False:
        findings.append(
            {
                "severity": "low",
                "code": "cross_filters_disabled",
                "problem": (
                    "Cross-filtering is off, so clicking a bar does not narrow the rest "
                    "of the dashboard."
                ),
                "fix": (
                    "Turn it on in the dashboard's properties; new dashboards the copilot "
                    "builds have it on by default."
                ),
            }
        )
    return findings


def _kpi_row_findings(
    rows: list[list[dict[str, Any]]], by_id: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The single-figure row must exist, and must be first.

    This is the one finding that changes what a dashboard *is* rather than how it
    looks: an executive dashboard opens on the headline numbers. Everything else
    here is polish.
    """
    if not rows:
        return []

    tile_rows = [
        index
        for index, row in enumerate(rows)
        if row and all(_viz_of(node, by_id) == "big_number_total" for node in row)
    ]
    if not tile_rows:
        available = [
            str(cid) for cid, c in by_id.items() if _viz(c) == "big_number_total"
        ]
        return [
            {
                "severity": "high",
                "code": "no_kpi_row",
                "problem": (
                    "This dashboard has no row of single-figure tiles, so it opens on detail "
                    "rather than on the headline numbers."
                ),
                "fix": (
                    "Build 2 to 4 big_number_total charts, then gather them with "
                    "move_dashboard_component kpi_row."
                    if not available
                    else f"Gather charts {', '.join(available)} with "
                         "move_dashboard_component kpi_row."
                ),
            }
        ]

    findings: list[dict[str, Any]] = []
    if tile_rows[0] != 0:
        findings.append(
            {
                "severity": "high",
                "code": "kpi_row_not_first",
                "problem": (
                    f"The row of single-figure tiles is row {tile_rows[0] + 1}, not the first "
                    "row, so the dashboard opens on something else."
                ),
                "fix": (
                    "Use move_dashboard_component kpi_row with those chart ids to move it to "
                    "the top."
                ),
            }
        )

    # EVERY tile row, not just the first. Widening one tile past the room left in
    # its row makes the compiler reflow the overflow onto a new row — by design,
    # since Superset squashes a row that sums past 12 — and the result is two
    # rows that are each entirely tiles. Looking only at `rows[tile_rows[0]]`
    # saw a tidy pair and reported the dashboard clean while the third tile sat
    # orphaned underneath, which is exactly what an acceptance run produced: a
    # 6 + 6 row above a lone 4. The copilot then repeated "the design check is
    # clean" to the user, about a layout they were looking at and could see was
    # not.
    tiles = [node for index in tile_rows for node in rows[index]]
    if len(tile_rows) > 1:
        ids = [str(_chart_id(node)) for node in tiles if _chart_id(node)]
        findings.append(
            {
                "severity": "high",
                "code": "kpi_row_split",
                "problem": (
                    f"The {len(tiles)} single-figure tiles are spread over {len(tile_rows)} "
                    "rows instead of sitting together across the top, so they read as "
                    "separate sections rather than as one summary."
                ),
                "fix": (
                    "Gather them with move_dashboard_component kpi_row"
                    + (f" using chart ids {', '.join(ids)}." if ids else ".")
                    + " That also gives them an even share of the row, which is what "
                    "makes them read as a set."
                ),
            }
        )
    if len(tiles) > MAX_KPI_TILES:
        findings.append(
            {
                "severity": "medium",
                "code": "too_many_kpi_tiles",
                "problem": (
                    f"{len(tiles)} single-figure tiles across the top is more than reads at a "
                    f"glance; {MAX_KPI_TILES} is the maximum."
                ),
                "fix": (
                    "Move the least important tiles into the body of the dashboard with "
                    "move_dashboard_component move_to_bottom, or remove them."
                ),
            }
        )
    elif len(tiles) < MIN_KPI_TILES:
        findings.append(
            {
                "severity": "low",
                "code": "too_few_kpi_tiles",
                "problem": (
                    "A single tile across the top reads as an orphan rather than as a summary."
                ),
                "fix": (
                    "Add another one or two big_number_total charts and include them in the "
                    "kpi_row."
                ),
            }
        )
    return findings


def _row_height_findings(
    rows: list[list[dict[str, Any]]], by_id: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Widgets of different heights in one row leave ragged whitespace."""
    findings: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if len(row) < 2:
            continue
        heights = [_meta(node).get("height") for node in row]
        heights = [h for h in heights if isinstance(h, int)]
        if len(heights) < 2:
            continue
        if max(heights) - min(heights) > HEIGHT_TOLERANCE:
            anchor = _chart_id(row[0])
            findings.append(
                {
                    "severity": "medium",
                    "code": "mixed_row_heights",
                    "problem": (
                        f"Row {index + 1} mixes widget heights ({min(heights)} to "
                        f"{max(heights)}), which leaves visible gaps."
                    ),
                    "fix": (
                        f"Use resize_dashboard_component equalize_row with chart_id "
                        f"{anchor} to even the row out."
                    ),
                }
            )
    return findings


def _duplicate_findings(
    rows: list[list[dict[str, Any]]], by_id: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The same chart placed twice is almost always a mistake mid-build."""
    counts: dict[int, int] = {}
    for row in rows:
        for node in row:
            chart_id = _chart_id(node)
            if isinstance(chart_id, int):
                counts[chart_id] = counts.get(chart_id, 0) + 1

    findings: list[dict[str, Any]] = []
    for chart_id, count in sorted(counts.items()):
        if count > 1:
            name = (by_id.get(chart_id) or {}).get("slice_name") or f"chart {chart_id}"
            findings.append(
                {
                    "severity": "high",
                    "code": "duplicate_chart",
                    "problem": f"“{name}” appears on this dashboard {count} times.",
                    "fix": (
                        f"Remove the extra copies with remove_chart_from_dashboard for chart "
                        f"{chart_id}. Note that a chart placed twice is also ambiguous to the "
                        "layout operations, which refuse to move it."
                    ),
                }
            )
    return findings


def _chart_findings(chart: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-chart problems: titles, formats, slice counts, redundant legends."""
    findings: list[dict[str, Any]] = []
    chart_id = chart.get("id")
    name = chart.get("slice_name") or ""
    params = _load(chart.get("params")) or {}
    viz = _viz(chart)

    if len(name) > MAX_TITLE_LENGTH:
        findings.append(
            {
                "severity": "medium",
                "code": "title_too_long",
                "chart_id": chart_id,
                "problem": (
                    f"“{name[:70]}…” is {len(name)} characters; the dashboard grid truncates "
                    f"anything past about {MAX_TITLE_LENGTH}."
                ),
                "fix": f"rename_chart {chart_id} to something under {MAX_TITLE_LENGTH} characters.",
            }
        )

    if _looks_like_an_expression(name):
        findings.append(
            {
                "severity": "high",
                "code": "title_is_an_expression",
                "chart_id": chart_id,
                "problem": (
                    f"“{name}” is a query expression, not a title. An executive should never "
                    "be shown one."
                ),
                "fix": (
                    f"rename_chart {chart_id} to plain English — 'Unique patients', not "
                    "'COUNT_DISTINCT(patient_id)'."
                ),
            }
        )

    if viz == "pie":
        limit = params.get("row_limit")
        if not isinstance(limit, int) or limit > MAX_PIE_SLICES:
            findings.append(
                {
                    "severity": "medium",
                    "code": "pie_too_many_slices",
                    "chart_id": chart_id,
                    "problem": (
                        f"“{name}” is a pie chart with no slice limit, so it can render as an "
                        "unreadable ring of colours."
                    ),
                    "fix": (
                        f"update_chart {chart_id} with params.row_limit of {MAX_PIE_SLICES} or "
                        "fewer, or use echarts_timeseries_bar instead — a bar chart compares "
                        "many categories perfectly well."
                    ),
                }
            )

    if viz == "big_number_total" and not _has_number_format(params):
        findings.append(
            {
                "severity": "medium",
                "code": "no_number_format",
                "chart_id": chart_id,
                "problem": (
                    f"“{name}” is a single-figure tile with no number format, so it can render "
                    "as 1234567.891."
                ),
                "fix": (
                    f"update_chart {chart_id} with params.number_format (',d' for a count, "
                    "'.1%' for a rate), or use a saved metric that already declares one."
                ),
            }
        )

    if params.get("show_legend") and not _is_multi_series(params):
        findings.append(
            {
                "severity": "low",
                "code": "redundant_legend",
                "chart_id": chart_id,
                "problem": (
                    f"“{name}” shows a legend for a single series, which spends space "
                    "restating the title."
                ),
                "fix": f"update_chart {chart_id} with params.show_legend set to false.",
            }
        )

    return findings


# --- Layout reading -------------------------------------------------------


def _rows(layout: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """The dashboard's chart nodes, grouped by row, in visual order.

    Walks from `ROOT_ID` rather than iterating the flat mapping, because the
    mapping has no order and the reading order *is* the design being reviewed.
    """
    rows: list[list[dict[str, Any]]] = []

    def walk(node_id: str, seen: set[str]) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        node = layout.get(node_id)
        if not isinstance(node, dict):
            return
        if node.get("type") == "ROW":
            charts = [
                layout[child]
                for child in node.get("children", [])
                if isinstance(layout.get(child), dict)
                and layout[child].get("type") == "CHART"
            ]
            if charts:
                rows.append(charts)
            # A ROW can nest a COLUMN holding further charts; keep walking.
        for child in node.get("children", []):
            if isinstance(child, str):
                walk(child, seen)

    walk("ROOT_ID", set())
    return rows


def _meta(node: dict[str, Any]) -> dict[str, Any]:
    meta = node.get("meta")
    return meta if isinstance(meta, dict) else {}


def _chart_id(node: dict[str, Any]) -> Any:
    return _meta(node).get("chartId")


def _viz_of(node: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> str:
    chart_id = _chart_id(node)
    if not isinstance(chart_id, int):
        return ""
    return _viz(by_id.get(chart_id) or {})


def _viz(chart: dict[str, Any]) -> str:
    if chart.get("viz_type"):
        return str(chart["viz_type"])
    params = _load(chart.get("params")) or {}
    return str(params.get("viz_type") or "")


def _has_number_format(params: dict[str, Any]) -> bool:
    """Whether the tile will format its number.

    Both spellings count. Superset reads `y_axis_format` in some builds and
    `number_format` in others for `big_number_total`, and a chart using a saved
    metric that declares a `d3format` needs neither — but that cannot be seen
    from here, so a false positive costs one `update_chart` and a false negative
    costs an executive seeing 1234567.891.
    """
    return bool(params.get("number_format") or params.get("y_axis_format"))


def _is_multi_series(params: dict[str, Any]) -> bool:
    return bool(params.get("groupby")) or len(params.get("metrics") or []) > 1


def _looks_like_an_expression(name: str) -> bool:
    """Whether a chart title is a query expression that escaped into the UI."""
    upper = name.upper()
    return "(" in name and any(
        token in upper
        for token in ("COUNT", "SUM(", "AVG(", "MIN(", "MAX(", "DISTINCT")
    )


def _load(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _charts_of(
    client: Any, dashboard: dict[str, Any], dashboard_id: int
) -> list[dict[str, Any]]:
    """The dashboard's charts with enough detail to review them.

    `get_dashboard` returns chart stubs without `params`, and `params` is where
    the formats, legends and row limits live — so each chart is fetched. Bounded
    by how many charts fit on a dashboard, and a failed read degrades that one
    chart to a stub rather than failing the review.
    """
    charts: list[dict[str, Any]] = []
    for stub in dashboard.get("charts") or []:
        chart_id = stub.get("id") if isinstance(stub, dict) else None
        if chart_id is None:
            continue
        try:
            charts.append(client.get_chart(int(chart_id)))
        except SupersetError:
            charts.append(stub)
    if charts:
        return charts

    # Some Superset versions return `charts` as a list of names rather than
    # objects. Fall back to the ids in the layout, which are authoritative.
    layout = _load(dashboard.get("position_json")) or {}
    for node in layout.values():
        if not isinstance(node, dict) or node.get("type") != "CHART":
            continue
        chart_id = _meta(node).get("chartId")
        if not isinstance(chart_id, int):
            continue
        try:
            charts.append(client.get_chart(chart_id))
        except SupersetError:
            continue
    return charts


REVIEW_TOOL_CLASSES: list[type[Tool]] = [ReviewDashboardDesignTool]
