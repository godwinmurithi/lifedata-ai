"""Ask Superset whether a chart will actually run, before anyone saves it.

# Why this exists

The API accepting a chart and the chart drawing something are different facts,
and this project learned the difference the expensive way. Five of the first
eight charts the copilot built came back `2xx` and then rendered a red **Data
error** on the dashboard, because `x_axis` and `groupby` named the same column
and Superset refuses that query as *"Duplicate column/metric labels"*. Nothing
server-side had any reason to notice: the payload was well-formed, the columns
existed, the validator has no network access and could only check shape.

So before an envelope is signed, the query is run — capped at two rows, purely
to see whether it runs at all. Superset's own MCP service does the same thing
(`ChartDataCommand` with `row_limit=2`), which is where the idea came from.

# What it catches, and what it cannot

Caught, at proposal time, with a message the model can act on. Verified against
6.1.0, each one by building it and watching it refuse:

* **a column that does not exist** — *"Columns missing in dataset:
  ['not_a_column']"*;
* **a metric that does not exist** — *"Metric 'no_such_metric' does not exist"*;
* **a query that returns nothing**, which renders as an empty widget, and the
  ungrouped variant that returns a single row of zeros and renders a confident
  `0`. Neither is refused — a quiet period is a real answer — but both are said
  out loud, because a tile reading zero and a tile quietly measuring the wrong
  period look identical to the reader.

The duplicate-label case that started all this is **not** caught here, and it
does not need to be: `_to_superset_form_data` drops a `groupby` that repeats the
`x_axis` before this ever runs. Verified — feeding it the original broken shape
now returns `ok`, because by the time it arrives the duplicate is gone.

**Not caught: anything that depends on the viz plugin's own reading of
form_data.** The query executed here is built from our params, whereas the
browser builds its query by running the plugin's `buildQuery` over the saved
form_data. When `big_number_total` drew nothing because it reads `metric` and we
had written only `metrics`, a check like this one would have passed happily —
the query it constructs carries the metric either way. That class of defect
belongs to `tests_browser/test_viz_renders.py`, which builds a chart of every
allowlisted type and looks at the rendered page. Superset's MCP has the same
blind spot for the same reason; a server cannot run the frontend.

# Why a failure here does not always refuse

A malformed query is the copilot's fault and refusing teaches it something. A
timeout or a 502 is the warehouse having a bad afternoon, and refusing would
make "can I build a chart?" depend on "is the database fast right now?" — so
those proceed. The check is a safety net, never a gate.
"""

from __future__ import annotations

import logging
from typing import Any

from lifedata_ai.superset_client import SupersetError

logger = logging.getLogger(__name__)

#: Rows to ask for. Two rather than one: some Superset result shapes report an
#: empty query and a single-null-row query identically at rowcount 1, and the
#: second row costs nothing on a query that is already limited.
VERIFY_ROW_LIMIT = 2

#: Our filter shape -> the query-context filter shape. `adhoc_filters` belong to
#: form_data; `filters` is what a query context takes.
_FILTER_KEYS = ("subject", "operator", "comparator")


class CompileVerdict:
    """What Superset said when asked to run this chart's query.

    Three outcomes, deliberately not two: `ok` and `empty` both mean the query
    is valid, and only `refused` means the chart would be visibly broken.
    """

    __slots__ = ("status", "message")

    def __init__(self, status: str, message: str = "") -> None:
        self.status = status
        self.message = message

    @property
    def refused(self) -> bool:
        return self.status == "refused"

    @property
    def empty(self) -> bool:
        return self.status == "empty"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CompileVerdict({self.status!r}, {self.message!r})"


OK = CompileVerdict("ok")
UNKNOWN = CompileVerdict("unknown")


