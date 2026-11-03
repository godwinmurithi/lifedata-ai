"""Read tools: live Superset metadata and chart results (spec §5).

Every tool here runs server-side with the caller's forwarded `session` cookie,
so results are permission-scoped to that user by Superset itself — there is no
role logic in this module and there must not be. A user who cannot see a
dataset in the UI gets a 403 here, which `tools/registry.py` turns into an
honest "you lack permission" tool result.

Three rules shape almost every design decision below.

* **Development data may include individual records.** Results are capped for size.
* **Nothing raw reaches the model either.** Superset hands back JSON *strings*
  for `params`, `query_context` and `position_json`; dumping those would burn
  tokens and invite the model to author layout JSON, which spec §6.7 reserves
  for the layout compiler. They are parsed and summarised here instead.
* **Superset 6.1.0 has sharp edges** that were verified against the running
  instance rather than assumed; each one is called out at the point it bites.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from django.conf import settings

from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode

logger = logging.getLogger(__name__)


# --- Caps ----------------------------------------------------------------

#: Listings are for orientation, not exhaustive inventory; beyond this the model
#: should narrow with `q` rather than read a wall of names.
MAX_LISTING_ITEMS = 50
#: A pathological dataset should not blow the context window on column metadata.
MAX_DATASET_COLUMNS = 100
#: Layout outlines stay navigable; a dashboard deeper than this is summarised
#: by counts alone rather than truncated into something misleading.
MAX_LAYOUT_NODES = 80


def _cap_rows() -> int:
    return int(settings.LIFEDATA_AI_MAX_ROWS_TO_MODEL)


def _cap_columns() -> int:
    return int(settings.LIFEDATA_AI_MAX_COLUMNS_TO_MODEL)


def _cap_row_limit() -> int:
    return int(settings.LIFEDATA_AI_MAX_QUERY_ROW_LIMIT)


# --- Shared helpers ------------------------------------------------------


def _parse_json_field(value: Any, field_name: str) -> tuple[Any, str | None]:
    """Decode a Superset field that arrives as a JSON *string*.

    `params`, `query_context`, `position_json` and `json_metadata` are all
    stored as text and returned as text. On a freshly created dashboard the last
    two are SQL NULL — JSON `null`, not `{}` (verified; see
    docs/superset-integration-findings.md §4) — so None is a first-class case
    here, not an error.

    Returns `(parsed_or_None, note_or_None)`; a note means the field was present
    but unreadable, which the model should be told rather than shielded from.
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, (dict, list)):
        return value, None
    if not isinstance(value, str):
        return None, f"{field_name} had unexpected type {type(value).__name__}"
    try:
        return json.loads(value), None
    except ValueError as exc:
        logger.warning("Unparseable %s from Superset: %s", field_name, exc)
        return None, f"{field_name} is not valid JSON and was ignored"


def _person(entry: Any) -> str:
    """A display name from Superset's `{first_name, last_name}` person stubs."""
    if not isinstance(entry, dict):
        return str(entry)
    name = " ".join(
        part for part in (entry.get("first_name"), entry.get("last_name")) if part
    ).strip()
    return name or entry.get("username") or f"user {entry.get('id')}"


def _truncate_list(items: list[Any], cap: int) -> tuple[list[Any], bool]:
    return (items[:cap], True) if len(items) > cap else (items, False)


# --- Databases and datasets ---------------------------------------------


class ListDatabasesTool(Tool):
    """Database connections visible to the calling user."""

    name = "list_databases"
    description = (
        "List the Superset database connections this user can see, with their id, "
        "name and backend. Call this only when the user asks about data sources or "
        "connections; for anything analytical start with list_datasets instead, "
        "since charts are built on datasets, not databases."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext) -> dict[str, Any]:
        client = context.superset_mcp_client() if context.use_mcp_reads else context.superset_client()
        rows = client.list_databases()
        items, truncated = _truncate_list(rows, MAX_LISTING_ITEMS)
        return {
            "databases": [
                {
                    "id": row.get("id"),
                    "name": row.get("database_name"),
                    "backend": row.get("backend"),
                }
                for row in items
            ],
            "count": len(items),
            "truncated": truncated,
        }


