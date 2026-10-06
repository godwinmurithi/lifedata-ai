"""Adversarial tests for the action validator (spec §6, spec §9).

Written as an attacker, not as a documenter: the aim of each case was to get a
malicious or malformed action *past* the gate, and the ones that succeeded are
marked `REGRESSION` so a future refactor cannot quietly reopen them.

Spec §9 requires a failing-case test for every validator rule; those are
grouped by rule below and named `test_<rule>_rejects_*`. Two groups matter
more than the rest:

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

`TestGeographyPolicy` and `TestColumnRoles` used to be here too, pinning the
semantics-registry-backed geography/person-identifier policy and the declared-
column-role allowlist. Both were removed along with the registry itself (see
validation/validator.py's module docstring) — there is no longer any
per-dataset grain, sensitivity or column-role concept anywhere in this
codebase for those tests to exercise.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lifedata_ai.validation.schemas import COLOR_SCHEMES, VIZ_ALLOWLIST
from lifedata_ai.validation.validator import (
    DEFAULT_MAX_ROW_LIMIT,
    RISK_LOW,
    RISK_PREVIEW,
    RULE_ORDER,
    ValidationContext,
    ValidationResult,
    _Check,
    _rule_no_adhoc_sql,
    validate_action,
)

#: Superset dataset ids used across this file. Arbitrary now that there is no
#: registry to declare them against — any valid-looking integer would do.
FACILITY_DATASET = 1
ENCOUNTERS_DATASET = 2


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
        assert "not evaluated" in result.reasons["viz_coherence"]


def chart_with_dataset_id(dataset_id: int) -> dict[str, Any]:
    payload = chart()
    payload["dataset_id"] = dataset_id
    return payload


# --- Rule 2: viz allowlist -------------------------------------------------


class TestNoVizAllowlist:
    """No viz-type restriction (explicit direction, 2026-10-01, to be applied
    later). Used to be `TestVizAllowlist`, pinning a `_rule_viz_allowlist` that
    no longer exists — every one of its tests asserted a *refusal* that would
    now be false. Renamed and rewritten to pin the opposite: any
    identifier-shaped viz type is accepted, while the schema's identifier
    pattern (a safety floor, not a policy) still blocks non-identifier
    garbage.
    """

    @pytest.mark.parametrize("viz_type", VIZ_ALLOWLIST)
    def test_accepts_every_previously_curated_viz(self, viz_type: str) -> None:
        """These were verified end-to-end before; confirm they still work."""
        params: dict[str, Any] = {"viz_type": viz_type, "metrics": ["count"]}
        if viz_type == "deck_polygon":
            params["groupby"] = ["district_name"]
        result = validate_action("create_chart", chart(**params), context=ctx())
        assert result.ok, result.message

    @pytest.mark.parametrize(
        "viz_type", ["deck_scatter", "deck_heatmap", "deck_hex", "mapbox"]
    )
    def test_accepts_point_map_types(self, viz_type: str) -> None:
        """Maps are explicitly no longer refused — see the class docstring."""
        result = validate_action(
            "create_chart", chart(viz_type=viz_type), context=ctx()
        )
        assert result.ok, result.message

    def test_accepts_an_uncurated_viz_type(self) -> None:
        """Any identifier-shaped string, known to this deployment or not."""
        result = validate_action(
            "create_chart", chart(viz_type="some_future_superset_plugin"),
            context=ctx(),
        )
        assert result.ok, result.message

    def test_still_rejects_a_non_identifier_viz_type(self) -> None:
        """The remaining safety floor: injection shapes, not policy."""
        result = validate_action(
            "create_chart", chart(viz_type="table'; DROP TABLE x; --"), context=ctx()
        )
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

    @pytest.mark.parametrize("mode", ["RAW", "raw_records"])
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

    def test_rule_accepts_raw_query_mode_when_reached_directly(self) -> None:
        errors, _ = _rule_no_adhoc_sql(
            check_for("create_chart", {"params": {"query_mode": "raw"}})
        )
        assert not errors

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


# --- Rule: colour scheme -----------------------------------------------


class TestColourScheme:
    """No colour-scheme allowlist any more (explicit direction, 2026-10-01, to
    be applied later) — the model may apply any identifier-shaped scheme value
    a user asks for. `_rule_color_scheme` (which checked registered-palette
    membership) is gone; the schema's injection-safety pattern on
    `color_scheme` is untouched and still real (see
    test_rejects_css_smuggled_through_the_colour_scheme below) — that was
    always a separate, structural control from the allowlist, not weakened by
    removing it.
    """

    @pytest.mark.parametrize("scheme", COLOR_SCHEMES)
    def test_accepts_a_previously_curated_palette(self, scheme: str) -> None:
        result = validate_action("create_chart", chart(color_scheme=scheme), context=ctx())
        assert result.ok, result.message

    def test_accepts_an_uncurated_palette(self) -> None:
        """Any identifier-shaped scheme name, approved by this deployment or not."""
        result = validate_action(
            "create_chart", chart(color_scheme="hot_pink_gradient"), context=ctx()
        )
        assert result.ok, result.message

    def test_rejects_css_smuggled_through_the_colour_scheme(self) -> None:
        """The schema's own identifier pattern, not an allowlist — still enforced."""
        result = validate_action(
            "create_chart", chart(color_scheme="red;background:url(x)"), context=ctx()
        )
        assert not result.ok

    def test_accepts_a_chart_with_no_palette_at_all(self) -> None:
        """Inheriting the dashboard's palette is the usual choice."""
        result = validate_action("create_chart", chart(), context=ctx())
        assert result.ok


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
            context=ValidationContext(identity_username="analyst"),
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
                "dataset_id": FACILITY_DATASET,
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
