from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.segmentation.rules import (
    RuleError,
    describe_rule,
    evaluate,
    field_catalog,
    validate_rule,
)

NOW = datetime(2025, 6, 1, 12, 0, 0)

CUSTOMER = {
    "lifecycle_stage": "AT_RISK",
    "city": "Auckland",
    "marketing_consent": True,
    "sms_consent": False,
    "is_suppressed": False,
    "total_orders": 8,
    "lifetime_revenue": 740.50,
    "days_since_last_order": 62,
    "churn_score": 58.0,
    "churn_risk_band": "HIGH",
    "rfm_segment": "At Risk",
    "preferred_categories": ["Wine", "Beer"],
    "last_order_at": NOW - timedelta(days=62),
    "signup_date": NOW - timedelta(days=400),
    "estimated_ltv": None,
}


def ev(rule):
    return evaluate(rule, CUSTOMER, now=NOW)


# --- validation ------------------------------------------------------------
def test_unknown_field_rejected():
    with pytest.raises(RuleError, match="Unknown field"):
        validate_rule({"field": "not_a_field", "operator": "eq", "value": 1})


def test_operator_must_match_field_type():
    with pytest.raises(RuleError, match="not valid for number"):
        validate_rule({"field": "lifetime_revenue", "operator": "starts_with", "value": "7"})


def test_between_requires_two_values():
    with pytest.raises(RuleError, match="two-element"):
        validate_rule({"field": "churn_score", "operator": "between", "value": [10]})


def test_in_requires_list():
    with pytest.raises(RuleError, match="requires a list"):
        validate_rule({"field": "lifecycle_stage", "operator": "in", "value": "AT_RISK"})


def test_bad_group_operator_rejected():
    with pytest.raises(RuleError, match="Unsupported group operator"):
        validate_rule({"op": "XOR", "conditions": []})


def test_excessive_nesting_rejected():
    rule = {"field": "total_orders", "operator": "gt", "value": 1}
    for _ in range(8):
        rule = {"op": "AND", "conditions": [rule]}
    with pytest.raises(RuleError, match="nesting"):
        validate_rule(rule)


def test_valid_nested_rule_accepted():
    validate_rule(
        {
            "op": "AND",
            "conditions": [
                {"field": "lifecycle_stage", "operator": "in", "value": ["AT_RISK", "DORMANT"]},
                {
                    "op": "OR",
                    "conditions": [
                        {"field": "churn_score", "operator": "gte", "value": 50},
                        {"field": "lifetime_revenue", "operator": "gt", "value": 500},
                    ],
                },
            ],
        }
    )


# --- evaluation ------------------------------------------------------------
def test_empty_rule_matches_everyone():
    assert ev({}) is True
    assert ev({"op": "AND", "conditions": []}) is True


def test_number_operators():
    assert ev({"field": "lifetime_revenue", "operator": "gt", "value": 700})
    assert not ev({"field": "lifetime_revenue", "operator": "gt", "value": 800})
    assert ev({"field": "total_orders", "operator": "gte", "value": 8})
    assert ev({"field": "total_orders", "operator": "lte", "value": 8})
    assert ev({"field": "total_orders", "operator": "eq", "value": 8})
    assert ev({"field": "total_orders", "operator": "neq", "value": 9})
    assert ev({"field": "churn_score", "operator": "between", "value": [50, 60]})
    assert not ev({"field": "churn_score", "operator": "between", "value": [10, 20]})


def test_enum_operators():
    assert ev({"field": "lifecycle_stage", "operator": "eq", "value": "AT_RISK"})
    assert ev({"field": "lifecycle_stage", "operator": "in", "value": ["AT_RISK", "DORMANT"]})
    assert not ev({"field": "lifecycle_stage", "operator": "in", "value": ["VIP"]})
    assert ev({"field": "lifecycle_stage", "operator": "not_in", "value": ["VIP"]})


def test_string_operators_are_case_insensitive():
    assert ev({"field": "city", "operator": "eq", "value": "auckland"})
    assert ev({"field": "city", "operator": "contains", "value": "AUCK"})
    assert ev({"field": "city", "operator": "starts_with", "value": "Auck"})
    assert ev({"field": "city", "operator": "ends_with", "value": "land"})
    assert not ev({"field": "city", "operator": "contains", "value": "Wellington"})


def test_boolean_operators():
    assert ev({"field": "marketing_consent", "operator": "is_true"})
    assert ev({"field": "sms_consent", "operator": "is_false"})
    assert not ev({"field": "is_suppressed", "operator": "is_true"})


