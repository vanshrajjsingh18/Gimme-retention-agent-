"""Step-based journey execution.

A journey is an ordered list of nodes. Each customer that enters holds a
position and an optional ``resume_at`` timestamp. The runner advances every
active customer as far as it can, stopping at a delay that has not elapsed or
at a condition that fails.

Deliberately step-based rather than a branching graph: reliable execution and a
readable audit trail matter more for the MVP than visual sophistication.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    Channel,
    EventType,
    JourneyExecutionStatus,
    JourneyNodeType,
    JourneyStatus,
    LifecycleStage,
    MessageStatus,
    OrderStatus,
)
from app.models.base import utcnow
from app.models.entities import (
    CommunicationEvent,
    Customer,
    CustomerSegment,
    Journey,
    JourneyCustomerState,
    JourneyExecution,
    JourneyNode,
    Message,
    Order,
    Segment,
    SystemLog,
)

logger = logging.getLogger(__name__)

TRIGGERS = [
    "CUSTOMER_CREATED",
    "ORDER_COMPLETED",
    "FIRST_ORDER_COMPLETED",
    "SECOND_ORDER_COMPLETED",
    "CUSTOMER_ENTERS_SEGMENT",
    "CUSTOMER_BECOMES_AT_RISK",
    "CUSTOMER_BECOMES_DORMANT",
    "CUSTOMER_CHURNS",
    "CUSTOMER_REACTIVATES",
]

DELAYS = ["WAIT_HOURS", "WAIT_DAYS", "WAIT_UNTIL_DATE", "WAIT_UNTIL_PURCHASE_WINDOW"]

CONDITIONS = [
    "HAS_ORDERED",
    "HAS_NOT_ORDERED",
    "IN_SEGMENT",
    "HAS_CONSENT",
    "OPENED_EMAIL",
    "CLICKED_MESSAGE",
    "REPLIED",
]

ACTIONS = [
    "SEND_EMAIL",
    "SEND_SMS",
    "SEND_WHATSAPP",
    "GENERATE_PERSONALIZED_MESSAGE",
    "ADD_TO_SEGMENT",
    "REMOVE_FROM_SEGMENT",
    "CREATE_INTERNAL_ALERT",
    "END_JOURNEY",
]

NODE_CATALOG = {
    "triggers": TRIGGERS,
    "delays": DELAYS,
    "conditions": CONDITIONS,
    "actions": ACTIONS,
}

STAGE_BY_TRIGGER = {
    "CUSTOMER_BECOMES_AT_RISK": LifecycleStage.AT_RISK.value,
    "CUSTOMER_BECOMES_DORMANT": LifecycleStage.DORMANT.value,
    "CUSTOMER_CHURNS": LifecycleStage.CHURNED.value,
    "CUSTOMER_REACTIVATES": LifecycleStage.REACTIVATED.value,
}

CHANNEL_BY_ACTION = {
    "SEND_EMAIL": Channel.EMAIL,
    "SEND_SMS": Channel.SMS,
    "SEND_WHATSAPP": Channel.WHATSAPP,
}


class JourneyError(ValueError):
    """Raised when a journey definition is invalid."""


def validate_journey(trigger_type: str, nodes: list[dict]) -> None:
    if trigger_type not in TRIGGERS:
        raise JourneyError(
            f"Unknown trigger '{trigger_type}'. Expected one of: {', '.join(TRIGGERS)}."
        )
    valid = {
        JourneyNodeType.DELAY.value: DELAYS,
        JourneyNodeType.CONDITION.value: CONDITIONS,
        JourneyNodeType.ACTION.value: ACTIONS,
        JourneyNodeType.TRIGGER.value: TRIGGERS,
    }
    for index, node in enumerate(nodes):
        node_type = node.get("node_type")
        if node_type not in valid:
            raise JourneyError(f"Node {index + 1} has an unknown type '{node_type}'.")
        if node.get("subtype") not in valid[node_type]:
            raise JourneyError(
                f"Node {index + 1}: '{node.get('subtype')}' is not a valid {node_type} step. "
                f"Expected one of: {', '.join(valid[node_type])}."
            )
    if not any(n.get("node_type") == JourneyNodeType.ACTION.value for n in nodes):
        raise JourneyError("A journey needs at least one action step to do anything.")


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------
def find_eligible_customers(db: Session, journey: Journey) -> list[Customer]:
    """Customers currently matching the journey's trigger condition."""
    trigger = journey.trigger_type
    config = journey.trigger_config or {}

    if trigger in STAGE_BY_TRIGGER:
        stmt = select(Customer).where(
            Customer.lifecycle_stage == STAGE_BY_TRIGGER[trigger]
        )
    elif trigger == "CUSTOMER_CREATED":
        days = int(config.get("within_days", 7))
        stmt = select(Customer).where(Customer.signup_date >= utcnow() - timedelta(days=days))
    elif trigger == "CUSTOMER_ENTERS_SEGMENT":
        segment_id = config.get("segment_id")
        if not segment_id:
            return []
        stmt = (
            select(Customer)
            .join(CustomerSegment, CustomerSegment.customer_id == Customer.id)
            .where(CustomerSegment.segment_id == segment_id)
        )
    elif trigger in ("ORDER_COMPLETED", "FIRST_ORDER_COMPLETED", "SECOND_ORDER_COMPLETED"):
        days = int(config.get("within_days", 7))
        since = utcnow() - timedelta(days=days)
        customer_ids = (
            db.execute(
                select(Order.customer_id)
                .where(Order.status == OrderStatus.COMPLETED.value, Order.ordered_at >= since)
                .distinct()
            )
            .scalars()
            .all()
        )
        if not customer_ids:
            return []
        candidates = (
            db.execute(select(Customer).where(Customer.id.in_(customer_ids))).scalars().all()
        )
        if trigger == "ORDER_COMPLETED":
            return candidates
        wanted = 1 if trigger == "FIRST_ORDER_COMPLETED" else 2
        return [c for c in candidates if _completed_order_count(db, c.id) == wanted]
    else:
        return []

    return db.execute(stmt).scalars().all()


