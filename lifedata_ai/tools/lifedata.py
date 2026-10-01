"""LifeData domain tools: dynamic forms (spec §5).

These tools read the dynamic-form metadata tables directly from Postgres,
because those tables are created by `manage.py seed_lifehealth` and have no
Django models to query through.

This module used to also hold two registry-backed tools (`explain_indicator`,
`recommend_dataset`) that quoted definitions from the LifeData semantics
registry; both were removed along with the registry itself.

**Form ANSWERS are never returned.** `form_answer` is submission-level data.
These tools read `dynamic_form`, `form_version` and `form_question` — metadata
— plus aggregate submission counts. Analysis of the answers happens through
the pre-built flattened reporting view, via Superset, under its own
permissions.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection, transaction

from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode

# A form version with more questions than this is a data problem, not a prompt
# budget problem; cap rather than flood the context window.
_MAX_QUESTIONS = 200

#: Message reused whenever the LifeHealth tables are absent. The operator fix is
#: a single command, so name it rather than surfacing a raw ProgrammingError.
_FORMS_MISSING = (
    "The dynamic-form metadata tables are not present in this database. "
    "They are created by `manage.py seed_lifehealth`."
)


# --- Dynamic-form metadata tools -----------------------------------------
#
# POC scope (spec §5): exactly one form — diabetes_screening — is wired end to
# end, and its flattened reporting view (reporting.rpt_diabetes_screening) is
# hand-written SQL that pivots the answers into columns. There is deliberately
# NO generic flattener: the view names, types and suppression rules for each
# form are a governance decision, not something to synthesise at runtime. These
# tools therefore report which forms have a wired reporting view and which do
# not, rather than pretending every form is analysable.


class ListFormsTool(Tool):
    """Every dynamic form with its versions, questions and wired reporting view."""

    name = "list_forms"
    description = (
        "List the dynamic data-collection forms in LifeData with their "
        "versions, submission counts and question metadata (key, label, type, "
        "unit, sensitivity flag), and say which forms have a pre-built "
        "flattened reporting view that can actually be charted. Call this when "
        "the user asks what forms exist or wants to analyse form data. Returns "
        "metadata and counts only — never individual submissions or answers."
    )
    mode = ToolMode.READ
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext) -> dict[str, Any]:
        limit = int(getattr(settings, "LIFEDATA_AI_MAX_ROWS_TO_MODEL", 50))

        rows = _query(
            """
            SELECT df.form_id,
                   df.form_key,
                   df.form_name,
                   v.form_version_id,
                   v.version_number,
                   v.published_on,
                   (SELECT COUNT(*)
                      FROM public.form_submission s
                     WHERE s.form_version_id = v.form_version_id) AS submission_count
              FROM public.dynamic_form df
              JOIN public.form_version v USING (form_id)
             ORDER BY df.form_id, v.version_number
            """
        )
        if not rows:
            return {"forms": [], "note": "No dynamic forms are defined in this database."}

        questions_by_version = _questions_by_version([r[3] for r in rows])

        forms: dict[int, dict[str, Any]] = {}
        for form_id, form_key, form_name, version_id, version_number, published_on, count in rows:
            form = forms.setdefault(
                form_id,
                {
                    "form_id": form_id,
                    "form_key": form_key,
                    "form_name": form_name,
                    "versions": [],
                    **_reporting_view_for(form_key),
                },
            )
            form["versions"].append(
                {
                    "form_version_id": version_id,
                    "version_number": version_number,
                    "published_on": published_on.isoformat() if published_on else None,
                    "submission_count": count,
                    "questions": questions_by_version.get(version_id, []),
                }
            )

        ordered = list(forms.values())[:limit]
        return {
            "forms": ordered,
            "truncated": len(forms) > len(ordered),
            "usage": (
                "Only forms with a reporting_view can be charted. Analyse them "
                "through that Superset dataset; individual answers are never "
                "available to this assistant."
            ),
        }


class GetFormSchemaTool(Tool):
    """One form's full question list, plus the reporting view that flattens it."""

    name = "get_form_schema"
    description = (
        "Return the complete question list for one dynamic form — every "
        "version, with each question's key, label, type, unit, sensitivity and "
        "display order — together with the flattened reporting view that makes "
        "it analysable and that view's columns. Identify the form by form_key "
        "(preferred, e.g. 'diabetes_screening') or form_id; pass null for the "
        "one you are not using. Call this before proposing a chart on form "
        "data, because the view's column names are hand-written and do not "
        "mechanically follow the question keys."
    )
    mode = ToolMode.READ
    # Both arguments are listed as required with a null option: that is what
    # keeps `strict: true` available (it needs required == properties) while
    # still letting the model supply just one of them.
    input_schema = {
        "type": "object",
        "properties": {
            "form_key": {
                "type": ["string", "null"],
                "description": "Stable form key, e.g. 'diabetes_screening'. Null if using form_id.",
            },
            "form_id": {
                "type": ["integer", "null"],
                "description": "Numeric form id from list_forms. Null if using form_key.",
            },
        },
        "required": ["form_key", "form_id"],
        "additionalProperties": False,
    }

    def run(
        self,
        context: ToolContext,
        form_key: str | None = None,
        form_id: int | None = None,
    ) -> dict[str, Any]:
        if not form_key and form_id is None:
            raise ToolError(
                "Supply either form_key or form_id. Call list_forms if you do "
                "not know them.",
                code="invalid_arguments",
            )

        if form_key:
            rows = _query(
                "SELECT form_id, form_key, form_name FROM public.dynamic_form "
                "WHERE form_key = %s",
                [form_key.strip()],
            )
        else:
            rows = _query(
                "SELECT form_id, form_key, form_name FROM public.dynamic_form "
                "WHERE form_id = %s",
                [form_id],
            )

        if not rows:
            known = _query("SELECT form_key FROM public.dynamic_form ORDER BY form_key")
            raise ToolError(
                f"No form matching {form_key or form_id!r}.",
                code="unknown_form",
                details={"known_form_keys": [r[0] for r in known]},
            )

        resolved_id, resolved_key, form_name = rows[0]

        versions = _query(
            """
            SELECT v.form_version_id,
                   v.version_number,
                   v.published_on,
                   (SELECT COUNT(*)
                      FROM public.form_submission s
                     WHERE s.form_version_id = v.form_version_id) AS submission_count
              FROM public.form_version v
             WHERE v.form_id = %s
             ORDER BY v.version_number
            """,
            [resolved_id],
        )
        questions_by_version = _questions_by_version([v[0] for v in versions])

        view = _reporting_view_for(resolved_key)

        payload: dict[str, Any] = {
            "form_id": resolved_id,
            "form_key": resolved_key,
            "form_name": form_name,
            "versions": [
                {
                    "form_version_id": version_id,
                    "version_number": number,
                    "published_on": published_on.isoformat() if published_on else None,
                    "submission_count": count,
                    "questions": questions_by_version.get(version_id, []),
                }
                for version_id, number, published_on, count in versions
            ],
            **view,
        }

        if view["reporting_view"]:
            payload["reporting_view_columns"] = _view_columns(view["reporting_view"])
            payload["reporting_view_description"] = _view_comment(view["reporting_view"])
            payload["usage"] = (
                "Chart this form through the reporting view's Superset dataset. "
                "Match questions to columns by reading the column list above — "
                "the view is hand-written, so column names are not derived from "
                "question keys (for example 'fasting_glucose' becomes "
                "'fasting_glucose_mmol_l'). Individual answers are not "
                "available."
            )
        else:
            payload["usage"] = (
                "This form has no flattened reporting view, so it cannot be "
                "charted. Say so plainly rather than proposing a chart; wiring "
                "a form is a governed change, not something to improvise."
            )

        return payload


