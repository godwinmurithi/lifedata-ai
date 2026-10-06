"""Development tables support synthetic individual records end to end."""
from jsonschema import validate

from lifedata_ai.tools.compile_check import saved_query_context_for
from lifedata_ai.tools.superset_read import _prepare_query_context
from lifedata_ai.tools.superset_write import _to_superset_form_data
from lifedata_ai.validation.schemas.actions import CHART_PARAMS_SCHEMA


def test_raw_table_preserves_columns_and_accepts_readback():
    params = {
        "viz_type": "table",
        "query_mode": "raw",
        "all_columns": ["person_id", "latitude", "hiv_status"],
        "row_limit": 299,
    }
    validate(params, CHART_PARAMS_SCHEMA)
    form = _to_superset_form_data(params, 42)
    saved = saved_query_context_for(form, 42)
    assert saved["queries"][0]["columns"] == params["all_columns"]
    assert not saved["queries"][0].get("metrics")
    assert saved["queries"][0]["row_limit"] == 299
    prepared = _prepare_query_context(saved, {"id": 123}, None)
    assert prepared["queries"][0]["columns"] == params["all_columns"]
    assert prepared["queries"][0]["row_limit"] <= 299
    assert saved["queries"][0]["row_limit"] == 299
