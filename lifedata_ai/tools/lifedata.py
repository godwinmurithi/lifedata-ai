"""LifeData domain tools: governed definitions, dataset choice, dynamic forms (spec §5).

These four tools are what stop the model inventing health semantics. Two read the
semantics registry (`semantics/registry.yaml`), which is also what the validator
enforces against, so what the model is *told* and what the system *permits* come
from one file. Two read the dynamic-form metadata tables directly from Postgres,
because those tables are created by `manage.py seed_lifehealth` and have no
Django models to query through.

Two boundaries are load-bearing here:

* **Definitions are quoted, never paraphrased.** `explain_indicator` returns the
  registry text verbatim and says so in the payload. A paraphrase of "counts
  tests, not patients" is exactly the failure mode the registry exists to
  prevent.
* **Form ANSWERS are never returned.** `form_answer` is submission-level data.
  These tools read `dynamic_form`, `form_version` and `form_question` — metadata
  — plus aggregate submission counts. Analysis of the answers happens through
  the pre-built flattened reporting view, via Superset, under its own
  permissions.
"""

from __future__ import annotations

import difflib
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection, transaction

from lifedata_ai.semantics.loader import Dataset, Indicator, Registry
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


# --- Registry-backed tools -----------------------------------------------


class ExplainIndicatorTool(Tool):
    """Return an indicator's governed definition verbatim from the registry."""

    name = "explain_indicator"
    description = (
        "Return the governed, authoritative definition of a LifeData health "
        "indicator (for example malaria_positivity, unique_patients, "
        "lab_turnaround_hours): its definition, numerator, denominator, unit, "
        "what it counts (tests vs patients vs encounters) and its caveats. "
        "Call this BEFORE describing, charting or explaining any named "
        "indicator, and quote the returned text rather than restating it in "
        "your own words — the wording encodes distinctions (order is not a "
        "result, a test is not a patient) that a paraphrase loses."
    )
    mode = ToolMode.READ
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Indicator key as declared in the registry, e.g. "
                    "'malaria_positivity'. Unknown names return the full list "
                    "of valid keys."
                ),
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, name: str) -> dict[str, Any]:
        registry = context.registry()
        indicator = registry.indicator(name.strip())
        if indicator is None:
            known = sorted(registry.indicators)
            # Listing the valid keys turns a dead end into a correctable call;
            # inventing a definition for an unknown indicator is the one thing
            # this tool exists to prevent.
            raise ToolError(
                f"No governed indicator named '{name}'. There is no definition "
                "for it, so do not invent one — choose from the known "
                "indicators or describe the calculation explicitly instead.",
                code="unknown_indicator",
                details={
                    "known_indicators": known,
                    "did_you_mean": difflib.get_close_matches(name, known, n=3, cutoff=0.5),
                },
            )

        dataset = registry.dataset(indicator.dataset)
        return {
            "indicator": _indicator_payload(indicator),
            "dataset": _dataset_payload(dataset) if dataset else None,
            "usage": (
                "This definition is governed. Quote it verbatim when explaining "
                "the indicator; do not paraphrase, widen or simplify it."
            ),
        }


class RecommendDatasetTool(Tool):
    """Rank the approved datasets against a stated analysis goal."""

    name = "recommend_dataset"
    description = (
        "Given an analysis goal in plain language (for example 'laboratory "
        "turnaround by facility' or 'where do our patients come from'), rank "
        "the approved LifeData datasets by their declared subject areas and "
        "return each one's Superset dataset id, grain, geography policy and "
        "interpretation notes. Call this first when choosing what to chart, so "
        "the dataset is picked on declared semantics rather than on a guess "
        "from its name — the notes carry the traps (an order is not a result, "
        "an encounter is not a patient)."
    )
    mode = ToolMode.READ
    input_schema = {
        "type": "object",
        "properties": {
            "analysis_goal": {
                "type": "string",
                "description": (
                    "What the user wants to analyse, in their own words. "
                    "Free text; keywords are matched against declared subject "
                    "areas."
                ),
            },
        },
        "required": ["analysis_goal"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, analysis_goal: str) -> dict[str, Any]:
        registry = context.registry()
        ranked = registry.rank_datasets(analysis_goal)

        if ranked:
            matched = True
        else:
            # A goal phrased in clinical language ("diabetes risk in Gulu")
            # often overlaps no declared subject area. Returning nothing would
            # push the model to guess a dataset name; returning the full
            # catalogue with its notes lets it choose on stated semantics.
            matched = False
            ranked = [
                (ds, "no subject-area keyword matched; listed for completeness")
                for ds in registry.datasets.values()
            ]

        return {
            "analysis_goal": analysis_goal,
            "matched_on_subject_areas": matched,
            "datasets": [
                {**_dataset_payload(ds), "why": why, "rank": position}
                for position, (ds, why) in enumerate(ranked, start=1)
            ],
            "usage": (
                "State which dataset you chose and why. Respect each dataset's "
                "grain: a row count on rpt_encounters is encounters, not "
                "people."
            ),
        }


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
        registry = context.registry()
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
                    **_reporting_view_for(registry, form_key),
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

        registry = context.registry()
        view = _reporting_view_for(registry, resolved_key)

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


def _indicator_payload(indicator: Indicator) -> dict[str, Any]:
    """The registry text, field for field, with nothing rewritten."""
    return {
        "name": indicator.name,
        "dataset": indicator.dataset,
        "definition": indicator.definition,
        "expression": indicator.expression or None,
        "numerator": indicator.numerator or None,
        "denominator": indicator.denominator or None,
        "unit": indicator.unit,
        "counts": indicator.counts,
        "caveats": indicator.caveats or None,
        "verbatim": True,
    }


def _dataset_payload(dataset: Dataset) -> dict[str, Any]:
    """Dataset metadata including the geography policy the validator enforces."""
    payload: dict[str, Any] = {
        "name": dataset.name,
        "superset_dataset_id": dataset.superset_dataset_id,
        "grain": dataset.grain,
        "subject_areas": list(dataset.subject_areas),
        "sensitivity": dataset.sensitivity,
        "geography": dataset.geography,
        "notes": dataset.notes,
    }
    if dataset.is_patient_geography:
        # Stated up front so the model never proposes a chart the validator will
        # reject on geography grounds (spec §6.4).
        payload["geography_policy"] = (
            "Patient geography: district is the finest permitted grouping, and "
            "point, scatter and heat maps are refused. Use a district-level "
            "choropleth."
        )
    if dataset.small_cell_threshold:
        payload["small_cell_threshold"] = dataset.small_cell_threshold
    return payload


def _reporting_view_for(registry: Registry, form_key: str) -> dict[str, Any]:
    """Which registry dataset flattens this form, if any.

    Derived from the registry's `form_key` field rather than hard-coded, so
    wiring a second form is a registry edit plus a view, not a code change here.
    """
    dataset = next(
        (ds for ds in registry.datasets.values() if ds.form_key == form_key),
        None,
    )
    if dataset is None:
        return {"reporting_view": None, "superset_dataset_id": None, "wired": False}
    return {
        "reporting_view": f"reporting.{dataset.name}",
        "superset_dataset_id": dataset.superset_dataset_id,
        "wired": True,
    }


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
