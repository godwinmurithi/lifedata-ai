"""Create a brand-new Superset dataset from values the model supplies.

Every other write tool assumes a Superset dataset already exists; this is the
one exception, for the case a user asks for a chart or dashboard over data
that is not there yet (a demo, a what-if, a dataset someone forgot to load).

The obvious implementation — have the model write a SQL `SELECT` that
fabricates rows (`generate_series`, literal `VALUES`, ...) and register it as
a Superset virtual dataset — is a dead end on this codebase by design:
`validation/validator.py`'s `_rule_no_adhoc_sql` refuses any SQL-shaped string
anywhere in a write payload (spec §6.3, "this copilot cannot write SQL"), and
that refusal exists specifically to keep a model-authored write action from
ever becoming a SQL injection vector. A synthetic-data tool is not an
exception to that; it is exactly the shape of thing the rule is for.

So this tool never builds SQL. The model supplies plain column names, column
types and row values — `_SCALAR` leaves, same as every other tool — and
`build_calls` turns those into one `multipart/form-data` call against
Superset's own, already-reviewed CSV-upload endpoint
(`POST /api/v1/database/{id}/upload/`), which both creates the physical table
and registers the Superset dataset, owned by the uploading user (see
`UploadCommand.run` in Superset's own `commands/database/uploaders/base.py`).
The panel executes it with the user's session, same as any other write — see
`static/panel/panel.js`'s `buildMultipartBody`.

`database_id` is deliberately not a payload field the model can set — see
`settings.LIFEDATA_AI_SYNTHETIC_DATASET_DATABASE_ID`'s docstring.

`ImportDatasetFromAttachmentTool` below is the sibling for data that arrived as
an upload instead: it forwards a `ChatAttachment` row's own bytes to the same
upload endpoint, so the model never retypes a value and there is no size-via-
transcription limit beyond the attachment's own cap.
"""

from __future__ import annotations

import base64
import csv
import io
from typing import Any, ClassVar

from django.conf import settings

from lifedata_ai.models import ChatAttachment
from lifedata_ai.tools.base import ToolError
from lifedata_ai.tools.superset_write import WriteTool
from lifedata_ai.validation.schemas import get_schema


