"""Audiences defined by what customers bought: "ordered beer at least twice".

The segment rule engine works on one flat view per customer, which can say
"prefers beer" but not "has placed two orders containing beer". That second
question needs the order lines, so it lives here as a deterministic query over
``orders`` and ``order_items``.

A cohort is stored as a MANUAL segment whose ``rule_definition`` carries the
query under ``cohort_query``. Everything that already understands segments —
campaigns, automations, exports, the segment pages — works with it unchanged,
and :func:`app.services.segments.refresh_segment_membership` re-runs the query
so a standing campaign's audience tracks new orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import LifecycleStage, OrderStatus, SegmentStatus, SegmentType
from app.models.base import utcnow
from app.models.entities import (
    Customer,
    CustomerMetrics,
    CustomerSegment,
    Order,
    OrderItem,
    Segment,
)

#: Orders that count as "they bought it". A cancelled or refunded order is
#: not a purchase; a pending one is, for recency — somebody with an order in
#: flight has not lapsed.
HISTORY_STATUSES = (OrderStatus.COMPLETED.value,)
RECENCY_STATUSES = (OrderStatus.COMPLETED.value, OrderStatus.PENDING.value)

MAX_COHORT_SIZE = 50_000


class CohortError(ValueError):
    """The criteria cannot be evaluated as given."""


@dataclass
class CohortCriteria:
    categories: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    #: Case-insensitive substrings of the product name.
    products: list[str] = field(default_factory=list)
    #: Orders containing a matching line. With no product filter, every order.
    min_matching_orders: int = 1
    #: Only count matching orders placed within this many days.
    matching_within_days: int | None = None
    min_days_since_last_order: int | None = None
    max_days_since_last_order: int | None = None
    min_lifetime_revenue: float | None = None
    min_average_order_value: float | None = None
    lifecycle_stages: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "CohortCriteria":
        if not isinstance(raw, dict):
            raise CohortError("Cohort criteria must be an object.")
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise CohortError(f"Unknown cohort criteria: {', '.join(unknown)}.")
        criteria = cls(**{k: v for k, v in raw.items() if v is not None})
        criteria.validate()
        return criteria

    def validate(self) -> None:
        for name in ("categories", "brands", "products", "lifecycle_stages"):
            value = getattr(self, name)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise CohortError(f"'{name}' must be a list of strings.")
        if int(self.min_matching_orders) < 1:
            raise CohortError("'min_matching_orders' must be at least 1.")
        for name in (
            "matching_within_days",
            "min_days_since_last_order",
            "max_days_since_last_order",
        ):
            value = getattr(self, name)
            if value is not None and int(value) < 0:
                raise CohortError(f"'{name}' cannot be negative.")
        valid_stages = {s.value for s in LifecycleStage}
        bad = [s for s in self.lifecycle_stages if s.upper() not in valid_stages]
        if bad:
            raise CohortError(
                f"Unknown lifecycle stage(s): {', '.join(bad)}. "
                f"Valid: {', '.join(sorted(valid_stages))}."
            )

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}

    def describe(self) -> str:
        parts: list[str] = []
        what = [*self.categories, *self.brands, *self.products]
        times = "once" if self.min_matching_orders == 1 else (
            "twice" if self.min_matching_orders == 2 else f"{self.min_matching_orders} times"
        )
        if what:
            parts.append(f"ordered {' or '.join(what)} at least {times}")
        elif self.min_matching_orders > 1:
            parts.append(f"placed at least {self.min_matching_orders} orders")
        if self.matching_within_days:
            parts.append(f"in the last {self.matching_within_days} days")
        if self.min_days_since_last_order is not None:
            parts.append(f"no order in the last {self.min_days_since_last_order} days")
        if self.max_days_since_last_order is not None:
            parts.append(f"ordered within the last {self.max_days_since_last_order} days")
        if self.min_lifetime_revenue is not None:
            parts.append(f"lifetime spend at least ${self.min_lifetime_revenue:,.0f}")
        if self.min_average_order_value is not None:
            parts.append(f"average order at least ${self.min_average_order_value:,.0f}")
        if self.lifecycle_stages:
            parts.append(f"lifecycle {'/'.join(s.upper() for s in self.lifecycle_stages)}")
        return "Customers who " + ", ".join(parts) if parts else "All customers with orders"


def find_customers(
    db: Session, criteria: CohortCriteria, *, now: datetime | None = None
) -> list[dict]:
    """Customers matching the criteria, most valuable first."""
    now = now or utcnow()

    line_filters = []
    if criteria.categories:
        line_filters.append(
            func.lower(OrderItem.category).in_([c.lower() for c in criteria.categories])
        )
    if criteria.brands:
        line_filters.append(func.lower(OrderItem.brand).in_([b.lower() for b in criteria.brands]))
    for product in criteria.products:
        line_filters.append(func.lower(OrderItem.product_name).contains(product.lower()))

    matching = (
        select(
            Order.customer_id.label("customer_id"),
            func.count(func.distinct(Order.id)).label("matching_orders"),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(Order.status.in_(HISTORY_STATUSES))
    )
    if line_filters:
        matching = matching.where(or_(*line_filters))
    if criteria.matching_within_days:
        matching = matching.where(
            Order.ordered_at >= now - timedelta(days=int(criteria.matching_within_days))
        )
    matching = (
        matching.group_by(Order.customer_id)
        .having(func.count(func.distinct(Order.id)) >= int(criteria.min_matching_orders))
        .subquery()
    )

    recency = (
        select(Order.customer_id.label("customer_id"), func.max(Order.ordered_at).label("last_at"))
        .where(Order.status.in_(RECENCY_STATUSES))
        .group_by(Order.customer_id)
        .subquery()
    )

    query = (
        select(
            Customer,
            matching.c.matching_orders,
            recency.c.last_at,
            CustomerMetrics.lifetime_revenue,
            CustomerMetrics.average_order_value,
        )
        .join(matching, matching.c.customer_id == Customer.id)
        .join(recency, recency.c.customer_id == Customer.id)
        .outerjoin(CustomerMetrics, CustomerMetrics.customer_id == Customer.id)
    )
    conditions = []
    if criteria.min_days_since_last_order is not None:
        conditions.append(
            recency.c.last_at <= now - timedelta(days=int(criteria.min_days_since_last_order))
        )
    if criteria.max_days_since_last_order is not None:
        conditions.append(
            recency.c.last_at >= now - timedelta(days=int(criteria.max_days_since_last_order))
        )
    if criteria.min_lifetime_revenue is not None:
        conditions.append(CustomerMetrics.lifetime_revenue >= float(criteria.min_lifetime_revenue))
    if criteria.min_average_order_value is not None:
        conditions.append(
            CustomerMetrics.average_order_value >= float(criteria.min_average_order_value)
        )
    if criteria.lifecycle_stages:
        conditions.append(
            Customer.lifecycle_stage.in_([s.upper() for s in criteria.lifecycle_stages])
        )
    if conditions:
        query = query.where(and_(*conditions))
    query = query.order_by(
        func.coalesce(CustomerMetrics.lifetime_revenue, 0).desc(), Customer.id
    ).limit(MAX_COHORT_SIZE)

    rows = []
    for customer, matching_orders, last_at, revenue, aov in db.execute(query).all():
        rows.append(
            {
                "id": customer.id,
                "name": customer.full_name,
                "matching_orders": int(matching_orders),
                "last_order_at": last_at.isoformat() if last_at else None,
                "days_since_last_order": (now - last_at).days if last_at else None,
                "lifetime_revenue": round(float(revenue or 0.0), 2),
                "average_order_value": round(float(aov or 0.0), 2),
                "lifecycle_stage": customer.lifecycle_stage,
                "marketing_consent": customer.marketing_consent,
            }
        )
    return rows


def is_cohort_segment(segment: Segment) -> bool:
    return segment.segment_type == SegmentType.MANUAL.value and bool(
        (segment.rule_definition or {}).get("cohort_query")
    )


def create_cohort_segment(
    db: Session,
    *,
    name: str,
    criteria: CohortCriteria,
    description: str = "",
    now: datetime | None = None,
    commit: bool = True,
) -> Segment:
    if db.execute(select(Segment.id).where(Segment.name == name)).first():
        raise CohortError(f"A segment named '{name}' already exists.")
    segment = Segment(
        name=name,
        description=description or criteria.describe(),
        segment_type=SegmentType.MANUAL.value,
        rule_definition={"cohort_query": criteria.as_dict()},
        status=SegmentStatus.ACTIVE.value,
        is_system=False,
    )
    db.add(segment)
    db.flush()
    refresh_cohort_members(db, segment, now=now)
    if commit:
        db.commit()
    return segment


def refresh_cohort_members(db: Session, segment: Segment, *, now: datetime | None = None) -> int:
    """Re-run a cohort's query and replace its membership. Returns the count."""
    criteria = CohortCriteria.from_dict(segment.rule_definition.get("cohort_query") or {})
    wanted = {row["id"] for row in find_customers(db, criteria, now=now)}
    existing = set(
        db.execute(
            select(CustomerSegment.customer_id).where(CustomerSegment.segment_id == segment.id)
        ).scalars()
    )
    if existing - wanted:
        db.execute(
            delete(CustomerSegment).where(
                CustomerSegment.segment_id == segment.id,
                CustomerSegment.customer_id.in_(existing - wanted),
            )
        )
    for customer_id in wanted - existing:
        db.add(CustomerSegment(segment_id=segment.id, customer_id=customer_id, source="cohort"))
    segment.member_count = len(wanted)
    segment.last_evaluated_at = now or utcnow()
    db.flush()
    return segment.member_count
