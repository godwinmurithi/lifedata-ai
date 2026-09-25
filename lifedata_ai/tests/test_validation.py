"""Adversarial tests for the action validator (spec §6, spec §9).

Written as an attacker, not as a documenter: the aim of each case was to get a
malicious or malformed action *past* the gate, and the ones that succeeded are
marked `REGRESSION` so a future refactor cannot quietly reopen them.

Spec §9 requires a failing-case test for every validator rule; those are
grouped by rule below and named `test_<rule>_rejects_*`. Three groups matter
more than the rest:

* `TestGeographyPolicy` — the crown jewel. Three bypasses were found here and
  are pinned: `x_axis`, `adhoc_filters[].column` and `add_native_filter.column`
  screened sub-district columns with a *denylist* of place words, so any word
  missing from it (`sub_county`, `sector`, `locality`, `umudugudu`) became a
  legal per-sub-district breakdown of patient data.
* `TestNoAdhocSql` — the injection corpus, applied to every payload slot a
  string can occupy, including nested array elements.
* `TestValidatorNeverRaises` — an exception escaping a security gate is worse
  than a rejection, because the caller sees no verdict at all. Two shapes did
  raise: an unhashable `action_type` (through an `lru_cache` lookup) and a
  deeply nested payload (through `copy.deepcopy`, before rule 1 could reject
  it).

Deliberate non-findings are recorded too, in `TestDocumentedBoundaries` —
values that look alarming but are accepted on purpose. They are assertions, not
prose, so that if the behaviour ever changes someone has to think about it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lifedata_ai.semantics.loader import load_registry
from lifedata_ai.validation.validator import (
    DEFAULT_MAX_ROW_LIMIT,
    RISK_LOW,
    RISK_PREVIEW,
    RULE_ORDER,
    ValidationContext,
    ValidationResult,
    _Check,
    _rule_geography,
    _rule_no_adhoc_sql,
    validate_action,
)

REGISTRY = load_registry()

#: Superset dataset ids as `registry.yaml` declares them. Hard-coded rather than
#: looked up, so that renumbering the registry without revisiting the geography
#: policy fails these tests loudly instead of silently testing the wrong dataset.
PATIENT_GEOGRAPHY_DATASET = 4  # rpt_patient_geography, geography: patient
FACILITY_DATASET = 1  # rpt_facilities, geography: facility
ENCOUNTERS_DATASET = 2  # rpt_encounters, geography: facility, has patient_id


# --- Helpers ---------------------------------------------------------------


def ctx(
    username: str = "analyst",
    owners: list[Any] | None = None,
    components: int | None = None,
) -> ValidationContext:
    """A context in which the user owns the dashboard unless told otherwise."""
    return ValidationContext(
        identity_username=username,
        dashboard_owners=["analyst"] if owners is None else owners,
        dashboard_component_count=components,
        registry=REGISTRY,
    )


def chart(dataset_id: int = ENCOUNTERS_DATASET, **params: Any) -> dict[str, Any]:
    """A minimal valid `create_chart` payload, with `params` overridden."""
    return {
        "chart_name": "Test chart",
        "dataset_id": dataset_id,
        "params": {"viz_type": "table", "metrics": ["count"], **params},
    }


def check_for(action_type: str, payload: dict[str, Any]) -> _Check:
    """Working state for calling one rule in isolation.

    Needed because several rules are unreachable end-to-end: rule 1 rejects the
    payloads that would exercise them. A rule that cannot currently fire still
    has to be correct for the day a schema is widened.
    """
    return _Check(action_type=action_type, payload=payload, context=ctx())


def test_registry_fixture_matches_the_live_registry() -> None:
    """Guard the assumptions the rest of this file is built on."""
    assert REGISTRY.dataset_by_superset_id(PATIENT_GEOGRAPHY_DATASET).is_patient_geography
    assert not REGISTRY.dataset_by_superset_id(FACILITY_DATASET).is_patient_geography
    assert not REGISTRY.dataset_by_superset_id(ENCOUNTERS_DATASET).is_patient_geography
    assert "deck_scatter" not in REGISTRY.viz_allowlist


# --- Rule 1: schema --------------------------------------------------------


class TestSchema:
    def test_accepts_a_well_formed_action(self) -> None:
        result = validate_action("create_chart", chart(), context=ctx())
        assert result.ok
        assert result.reasons["schema"].startswith("pass")

    def test_rejects_unknown_action_type(self) -> None:
        result = validate_action("delete_all_dashboards", {}, context=ctx())
        assert not result.ok
        assert "Unknown action_type" in result.message
        # The refusal has to name the alternatives, or the model cannot correct.
        assert "create_chart" in result.message

    def test_rejects_missing_required_field(self) -> None:
        payload = chart()
        del payload["chart_name"]
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok
        assert "chart_name" in result.message

    def test_rejects_unexpected_key(self) -> None:
        """`additionalProperties: false` is the structural half of the SQL ban."""
        result = validate_action(
            "rename_chart",
            {"chart_id": 1, "new_name": "n", "position_json": "{...}"},
            context=ctx(),
        )
        assert not result.ok
        assert "position_json" in result.message

    def test_rejects_unexpected_key_smuggled_into_params(self) -> None:
        payload = chart()
        payload["params"]["granularity_sqla"] = "ds"
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok

    @pytest.mark.parametrize("payload", [None, [], "create_chart", 42, True])
    def test_rejects_non_object_payload(self, payload: Any) -> None:
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok
        assert result.normalised_payload is None

    def test_rejects_invalid_layout_operation_and_lists_the_permitted_ones(self) -> None:
        result = validate_action(
            "resize_dashboard_component",
            {"operation": "shrink_a_bit", "dashboard_id": 1, "chart_id": 2},
            context=ctx(),
        )
        assert not result.ok
        assert "make_full_width" in result.message

    def test_rejects_out_of_range_identifiers(self) -> None:
        assert not validate_action("create_chart", chart_with_dataset_id(0), context=ctx()).ok
        assert not validate_action("create_chart", chart_with_dataset_id(-1), context=ctx()).ok

    def test_a_rejected_action_never_returns_a_payload_or_a_risk(self) -> None:
        result = validate_action("create_chart", {}, context=ctx())
        assert not result.ok
        assert result.risk is None
        assert result.normalised_payload is None

    def test_later_rules_are_marked_not_evaluated(self) -> None:
        result = validate_action("create_chart", {}, context=ctx())
        assert set(result.reasons) == set(RULE_ORDER)
        assert "not evaluated" in result.reasons["geography"]


def chart_with_dataset_id(dataset_id: int) -> dict[str, Any]:
    payload = chart()
    payload["dataset_id"] = dataset_id
    return payload


# --- Rule 2: viz allowlist -------------------------------------------------


class TestVizAllowlist:
    @pytest.mark.parametrize("viz_type", REGISTRY.viz_allowlist)
    def test_accepts_every_allowlisted_viz(self, viz_type: str) -> None:
        """Every approved viz must be reachable, or the allowlist is a lie."""
        params: dict[str, Any] = {"viz_type": viz_type, "metrics": ["count"]}
        if viz_type == "deck_polygon":
            params["groupby"] = ["district_name"]
        result = validate_action("create_chart", chart(**params), context=ctx())
        assert result.ok, result.message

    @pytest.mark.parametrize("viz_type", REGISTRY.patient_geography_forbidden_viz)
    def test_rejects_point_map_types_everywhere(self, viz_type: str) -> None:
        result = validate_action(
            "create_chart", chart(viz_type=viz_type), context=ctx()
        )
        assert not result.ok
        # Phase 4's acceptance criterion: the *policy* must be stated, not just
        # "unknown viz type" — the user has to learn why, not only that.
        assert "individual locations" in result.message or "plot" in result.message

    def test_rejects_an_invented_viz_type(self) -> None:
        result = validate_action(
            "create_chart", chart(viz_type="leaflet_patient_pins"), context=ctx()
        )
        assert not result.ok

    def test_rejects_a_viz_type_that_differs_only_in_case(self) -> None:
        """Superset viz types are case-sensitive; a near-miss must not slip by."""
        result = validate_action("create_chart", chart(viz_type="TABLE"), context=ctx())
        assert not result.ok


# --- Rule 3: no adhoc SQL --------------------------------------------------

INJECTIONS = [
    "'; DROP TABLE patient; --",
    "1 UNION SELECT * FROM information_schema.tables",
    "x'; DELETE FROM encounters WHERE '1'='1",
    "*/ UNION ALL SELECT patient_id FROM patients /*",
    "1); DROP TABLE lab_orders; --",
    "a' AND pg_sleep(10) --",
]


class TestNoAdhocSql:
    def test_accepts_a_chart_with_no_sql_in_it(self) -> None:
        payload = chart(
            adhoc_filters=[{"column": "facility_name", "operator": "==", "value": "Clinic A"}],
            time_range="2024-01-01 : 2024-12-31",
            y_axis_format=",.1f",
        )
        payload["chart_name"] = "Deleted records over time"
        result = validate_action("create_chart", payload, context=ctx())
        # A chart legitimately about deletions must not trip the keyword scan,
        # or the rule gets relaxed by whoever hits the false positive.
        assert result.ok, result.message

    @pytest.mark.parametrize("attack", INJECTIONS)
    def test_rejects_sql_in_a_chart_title(self, attack: str) -> None:
        payload = chart()
        payload["chart_name"] = attack[:120]
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok
        assert "chart_name" in result.message

    @pytest.mark.parametrize("attack", INJECTIONS)
    def test_rejects_sql_in_a_metric_label(self, attack: str) -> None:
        payload = chart(
            metrics=[{"aggregate": "SUM", "column": {"column_name": "n"}, "label": attack}]
        )
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok

    @pytest.mark.parametrize("attack", INJECTIONS)
    def test_rejects_sql_in_a_filter_value(self, attack: str) -> None:
        payload = chart(
            adhoc_filters=[{"column": "facility_name", "operator": "==", "value": attack}]
        )
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok

    @pytest.mark.parametrize("attack", INJECTIONS)
    def test_rejects_sql_in_a_nested_array_element(self, attack: str) -> None:
        """The scan must reach `value[2]`, not stop at the first array level."""
        payload = chart(
            adhoc_filters=[
                {"column": "facility_name", "operator": "IN", "value": ["A", "B", attack]}
            ]
        )
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok
        assert "[2]" in result.message

    @pytest.mark.parametrize("attack", INJECTIONS)
    def test_rejects_sql_in_a_dashboard_title(self, attack: str) -> None:
        result = validate_action(
            "create_dashboard", {"dashboard_title": attack[:120]}, context=ctx()
        )
        assert not result.ok

    @pytest.mark.parametrize("attack", INJECTIONS)
    def test_rejects_sql_in_a_native_filter_default_value(self, attack: str) -> None:
        result = validate_action(
            "add_native_filter",
            {
                "dashboard_id": 1,
                "filter_type": "value",
                "name": "Facility",
                "dataset_id": ENCOUNTERS_DATASET,
                "column": "facility_name",
                "default_value": [attack],
            },
            context=ctx(),
        )
        assert not result.ok

    def test_rejects_an_adhoc_metric_with_expression_type_sql(self) -> None:
        payload = chart(
            metrics=[
                {
                    "expressionType": "SQL",
                    "sqlExpression": "SELECT patient_id FROM patients",
                    "label": "leak",
                }
            ]
        )
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok

    def test_rejects_an_adhoc_filter_carrying_a_raw_sql_clause(self) -> None:
        payload = chart(
            adhoc_filters=[{"expressionType": "SQL", "sqlExpression": "1=1 OR 1=1"}]
        )
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok

    @pytest.mark.parametrize("mode", ["raw", "RAW", "raw_records"])
    def test_rejects_non_aggregate_query_mode(self, mode: str) -> None:
        payload = chart()
        payload["params"]["query_mode"] = mode
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok

    def test_rejects_a_comment_smuggled_into_a_time_range(self) -> None:
        result = validate_action(
            "create_chart", chart(time_range="Last 30 days -- x"), context=ctx()
        )
        assert not result.ok
        assert "time_range" in result.message

    @pytest.mark.parametrize(
        "key",
        ["sqlExpression", "expressionType", "custom_sql", "where", "having", "sql"],
    )
    def test_rule_rejects_sql_bearing_keys_when_reached_directly(self, key: str) -> None:
        """Unreachable through `validate_action`, which is the point.

        Rule 1's `additionalProperties: false` kills these first, so this rule is
        defence in depth for a widened schema or a caller that skipped rule 1.
        Exercised directly because it must still be correct then.
        """
        errors, verdict = _rule_no_adhoc_sql(
            check_for("create_chart", {"params": {key: "SELECT 1"}})
        )
        assert errors
        assert verdict.startswith("fail")

    def test_rule_rejects_query_mode_when_reached_directly(self) -> None:
        errors, _ = _rule_no_adhoc_sql(
            check_for("create_chart", {"params": {"query_mode": "raw"}})
        )
        assert errors

    def test_rule_accepts_aggregate_query_mode_when_reached_directly(self) -> None:
        errors, _ = _rule_no_adhoc_sql(
            check_for("create_chart", {"params": {"query_mode": "aggregate"}})
        )
        assert not errors

    def test_sql_bearing_key_detection_is_case_insensitive(self) -> None:
        errors, _ = _rule_no_adhoc_sql(
            check_for("create_chart", {"params": {"SQLEXPRESSION": "SELECT 1"}})
        )
        assert errors


# --- Rule 4: geography policy ----------------------------------------------


class TestGeographyPolicy:
    # -- what must still work --

    @pytest.mark.parametrize("column", ["district_name", "region"])
    def test_accepts_district_and_region_groupings(self, column: str) -> None:
        result = validate_action(
            "create_chart",
            chart(PATIENT_GEOGRAPHY_DATASET, groupby=[column]),
            context=ctx(),
        )
        assert result.ok, result.message

    @pytest.mark.parametrize("column", ["district", "region_name"])
    def test_district_aliases_pass_geography_but_fail_the_column_allowlist(
        self, column: str
    ) -> None:
        """`district` and `region_name` are district-level and still refused.

        The geography allowlist accepts them — they are not sub-district and not
        coordinates — but rpt_patient_geography declares `district_name` and
        `region`, and nothing else. A chart grouped by a column the view does not
        have saves happily and then renders a red Data error on the dashboard,
        which the executive discovers and the copilot never learns about.

        So the two rules are complementary, not redundant: geography decides how
        *fine* a grouping may be, and the column allowlist decides whether the
        column exists at all.
        """
        result = validate_action(
            "create_chart",
            chart(PATIENT_GEOGRAPHY_DATASET, groupby=[column]),
            context=ctx(),
        )

        assert not result.ok
        assert result.reasons["geography"].startswith("pass")
        assert result.reasons["column_roles"].startswith("fail")
        # The refusal names the real columns, so the model corrects in one turn.
        assert "district_name" in result.message

    def test_accepts_a_district_ranking(self) -> None:
        """A bar chart of districts is what this dataset can actually render.

        `deck_polygon` used to be the approved answer here and was dropped: the
        view carries district CENTROIDS, which are points, and a choropleth
        needs polygon boundaries. A bar or a table answers the question the
        executive was actually asking.
        """
        result = validate_action(
            "create_chart",
            chart(
                PATIENT_GEOGRAPHY_DATASET,
                viz_type="echarts_timeseries_bar",
                x_axis="district_name",
                metrics=["patients"],
            ),
            context=ctx(),
        )
        assert result.ok, result.message
        # The small-cell caveat must travel with the action, or the model will
        # describe suppressed districts as districts with no patients.
        assert any("suppress" in w for w in result.warnings)

    @pytest.mark.parametrize(
        "column", ["month", "encounter_month", "period_start", "__timestamp", "report_date"]
    )
    def test_the_geography_rule_permits_a_time_axis(self, column: str) -> None:
        """District counts over time are not a disclosure question.

        Asserted against the geography rule rather than end to end, because
        rpt_patient_geography is already aggregated and carries no date column at
        all — see the test below. The property under test here is that the
        *privacy* policy does not stand in the way of a time axis; whether the
        column exists is a separate question with a separate rule.
        """
        errors, verdict = _rule_geography(
            check_for(
                "create_chart",
                chart(
                    PATIENT_GEOGRAPHY_DATASET,
                    viz_type="echarts_timeseries_line",
                    x_axis=column,
                    groupby=["district_name"],
                ),
            )
        )

        assert not errors, errors
        assert verdict.startswith("pass")

    def test_a_time_axis_on_patient_geography_is_refused_for_having_no_dates(self) -> None:
        """rpt_patient_geography has no time column, so a trend over it is a lie.

        The view is `GROUP BY district` with a small-cell HAVING clause: there is
        no date in it. A chart asking for one used to save happily and render a
        red Data error on the dashboard. The column allowlist catches it before
        anything is written, and names what the dataset does have.
        """
        result = validate_action(
            "create_chart",
            chart(
                PATIENT_GEOGRAPHY_DATASET,
                viz_type="echarts_timeseries_line",
                x_axis="report_date",
                groupby=["district_name"],
            ),
            context=ctx(),
        )

        assert not result.ok
        assert result.reasons["column_roles"].startswith("fail")
        assert "district_name" in result.message

    # -- sub-district groupings --

    SUB_DISTRICT_COLUMNS = [
        "subcounty",
        "sub_county",
        "subcounty_name",
        "sub_district",
        "sector",
        "sector_name",
        "parish",
        "village",
        "village_name",
        "cell",
        "cell_name",
        "umudugudu",
        "lc1",
        "ward",
        "zone",
        "locality",
        "settlement",
        "hamlet",
        "neighbourhood",
        "suburb",
        "commune",
        "kebele",
        "barangay",
        "catchment_area",
        "street",
        "home_area",
        "residence",
        "household_id",
        "facility_name",
        "patient_id",
        "gps_lat",
        "patient_latitude",
        "longitude",
        "geom",
        "location_point",
        "home_coords",
    ]

    @pytest.mark.parametrize("column", SUB_DISTRICT_COLUMNS)
    def test_rejects_sub_district_groupby(self, column: str) -> None:
        result = validate_action(
            "create_chart",
            chart(PATIENT_GEOGRAPHY_DATASET, groupby=[column]),
            context=ctx(),
        )
        assert not result.ok, f"{column} was accepted as a groupby"

    @pytest.mark.parametrize("column", SUB_DISTRICT_COLUMNS)
    def test_rejects_sub_district_x_axis(self, column: str) -> None:
        """REGRESSION — this was the bypass.

        `x_axis` was screened with a denylist of place words while `groupby`
        used an allowlist, so every spelling the denylist had not heard of
        (`sub_county`, `sector`, `locality`, `umudugudu`, `settlement`,
        `hamlet`, `street`, `home_area`, `residence`, `lc1`, `sub_district`,
        `neighbourhood`, `catchment_area`) was accepted as a bar chart's
        category axis — a per-sub-district breakdown of patient counts under
        another name. Fixed by allowlisting the axis.
        """
        result = validate_action(
            "create_chart",
            chart(PATIENT_GEOGRAPHY_DATASET, viz_type="bar", x_axis=column),
            context=ctx(),
        )
        assert not result.ok, f"{column} was accepted as an x_axis"

    @pytest.mark.parametrize("column", SUB_DISTRICT_COLUMNS)
    def test_rejects_sub_district_adhoc_filter(self, column: str) -> None:
        """REGRESSION — same denylist, same gap, on the filter path."""
        result = validate_action(
            "create_chart",
            chart(
                PATIENT_GEOGRAPHY_DATASET,
                adhoc_filters=[{"column": column, "operator": "==", "value": "x"}],
            ),
            context=ctx(),
        )
        assert not result.ok, f"a filter on {column} was accepted"

    @pytest.mark.parametrize("column", SUB_DISTRICT_COLUMNS)
    def test_rejects_sub_district_native_filter(self, column: str) -> None:
        """REGRESSION — a dashboard filter discloses membership just as well."""
        result = validate_action(
            "add_native_filter",
            {
                "dashboard_id": 1,
                "filter_type": "value",
                "name": "Area",
                "dataset_id": PATIENT_GEOGRAPHY_DATASET,
                "column": column,
            },
            context=ctx(),
        )
        assert not result.ok, f"a native filter on {column} was accepted"

    @pytest.mark.parametrize(
        "column", ["average_latitude", "mean_lat", "total_gps_points", "count_of_villages"]
    )
    def test_the_measure_exemption_never_covers_a_coordinate(self, column: str) -> None:
        """A filter column reading as a measure is exempt from the place-word
        denylist so `patient_count` stays filterable — but an averaged
        coordinate is still a location, so coordinates override the exemption.
        """
        result = validate_action(
            "create_chart",
            chart(
                PATIENT_GEOGRAPHY_DATASET,
                adhoc_filters=[{"column": column, "operator": ">", "value": 1}],
            ),
            context=ctx(),
        )
        assert result.ok is ("gps" not in column and "lat" not in column), column

    def test_rejects_a_sub_district_column_hidden_among_valid_ones(self) -> None:
        result = validate_action(
            "create_chart",
            chart(PATIENT_GEOGRAPHY_DATASET, groupby=["district_name", "region", "village"]),
            context=ctx(),
        )
        assert not result.ok

    @pytest.mark.parametrize("column", ["VILLAGE", "Sub_County", "Patient_Latitude"])
    def test_grouping_rejection_is_case_insensitive(self, column: str) -> None:
        result = validate_action(
            "create_chart",
            chart(PATIENT_GEOGRAPHY_DATASET, groupby=[column]),
            context=ctx(),
        )
        assert not result.ok

    # -- point maps --

    @pytest.mark.parametrize("viz_type", REGISTRY.patient_geography_forbidden_viz)
    def test_rule_rejects_point_maps_on_patient_geography(self, viz_type: str) -> None:
        """Rule 4 in isolation, because rule 2 short-circuits end-to-end.

        `deck_scatter` is not on the viz allowlist at all, so `validate_action`
        never reaches rule 4 for it. Calling rule 4 directly is the only way to
        prove the *geography* half is enforced independently — which matters
        because the allowlist is a deployment setting and could widen.
        """
        errors, verdict = _rule_geography(
            check_for(
                "create_chart",
                chart(PATIENT_GEOGRAPHY_DATASET, viz_type=viz_type, groupby=["district_name"]),
            )
        )
        assert errors
        assert verdict.startswith("fail")

    @pytest.mark.parametrize("viz_type", REGISTRY.patient_geography_forbidden_viz)
    def test_rule_allows_point_maps_on_facility_geography(self, viz_type: str) -> None:
        """Facilities are institutions, not people (spec §6.4).

        Note the honest caveat: end-to-end these are still refused, because the
        POC viz allowlist contains no point type for any dataset. It is the
        geography rule alone that treats the two datasets differently.
        """
        errors, verdict = _rule_geography(
            check_for("create_chart", chart(FACILITY_DATASET, viz_type=viz_type))
        )
        assert not errors
        assert "not applicable" in verdict

    def test_point_map_refusal_states_the_policy_end_to_end(self) -> None:
        result = validate_action(
            "create_chart",
            chart(PATIENT_GEOGRAPHY_DATASET, viz_type="deck_scatter"),
            context=ctx(),
        )
        assert not result.ok
        assert "district" in result.message

    # -- failing closed --

    @pytest.mark.parametrize("column", ["village_name", "patient_latitude", "subcounty"])
    def test_rejects_a_native_filter_that_names_a_column_but_no_dataset(
        self, column: str
    ) -> None:
        """REGRESSION — omitting `dataset_id` skipped the policy entirely.

        `dataset_id` is optional in the schema because a `time_range` filter
        needs none. A `value` filter on `village_name` with the dataset left out
        therefore reached "not applicable — filter names no dataset" and was
        accepted, which is the one case rule 4's fail-closed doctrine exists for.
        """
        result = validate_action(
            "add_native_filter",
            {"dashboard_id": 1, "filter_type": "value", "name": "Area", "column": column},
            context=ctx(),
        )
        assert not result.ok
        assert "dataset_id" in result.message

    def test_accepts_a_time_range_filter_with_no_dataset(self) -> None:
        """The reason `dataset_id` is optional at all — keep it working."""
        result = validate_action(
            "add_native_filter",
            {"dashboard_id": 1, "filter_type": "time_range", "name": "Period"},
            context=ctx(),
        )
        assert result.ok, result.message

    def test_rejects_an_unregistered_dataset(self) -> None:
        result = validate_action("create_chart", chart(9999), context=ctx())
        assert not result.ok
        assert "registry" in result.message

    def test_rejects_a_chart_update_that_names_no_dataset(self) -> None:
        """Unresolvable dataset means the policy cannot be applied, so refuse."""
        result = validate_action(
            "update_chart",
            {"chart_id": 7, "params": {"viz_type": "table", "metrics": ["count"]}},
            context=ctx(),
        )
        assert not result.ok
        assert "datasource" in result.message

    def test_rejects_a_payload_naming_two_different_datasets(self) -> None:
        """Otherwise the policy is evaluated against a dataset the chart drops."""
        payload = chart(ENCOUNTERS_DATASET)
        payload["params"]["datasource"] = {"id": PATIENT_GEOGRAPHY_DATASET, "type": "table"}
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok

    def test_patient_geography_policy_applies_via_the_datasource_alone(self) -> None:
        result = validate_action(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    "viz_type": "bar",
                    "metrics": ["count"],
                    "groupby": ["village_name"],
                    "datasource": {"id": PATIENT_GEOGRAPHY_DATASET, "type": "table"},
                },
            },
            context=ctx(),
        )
        assert not result.ok

    # -- row-level grain on any dataset --

    @pytest.mark.parametrize(
        "column",
        ["patient_id", "patient_uuid", "patient_name", "client_id", "mrn", "national_id"],
    )
    def test_rejects_grouping_any_dataset_by_a_person_identifier(self, column: str) -> None:
        """FINDING — a control added beyond spec §6.4.

        `rpt_encounters` is `geography: facility` and carries `patient_id`, so
        the registry's geography flag never fired and `groupby: [patient_id]`
        produced a row-per-patient table on a shared dashboard. Spec §1's
        privacy boundary forbids row-level output; the geography rule now
        refuses person identifiers on every dataset.
        """
        result = validate_action(
            "create_chart", chart(ENCOUNTERS_DATASET, groupby=[column]), context=ctx()
        )
        assert not result.ok, f"{column} was accepted as a groupby"

    @pytest.mark.parametrize("column", ["patient_count", "unique_patients", "facility_name"])
    def test_person_identifier_check_does_not_catch_aggregates(self, column: str) -> None:
        """A count of patients is the opposite of a list of them.

        Asserted against the person-identifier check specifically. Only
        `facility_name` is a real column of rpt_encounters, so the other two are
        additionally refused by the column allowlist — which is correct and is a
        different rule. What must never happen is the *person-identifier* check
        firing on a word that merely contains "patient".
        """
        errors, _verdict = _rule_geography(
            check_for("create_chart", chart(ENCOUNTERS_DATASET, groupby=[column]))
        )

        assert not errors, errors

    def test_counting_distinct_patients_remains_available(self) -> None:
        """The governed way to count people must not be collateral damage."""
        result = validate_action(
            "create_chart",
            chart(
                ENCOUNTERS_DATASET,
                metrics=[
                    {"aggregate": "COUNT_DISTINCT", "column": {"column_name": "patient_id"}}
                ],
                groupby=["facility_name"],
            ),
            context=ctx(),
        )
        assert result.ok, result.message


# --- Rule 5: column roles --------------------------------------------------


class TestColumnRoles:
    """Only declared dimension or temporal columns may set a chart's grain.

    An allowlist, not a denylist, and the difference is the whole point: a
    column the registry has never declared — a new column on a reporting view, a
    dataset added without curation — is refused rather than waved through.
    """

    def test_a_declared_dimension_is_accepted(self) -> None:
        result = validate_action(
            "create_chart", chart(ENCOUNTERS_DATASET, groupby=["facility_level"]), context=ctx()
        )
        assert result.ok, result.message

    def test_a_temporal_column_may_be_the_axis(self) -> None:
        result = validate_action(
            "create_chart",
            chart(
                ENCOUNTERS_DATASET,
                viz_type="echarts_timeseries_line",
                x_axis="encounter_date",
                time_grain_sqla="P1M",
            ),
            context=ctx(),
        )
        assert result.ok, result.message

    def test_an_internal_key_is_refused_and_the_alternative_is_named(self) -> None:
        """The failure this rule exists for: a chart with one row per record."""
        result = validate_action(
            "create_chart", chart(ENCOUNTERS_DATASET, groupby=["facility_id"]), context=ctx()
        )

        assert not result.ok
        assert "internal key" in result.message
        # Naming the fix is what turns a dead end into a self-healing tool error.
        assert "facility_name" in result.message

    def test_a_measure_column_cannot_be_a_grouping(self) -> None:
        result = validate_action(
            "create_chart", chart(ENCOUNTERS_DATASET, groupby=["age_years"]), context=ctx()
        )

        assert not result.ok
        assert "not a category" in result.message

    def test_an_internal_helper_column_is_refused_with_its_note(self) -> None:
        """`encounter_month` is pre-truncated; the honest answer is time_grain_sqla."""
        result = validate_action(
            "create_chart", chart(ENCOUNTERS_DATASET, groupby=["encounter_month"]), context=ctx()
        )

        assert not result.ok
        assert "time_grain_sqla" in result.message

    def test_a_column_the_registry_has_never_heard_of_is_refused(self) -> None:
        """The case a denylist leaves open.

        A denylist forbidding `role: key` would wave this through, because there
        is no role to forbid — the column simply is not declared. That is the
        exact shape of "someone added a column to a reporting view and nobody
        told the registry".
        """
        result = validate_action(
            "create_chart", chart(ENCOUNTERS_DATASET, groupby=["some_new_column"]), context=ctx()
        )

        assert not result.ok
        assert "not a column of rpt_encounters" in result.message

    def test_the_x_axis_goes_through_the_same_gate_as_groupby(self) -> None:
        """On a bar chart the axis IS a grouping, so it cannot be looser."""
        result = validate_action(
            "create_chart",
            chart(ENCOUNTERS_DATASET, viz_type="echarts_timeseries_bar", x_axis="patient_id"),
            context=ctx(),
        )
        assert not result.ok

    # -- saved metrics --

    def test_a_declared_saved_metric_is_accepted(self) -> None:
        result = validate_action(
            "create_chart",
            chart(ENCOUNTERS_DATASET, metrics=["unique_patients"], groupby=["facility_name"]),
            context=ctx(),
        )
        assert result.ok, result.message

    def test_supersets_own_count_metric_is_accepted(self) -> None:
        """Superset creates `count` on every dataset without anyone declaring it."""
        result = validate_action(
            "create_chart", chart(ENCOUNTERS_DATASET, metrics=["count"]), context=ctx()
        )
        assert result.ok, result.message

    def test_an_undeclared_metric_is_refused_before_it_can_render_an_error(self) -> None:
        """An unknown saved metric saves fine and then fails at query time.

        The chart appears on the dashboard as a red Data error, which the
        executive discovers and the copilot never learns about. Refusing here
        turns it into a tool error the model corrects in the same turn.
        """
        result = validate_action(
            "create_chart", chart(ENCOUNTERS_DATASET, metrics=["made_up_metric"]), context=ctx()
        )

        assert not result.ok
        assert "not a saved metric" in result.message
        assert "unique_patients" in result.message

    def test_an_aggregation_over_a_measure_column_is_untouched(self) -> None:
        """Metric objects name a column; the column rules already govern those."""
        result = validate_action(
            "create_chart",
            chart(
                ENCOUNTERS_DATASET,
                metrics=[{"aggregate": "AVG", "column": {"column_name": "age_years"}}],
                groupby=["facility_name"],
            ),
            context=ctx(),
        )
        assert result.ok, result.message


# --- Rule 6: colour scheme -------------------------------------------------


class TestColourScheme:
    """What survived dropping `apply_dashboard_theme`.

    The theme tool wrote `json_metadata["theme"]`, but Superset 6 exposes theme
    *objects* at /api/v1/theme/ and nothing ever created the three registry ids —
    so it was almost certainly a silent no-op, and its validator rule could only
    ever police a value nothing consumed. A per-chart `color_scheme` is the part
    that actually reaches the rendered dashboard.
    """

    @pytest.mark.parametrize("scheme", REGISTRY.color_schemes)
    def test_accepts_a_registered_palette(self, scheme: str) -> None:
        result = validate_action("create_chart", chart(color_scheme=scheme), context=ctx())
        assert result.ok, result.message

    def test_rejects_an_unregistered_palette(self) -> None:
        """It would not error — Superset falls back silently, so the chart
        renders in the wrong palette and nobody is told."""
        result = validate_action(
            "create_chart", chart(color_scheme="hot_pink_gradient"), context=ctx()
        )

        assert not result.ok
        assert "Approved values" in result.message

    def test_rejects_css_smuggled_through_the_colour_scheme(self) -> None:
        result = validate_action(
            "create_chart", chart(color_scheme="red;background:url(x)"), context=ctx()
        )
        assert not result.ok

    def test_accepts_a_chart_with_no_palette_at_all(self) -> None:
        """Inheriting the dashboard's palette is the usual choice."""
        result = validate_action("create_chart", chart(), context=ctx())

        assert result.ok
        assert "not applicable" in result.reasons["color_scheme"]