class ListDatasetsTool(Tool):
    """Dataset discovery — the usual first call in an analytical turn."""

    name = "list_datasets"
    description = (
        "List the Superset datasets this user can query, with id, table name and "
        "schema. Call this first whenever the user asks what data is available, or "
        "before proposing any chart, so you name datasets that actually exist. "
        "Pass a search fragment in `q` to filter by table name (e.g. 'lab'), or an "
        "empty string to list everything. Follow up with get_dataset for columns "
        "and saved metrics."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": (
                    "Case-insensitive fragment matched against the table name. "
                    "Use an empty string to list all datasets."
                ),
            },
        },
        # Every property is required so `strict` can be enabled; an empty string
        # is the documented "no filter" value rather than an omitted argument.
        "required": ["q"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, q: str) -> dict[str, Any]:
        client = context.superset_mcp_client() if context.use_mcp_reads else context.superset_client()
        rows = client.list_datasets(q or None)
        items, truncated = _truncate_list(rows, MAX_LISTING_ITEMS)
        datasets = [
            {
                "id": row.get("id"),
                "table_name": row.get("table_name"),
                "schema": row.get("schema"),
                "kind": row.get("kind"),
                # No `database` here on purpose: `SupersetClient._listing`
                # asks for a narrow column set, so it would always be null.
                # Use get_dataset when the connection matters.
            }
            for row in items
        ]
        return {
            "datasets": datasets,
            "count": len(datasets),
            "truncated": truncated,
            "query": q,
        }


class GetDatasetTool(Tool):
    """Columns, types and saved metrics for one dataset."""

    name = "get_dataset"
    description = (
        "Return the columns (name, type, whether temporal, whether groupable or "
        "filterable) and the saved metrics of one Superset dataset. Call this "
        "before proposing or explaining any chart on the dataset, so that the "
        "columns and metrics you name are real."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "dataset_id": {
                "type": "integer",
                "description": "Superset dataset id, as returned by list_datasets.",
            },
        },
        "required": ["dataset_id"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, dataset_id: int) -> dict[str, Any]:
        client = context.superset_mcp_client() if context.use_mcp_reads else context.superset_client()
        payload = client.get_dataset(dataset_id)
        if not payload:
            raise ToolError(
                f"No dataset with id {dataset_id} is visible to you.",
                code="not_found",
            )

        raw_columns = payload.get("columns") or []
        columns, columns_truncated = _truncate_list(raw_columns, MAX_DATASET_COLUMNS)
        return {
            "id": payload.get("id", dataset_id),
            "table_name": payload.get("table_name"),
            "schema": payload.get("schema"),
            "kind": payload.get("kind"),
            "description": payload.get("description"),
            "database": (payload.get("database") or {}).get("database_name"),
            "main_datetime_column": payload.get("main_dttm_col"),
            # `time_grain_sqla` is deliberately not surfaced: verified against
            # 6.1.0, it returns the full list of available grain choices, not the
            # dataset's default, and reads as meaningful noise to the model.
            "owners": [_person(o) for o in payload.get("owners") or []],
            "columns": [
                {
                    "name": column.get("column_name"),
                    "type": column.get("type"),
                    "verbose_name": column.get("verbose_name"),
                    "description": column.get("description"),
                    "is_temporal": bool(column.get("is_dttm")),
                    "groupable": bool(column.get("groupby")),
                    "filterable": bool(column.get("filterable")),
                    # Present only on calculated columns; useful for spotting a
                    # column that is already an expression rather than a field.
                    "expression": column.get("expression"),
                }
                for column in columns
            ],
            "columns_truncated": columns_truncated,
            "metrics": [
                {
                    "name": metric.get("metric_name"),
                    "verbose_name": metric.get("verbose_name"),
                    "expression": metric.get("expression"),
                    "description": metric.get("description"),
                    "number_format": metric.get("d3format"),
                    "currency": metric.get("currency"),
                }
                for metric in payload.get("metrics") or []
            ],
        }


# --- Charts --------------------------------------------------------------


class ListChartsTool(Tool):
    """Saved chart discovery."""

    name = "list_charts"
    description = (
        "List the saved Superset charts this user can see, with id, name and viz "
        "type. Call this when the user refers to an existing chart by name, or to "
        "check whether something similar already exists before proposing a new "
        "chart. Pass a name fragment in `q`, or an empty string to list everything."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": (
                    "Case-insensitive fragment matched against the chart name. "
                    "Use an empty string to list all charts."
                ),
            },
        },
        "required": ["q"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, q: str) -> dict[str, Any]:
        client = context.superset_mcp_client() if context.use_mcp_reads else context.superset_client()
        rows = client.list_charts(q or None)
        items, truncated = _truncate_list(rows, MAX_LISTING_ITEMS)
        return {
            "charts": [
                {
                    "id": row.get("id"),
                    "name": row.get("slice_name"),
                    "viz_type": row.get("viz_type"),
                }
                for row in items
            ],
            "count": len(items),
            "truncated": truncated,
            "query": q,
        }


