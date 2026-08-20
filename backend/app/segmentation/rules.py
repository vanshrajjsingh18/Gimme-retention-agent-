"""Segment rule definition, validation and evaluation.

A rule definition is a nested group:

    {
      "op": "AND",
      "conditions": [
        {"field": "lifecycle_stage", "operator": "in", "value": ["AT_RISK"]},
        {"op": "OR", "conditions": [...]}
      ]
    }

Evaluation happens in Python against a flat ``CustomerView`` dict rather than
being translated to SQL. This keeps every operator available on derived fields
(churn score, RFM cell, days since last order) without a query builder, at the
cost of loading the candidate set. For the MVP's scale that is the right trade;
``FIELD_DEFINITIONS`` is the single place a SQL translator would hook in later.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.core.enums import Channel, ChurnRiskBand, LifecycleStage, NextBestAction


class RuleError(ValueError):
    """Raised when a rule definition is structurally invalid."""


# field -> (type, label, choices)
FIELD_DEFINITIONS: dict[str, dict[str, Any]] = {
    # Identity / profile
    "lifecycle_stage": {
        "type": "enum",
        "label": "Lifecycle stage",
        "choices": [s.value for s in LifecycleStage],
        "group": "Profile",
    },
    "city": {"type": "string", "label": "City", "group": "Profile"},
    "region": {"type": "string", "label": "Region", "group": "Profile"},
    "acquisition_source": {"type": "string", "label": "Acquisition source", "group": "Profile"},
    "preferred_channel": {
        "type": "enum",
        "label": "Preferred channel",
        "choices": [c.value for c in Channel],
        "group": "Profile",
    },
    "signup_date": {"type": "date", "label": "Signup date", "group": "Profile"},
    "age_verified": {"type": "boolean", "label": "Age verified", "group": "Profile"},
    # Consent / suppression
    "marketing_consent": {"type": "boolean", "label": "Marketing consent", "group": "Consent"},
    "email_consent": {"type": "boolean", "label": "Email consent", "group": "Consent"},
    "sms_consent": {"type": "boolean", "label": "SMS consent", "group": "Consent"},
    "whatsapp_consent": {"type": "boolean", "label": "WhatsApp consent", "group": "Consent"},
    "is_suppressed": {"type": "boolean", "label": "Suppressed", "group": "Consent"},
    # Behaviour
    "total_orders": {"type": "number", "label": "Total orders", "group": "Behaviour"},
    "completed_orders": {"type": "number", "label": "Completed orders", "group": "Behaviour"},
    "lifetime_revenue": {"type": "number", "label": "Lifetime revenue", "group": "Behaviour"},
    "average_order_value": {"type": "number", "label": "Average order value", "group": "Behaviour"},
    "days_since_last_order": {
        "type": "number",
        "label": "Days since last order",
        "group": "Behaviour",
    },
    "days_since_first_order": {
        "type": "number",
        "label": "Days since first order",
        "group": "Behaviour",
    },
    "orders_last_30d": {"type": "number", "label": "Orders in last 30 days", "group": "Behaviour"},
    "orders_last_90d": {"type": "number", "label": "Orders in last 90 days", "group": "Behaviour"},
    "purchase_frequency_per_month": {
        "type": "number",
        "label": "Orders per month",
        "group": "Behaviour",
    },
    "median_purchase_interval_days": {
        "type": "number",
        "label": "Median days between orders",
        "group": "Behaviour",
    },
    "discount_dependency": {
        "type": "number",
        "label": "Discount dependency (0-1)",
        "group": "Behaviour",
    },
    "estimated_ltv": {"type": "number", "label": "Estimated LTV", "group": "Behaviour"},
    "engagement_score": {"type": "number", "label": "Engagement score", "group": "Behaviour"},
    "last_order_at": {"type": "date", "label": "Last order date", "group": "Behaviour"},
    "preferred_categories": {
        "type": "list",
        "label": "Preferred categories",
        "group": "Behaviour",
    },
    "preferred_brands": {"type": "list", "label": "Preferred brands", "group": "Behaviour"},
    # Intelligence
    "churn_score": {"type": "number", "label": "Churn score (0-100)", "group": "Intelligence"},
    "churn_risk_band": {
        "type": "enum",
        "label": "Churn risk band",
        "choices": [b.value for b in ChurnRiskBand],
        "group": "Intelligence",
    },
    "rfm_segment": {"type": "string", "label": "RFM segment", "group": "Intelligence"},
    "rfm_cell": {"type": "string", "label": "RFM cell", "group": "Intelligence"},
    "recency_score": {"type": "number", "label": "RFM recency score", "group": "Intelligence"},
    "frequency_score": {"type": "number", "label": "RFM frequency score", "group": "Intelligence"},
    "monetary_score": {"type": "number", "label": "RFM monetary score", "group": "Intelligence"},
    "recommended_action": {
        "type": "enum",
        "label": "Next best action",
        "choices": [a.value for a in NextBestAction],
        "group": "Intelligence",
    },
}

NUMBER_OPERATORS = ["eq", "neq", "gt", "gte", "lt", "lte", "between", "is_null", "is_not_null"]
STRING_OPERATORS = [
    "eq",
    "neq",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "in",
    "not_in",
    "is_null",
    "is_not_null",
]
ENUM_OPERATORS = ["eq", "neq", "in", "not_in"]
BOOLEAN_OPERATORS = ["is_true", "is_false"]
DATE_OPERATORS = [
    "before",
    "after",
    "on",
    "between",
    "in_last_days",
    "not_in_last_days",
    "is_null",
    "is_not_null",
]
LIST_OPERATORS = ["contains_any", "contains_all", "not_contains", "is_empty", "is_not_empty"]

OPERATORS_BY_TYPE = {
    "number": NUMBER_OPERATORS,
    "string": STRING_OPERATORS,
    "enum": ENUM_OPERATORS,
    "boolean": BOOLEAN_OPERATORS,
    "date": DATE_OPERATORS,
    "list": LIST_OPERATORS,
}

MAX_NESTING_DEPTH = 5


def field_catalog() -> list[dict]:
    """Describe every filterable field for the frontend rule builder."""
    return [
        {
            "field": name,
            "label": meta["label"],
            "type": meta["type"],
            "group": meta.get("group", "Other"),
            "choices": meta.get("choices", []),
            "operators": OPERATORS_BY_TYPE[meta["type"]],
        }
        for name, meta in FIELD_DEFINITIONS.items()
    ]


def validate_rule(rule: Any, depth: int = 0) -> None:
    """Raise ``RuleError`` if ``rule`` is not a well-formed rule tree."""
    if depth > MAX_NESTING_DEPTH:
        raise RuleError(f"Rule nesting exceeds the maximum depth of {MAX_NESTING_DEPTH}.")
    if not isinstance(rule, dict):
        raise RuleError("Each rule node must be an object.")

    if "conditions" in rule:
        op = str(rule.get("op", "AND")).upper()
        if op not in ("AND", "OR"):
            raise RuleError(f"Unsupported group operator '{op}'. Use AND or OR.")
        conditions = rule.get("conditions")
        if not isinstance(conditions, list):
            raise RuleError("'conditions' must be a list.")
        for child in conditions:
            validate_rule(child, depth + 1)
        return

    field = rule.get("field")
    if field not in FIELD_DEFINITIONS:
        raise RuleError(f"Unknown field '{field}'.")
    ftype = FIELD_DEFINITIONS[field]["type"]
    operator = rule.get("operator")
    if operator not in OPERATORS_BY_TYPE[ftype]:
        raise RuleError(
            f"Operator '{operator}' is not valid for {ftype} field '{field}'. "
            f"Valid operators: {', '.join(OPERATORS_BY_TYPE[ftype])}."
        )
    if operator == "between":
        value = rule.get("value")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise RuleError(f"Operator 'between' on '{field}' requires a two-element value.")
    if operator in ("in", "not_in", "contains_any", "contains_all"):
        if not isinstance(rule.get("value"), (list, tuple)):
            raise RuleError(f"Operator '{operator}' on '{field}' requires a list value.")


def evaluate(rule: dict, customer: dict, *, now: datetime | None = None) -> bool:
    """Evaluate a validated rule tree against a flat customer view."""
    now = now or datetime.utcnow()
    if not rule:
        return True

    if "conditions" in rule:
        op = str(rule.get("op", "AND")).upper()
        conditions = rule.get("conditions") or []
        if not conditions:
            return True
        results = (evaluate(c, customer, now=now) for c in conditions)
        return all(results) if op == "AND" else any(results)

    return _evaluate_condition(rule, customer, now)


def _evaluate_condition(cond: dict, customer: dict, now: datetime) -> bool:
    field = cond["field"]
    operator = cond["operator"]
    expected = cond.get("value")
    actual = customer.get(field)
    ftype = FIELD_DEFINITIONS[field]["type"]

    if operator == "is_null":
        return actual is None
    if operator == "is_not_null":
        return actual is not None

    if ftype == "boolean":
        return bool(actual) if operator == "is_true" else not bool(actual)

    if ftype == "list":
        values = {str(v).lower() for v in (actual or [])}
        if operator == "is_empty":
            return not values
        if operator == "is_not_empty":
            return bool(values)
        wanted = {str(v).lower() for v in (expected or [])}
        if operator == "contains_any":
            return bool(values & wanted)
        if operator == "contains_all":
            return wanted.issubset(values)
        if operator == "not_contains":
            return not (values & wanted)
        return False

    if actual is None:
        # A missing value cannot satisfy a positive comparison. Negative
        # operators ("not equal to X") are satisfied by absence.
        return operator in ("neq", "not_in", "not_contains", "not_in_last_days")

    if ftype == "number":
        return _compare_number(operator, _to_float(actual), expected)
    if ftype == "date":
        return _compare_date(operator, actual, expected, now)
    return _compare_string(operator, actual, expected)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _compare_number(operator: str, actual: float, expected: Any) -> bool:
    if actual != actual:  # NaN
        return False
    if operator == "between":
        low, high = _to_float(expected[0]), _to_float(expected[1])
        return low <= actual <= high
    target = _to_float(expected)
    if target != target:
        return False
    return {
        "eq": actual == target,
        "neq": actual != target,
        "gt": actual > target,
        "gte": actual >= target,
        "lt": actual < target,
        "lte": actual <= target,
    }.get(operator, False)


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        text = value.strip().replace("Z", "")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def _compare_date(operator: str, actual: Any, expected: Any, now: datetime) -> bool:
    actual_dt = _coerce_datetime(actual)
    if actual_dt is None:
        return False

    if operator in ("in_last_days", "not_in_last_days"):
        try:
            days = float(expected)
        except (TypeError, ValueError):
            return False
        within = (now - actual_dt).total_seconds() / 86400.0 <= days
        return within if operator == "in_last_days" else not within

    if operator == "between":
        low = _coerce_datetime(expected[0])
        high = _coerce_datetime(expected[1])
        if low is None or high is None:
            return False
        return low <= actual_dt <= high

    target = _coerce_datetime(expected)
    if target is None:
        return False
    if operator == "before":
        return actual_dt < target
    if operator == "after":
        return actual_dt > target
    if operator == "on":
        return actual_dt.date() == target.date()
    return False


def _compare_string(operator: str, actual: Any, expected: Any) -> bool:
    text = str(actual).lower()
    if operator in ("in", "not_in"):
        options = {str(v).lower() for v in (expected or [])}
        return (text in options) if operator == "in" else (text not in options)
    target = str(expected).lower() if expected is not None else ""
    return {
        "eq": text == target,
        "neq": text != target,
        "contains": target in text,
        "not_contains": target not in text,
        "starts_with": text.startswith(target),
        "ends_with": text.endswith(target),
    }.get(operator, False)


def describe_rule(rule: dict, depth: int = 0) -> str:
    """Render a rule tree as a readable sentence for audit trails and the UI."""
    if not rule:
        return "All customers"
    if "conditions" in rule:
        op = str(rule.get("op", "AND")).upper()
        parts = [describe_rule(c, depth + 1) for c in rule.get("conditions", [])]
        if not parts:
            return "All customers"
        joined = f" {op} ".join(parts)
        return f"({joined})" if depth > 0 and len(parts) > 1 else joined

    field = rule["field"]
    label = FIELD_DEFINITIONS.get(field, {}).get("label", field)
    operator = rule["operator"]
    value = rule.get("value")
    phrases = {
        "eq": "is",
        "neq": "is not",
        "gt": "is greater than",
        "gte": "is at least",
        "lt": "is less than",
        "lte": "is at most",
        "between": "is between",
        "contains": "contains",
        "not_contains": "does not contain",
        "starts_with": "starts with",
        "ends_with": "ends with",
        "in": "is one of",
        "not_in": "is not one of",
        "is_null": "is not set",
        "is_not_null": "is set",
        "is_true": "is true",
        "is_false": "is false",
        "before": "is before",
        "after": "is after",
        "on": "is on",
        "in_last_days": "is within the last (days)",
        "not_in_last_days": "is not within the last (days)",
        "contains_any": "includes any of",
        "contains_all": "includes all of",
        "is_empty": "is empty",
        "is_not_empty": "is not empty",
    }
    phrase = phrases.get(operator, operator)
    if operator in ("is_null", "is_not_null", "is_true", "is_false", "is_empty", "is_not_empty"):
        return f"{label} {phrase}"
    if isinstance(value, (list, tuple)):
        rendered = ", ".join(str(v) for v in value)
        if operator == "between":
            rendered = " and ".join(str(v) for v in value)
        return f"{label} {phrase} {rendered}"
    return f"{label} {phrase} {value}"