def test_the_dropped_actions_have_no_schema_and_no_tool() -> None:
    """§12: `apply_dashboard_theme` and `refresh_chart` are gone, wholly.

    A dropped tool leaves live policy behind it, and a schema entry matching no
    registered tool is misdirection for the next reader. `refresh_chart` in
    particular was a persistent config change (`cache_timeout: 0`) mislabelled
    as a refresh, which no executive ever asks for.
    """
    from lifedata_ai.tools import build_registry
    from lifedata_ai.validation.schemas import ALLOWED_ACTION_TYPES

    for dropped in ("apply_dashboard_theme", "refresh_chart"):
        assert dropped not in ALLOWED_ACTION_TYPES
        assert build_registry().get(dropped) is None


@pytest.mark.parametrize("viz_type", ["deck_polygon", "histogram_v2"])
def test_the_dropped_viz_types_are_refused(viz_type: str) -> None:
    """§12: neither had been verified to render on this data.

    `deck_polygon` needs POLYGON geometry and rpt_patient_geography carries
    district CENTROIDS, which are points. Superset accepts the save either way
    and only the browser reveals the truth, so an unverified viz type is a chart
    the executive discovers is broken.

    Refused by rule 1, not rule 2 — and that distinction is deliberate.
    `KNOWN_VIZ_TYPES` is the allowlist *plus* the point-map types, so that a
    `deck_scatter` request can be refused by the geography rule *with the policy
    stated* rather than with a bland "not on the list". These two are on neither
    list, so they are simply not viz types this deployment knows, and the schema
    says so first.
    """
    result = validate_action("create_chart", chart(viz_type=viz_type), context=ctx())

    assert not result.ok
    assert result.reasons["schema"].startswith("fail")
    # The refusal still names what IS available, so the model corrects in one turn.
    assert "echarts_timeseries_bar" in result.message