class GetChartTool(Tool):
    """One chart's definition: viz type, dataset and the form data behind it."""

    name = "get_chart"
    description = (
        "Return one saved chart's viz type, dataset, and its stored form data "
        "(metrics, group-bys, filters, row limit, sort) in readable form. Call this "
        "before explaining or modifying a chart, so you describe what it actually "
        "measures rather than guessing from its title. Use get_chart_data if you "
        "also need the numbers."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "chart_id": {
                "type": "integer",
                "description": "Superset chart (slice) id, as returned by list_charts.",
            },
        },
        "required": ["chart_id"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, chart_id: int) -> dict[str, Any]:
        client = context.superset_mcp_client() if context.use_mcp_reads else context.superset_client()
        payload = client.get_chart(chart_id)
        if not payload:
            raise ToolError(
                f"No chart with id {chart_id} is visible to you.", code="not_found"
            )

        params, params_note = _parse_json_field(payload.get("params"), "params")
        query_context, _ = _parse_json_field(
            payload.get("query_context"), "query_context"
        )
        dataset_id = payload.get("datasource_id")

        result: dict[str, Any] = {
            "id": payload.get("id", chart_id),
            "name": payload.get("slice_name"),
            "viz_type": payload.get("viz_type"),
            "description": payload.get("description"),
            "dataset_id": dataset_id,
            "dataset_name": payload.get("datasource_name_text"),
            "owners": [_person(o) for o in payload.get("owners") or []],
            # `dashboards` here is a list of {id, dashboard_title} stubs.
            "on_dashboards": [
                {"id": d.get("id"), "title": d.get("dashboard_title")}
                for d in payload.get("dashboards") or []
                if isinstance(d, dict)
            ],
            "form_data": _summarise_form_data(params),
            # Stored context is optional for reconstructable simple chart definitions.
            "has_stored_query_context": bool(query_context),
            "can_reconstruct_data_query": not bool(query_context) and _query_context_from_saved_definition(payload) is not None,
        }
        if params_note:
            result["warning"] = params_note
        return result


def _summarise_form_data(params: Any) -> dict[str, Any]:
    """Reduce Superset form data to the fields that carry analytical meaning.

    Form data accumulates dozens of presentation keys. Returning it whole is
    mostly noise, and the interesting part — what is measured, split and
    filtered — gets lost in it.
    """
    if not isinstance(params, dict):
        return {}

    summary: dict[str, Any] = {}
    for source, label in (
        ("metrics", "metrics"),
        ("metric", "metric"),
        ("groupby", "group_by"),
        ("columns", "columns"),
        ("x_axis", "x_axis"),
        ("granularity_sqla", "time_column"),
        ("time_grain_sqla", "time_grain"),
        ("time_range", "time_range"),
        ("row_limit", "row_limit"),
        ("series_limit", "series_limit"),
        ("order_desc", "order_descending"),
        ("query_mode", "query_mode"),
        ("color_scheme", "colour_scheme"),
    ):
        value = params.get(source)
        if value not in (None, "", [], {}):
            summary[label] = _describe_value(value)

    adhoc_filters = params.get("adhoc_filters") or []
    if adhoc_filters:
        summary["filters"] = [_describe_filter(f) for f in adhoc_filters]
    return summary


def _describe_value(value: Any) -> Any:
    """Render adhoc metric/column objects as short labels, plain values as-is."""
    if isinstance(value, list):
        return [_describe_value(item) for item in value]
    if isinstance(value, dict):
        return (
            value.get("label")
            or value.get("sqlExpression")
            or value.get("column_name")
            or value.get("expressionType")
            or value
        )
    return value


def _describe_filter(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    if entry.get("expressionType") == "SQL":
        # Surfaced deliberately: an existing chart may contain raw SQL, and the
        # model must know it cannot reproduce that in a new chart (spec §6.3).
        return {
            "type": "raw_sql_filter",
            "clause": entry.get("clause"),
            "note": "This chart uses a raw SQL filter; new charts may not.",
        }
    return {
        "column": _describe_value(entry.get("subject")),
        "operator": entry.get("operator"),
        "value": entry.get("comparator"),
    }


# --- Dashboards ----------------------------------------------------------


class ListDashboardsTool(Tool):
    """Dashboard discovery."""

    name = "list_dashboards"
    description = (
        "List the Superset dashboards this user can see, with id, title and "
        "published status. Call this when the user names a dashboard you do not "
        "have an id for. Pass a title fragment in `q`, or an empty string to list "
        "everything."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": (
                    "Case-insensitive fragment matched against the dashboard title. "
                    "Use an empty string to list all dashboards."
                ),
            },
        },
        "required": ["q"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, q: str) -> dict[str, Any]:
        client = context.superset_mcp_client() if context.use_mcp_reads else context.superset_client()
        rows = client.list_dashboards(q or None)
        items, truncated = _truncate_list(rows, MAX_LISTING_ITEMS)
        return {
            "dashboards": [
                {
                    "id": row.get("id"),
                    "title": row.get("dashboard_title"),
                    "status": row.get("status"),
                }
                for row in items
            ],
            "count": len(items),
            "truncated": truncated,
            "query": q,
        }