def _format_cell(value: Any) -> str:
    """Render one `_SCALAR` leaf as CSV text. No coercion beyond this."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_csv(columns: list[dict[str, Any]], rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([column["name"] for column in columns])
    for row in rows:
        writer.writerow([_format_cell(value) for value in row])
    return buffer.getvalue().encode("utf-8")


def _synthetic_database_id() -> int:
    """The sandbox connection both dataset-creation tools upload into, or raise."""
    database_id = settings.LIFEDATA_AI_SYNTHETIC_DATASET_DATABASE_ID
    if not database_id:
        raise ToolError(
            "Creating a new dataset is not configured on this deployment "
            "(LIFEDATA_AI_SYNTHETIC_DATASET_DATABASE_ID is unset). Ask an operator to point "
            "it at a sandbox database connection with file uploads enabled, or build the "
            "dashboard from an existing dataset instead.",
            code="not_configured",
        )
    return database_id


class CreateSyntheticDatasetTool(WriteTool):
    name: ClassVar[str] = "create_dataset_from_values"
    action_type: ClassVar[str] = "create_dataset_from_values"
    object_type: ClassVar[str] = "dataset"
    description: ClassVar[str] = (
        "Create a brand-new Superset dataset from invented or illustrative data, for when "
        "a user wants a chart or dashboard over data that does not exist yet (a demo, a "
        "mock, a what-if). Pass table_name, a list of columns ({name, type}), and rows as "
        "arrays of plain values in column order — never SQL, never an expression; this tool "
        "cannot and will not accept one. Only use this when the user has confirmed the data "
        "is invented, illustrative or synthetic, never to paper over a missing real dataset "
        "silently. The new table belongs to a dedicated sandbox connection, never the "
        "deployment's real operational data."
    )
    input_schema: ClassVar[dict[str, Any]] = {}  # set below from the action schema

    def build_calls(
        self, context, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        database_id = _synthetic_database_id()

        columns = payload["columns"]
        rows = payload["rows"]

        names = [column["name"] for column in columns]
        if len(names) != len(set(names)):
            raise ToolError(
                "Column names must be unique within one dataset.",
                code="invalid_arguments",
            )
        bad_row = next((i for i, row in enumerate(rows) if len(row) != len(columns)), None)
        if bad_row is not None:
            raise ToolError(
                f"rows[{bad_row}] has {len(rows[bad_row])} values but {len(columns)} "
                "columns were declared. Every row must supply exactly one value per column, "
                "in the same order.",
                code="invalid_arguments",
            )

        csv_bytes = _build_csv(columns, rows)
        body = {
            "type": "csv",
            "table_name": payload["table_name"],
            "already_exists": payload.get("already_exists", "fail"),
            "header_row": "0",
            "delimiter": ",",
        }
        return [
            {
                "method": "POST",
                "path": f"/api/v1/database/{database_id}/upload/",
                "body": body,
                # The body above is plain form fields; the file itself travels
                # separately because JSON cannot carry raw bytes. See
                # panel.js's buildMultipartBody for how this is turned into a
                # FormData — this call shape has no JSON-body analogue.
                "multipart": {
                    "field": "file",
                    "filename": f"{payload['table_name']}.csv",
                    "content_type": "text/csv",
                    "content_base64": base64.b64encode(csv_bytes).decode("ascii"),
                },
            }
        ]

    def summarise(self, payload: dict[str, Any]) -> str:
        rows = payload.get("rows") or []
        columns = payload.get("columns") or []
        return (
            f"create dataset “{payload.get('table_name')}” "
            f"({len(rows)} rows, {len(columns)} columns) from invented values"
        )


class ImportDatasetFromAttachmentTool(WriteTool):
    name: ClassVar[str] = "import_dataset_from_attachment"
    action_type: ClassVar[str] = "import_dataset_from_attachment"
    object_type: ClassVar[str] = "dataset"
    description: ClassVar[str] = (
        "Create a brand-new Superset dataset from a CSV, TSV or XLSX file the user attached "
        "to this conversation, uploading that file's own bytes unmodified. Use this instead "
        "of create_dataset_from_values whenever the data came in as an attachment rather than "
        "being invented in the conversation — never retype an attached file's rows into that "
        "tool's columns/rows arguments. Pass attachment_id exactly as noted after the "
        "attachment, and table_name."
    )
    input_schema: ClassVar[dict[str, Any]] = {}  # set below from the action schema

    def build_calls(
        self, context, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        database_id = _synthetic_database_id()

        try:
            attachment = ChatAttachment.objects.get(
                pk=payload["attachment_id"], conversation=context.conversation
            )
        except ChatAttachment.DoesNotExist:
            # Scoped to `context.conversation` rather than a bare pk lookup —
            # the same check that makes a wrong id "not found" also makes
            # another conversation's attachment id "not found", which is the
            # correct answer either way (spec §1: never exceed what this
            # conversation's own turn was given).
            raise ToolError(
                "No such attachment on this conversation. Ask the user to attach the file "
                "again.",
                code="not_found",
            )

        is_csv = attachment.file_type == ChatAttachment.FileType.CSV
        body = {
            "type": attachment.file_type,
            "table_name": payload["table_name"],
            "already_exists": payload.get("already_exists", "fail"),
            "header_row": "0",
        }
        if is_csv:
            body["delimiter"] = attachment.delimiter or ","
        return [
            {
                "method": "POST",
                "path": f"/api/v1/database/{database_id}/upload/",
                "body": body,
                "multipart": {
                    "field": "file",
                    "filename": attachment.filename,
                    "content_type": "text/csv" if is_csv else (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                    "content_base64": base64.b64encode(bytes(attachment.data)).decode("ascii"),
                },
            }
        ]

    def summarise(self, payload: dict[str, Any]) -> str:
        return f"create dataset “{payload.get('table_name')}” from an attached file"


# Same pattern as superset_write.py's `_bind_schemas`: the model-facing schema
# is always the governed action schema, never hand-duplicated here.
CreateSyntheticDatasetTool.input_schema = get_schema(CreateSyntheticDatasetTool.action_type)
ImportDatasetFromAttachmentTool.input_schema = get_schema(ImportDatasetFromAttachmentTool.action_type)
