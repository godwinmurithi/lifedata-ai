"""Generic discovery criteria compiled into permission-scoped Superset queries."""

from __future__ import annotations

import math
from typing import Any

from lifedata_ai.tools.base import ToolError

STAGES = [
    "Total population",
    "Willingness / participation",
    "Required blood group",
    "Selected screening results",
    "Age & clinical criteria",
    "Geography & proximity",
]

# Field names refer to the existing Sandbox join; no clinical thresholds are inferred.
FIELDS: dict[str, dict[str, Any]] = {}


def _fields(group, stage, kind, fields):
    for name, label in fields:
        FIELDS[name] = {"field": name, "label": label, "group": group, "stage": stage, "kind": kind}


_fields(
    "Participation",
    1,
    "category",
    [
        ("blood_donor", "Willing to donate blood"),
        ("organ_donor", "Organ donor willingness"),
        ("clinical_trial", "Clinical study participation"),
    ],
)
_fields("Blood group", 2, "category", [("blood_group", "Blood group")])
_fields(
    "Screening",
    3,
    "category",
    [
        ("hiv_status", "HIV screening"),
        ("s_hbsag", "Hepatitis B antigen screening"),
        ("s_hcv", "Hepatitis C screening"),
        ("s_syphillis", "Syphilis screening"),
        ("malaria", "Malaria screening"),
        ("malaria_in_past_six_months", "Malaria in past six months"),
        ("tb", "TB screening"),
        ("tb_in_past_year", "TB in past year"),
        ("shbsab", "Hepatitis B surface antibody"),
        ("s_hbcab", "Hepatitis B core antibody"),
        ("hb_electro", "Haemoglobin electrophoresis"),
        ("urine", "Urine screening"),
    ],
)
_fields(
    "Demographics & clinical",
    4,
    "category",
    [
        ("sex", "Sex"),
        ("sickle_cells_disease", "Recorded sickle cell disease"),
        ("sickle_cell_carrier", "Recorded sickle cell carrier"),
        ("sickle_cell_type", "Sickle cell type"),
        ("disability", "Disability"),
        ("mental_health", "Mental health"),
        ("bp_results", "Blood pressure category"),
        ("visual_issue", "Visual issues"),
        ("hearing_issues", "Hearing issues"),
        ("marital_status", "Marital status"),
    ],
)
_fields(
    "Geography",
    5,
    "category",
    [(f, f.title()) for f in ("district", "county", "subcounty", "parish", "village")],
)
_fields(
    "Demographics & clinical",
    4,
    "number",
    [
        ("age", "Age (recorded years)"),
        ("weight", "Weight (recorded value)"),
        ("height", "Height (recorded value)"),
        ("bmi", "BMI"),
        ("blood_pressure_sys", "Systolic blood pressure"),
        ("blood_pressure_dys", "Diastolic blood pressure"),
        ("temp", "Temperature"),
        ("pulse_rate", "Pulse rate"),
        ("spo2", "Oxygen saturation"),
    ],
)
_fields(
    "Laboratory values",
    3,
    "number",
    [
        ("hgb", "Haemoglobin (recorded value)"),
        ("hct", "Haematocrit"),
        ("wbc", "White blood cells"),
        ("rbc", "Red blood cells"),
        ("plt", "Platelets"),
        ("mcv", "Mean corpuscular volume"),
        ("mchc", "Mean corpuscular haemoglobin concentration"),
        ("rdw", "Red cell distribution width"),
        ("mpv", "Mean platelet volume"),
        ("neu", "Neutrophils"),
        ("neu_", "Neutrophils percentage"),
        ("lym", "Lymphocytes"),
        ("lym_", "Lymphocytes percentage"),
        ("mono", "Monocytes"),
        ("mono_", "Monocytes percentage"),
        ("eos", "Eosinophils"),
        ("eos_", "Eosinophils percentage"),
        ("baso", "Basophils"),
        ("baso_", "Basophils percentage"),
        ("crp", "C-reactive protein"),
        ("fasting_glucose", "Fasting glucose"),
        ("hba1c", "HbA1c"),
        ("vitaminb12", "Vitamin B12"),
        ("folate", "Folate"),
        ("total_cholesterol", "Total cholesterol"),
        ("hdl", "HDL"),
        ("ldl", "LDL"),
        ("tri_glycerides", "Triglycerides"),
        ("trans_ferrin", "Transferrin"),
        ("tibc", "TIBC"),
        ("ferritin", "Ferritin"),
        ("alt", "ALT"),
        ("alp", "ALP"),
        ("ast", "AST"),
        ("ggt", "GGT"),
        ("total_bilirubin", "Total bilirubin"),
        ("direct_bilirubin", "Direct bilirubin"),
        ("indirect_bilirubin", "Indirect bilirubin"),
        ("total_protein", "Total protein"),
        ("albumin", "Albumin"),
        ("globulin", "Globulin"),
        ("creatinine", "Creatinine"),
        ("urea", "Urea"),
        ("gfr", "GFR"),
        ("vitamin_d", "Vitamin D"),
        ("psa", "PSA"),
        ("tsh__thyroid", "TSH"),
        ("ck", "CK"),
        ("ckmb", "CK-MB"),
        ("ldh", "LDH"),
        ("uric_acid", "Uric acid"),
        ("calcium", "Calcium"),
        ("phosphorus", "Phosphorus"),
        ("magnesium", "Magnesium"),
        ("amylase", "Amylase"),
        ("lipase", "Lipase"),
        ("testorterone", "Testosterone"),
    ],
)
_fields("Geography", 5, "number", [("distance_km", "Distance from selected facility (km)")])
# Expose the original recorded status alongside its numeric laboratory value.
for _name, _definition in list(FIELDS.items()):
    if _definition["group"] == "Laboratory values":
        _fields(
            "Laboratory flags", 3, "category", [("s_" + _name, _definition["label"] + " status")]
        )