class GetDashboardTool(Tool):
    """One dashboard: its charts, its layout in outline, and its native filters."""

    name = "get_dashboard"
    description = (
        "Return one dashboard's charts, an outline of its layout (rows, columns, "
        "tabs and widget widths), its native filters, its theme and colour scheme, "
        "and its owners. Call this before discussing or changing a dashboard — "
        "including when the page context says the user is looking at one — so you "
        "know what is already there and how it is arranged."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "dashboard_id": {
                "type": "integer",
                "description": "Superset dashboard id, as returned by list_dashboards.",
            },
        },
        "required": ["dashboard_id"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, dashboard_id: int) -> dict[str, Any]:
        client = context.superset_client()
        payload = client.get_dashboard(dashboard_id)
        if not payload:
            raise ToolError(
                f"No dashboard with id {dashboard_id} is visible to you.",
                code="not_found",
            )

        position, position_note = _parse_json_field(
            payload.get("position_json"), "position_json"
        )
        metadata, metadata_note = _parse_json_field(
            payload.get("json_metadata"), "json_metadata"
        )
        metadata = metadata if isinstance(metadata, dict) else {}

        # The `charts` field on the dashboard payload is a list of chart *names*
        # only; the membership endpoint carries ids and form data, which is what
        # any later layout or edit reasoning needs.
        try:
            members = client.get_dashboard_charts(dashboard_id)
        except SupersetError as exc:
            logger.warning("Dashboard %s chart membership unavailable: %s", dashboard_id, exc)
            members = []

        result: dict[str, Any] = {
            "id": payload.get("id", dashboard_id),
            "title": payload.get("dashboard_title"),
            "published": payload.get("published"),
            "url": payload.get("url"),
            "changed_on": payload.get("changed_on"),
            "owners": [_person(o) for o in payload.get("owners") or []],
            "owned_by_you": _is_owned_by(payload.get("owners") or [], context),
            "theme": payload.get("theme"),
            "colour_scheme": metadata.get("color_scheme"),
            "refresh_frequency": metadata.get("refresh_frequency"),
            "charts": [
                {
                    "id": member.get("id"),
                    "name": member.get("slice_name"),
                    "viz_type": (member.get("form_data") or {}).get("viz_type"),
                    "dataset": (member.get("form_data") or {}).get("datasource"),
                }
                for member in members
            ],
            "chart_count": len(members),
            "layout": _summarise_position(position),
            "native_filters": _summarise_native_filters(metadata),
        }

        warnings = [note for note in (position_note, metadata_note) if note]
        if warnings:
            result["warnings"] = warnings
        return result


def _is_owned_by(owners: list[Any], context: ToolContext) -> bool:
    """Whether the calling user owns this dashboard.

    Ownership drives the §6.6 risk rule (anything on a dashboard the user does
    not own must go through a preview card), so the model benefits from knowing
    it before it proposes anything.
    """
    identity = context.identity
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        if owner.get("id") is not None and owner.get("id") == getattr(identity, "user_id", None):
            return True
        if _person(owner).lower() == (identity.display_name or "").lower():
            return True
    return False


#: Layout node types that carry a widget the model may be asked to move.
_WIDGET_NODE_TYPES = {"CHART", "MARKDOWN", "DIVIDER", "HEADER"}


def _summarise_position(position: Any) -> dict[str, Any]:
    """Turn `position_json` into a compact outline.

    Spec §6.7 reserves `position_json` mutation for the layout compiler and says
    the model never emits raw layout JSON — so it should not read raw layout
    JSON either. What it genuinely needs is the order of rows, which widgets sit
    in each, and how wide they are.

    None is the normal state of a dashboard that has never been laid out
    (verified: SQL NULL, not `{}`), and is reported as such rather than as an
    error or an empty grid.
    """
    if position is None or position == {}:
        return {
            "has_layout": False,
            "note": "This dashboard has no saved layout yet.",
        }
    if not isinstance(position, dict):
        return {"has_layout": False, "note": "Layout could not be interpreted."}

    nodes = {
        key: value
        for key, value in position.items()
        if isinstance(value, dict) and value.get("type")
    }
    counts: dict[str, int] = {}
    for node in nodes.values():
        node_type = str(node.get("type"))
        counts[node_type] = counts.get(node_type, 0) + 1

    state = {"budget": MAX_LAYOUT_NODES, "truncated": False}

    def walk(node_id: str, seen: frozenset[str]) -> dict[str, Any] | None:
        if node_id in seen:
            return None
        if state["budget"] <= 0:
            state["truncated"] = True
            return None
        node = nodes.get(node_id)
        if node is None:
            return None
        state["budget"] -= 1
        meta = node.get("meta") or {}
        entry: dict[str, Any] = {"id": node_id, "type": node.get("type")}
        for key, label in (("chartId", "chart_id"), ("width", "width"), ("height", "height")):
            if meta.get(key) is not None:
                entry[label] = meta.get(key)
        label = meta.get("sliceName") or meta.get("text")
        if label:
            entry["name"] = label
        children = [
            child
            for child in (
                walk(str(child_id), seen | {node_id})
                for child_id in node.get("children") or []
            )
            if child is not None
        ]
        if children:
            entry["children"] = children
        return entry

    root = walk("GRID_ID", frozenset()) or walk("ROOT_ID", frozenset())
    summary: dict[str, Any] = {
        "has_layout": True,
        "grid_columns": 12,
        "node_counts": counts,
        "widget_count": sum(counts.get(t, 0) for t in _WIDGET_NODE_TYPES),
        "outline": root.get("children", []) if root else [],
    }
    if state["truncated"]:
        summary["outline_truncated"] = True
        summary["note"] = (
            "The layout is larger than the outline shown; counts above are complete."
        )
    tab_titles = [
        (node.get("meta") or {}).get("text")
        for node in nodes.values()
        if node.get("type") == "TAB"
    ]
    if tab_titles:
        summary["tabs"] = [title for title in tab_titles if title]
    return summary