def test_list_operators():
    assert ev({"field": "preferred_categories", "operator": "contains_any", "value": ["Wine"]})
    assert ev({"field": "preferred_categories", "operator": "contains_all", "value": ["Wine", "Beer"]})
    assert not ev({"field": "preferred_categories", "operator": "contains_all", "value": ["Wine", "Spirits"]})
    assert ev({"field": "preferred_categories", "operator": "not_contains", "value": ["Spirits"]})
    assert ev({"field": "preferred_categories", "operator": "is_not_empty"})


def test_date_operators():
    assert ev({"field": "last_order_at", "operator": "in_last_days", "value": 90})
    assert not ev({"field": "last_order_at", "operator": "in_last_days", "value": 30})
    assert ev({"field": "last_order_at", "operator": "not_in_last_days", "value": 30})
    assert ev({"field": "signup_date", "operator": "before", "value": "2025-01-01"})
    assert ev({"field": "last_order_at", "operator": "after", "value": "2025-01-01"})


def test_date_accepts_iso_strings_from_json():
    customer = dict(CUSTOMER, last_order_at="2025-03-31T12:00:00")
    assert evaluate(
        {"field": "last_order_at", "operator": "in_last_days", "value": 90}, customer, now=NOW
    )


def test_null_handling():
    assert ev({"field": "estimated_ltv", "operator": "is_null"})
    assert not ev({"field": "estimated_ltv", "operator": "is_not_null"})
    # A positive comparison against a missing value must not match.
    assert not ev({"field": "estimated_ltv", "operator": "gt", "value": 0})
    # A negative comparison is satisfied by absence.
    assert ev({"field": "estimated_ltv", "operator": "neq", "value": 5})


def test_and_group_requires_all():
    rule = {
        "op": "AND",
        "conditions": [
            {"field": "lifecycle_stage", "operator": "eq", "value": "AT_RISK"},
            {"field": "lifetime_revenue", "operator": "gt", "value": 500},
        ],
    }
    assert ev(rule)
    rule["conditions"][1]["value"] = 5000
    assert not ev(rule)


def test_or_group_requires_any():
    rule = {
        "op": "OR",
        "conditions": [
            {"field": "lifecycle_stage", "operator": "eq", "value": "VIP"},
            {"field": "lifetime_revenue", "operator": "gt", "value": 500},
        ],
    }
    assert ev(rule)
    rule["conditions"][1]["value"] = 5000
    assert not ev(rule)


def test_nested_groups_evaluate_correctly():
    rule = {
        "op": "AND",
        "conditions": [
            {"field": "marketing_consent", "operator": "is_true"},
            {
                "op": "OR",
                "conditions": [
                    {"field": "churn_score", "operator": "gte", "value": 90},
                    {
                        "op": "AND",
                        "conditions": [
                            {"field": "lifecycle_stage", "operator": "eq", "value": "AT_RISK"},
                            {"field": "lifetime_revenue", "operator": "gt", "value": 500},
                        ],
                    },
                ],
            },
        ],
    }
    assert ev(rule)
    # Break the inner AND: the OR now has no true branch.
    rule["conditions"][1]["conditions"][1]["conditions"][1]["value"] = 100000
    assert not ev(rule)


def test_high_value_at_risk_segment_excludes_suppressed():
    rule = {
        "op": "AND",
        "conditions": [
            {"field": "lifecycle_stage", "operator": "in", "value": ["AT_RISK", "DORMANT"]},
            {"field": "lifetime_revenue", "operator": "gte", "value": 500},
            {"field": "is_suppressed", "operator": "is_false"},
        ],
    }
    assert ev(rule)
    assert not evaluate(rule, dict(CUSTOMER, is_suppressed=True), now=NOW)


# --- catalog & description -------------------------------------------------
def test_field_catalog_shape():
    catalog = field_catalog()
    assert len(catalog) > 20
    for entry in catalog:
        assert entry["field"] and entry["label"] and entry["type"]
        assert entry["operators"]


def test_describe_rule_reads_naturally():
    text = describe_rule(
        {
            "op": "AND",
            "conditions": [
                {"field": "lifecycle_stage", "operator": "in", "value": ["AT_RISK"]},
                {"field": "lifetime_revenue", "operator": "gte", "value": 500},
            ],
        }
    )
    assert "Lifecycle stage is one of AT_RISK" in text
    assert "AND" in text
    assert "Lifetime revenue is at least 500" in text


def test_describe_empty_rule():
    assert describe_rule({}) == "All customers"