def saved_query_context_for(form_data: dict[str, Any], dataset_id: int) -> dict[str, Any]:
    """The query context to STORE on a chart, as Superset's own UI would.

    A chart saved with `params` and no `query_context` renders perfectly well —
    Explore rebuilds the query from form_data — but it cannot be read back
    through `POST /api/v1/chart/data`, which is what `get_chart_data` uses. So
    the copilot could build a chart and then not read its own numbers:

        Chart 195 has no stored query context, so its data cannot be run.

    Observed on the CTI sandbox, on the first chart the copilot ever built
    there. It degraded honestly — it told the user it could not read the figures
    — but a copilot that cannot see what it just made cannot check its own work,
    and `review_dashboard_design` is weaker for the same reason.

    Same builder as the verification context, with the chart's real row limit and
    its form_data attached. Deriving both from one function is the point: two
    copies would drift, and the one that drifted would be this one, silently,
    because nothing renders from it.
    """
    context = query_context_for(form_data, dataset_id)
    row_limit = form_data.get("row_limit")
    if isinstance(row_limit, int) and row_limit > 0:
        context["queries"][0]["row_limit"] = row_limit
    # Superset stores form_data alongside the queries; Explore reads it back when
    # the chart is opened for editing.
    context["form_data"] = form_data
    return context


def query_context_for(form_data: dict[str, Any], dataset_id: int) -> dict[str, Any]:
    """The smallest query context that proves this chart's query runs.

    Mirrors what the chart will ask for — same metrics, same grouping, same
    filters, same period — with the row limit forced down. It is deliberately
    *not* the chart's full query context: a query that returns two rows proves
    everything a query returning eight hundred would.

    `saved_query_context_for` builds on this when the context is to be persisted.
    """
    columns: list[Any] = []
    x_axis = form_data.get("x_axis")
    if x_axis:
        columns.append(x_axis)
    # A choropleth carries its grain in `entity`, not in `groupby` — verifying it
    # without this would test an ungrouped total and call a broken map healthy.
    entity = form_data.get("entity")
    if entity and entity not in columns:
        columns.append(entity)
    for column in form_data.get("groupby") or []:
        if column not in columns:
            columns.append(column)

    query: dict[str, Any] = {"row_limit": VERIFY_ROW_LIMIT}
    metrics = form_data.get("metrics")
    if metrics:
        query["metrics"] = metrics
    if columns:
        query["columns"] = columns

    filters = [
        {
            "col": item["subject"],
            "op": item["operator"],
            "val": item.get("comparator"),
        }
        for item in form_data.get("adhoc_filters") or []
        if isinstance(item, dict) and all(key in item for key in _FILTER_KEYS[:2])
    ]
    temporal = _temporal_filter(form_data, dataset_id)
    if temporal:
        filters.append(temporal)
    if filters:
        query["filters"] = filters

    return {
        "datasource": {"id": int(dataset_id), "type": "table"},
        "queries": [query],
        "result_format": "json",
        "result_type": "full",
    }


#: Time ranges meaning "do not filter", which need no temporal filter at all.
_NO_TIME_FILTER = {"", "no filter", "no_filter"}


def _temporal_filter(form_data: dict[str, Any], dataset_id: int) -> dict[str, Any] | None:
    """Express `time_range` the way a query context actually applies it.

    A bare `time_range` key on a query is **silently ignored** unless the
    datasource carries a main datetime column for it to attach to. Verified
    against 6.1.0: asking `rpt_lab_orders` for January 2019 returned all 14,355
    rows, because nothing had told the query which column the range applied to.

    A query context applies a period through a `TEMPORAL_RANGE` filter naming
    the column. The column comes from the registry, which already declares
    `time_column` per dataset and validates that it exists.
    """
    time_range = form_data.get("time_range")
    if not time_range or str(time_range).strip().lower() in _NO_TIME_FILTER:
        return None
    if any(
        isinstance(item, dict) and item.get("operator") == "TEMPORAL_RANGE"
        for item in form_data.get("adhoc_filters") or []
    ):
        # Already carried as a filter, which the mapping above picks up. This is
        # the normal path now that `_apply_time_range` writes one.
        return None

    # Fallback, for form_data that predates `_apply_time_range` — an existing
    # chart being edited, whose stored params carry only the legacy key. Without
    # this the verification would run against the whole table while the chart
    # under test runs against a period, and "returns rows" would be measured on
    # the wrong query.
    from lifedata_ai.tools.superset_write import _temporal_column

    column = _temporal_column(form_data, dataset_id)
    return {"col": column, "op": "TEMPORAL_RANGE", "val": time_range} if column else None