def test_no_refusal_message_recommends_a_dropped_viz_type() -> None:
    """A refusal that names a tool which no longer exists is worse than none.

    The geography messages used to end "Use deck_polygon for a district
    choropleth", which would now send the model to a viz type the allowlist
    refuses — one wasted round trip and a second, more confusing refusal.
    """
    result = validate_action(
        "create_chart",
        chart(PATIENT_GEOGRAPHY_DATASET, viz_type="deck_scatter", groupby=["district_name"]),
        context=ctx(),
    )

    assert not result.ok
    assert "deck_polygon" not in result.message
    assert "table" in result.message


# --- Rule 6: risk classification -------------------------------------------


class TestRiskClassification:
    def test_a_rename_is_low_risk(self) -> None:
        result = validate_action(
            "rename_chart", {"chart_id": 1, "new_name": "Monthly encounters"}, context=ctx()
        )
        assert result.ok
        assert result.risk == RISK_LOW

    def test_a_single_chart_creation_is_low_risk(self) -> None:
        assert validate_action("create_chart", chart(), context=ctx()).risk == RISK_LOW

    def test_a_removal_is_always_preview(self) -> None:
        result = validate_action(
            "remove_chart_from_dashboard", {"dashboard_id": 1, "chart_id": 2}, context=ctx()
        )
        assert result.risk == RISK_PREVIEW

    def test_an_action_on_someone_elses_dashboard_is_preview(self) -> None:
        result = validate_action(
            "add_chart_to_dashboard",
            {"dashboard_id": 1, "chart_id": 2},
            context=ctx(owners=["another_analyst"]),
        )
        assert result.risk == RISK_PREVIEW
        assert "does not own" in result.reasons["risk"]

    def test_unknown_ownership_fails_closed_to_preview(self) -> None:
        result = validate_action(
            "add_chart_to_dashboard",
            {"dashboard_id": 1, "chart_id": 2},
            context=ValidationContext(identity_username="analyst", registry=REGISTRY),
        )
        assert result.risk == RISK_PREVIEW
        assert "unknown" in result.reasons["risk"]

    def test_an_empty_owner_list_fails_closed_to_preview(self) -> None:
        result = validate_action(
            "add_chart_to_dashboard", {"dashboard_id": 1, "chart_id": 2}, context=ctx(owners=[])
        )
        assert result.risk == RISK_PREVIEW

    def test_an_unknown_acting_identity_fails_closed_to_preview(self) -> None:
        for username in ["", "   "]:
            result = validate_action(
                "add_chart_to_dashboard",
                {"dashboard_id": 1, "chart_id": 2},
                context=ctx(username=username),
            )
            assert result.risk == RISK_PREVIEW

    def test_superset_owner_objects_are_understood(self) -> None:
        """`get_dashboard` returns owner objects, not usernames."""
        result = validate_action(
            "add_chart_to_dashboard",
            {"dashboard_id": 1, "chart_id": 2},
            context=ctx(owners=[{"username": "analyst", "first_name": "A"}]),
        )
        assert result.risk == RISK_LOW

    def test_ownership_comparison_ignores_case_and_padding(self) -> None:
        result = validate_action(
            "add_chart_to_dashboard",
            {"dashboard_id": 1, "chart_id": 2},
            context=ctx(username=" Analyst ", owners=["ANALYST"]),
        )
        assert result.risk == RISK_LOW

    def test_a_malformed_owner_list_does_not_grant_ownership(self) -> None:
        for owners in ["analyst", [None], [42], [{"id": 3}], [{}]]:
            result = validate_action(
                "add_chart_to_dashboard",
                {"dashboard_id": 1, "chart_id": 2},
                context=ctx(owners=owners),
            )
            assert result.risk == RISK_PREVIEW, owners

    @pytest.mark.parametrize(
        ("chart_ids", "expected"), [([1], RISK_LOW), ([1, 2], RISK_LOW), ([1, 2, 3], RISK_PREVIEW)]
    )
    def test_dashboard_creation_previews_above_two_charts(
        self, chart_ids: list[int], expected: str
    ) -> None:
        result = validate_action(
            "create_dashboard", {"dashboard_title": "D", "chart_ids": chart_ids}, context=ctx()
        )
        assert result.risk == expected

    def test_a_kpi_row_rebuild_is_preview(self) -> None:
        result = validate_action(
            "move_dashboard_component",
            {"operation": "kpi_row", "dashboard_id": 1, "chart_ids": [1, 2]},
            context=ctx(),
        )
        assert result.risk == RISK_PREVIEW

    def test_equalize_row_with_unknown_component_count_is_preview(self) -> None:
        result = validate_action(
            "resize_dashboard_component",
            {"operation": "equalize_row", "dashboard_id": 1, "chart_id": 2},
            context=ctx(),
        )
        assert result.risk == RISK_PREVIEW

    def test_moving_one_component_on_an_owned_dashboard_is_low(self) -> None:
        result = validate_action(
            "resize_dashboard_component",
            {"operation": "make_full_width", "dashboard_id": 1, "chart_id": 2},
            context=ctx(components=4),
        )
        assert result.risk == RISK_LOW

    def test_someone_elses_dashboard_is_always_preview(self) -> None:
        """Ownership is a second layer behind the hard refusal in the write tool.

        Nothing is ownership-exempt any more: `refresh_chart` was the sole entry
        and it has been dropped, because it permanently set `cache_timeout: 0` —
        a persistent configuration change mislabelled as a refresh.
        """
        result = validate_action(
            "add_chart_to_dashboard",
            {"dashboard_id": 1, "chart_id": 2},
            context=ctx(owners=["someone_else"]),
        )
        assert result.risk == RISK_PREVIEW
        assert "does not own" in result.reasons["risk"]