def _summarise_native_filters(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Readable summary of `json_metadata.native_filter_configuration`."""
    filters = metadata.get("native_filter_configuration") or []
    if not isinstance(filters, list):
        return []

    summary = []
    for entry in filters:
        if not isinstance(entry, dict):
            continue
        targets = entry.get("targets") or []
        target = targets[0] if targets and isinstance(targets[0], dict) else {}
        column = target.get("column") or {}
        summary.append(
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "filter_type": entry.get("filterType"),
                "dataset_id": target.get("datasetId"),
                "column": column.get("name") if isinstance(column, dict) else column,
            }
        )
    return summary


# --- Chart data ----------------------------------------------------------


class GetChartDataTool(Tool):
    """Results for one saved chart, row- and column-capped."""

    name = "get_chart_data"
    description = (
        "Run a saved chart's own query and return its results. Simple charts without "
        "stored query_context are reconstructed from their own saved dataset and params, "
        "capped for size. Call this when the user asks what a chart shows, what the "
        "numbers are, or to compare values — never guess figures. Results are "
        "allowed to include individual synthetic development records. Always state "
        "that the figures are the top rows of a capped "
        "result when the response says it was truncated."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "chart_id": {
                "type": "integer",
                "description": "Superset chart (slice) id whose stored query should run.",
            },
        },
        "required": ["chart_id"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, chart_id: int) -> dict[str, Any]:
        if context.use_mcp_reads:
            return self._run_mcp(context, chart_id)
        return self._run_rest(context, chart_id)

    def _run_mcp(self, context: ToolContext, chart_id: int) -> dict[str, Any]:
        """Native-MCP path: calls Superset's own get_chart_data tool by chart id.

        DELIBERATELY HAS NO REGISTRY GATE. The REST path below (`_run_rest`)
        carries the one that closed the exact gap documented in
        semantics/registry.sandbox.yaml -- a table chart built over a
        row-level, PII-bearing dataset (farmers, sickle_cells_disease_
        questionnaire, etc.) would otherwise hand real names and clinical
        values straight to the model. That check is not reproduced here.
        This is a deliberate, explicit product decision (2026-09-25), not an
        oversight -- gating is deferred to a production pass. See
        AI_NATIVE_STATUS.md and superset_mcp_client.py's module docstring
        before pointing this at any dataset with real patient data.
        """
        mcp = context.superset_mcp_client()
        try:
            payload = mcp.get_chart_data_by_id(chart_id, row_limit=_cap_row_limit())
        except SupersetError as exc:
            message = str(exc).lower()
            if not exc.is_permission_denied and ('no stored query context' in message or 'no query context' in message):
                return self._run_rest(context, chart_id)
            raise ToolError(
                f"Could not run chart {chart_id}: {exc}", code="query_failed"
            ) from exc
        diagnostic = str(payload.get('summary') or payload.get('raw') or payload.get('message') or '').lower()
        if not payload.get('data') and ('no stored query context' in diagnostic or 'no query context' in diagnostic):
            return self._run_rest(context, chart_id)
        return _summarise_mcp_chart_data(payload)

    def _run_rest(self, context: ToolContext, chart_id: int) -> dict[str, Any]:
        client = context.superset_client()
        chart = client.get_chart(chart_id)
        if not chart:
            raise ToolError(
                f"No chart with id {chart_id} is visible to you.", code="not_found"
            )

        # THE READ-SIDE PRIVACY GATE THAT USED TO LIVE HERE HAS BEEN REMOVED
        # ALONG WITH THE SEMANTICS REGISTRY, DELIBERATELY, WITH THE CONSEQUENCE
        # KNOWN AND ACCEPTED (not an oversight — read this before re-adding it
        # casually or assuming it is a bug).
        #
        # What used to be here: this tool replays whatever query a chart
        # already carries, so it refused to do that for a dataset the registry
        # did not declare, because an undeclared dataset had no privacy
        # classification. On a deployment where Superset also holds row-level
        # tables (names, dates of birth, clinical results), that check was the
        # entire thing standing between "a table chart someone built over a
        # PII-bearing dataset" and "those rows go straight to the model". It is
        # gone now. This method will happily return exactly that.
        #
        # The MCP path (_run_mcp above) never had an equivalent check at all
        # (see its own docstring) — so removing this one makes both read paths
        # consistent, not newly unsafe in a way the other path wasn't already.

        query_context, note = _parse_json_field(
            chart.get("query_context"), "query_context"
        )
        prepared = _prepare_query_context(query_context, chart, note)

        try:
            payload = client.chart_data(prepared)
        except SupersetError as exc:
            if exc.status_code == 500:
                # Verified finding §3: /api/v1/chart/data answers a malformed body
                # with 500, not 400. A 500 here means our query context was bad,
                # so say that rather than blaming Superset.
                raise ToolError(
                    f"Chart {chart_id} has a query definition Superset could not run. "
                    "Describe the chart from get_chart instead of quoting figures.",
                    code="query_context_rejected",
                    details={"status_code": 500},
                ) from exc
            raise

        return _summarise_chart_data(payload, chart, prepared)


def _query_context_from_saved_definition(chart: dict[str, Any]) -> dict[str, Any] | None:
    """Recover simple chart queries from their OWN params, never design references.

    MCP-created categorical charts may omit query_context. Refuse reconstruction
    for plugin-specific transformations or filters that our builder cannot retain.
    """
    from lifedata_ai.tools.compile_check import saved_query_context_for

    params, note = _parse_json_field(chart.get("params"), "params")
    dataset_id = chart.get("datasource_id")
    if note or not isinstance(params, dict) or type(dataset_id) is not int or dataset_id < 1:
        return None
    if chart.get("datasource_type", "table") != "table":
        return None
    viz = params.get("viz_type") or chart.get("viz_type")
    if viz not in {"echarts_timeseries_bar", "pie", "table", "big_number_total"}:
        return None
    # These features require the plugin's complete buildQuery; dropping any of
    # them would silently change the chart's question.
    if any(params.get(key) for key in ("granularity_sqla", "time_grain_sqla", "post_processing",
                                       "contributionMode", "rolling_type", "resample_rule",
                                       "time_compare", "timeseries_limit", "series_limit", "where", "having")):
        return None
    if str(params.get("time_range") or "").strip().lower() not in {"", "no filter", "no_filter"}:
        return None
    filters = params.get("adhoc_filters") or []
    legacy_filters = params.get("filters") or []
    if not isinstance(filters, list) or not isinstance(legacy_filters, list):
        return None
    if any(not isinstance(f, dict) or f.get("expressionType", "SIMPLE") != "SIMPLE"
           or f.get("clause", "WHERE") != "WHERE" or not {"subject", "operator", "comparator"}.issubset(f)
           for f in filters):
        return None
    if any(not isinstance(f, dict) or not {"col", "op", "val"}.issubset(f) for f in legacy_filters):
        return None
    form = copy.deepcopy(params)
    if not form.get("metrics") and form.get("metric"):
        form["metrics"] = [form["metric"]]
    raw = form.get("query_mode") == "raw"
    if raw:
        if not isinstance(form.get("all_columns"), list) or not form["all_columns"]:
            return None
        form.pop("metrics", None)
    elif not isinstance(form.get("metrics"), list) or not form["metrics"]:
        return None
    if form.get("groupby") is not None and not isinstance(form["groupby"], list):
        return None
    # Saved params without a limit must not inherit the compile-check's two-row
    # probe limit. The caller applies the appropriate model/viewer cap below.
    if type(form.get("row_limit")) is not int or form["row_limit"] <= 0:
        form["row_limit"] = 10000
    context = saved_query_context_for(form, dataset_id)
    query = context["queries"][0]
    if legacy_filters:
        query.setdefault("filters", []).extend(copy.deepcopy(legacy_filters))
    if form.get("order_by_cols"):
        try:
            orderby = [json.loads(value) if isinstance(value, str) else value for value in form["order_by_cols"]]
        except (ValueError, TypeError):
            return None
        if any(not isinstance(value, list) or len(value) != 2 or type(value[1]) is not bool for value in orderby):
            return None
        query["orderby"] = orderby
    return context


def _prepare_query_context(
    query_context: Any, chart: dict[str, Any], note: str | None, row_limit_cap: int | None = None
) -> dict[str, Any]:
    """Validate and cap the stored query context before it is sent to Superset.

    Raw records and aggregate queries are both supported for dummy development data.
    Cap the row limit at LIFEDATA_AI_MAX_QUERY_ROW_LIMIT on every query to keep
    results within the model context budget.

    Validating first is not optional: an empty or malformed body comes back as a
    500 that reads like a Superset outage (verified finding §3).
    """
    chart_id = chart.get("id")
    if query_context is None and note is None:
        query_context = _query_context_from_saved_definition(chart)
    if not isinstance(query_context, dict):
        raise ToolError(
            f"Chart {chart_id} has no usable saved data query. "
            "Inspect its own dataset and definition with get_dataset/get_chart, then "
            "create or update an executable chart. Design references supply styling only.",
            code="no_query_context",
            details={"parse_note": note} if note else None,
        )

    queries = query_context.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ToolError(
            f"Chart {chart_id} has an empty query context and cannot be run.",
            code="no_query_context",
        )
    if not query_context.get("datasource"):
        raise ToolError(
            f"Chart {chart_id} has a query context with no datasource and cannot be run.",
            code="no_query_context",
        )

    row_cap = row_limit_cap if row_limit_cap is not None else _cap_row_limit()
    prepared = copy.deepcopy(query_context)
    capped_queries = []
    for query in prepared.get("queries") or []:
        if not isinstance(query, dict):
            continue
        existing = query.get("row_limit")
        query["row_limit"] = (
            min(int(existing), row_cap) if isinstance(existing, int) and existing > 0 else row_cap
        )
        capped_queries.append(query)

    if not capped_queries:
        raise ToolError(
            f"Chart {chart_id} has an unusable query context and cannot be run.",
            code="no_query_context",
        )

    prepared["queries"] = capped_queries
    # `force: false` keeps Superset's own cache in play; the JSON/full pair is
    # the only result shape this tool knows how to summarise.
    prepared["force"] = False
    prepared["result_format"] = "json"
    prepared["result_type"] = "full"
    return prepared


def _summarise_chart_data(
    payload: dict[str, Any], chart: dict[str, Any], prepared: dict[str, Any]
) -> dict[str, Any]:
    """Truncate and describe a chart/data response.

    Truncation is stated explicitly in the payload because the alternative is the
    model reporting the top 50 of 800 facilities as though it had seen them all.
    """
    results = payload.get("result") or []
    first = results[0] if results and isinstance(results[0], dict) else {}

    if first.get("status") == "failed" or first.get("error"):
        raise ToolError(
            f"Superset could not run chart {chart.get('id')}: {first.get('error')}",
            code="query_failed",
        )

    rows = first.get("data")
    if not isinstance(rows, list):
        raise ToolError(
            "That chart returned a result shape I cannot read as a table.",
            code="unsupported_result_shape",
        )

    colnames = [str(name) for name in (first.get("colnames") or [])]
    if not colnames and rows and isinstance(rows[0], dict):
        colnames = [str(key) for key in rows[0]]

    kept_columns, columns_truncated = _truncate_list(colnames, _cap_columns())
    kept_rows, rows_truncated = _truncate_list(rows, _cap_rows())

    column_set = set(kept_columns)
    trimmed_rows = [
        {key: value for key, value in row.items() if key in column_set}
        if isinstance(row, dict)
        else row
        for row in kept_rows
    ]

    query = (prepared.get("queries") or [{}])[0]
    result: dict[str, Any] = {
        "chart_id": chart.get("id"),
        "chart_name": chart.get("slice_name"),
        "viz_type": chart.get("viz_type"),
        "dataset": chart.get("datasource_name_text"),
        "columns": kept_columns,
        "rows": trimmed_rows,
        "rows_returned": len(rows),
        "rows_shown": len(trimmed_rows),
        "truncated": {
            "rows": rows_truncated,
            "columns": columns_truncated,
            "row_limit_applied": query.get("row_limit"),
        },
        "query": {
            "metrics": _describe_value(query.get("metrics")),
            "group_by": _describe_value(query.get("columns")),
            "filters": query.get("filters") or [],
            "time_range": query.get("time_range"),
            "order_by": query.get("orderby"),
        },
        "applied_filters": first.get("applied_filters") or [],
        "rejected_filters": first.get("rejected_filters") or [],
        "from_cache": bool(first.get("is_cached")),
        "queried_at": first.get("queried_dttm"),
    }
    row_limit = query.get("row_limit")
    # Two different truncations can stack: our row_limit cap on the query, then
    # the display cap here. When the query came back exactly at the cap, the true
    # total is unknown — saying "50 of 200" would imply 200 is the whole answer.
    hit_query_cap = isinstance(row_limit, int) and len(rows) >= row_limit
    result["complete_result"] = not (rows_truncated or columns_truncated or hit_query_cap)
    if not result["complete_result"]:
        total = f"at least {len(rows)}" if hit_query_cap else str(len(rows))
        result["note"] = (
            f"Showing {len(trimmed_rows)} of {total} rows"
            f"{' and a subset of columns' if columns_truncated else ''}"
            f"{f'; the query itself was capped at {row_limit} rows' if hit_query_cap else ''}. "
            "Say so if you quote these figures — this is not the complete result."
        )
    if len(results) > 1:
        result["additional_query_results_ignored"] = len(results) - 1
    return result


def _summarise_mcp_chart_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Truncate and describe native MCP's `get_chart_data` ChartData response.

    Superset builds and runs the query server-side here (unlike the REST path,
    which replays a stored query_context this process assembles) -- row_count/
    total_rows/columns/data come back already shaped, so there is less to do
    than `_summarise_chart_data`, not more.
    """
    colnames = [c.get("name") for c in payload.get("columns") or [] if isinstance(c, dict)]
    rows = payload.get("data") or []

    kept_columns, columns_truncated = _truncate_list(colnames, _cap_columns())
    kept_rows, rows_truncated = _truncate_list(rows, _cap_rows())

    column_set = set(kept_columns)
    trimmed_rows = [
        {key: value for key, value in row.items() if key in column_set}
        if isinstance(row, dict)
        else row
        for row in kept_rows
    ]

    total_rows = payload.get("total_rows")
    row_count = payload.get("row_count", len(rows))
    complete_result = not (rows_truncated or columns_truncated) and (
        total_rows is None or total_rows <= row_count
    )

    result: dict[str, Any] = {
        "chart_id": payload.get("chart_id"),
        "chart_name": payload.get("chart_name"),
        "viz_type": payload.get("chart_type"),
        "columns": kept_columns,
        "rows": trimmed_rows,
        "rows_returned": row_count,
        "rows_shown": len(trimmed_rows),
        "complete_result": complete_result,
        "summary": payload.get("summary"),
        "insights": payload.get("insights") or [],
    }
    if not complete_result:
        total = total_rows if total_rows is not None else row_count
        result["note"] = (
            f"Showing {len(trimmed_rows)} of {total} rows"
            f"{' and a subset of columns' if columns_truncated else ''}. "
            "Say so if you quote these figures — this is not the complete result."
        )
    cache_status = payload.get("cache_status")
    if cache_status:
        result["from_cache"] = cache_status
    return result


# --- Page context --------------------------------------------------------


class GetPageContextTool(Tool):
    """What the user is currently looking at, as reported by the panel."""

    name = "get_page_context"
    description = (
        "Return what the user currently has open in Superset: page type, dashboard "
        "or chart or dataset id, active filters and any selected widget. Call this "
        "first whenever the user says 'this chart', 'this dashboard', 'here' or "
        "similar, so you resolve the reference correctly. The selected widget is a "
        "hint from the browser, not authority — confirm the object by name with "
        "get_chart or get_dashboard before acting on it. On custom dashboard pages "
        "the response also carries the publication's title, preset, external url "
        "and the dataset ids declared for that page (when present), so questions "
        "about an external application can be answered from its backing datasets."
    )
    mode = ToolMode.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext) -> dict[str, Any]:
        page_context = context.page_context
        source = "panel"
        if not page_context and context.conversation is not None:
            # Falls back to where the conversation started, which is better than
            # nothing when the panel sent no context on this particular turn.
            page_context = context.conversation.page_scope
            source = "conversation_start"

        if not isinstance(page_context, dict) or not page_context:
            return {
                "available": False,
                "note": (
                    "The panel did not report a page context. Ask the user which "
                    "dashboard or chart they mean, or use list_dashboards."
                ),
            }

        result = {
            "available": True,
            "source": source,
            "user": context.username,
            "page": page_context,
        }
        publication_id = page_context.get("publication_id")
        if publication_id:
            publication = None
            try:
                from django.core.exceptions import ValidationError
                from lifedata_ai.models import DashboardPublication
                publication = DashboardPublication.objects.filter(
                    pk=publication_id).first()
            except (ValueError, ValidationError):
                publication = None
            if publication is not None:
                configuration = publication.configuration or {}
                dataset_ids = configuration.get("dataset_ids") or []
                datasets = []
                if dataset_ids:
                    client = context.superset_client()
                    for identifier in dataset_ids:
                        if type(identifier) is not int or identifier < 1:
                            continue
                        try:
                            datasets.append(client.get_dataset(identifier))
                        except SupersetError:
                            continue
                result["publication"] = {
                    "id": str(publication.id),
                    "title": publication.title,
                    "theme": publication.theme,
                    "preset": configuration.get("preset"),
                    "url": configuration.get("url"),
                    "dataset_ids": dataset_ids,
                    "datasets": [
                        {
                            "id": dataset.get("id"),
                            "table_name": dataset.get("table_name"),
                            "schema": dataset.get("schema"),
                            "database_id": (dataset.get("database") or {}).get("id"),
                        }
                        for dataset in datasets
                    ],
                }
        return result