for _name in (
    "v_chickenpox",
    "v_diptheria__tetanus__",
    "v_hib",
    "v_hepatitus_a",
    "v_hepatitus_b",
    "v_hpv",
    "v_influenza",
    "v_mmr",
    "v_meningococal",
    "v_tdap",
    "v_td",
    "v_pcv13",
    "v_ppsv23",
    "v_rotarius",
    "v_yellow_fever",
    "v_covid_pfizer",
    "v_covid_moderna",
    "v_covid_j_j",
    "v_covid_astrozenica_oxford",
    "v_sinovac",
    "v_shingles",
):
    _fields("Vaccination records", 4, "category", [(_name, _name[2:].replace("_", " ").title())])

PERSON_COLUMNS = [
    "person_id",
    "age",
    "sex",
    "blood_group",
    "blood_donor",
    "district",
    "county",
    "subcounty",
    "parish",
    "village",
    "latitude",
    "longitude",
    "distance_km",
    "hiv_status",
    "s_hbsag",
    "s_hcv",
    "s_syphillis",
    "hgb",
    "bp_results",
    "sickle_cells_disease",
    "malaria",
]
COUNT = {"expressionType": "SQL", "sqlExpression": "COUNT(DISTINCT person_id)", "label": "people"}


def criteria_filters(criteria, available):
    """Allow only typed, known fields; values travel as structured API filters."""
    if not isinstance(criteria, dict) or len(criteria) > len(FIELDS):
        raise ToolError("Criteria must be a field/value object.", code="invalid_criteria")
    by_stage: dict[int, list[dict]] = {stage: [] for stage in range(6)}
    for field, value in criteria.items():
        if field not in FIELDS or field not in available:
            raise ToolError("Unknown discovery criterion.", code="invalid_criteria")
        definition = FIELDS[field]
        if definition["kind"] == "category":
            if (
                not isinstance(value, list)
                or not value
                or len(value) > 100
                or any(not isinstance(v, str) or len(v) > 250 for v in value)
            ):
                raise ToolError(
                    "Category criteria must contain selected values.", code="invalid_criteria"
                )
            by_stage[definition["stage"]].append({"col": field, "op": "IN", "val": value})
        else:
            if not isinstance(value, dict) or not value or set(value) - {"min", "max"}:
                raise ToolError(
                    "Numeric criteria need minimum and/or maximum.", code="invalid_criteria"
                )
            for key, number in value.items():
                if type(number) not in (int, float) or not math.isfinite(number):
                    raise ToolError("Ranges require finite numbers.", code="invalid_criteria")
                by_stage[definition["stage"]].append(
                    {"col": field, "op": ">=" if key == "min" else "<=", "val": number}
                )
            if value.get("min", -math.inf) > value.get("max", math.inf):
                raise ToolError("Minimum cannot exceed maximum.", code="invalid_criteria")
    return by_stage


def query(columns=None, filters=None, metrics=None, row_limit=10000):
    return {
        "columns": columns or [],
        "filters": filters or [],
        "metrics": metrics or [],
        "row_limit": row_limit,
    }


def run_queries(client, dataset_id, queries, form_data=None):
    if form_data is None:
        form_data = {"adhoc_filters": [{"expressionType":"SIMPLE", "subject":f["col"],
            "operator":f["op"], "comparator":f["val"], "clause":"WHERE"}
            for f in queries[0].get('filters', [])]}
    payload = client.chart_data(
        {
            "datasource": {"id": dataset_id, "type": "table"},
            "queries": queries,
            "result_format": "json",
            "result_type": "full",
            "force": False,
            "form_data": form_data or {},
        }
    )
    results = payload.get("result") or []
    if len(results) != len(queries) or any(r.get("error") for r in results):
        raise ToolError("Superset could not complete the discovery queries.", code="query_failed")
    return [r.get("data") or [] for r in results]