# --- Rule 7: row-limit clamping --------------------------------------------


class TestRowLimit:
    @pytest.mark.parametrize("requested", [100000, 10000, 201])
    def test_clamps_an_over_large_row_limit_and_reports_it(self, requested: int) -> None:
        payload = chart(row_limit=requested)
        result = validate_action("create_chart", payload, context=ctx())
        if not result.ok:
            # 100000 exceeds the schema maximum, so it never reaches the clamp.
            assert requested > 10000
            return
        assert result.normalised_payload["params"]["row_limit"] == DEFAULT_MAX_ROW_LIMIT
        assert any(str(DEFAULT_MAX_ROW_LIMIT) in w for w in result.warnings)
        assert "clamped" in result.reasons["row_limit"]

    def test_leaves_a_reasonable_row_limit_alone(self) -> None:
        result = validate_action("create_chart", chart(row_limit=50), context=ctx())
        assert result.normalised_payload["params"]["row_limit"] == 50
        assert not result.warnings

    def test_clamping_does_not_mutate_the_callers_payload(self) -> None:
        """The audit trail records what was proposed; the executor runs the copy."""
        payload = chart(row_limit=9000)
        validate_action("create_chart", payload, context=ctx())
        assert payload["params"]["row_limit"] == 9000

    def test_the_clamp_is_visible_in_the_message(self) -> None:
        result = validate_action("create_chart", chart(row_limit=9000), context=ctx())
        assert "clamped" in result.message