def _completed_order_count(db: Session, customer_id: int) -> int:
    from sqlalchemy import func

    return db.execute(
        select(func.count(Order.id)).where(
            Order.customer_id == customer_id,
            Order.status == OrderStatus.COMPLETED.value,
        )
    ).scalar_one()


def enrol_customers(db: Session, journey: Journey, *, limit: int | None = None) -> int:
    """Enrol newly-eligible customers into an active journey."""
    if journey.status != JourneyStatus.ACTIVE.value:
        return 0

    candidates = find_eligible_customers(db, journey)
    existing = set(
        db.execute(
            select(JourneyCustomerState.customer_id).where(
                JourneyCustomerState.journey_id == journey.id
            )
        )
        .scalars()
        .all()
    )

    enrolled = 0
    for customer in candidates:
        if customer.id in existing and not journey.allow_reentry:
            continue
        if limit is not None and enrolled >= limit:
            break
        state = db.execute(
            select(JourneyCustomerState).where(
                JourneyCustomerState.journey_id == journey.id,
                JourneyCustomerState.customer_id == customer.id,
            )
        ).scalar_one_or_none()
        if state is None:
            state = JourneyCustomerState(
                journey_id=journey.id, customer_id=customer.id
            )
            db.add(state)
        state.status = JourneyExecutionStatus.ACTIVE.value
        state.current_position = 0
        state.resume_at = None
        state.entered_at = utcnow()
        state.completed_at = None
        enrolled += 1

    journey.total_entered = (journey.total_entered or 0) + enrolled
    db.commit()
    return enrolled


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def run_journey(
    db: Session, journey: Journey, *, now: datetime | None = None, max_customers: int = 500
) -> dict:
    """Advance every active customer through the journey as far as possible."""
    now = now or utcnow()
    if journey.status != JourneyStatus.ACTIVE.value:
        return {"advanced": 0, "completed": 0, "waiting": 0, "exited": 0, "actions": 0}

    nodes = sorted(journey.nodes, key=lambda n: n.position)
    states = (
        db.execute(
            select(JourneyCustomerState)
            .where(
                JourneyCustomerState.journey_id == journey.id,
                JourneyCustomerState.status == JourneyExecutionStatus.ACTIVE.value,
            )
            .limit(max_customers)
        )
        .scalars()
        .all()
    )

    stats = {"advanced": 0, "completed": 0, "waiting": 0, "exited": 0, "actions": 0}

    for state in states:
        if state.resume_at is not None and state.resume_at > now:
            stats["waiting"] += 1
            continue

        customer = db.get(Customer, state.customer_id)
        if customer is None:
            state.status = JourneyExecutionStatus.FAILED.value
            continue

        state.resume_at = None
        advanced = False

        while state.current_position < len(nodes):
            node = nodes[state.current_position]

            if node.node_type == JourneyNodeType.TRIGGER.value:
                state.current_position += 1
                continue

            if node.node_type == JourneyNodeType.DELAY.value:
                resume_at = _delay_until(node, customer, state, now, db)
                if resume_at > now:
                    state.resume_at = resume_at
                    _record(
                        db, journey, state, node, "WAIT",
                        f"Waiting until {resume_at:%Y-%m-%d %H:%M}.", now
                    )
                    stats["waiting"] += 1
                    break
                state.current_position += 1
                advanced = True
                continue

            if node.node_type == JourneyNodeType.CONDITION.value:
                passed = _evaluate_condition(db, node, customer, now)
                _record(
                    db, journey, state, node,
                    "CONDITION_PASSED" if passed else "CONDITION_FAILED",
                    f"{node.subtype} evaluated to {passed}.", now,
                )
                if not passed:
                    state.status = JourneyExecutionStatus.EXITED.value
                    state.completed_at = now
                    stats["exited"] += 1
                    break
                state.current_position += 1
                advanced = True
                continue

            # ACTION
            outcome, detail, ended = _execute_action(db, journey, node, customer, now)
            _record(db, journey, state, node, outcome, detail, now)
            stats["actions"] += 1
            state.current_position += 1
            advanced = True
            if ended:
                state.status = JourneyExecutionStatus.COMPLETED.value
                state.completed_at = now
                stats["completed"] += 1
                break
        else:
            # Ran off the end of the node list.
            state.status = JourneyExecutionStatus.COMPLETED.value
            state.completed_at = now
            stats["completed"] += 1

        if advanced:
            stats["advanced"] += 1

    from sqlalchemy import func

    journey.total_completed = db.execute(
        select(func.count(JourneyCustomerState.id)).where(
            JourneyCustomerState.journey_id == journey.id,
            JourneyCustomerState.status == JourneyExecutionStatus.COMPLETED.value,
        )
    ).scalar_one()
    db.commit()
    return stats