def verify(client: Any, form_data: dict[str, Any], dataset_id: int) -> CompileVerdict:
    """Run the query behind this chart and report whether it works.

    Never raises. Everything it cannot determine comes back `unknown`, because
    the caller's decision in that case is to carry on.
    """
    if not form_data.get("metrics"):
        # Nothing to run. Viz types that need no metric exist, and a chart with
        # no metric is the schema's business, not this function's.
        return UNKNOWN

    try:
        payload = client.chart_data(query_context_for(form_data, dataset_id))
    except SupersetError as exc:
        # BOTH, and the distinction cost a round of debugging. A malformed query
        # context comes back 500 (Superset never validates it into a 400), while
        # a well-formed one naming a column or metric that does not exist comes
        # back 400. Either way we sent something wrong, and the chart would not
        # have drawn. Anything else — 401, 403, a gateway timeout — is the stack
        # having a bad moment and must not block the build.
        if exc.status_code in (400, 500):
            return CompileVerdict("refused", _clean(_detail_of(exc) or str(exc)))
        logger.info("Compile check skipped (%s): %s", exc.status_code, exc)
        return UNKNOWN
    except Exception as exc:  # noqa: BLE001 - a safety net must never be the failure
        logger.warning("Compile check failed to run: %s", exc)
        return UNKNOWN

    results = payload.get("result") or []
    first = results[0] if results and isinstance(results[0], dict) else {}

    error = first.get("error") or (
        first.get("errors")[0].get("message")
        if isinstance(first.get("errors"), list) and first.get("errors")
        else None
    )
    if first.get("status") == "failed" or error:
        return CompileVerdict("refused", _clean(str(error or "Superset rejected the query")))

    rows = first.get("rowcount")
    data = first.get("data")
    if rows == 0 or (isinstance(data, list) and not data):
        return CompileVerdict("empty", "the query returns no rows, so the widget renders empty")

    # An ungrouped aggregate over nothing still returns ONE row, holding zeros —
    # `COUNT(*)` of an empty period is `[{"orders": 0}]`, not `[]`. That renders
    # as a big honest "0" rather than "No data", so it is not broken; it is
    # still worth saying out loud, because a tile reading zero and a tile that
    # is quietly measuring the wrong period look identical to the reader.
    grouped = bool(form_data.get("groupby")) or bool(form_data.get("x_axis"))
    if not grouped and isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        values = list(data[0].values())
        if values and all(value in (0, None) for value in values):
            return CompileVerdict("empty", "every figure comes back zero for the period requested")

    return OK


def refusal_message(verdict: CompileVerdict, chart_name: str) -> str:
    """What the model is told when a chart will not draw.

    Names the likely cause when Superset's wording is recognisable. The duplicate
    label case is worth spelling out because it has one fix and the model has
    reached for the wrong one before: dropping the axis rather than the grouping.
    """
    detail = verdict.message
    hint = ""
    lowered = detail.lower()
    if "duplicate" in lowered and "label" in lowered:
        hint = (
            " This is almost always the same column used as both the axis and the "
            "grouping. Keep it as the axis and drop it from groupby."
        )
    elif "not found" in lowered or "unknown column" in lowered:
        hint = " Check the column against the catalogue — only what is listed there exists."
    return (
        f"“{chart_name}” would not render: Superset refused its query — {detail}.{hint} "
        "Nothing has been created. Fix the definition and try again."
    )


def _detail_of(exc: SupersetError) -> str:
    """The message Superset put in the body, not our wrapper's summary.

    `SupersetError.__str__` is "Superset POST /api/v1/chart/data failed with
    400", which tells the model nothing it can fix. The useful sentence — which
    column was not found, which label collided — is in the body.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or "")
        if body.get("message"):
            return str(body["message"])
    return ""


def _clean(message: str, limit: int = 260) -> str:
    """One line, bounded. This text reaches the model and the user."""
    flattened = " ".join(str(message).split())
    return flattened[:limit] + ("…" if len(flattened) > limit else "")