# --- Robustness: the validator must always return a verdict ----------------


def _self_referential() -> dict[str, Any]:
    payload: dict[str, Any] = {"chart_name": "x"}
    payload["params"] = payload
    return payload


def _deeply_nested(depth: int = 4000) -> dict[str, Any]:
    root: dict[str, Any] = {}
    node = root
    for _ in range(depth):
        node["params"] = {}
        node = node["params"]
    return root


def _deeply_nested_lists(depth: int = 4000) -> list[Any]:
    node: list[Any] = []
    for _ in range(depth):
        node = [node]
    return node


ODD_VALUES: list[Any] = [
    None,
    "",
    " ",
    "\x00",
    "a\x00b",
    "\n\r\t",
    "​‮",
    "Ｕｎｉｏｎ　Ｓｅｌｅｃｔ",
    "北京市朝阳区",
    "x" * 100_000,
    "%s%n%x",
    "{{7*7}}",
    "../../etc/passwd",
    "<script>alert(1)</script>",
    [],
    {},
    [[[[[]]]]],
    {"a": {"b": {"c": {}}}},
    0,
    -1,
    2**63,
    -(2**63),
    3.14,
    float("inf"),
    float("nan"),
    True,
    False,
]


class TestValidatorNeverRaises:
    """An exception is not a verdict.

    A caller that catches nothing crashes the turn; a caller that catches
    broadly may read "no exception path taken" as "nothing to refuse". Either
    way an unhandled exception in a security gate is a security failure, so
    every shape below must come back as a `ValidationResult`.
    """

    @pytest.mark.parametrize("value", ODD_VALUES, ids=lambda v: repr(v)[:24])
    def test_odd_values_in_every_payload_slot(self, value: Any) -> None:
        payloads = [
            {"chart_name": value, "dataset_id": ENCOUNTERS_DATASET, "params": {"viz_type": "table"}},
            {"chart_name": "c", "dataset_id": value, "params": {"viz_type": "table"}},
            {"chart_name": "c", "dataset_id": ENCOUNTERS_DATASET, "params": value},
            {"chart_name": "c", "dataset_id": ENCOUNTERS_DATASET, "params": {"viz_type": value}},
            {
                "chart_name": "c",
                "dataset_id": ENCOUNTERS_DATASET,
                "params": {"viz_type": "table", "groupby": value},
            },
            {
                "chart_name": "c",
                "dataset_id": ENCOUNTERS_DATASET,
                "params": {"viz_type": "table", "groupby": [value]},
            },
            {
                "chart_name": "c",
                "dataset_id": PATIENT_GEOGRAPHY_DATASET,
                "params": {"viz_type": "table", "x_axis": value, "adhoc_filters": [value]},
            },
            {
                "chart_name": "c",
                "dataset_id": ENCOUNTERS_DATASET,
                "params": {"viz_type": "table", "metrics": [value], "row_limit": value},
            },
            {"dashboard_id": value, "theme_id": value},
            {"operation": value, "dashboard_id": 1, "chart_id": value},
            {"dashboard_id": 1, "filter_type": "value", "name": "n", "column": value},
        ]
        for action_type in ["create_chart", "apply_dashboard_theme", "add_native_filter"]:
            for payload in payloads:
                result = validate_action(action_type, payload, context=ctx())
                assert isinstance(result, ValidationResult)
                assert isinstance(result.ok, bool)
                if not result.ok:
                    assert result.errors
                    assert result.normalised_payload is None

    @pytest.mark.parametrize("action_type", [None, 42, {"a": 1}, ["create_chart"], object()])
    def test_a_non_string_action_type_is_refused_not_raised(self, action_type: Any) -> None:
        """REGRESSION — an unhashable `action_type` raised TypeError.

        `_validator_for` is `lru_cache`d, so a dict or list arriving from a
        malformed tool call blew up in the cache lookup before any rule ran.
        """
        result = validate_action(action_type, chart(), context=ctx())
        assert not result.ok
        assert result.risk is None

    def test_a_deeply_nested_payload_is_refused_not_raised(self) -> None:
        """REGRESSION — `copy.deepcopy` exhausted the stack before rule 1 ran."""
        result = validate_action("create_chart", _deeply_nested(), context=ctx())
        assert not result.ok
        assert result.errors

    def test_a_deeply_nested_list_is_refused_not_raised(self) -> None:
        result = validate_action(
            "create_chart",
            {"chart_name": "c", "dataset_id": 2, "params": {"groupby": _deeply_nested_lists()}},
            context=ctx(),
        )
        assert not result.ok

    def test_a_self_referential_payload_is_refused_not_raised(self) -> None:
        result = validate_action("create_chart", _self_referential(), context=ctx())
        assert not result.ok

    def test_a_very_wide_payload_is_refused_not_raised(self) -> None:
        payload = {f"key_{i}": i for i in range(5000)}
        result = validate_action("create_chart", payload, context=ctx())
        assert not result.ok

    @pytest.mark.parametrize("action_type", sorted(["create_chart", "create_dashboard"]))
    def test_an_empty_payload_is_refused_for_every_action(self, action_type: str) -> None:
        result = validate_action(action_type, {}, context=ctx())
        assert not result.ok

    def test_a_failed_verdict_is_json_serialisable(self) -> None:
        """`ActionRecord.validation_result` stores this; it must round-trip."""
        result = validate_action("create_chart", _deeply_nested(), context=ctx())
        assert json.loads(json.dumps(result.to_payload()))["ok"] is False

    def test_a_successful_verdict_is_json_serialisable(self) -> None:
        result = validate_action("create_chart", chart(row_limit=9000), context=ctx())
        payload = json.loads(json.dumps(result.to_payload()))
        assert payload["risk"] == RISK_LOW
        assert set(payload["reasons"]) == set(RULE_ORDER)