def _record(
    db: Session,
    journey: Journey,
    state: JourneyCustomerState,
    node: JourneyNode,
    outcome: str,
    detail: str,
    now: datetime,
) -> None:
    db.add(
        JourneyExecution(
            journey_id=journey.id,
            customer_id=state.customer_id,
            node_id=node.id,
            action=node.subtype,
            outcome=outcome,
            detail=detail,
            executed_at=now,
        )
    )


def _delay_until(
    node: JourneyNode,
    customer: Customer,
    state: JourneyCustomerState,
    now: datetime,
    db: Session,
) -> datetime:
    config = node.config or {}
    base = state.entered_at or now

    if node.subtype == "WAIT_HOURS":
        return base + timedelta(hours=float(config.get("hours", 24)))
    if node.subtype == "WAIT_DAYS":
        return base + timedelta(days=float(config.get("days", 1)))
    if node.subtype == "WAIT_UNTIL_DATE":
        raw = config.get("date")
        try:
            return datetime.fromisoformat(str(raw).replace("Z", ""))
        except (TypeError, ValueError):
            return now
    if node.subtype == "WAIT_UNTIL_PURCHASE_WINDOW":
        from app.models.entities import CustomerMetrics

        metrics = db.execute(
            select(CustomerMetrics).where(CustomerMetrics.customer_id == customer.id)
        ).scalar_one_or_none()
        if metrics is None or metrics.last_order_at is None:
            return now
        cycle = (
            metrics.median_purchase_interval_days
            or metrics.average_purchase_interval_days
            or 45.0
        )
        # Land just before the customer's usual reorder point.
        return metrics.last_order_at + timedelta(days=float(cycle) * 0.85)
    return now