# --- Shared helpers ------------------------------------------------------


def _reporting_view_for(form_key: str) -> dict[str, Any]:  # noqa: ARG001
    """Which Superset dataset flattens this form, if any.

    Used to look this up in the LifeData semantics registry's per-dataset
    `form_key` field (removed along with the registry). At removal time, no
    dataset in the registry actually declared a `form_key` — every form was
    already reporting "not wired" in production — so this always returning
    "not wired" is the verified current behaviour, not a regression. Wiring a
    form's reporting view now needs a code change here instead of a registry
    edit.
    """
    return {"reporting_view": None, "superset_dataset_id": None, "wired": False}


def _questions_by_version(version_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Question metadata for the given versions, grouped by version id.

    Note what is absent: no join to `form_answer`. Question definitions are
    metadata; answers are submission-level data this assistant never sees.
    """
    if not version_ids:
        return {}

    rows = _query(
        """
        SELECT form_version_id,
               question_id,
               question_key,
               question_label,
               question_type,
               unit,
               is_sensitive,
               display_order
          FROM public.form_question
         WHERE form_version_id = ANY(%s)
         ORDER BY form_version_id, display_order
         LIMIT %s
        """,
        [list(version_ids), _MAX_QUESTIONS],
    )

    grouped: dict[int, list[dict[str, Any]]] = {}
    for version_id, qid, key, label, qtype, unit, sensitive, order in rows:
        grouped.setdefault(version_id, []).append(
            {
                "question_id": qid,
                "question_key": key,
                "label": label,
                "type": qtype,
                "unit": unit,
                "is_sensitive": sensitive,
                "display_order": order,
            }
        )
    return grouped


def _view_columns(qualified_view: str) -> list[dict[str, str]]:
    """Columns of a reporting view, read from the live catalogue."""
    schema, _, view = qualified_view.partition(".")
    rows = _query(
        """
        SELECT column_name, data_type
          FROM information_schema.columns
         WHERE table_schema = %s AND table_name = %s
         ORDER BY ordinal_position
        """,
        [schema, view],
    )
    return [{"column": name, "type": data_type} for name, data_type in rows]


def _view_comment(qualified_view: str) -> str | None:
    """The view's COMMENT, which is where its interpretation traps are written.

    `to_regclass` returns NULL rather than raising for a missing view, so a
    database that has not been seeded degrades to None instead of an error.
    """
    rows = _query(
        "SELECT obj_description(to_regclass(%s), 'pg_class')", [qualified_view]
    )
    return rows[0][0] if rows else None


def _query(sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    """Run a fixed, parameterised query against the LifeHealth tables.

    These tables are created by `manage.py seed_lifehealth` and have no Django
    models, hence raw SQL. Every statement here is a literal in this module —
    no model-supplied SQL reaches the database, and none ever will.

    A `DatabaseError` almost always means the schema has not been seeded, so it
    is translated into a `ToolError` the model can report intelligibly rather
    than a 500 that ends the turn.

    The `atomic()` wrapper is what makes that translation safe. PostgreSQL
    aborts the entire transaction on a failed statement, so simply catching the
    error would leave the connection refusing every subsequent command —
    including the `ToolCallLog` INSERT that audits this very call, and any tool
    called later in the same turn. Running inside a savepoint contains the
    failure to this query.
    """
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params or [])
            return cursor.fetchall()
    except DatabaseError as exc:
        raise ToolError(
            _FORMS_MISSING,
            code="forms_unavailable",
            details={"database_error": str(exc)[:300]},
        ) from exc