# --- Deliberate boundaries -------------------------------------------------


class TestDocumentedBoundaries:
    """Values that look alarming and are accepted on purpose.

    Written as assertions rather than comments so that changing any of them
    forces a conversation rather than passing unnoticed.
    """

    def test_a_tautology_in_a_display_only_string_is_accepted(self) -> None:
        """Free text is scanned only for unambiguous injection.

        `y_axis_title` is rendered into a chart label and never interpolated
        into a query, and applying the query-value patterns to free text would
        refuse honest titles ("Deleted records over time"). The compensating
        control is structural: no free-text key reaches the query builder.
        """
        result = validate_action("create_chart", chart(y_axis_title="count OR 1=1"), context=ctx())
        assert result.ok

    def test_the_same_tautology_in_a_filter_value_is_rejected(self) -> None:
        result = validate_action(
            "create_chart",
            chart(adhoc_filters=[{"column": "x", "operator": "==", "value": "a' OR '1'='1"}]),
            context=ctx(),
        )
        assert not result.ok

    def test_fullwidth_unicode_lookalikes_in_a_filter_value_are_accepted(self) -> None:
        """RESIDUAL — the keyword scan is ASCII.

        Harmless today because filter values are bound as parameters and never
        concatenated: `Ｕ Ｎ Ｉ Ｏ Ｎ` is not a SQL keyword to any engine. It is
        recorded because it would matter if a value ever reached string
        interpolation, which is exactly the change that must not be made.
        """
        result = validate_action(
            "create_chart",
            chart(
                adhoc_filters=[
                    {"column": "x", "operator": "==", "value": "ＵＮＩＯＮ ＳＥＬＥＣＴ"}
                ]
            ),
            context=ctx(),
        )
        assert result.ok

    def test_a_filter_on_a_measure_column_remains_available(self) -> None:
        """The axis allowlist is strict; the filter check deliberately is not.

        A filter narrows the population without setting the output grain, and
        the view itself suppresses small cells (spec §1), so "districts with at
        least fifty patients" stays possible. The price is that the filter path
        still fails open on a place word nobody has added to the denylist.
        """
        result = validate_action(
            "create_chart",
            chart(
                PATIENT_GEOGRAPHY_DATASET,
                groupby=["district_name"],
                adhoc_filters=[{"column": "patient_count", "operator": ">=", "value": 50}],
            ),
            context=ctx(),
        )
        assert result.ok, result.message

    def test_a_metric_label_is_treated_as_free_text(self) -> None:
        """RESIDUAL — the one free-text key that reaches SQL, as an alias.

        Superset renders an adhoc metric label as a quoted column alias, so a
        stray quote is escaped by SQLAlchemy rather than breaking out. Closing
        it properly belongs in the schema (a `pattern` on the label), not in a
        keyword scan that would refuse "Deleted records".
        """
        result = validate_action(
            "create_chart",
            chart(metrics=[{"aggregate": "SUM", "column": {"column_name": "n"}, "label": 'a"b'}]),
            context=ctx(),
        )
        assert result.ok

    def test_a_native_filter_driving_many_charts_is_still_low_risk(self) -> None:
        """RESIDUAL — component counting treats a filter as one component.

        It adds one object to the dashboard, so `> 2 components` never fires,
        even with fifty targets. Ownership is still checked, which is the
        control that matters for someone else's dashboard.
        """
        result = validate_action(
            "add_native_filter",
            {
                "dashboard_id": 1,
                "filter_type": "value",
                "name": "Facility",
                "dataset_id": ENCOUNTERS_DATASET,
                "column": "facility_name",
                "target_chart_ids": list(range(1, 51)),
            },
            context=ctx(),
        )
        assert result.risk == RISK_LOW

    def test_update_chart_trusts_the_datasource_it_is_given(self) -> None:
        """RESIDUAL — requires a compensating control in `superset_write.py`.

        The validator has no network access, so on `update_chart` it can only
        apply the geography policy to the dataset the payload *claims*. A
        payload naming dataset 1 while `chart_id` actually belongs to dataset 4
        is validated against the wrong policy. `superset_write.py` must call
        `get_chart` and refuse a `params.datasource` that disagrees with the
        chart's real dataset before executing.
        """
        result = validate_action(
            "update_chart",
            {
                "chart_id": 7,
                "params": {
                    # `bar` is not a registered plugin in Superset 6.1.0 and was
                    # removed from the allowlist; the categorical bar chart is
                    # echarts_timeseries_bar.
                    "viz_type": "echarts_timeseries_bar",
                    "metrics": ["count"],
                    "groupby": ["facility_level"],
                    "datasource": {"id": FACILITY_DATASET, "type": "table"},
                },
            },
            context=ctx(),
        )
        assert result.ok