def _evaluate_condition(
    db: Session, node: JourneyNode, customer: Customer, now: datetime
) -> bool:
    config = node.config or {}
    days = int(config.get("within_days", 30))
    since = now - timedelta(days=days)

    if node.subtype in ("HAS_ORDERED", "HAS_NOT_ORDERED"):
        from sqlalchemy import func

        count = db.execute(
            select(func.count(Order.id)).where(
                Order.customer_id == customer.id,
                Order.status == OrderStatus.COMPLETED.value,
                Order.ordered_at >= since,
            )
        ).scalar_one()
        return count > 0 if node.subtype == "HAS_ORDERED" else count == 0

    if node.subtype == "IN_SEGMENT":
        segment_id = config.get("segment_id")
        if not segment_id:
            return False
        return (
            db.execute(
                select(CustomerSegment.id).where(
                    CustomerSegment.customer_id == customer.id,
                    CustomerSegment.segment_id == segment_id,
                )
            ).first()
            is not None
        )

    if node.subtype == "HAS_CONSENT":
        channel = str(config.get("channel", "EMAIL")).upper()
        if not customer.marketing_consent or customer.is_suppressed:
            return False
        return {
            "EMAIL": customer.email_consent,
            "SMS": customer.sms_consent,
            "WHATSAPP": customer.whatsapp_consent,
        }.get(channel, customer.marketing_consent)

    event_by_condition = {
        "OPENED_EMAIL": EventType.EMAIL_OPENED.value,
        "CLICKED_MESSAGE": EventType.EMAIL_CLICKED.value,
        "REPLIED": EventType.WHATSAPP_REPLIED.value,
    }
    event_type = event_by_condition.get(node.subtype)
    if event_type:
        return (
            db.execute(
                select(CommunicationEvent.id).where(
                    CommunicationEvent.customer_id == customer.id,
                    CommunicationEvent.event_type == event_type,
                    CommunicationEvent.occurred_at >= since,
                )
            ).first()
            is not None
        )
    return False


def _execute_action(
    db: Session, journey: Journey, node: JourneyNode, customer: Customer, now: datetime
) -> tuple[str, str, bool]:
    """Run one action node. Returns ``(outcome, detail, ends_journey)``."""
    from app.compliance.engine import check_recipient
    from app.integrations.mock_adapters import BaseMockAdapter
    from app.integrations.registry import get_adapter
    from app.services.brand import build_compliance_config
    from app.services.events import make_idempotency_key, record_communication_event
    from app.services.messaging import generate_message

    config = node.config or {}

    if node.subtype == "END_JOURNEY":
        return "OK", "Journey ended.", True

    if node.subtype == "CREATE_INTERNAL_ALERT":
        db.add(
            SystemLog(
                level="INFO",
                source=f"journey:{journey.name}",
                message=config.get("message")
                or f"Journey alert for {customer.full_name} ({customer.external_id}).",
                context={"customer_id": customer.id, "journey_id": journey.id},
            )
        )
        return "OK", "Internal alert created.", False

    if node.subtype in ("ADD_TO_SEGMENT", "REMOVE_FROM_SEGMENT"):
        segment_id = config.get("segment_id")
        if not segment_id or db.get(Segment, segment_id) is None:
            return "SKIPPED", "No valid segment configured on this step.", False
        existing = db.execute(
            select(CustomerSegment).where(
                CustomerSegment.customer_id == customer.id,
                CustomerSegment.segment_id == segment_id,
            )
        ).scalar_one_or_none()
        if node.subtype == "ADD_TO_SEGMENT":
            if existing is None:
                db.add(
                    CustomerSegment(
                        customer_id=customer.id, segment_id=segment_id, source="journey"
                    )
                )
            return "OK", f"Added to segment {segment_id}.", False
        if existing is not None:
            db.delete(existing)
        return "OK", f"Removed from segment {segment_id}.", False

    # Messaging actions
    channel = CHANNEL_BY_ACTION.get(node.subtype)
    if channel is None and node.subtype == "GENERATE_PERSONALIZED_MESSAGE":
        channel = Channel(str(config.get("channel", Channel.EMAIL.value)).upper())
    if channel is None:
        return "SKIPPED", f"Action '{node.subtype}' is not implemented.", False

    compliance_config = build_compliance_config(db)
    from app.campaigns.service import build_recipient_view

    view = build_recipient_view(db, customer, now=now)
    status, reason = check_recipient(view, channel, compliance_config, send_time=now)
    if status.value != "ELIGIBLE":
        return "BLOCKED", f"Not eligible for {channel.value}: {reason}", False

    message = generate_message(
        db,
        customer,
        channel=channel,
        objective=config.get("objective", journey.trigger_type),
        campaign_name=journey.name,
        config=compliance_config,
        persist=True,
    )
    if message.status == MessageStatus.VALIDATION_FAILED.value:
        return "FAILED", "Generated message failed content validation and was not sent.", False

    if node.subtype == "GENERATE_PERSONALIZED_MESSAGE" and not config.get("send", True):
        return "OK", f"Message {message.id} generated for review.", False

    adapter = get_adapter(db, channel)
    to = customer.email if channel == Channel.EMAIL else customer.phone
    result = adapter.send_message(
        to=to or "",
        subject=message.subject,
        body=message.body,
        metadata={"journey_id": journey.id},
    )
    message.provider = adapter.provider
    message.provider_message_id = result.provider_message_id

    if not result.success:
        message.status = MessageStatus.FAILED.value
        message.error_message = result.error
        return "FAILED", f"Send failed: {result.error}", False

    message.status = MessageStatus.SENT.value
    message.sent_at = now
    record_communication_event(
        db,
        event_type={
            Channel.EMAIL: EventType.EMAIL_SENT,
            Channel.SMS: EventType.SMS_SENT,
            Channel.WHATSAPP: EventType.WHATSAPP_SENT,
        }[channel],
        customer_id=customer.id,
        message_id=message.id,
        channel=channel,
        provider=adapter.provider,
        occurred_at=now,
        is_simulated=isinstance(adapter, BaseMockAdapter),
        payload={"journey_id": journey.id},
        idempotency_key=make_idempotency_key("journey", journey.id, message.id),
    )
    return "OK", f"{channel.value} message {message.id} sent.", False


def run_all_journeys(db: Session, *, now: datetime | None = None) -> dict:
    """Enrol and advance every active journey."""
    journeys = (
        db.execute(select(Journey).where(Journey.status == JourneyStatus.ACTIVE.value))
        .scalars()
        .all()
    )
    results = {}
    for journey in journeys:
        enrolled = enrol_customers(db, journey)
        stats = run_journey(db, journey, now=now)
        results[journey.name] = {"enrolled": enrolled, **stats}
    return results
