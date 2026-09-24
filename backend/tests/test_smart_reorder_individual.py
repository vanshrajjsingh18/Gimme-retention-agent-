"""Smart Reorder, one customer at a time.

The thing this feature must not be is a campaign with one send time. So the
tests are written against a single named customer with a real routine, and
what they assert is that *his* message exists, says his name, is scheduled for
his minute, and is called off if he beats us to it.

Sam is the customer from the brief: five Wednesday-evening orders around
7:37 PM, a week apart. Everything below is checked against what the engine
works out from those five rows, not against numbers written into the test —
a test that hard-codes the answer proves the assertion, not the engine.

The nine numbered cases are the end-to-end sequence the brief asks for, run in
order: prediction, message, schedule, preview, duplication, already-ordered,
send, conversion, re-plan.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import count

import pytest
from sqlalchemy import select

from app.automations.service import activate, approve, create_automation
from app.core.enums import (
    AutomationKind,
    AutomationStatus,
    CancellationReason,
    Channel,
    OrderStatus,
    ScheduledMessageStatus,
)
from app.core.timezones import to_local, to_utc_naive
from app.models.entities import Automation, Customer, Order, OrderItem, ScheduledMessage
from app.services import smart_reorder_queue as queue

#: Five Wednesday evenings around 7:37 PM, in Sam's own local time.
ORDER_TIMES = ["19:42", "19:18", "19:51", "19:36", "19:40"]
FIRST_WEDNESDAY = date(2026, 8, 5)
TEST_PRODUCT = "Example Test Product"

_RUN = count(1)


def _local(week: int, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":"))
    day = FIRST_WEDNESDAY + timedelta(weeks=week)
    return datetime(day.year, day.month, day.day, hour, minute)


@pytest.fixture()
def sam(db) -> Customer:
    """SMART-REORDER-TEST-001 — a deterministic, unmistakable routine."""
    previous = (
        db.execute(select(Customer).where(Customer.external_id == "SMART-REORDER-TEST-001"))
        .scalars()
        .first()
    )
    if previous is not None:
        db.query(ScheduledMessage).filter(
            ScheduledMessage.customer_id == previous.id
        ).delete()
        db.delete(previous)
        db.commit()

    customer = Customer(
        external_id="SMART-REORDER-TEST-001",
        email="sam@smartreorder.test",
        phone="+64211000777",
        first_name="Sam",
        last_name="Rangi",
        city="Auckland",
        date_of_birth=date(1990, 5, 1),
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        email_consent=True,
        signup_date=to_utc_naive(_local(0, "19:42")) - timedelta(days=200),
    )
    db.add(customer)
    db.flush()
    for week, clock in enumerate(ORDER_TIMES):
        order = Order(
            external_id=f"SMART-REORDER-ORD-{week}",
            customer_id=customer.id,
            ordered_at=to_utc_naive(_local(week, clock)),
            status=OrderStatus.COMPLETED.value,
            total_amount=68.50,
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                external_id=f"SMART-REORDER-ITEM-{week}",
                order_id=order.id,
                sku="test-product",
                product_name=TEST_PRODUCT,
                category="Beer",
                brand="Example",
                quantity=1,
                unit_price=68.50,
                line_total=68.50,
            )
        )
    db.commit()

    from app.services.intelligence import refresh_customer

    refresh_customer(db, customer)
    db.refresh(customer)
    return customer


@pytest.fixture()
def campaign(db, bootstrapped, sam) -> Automation:
    """One Smart Reorder campaign, configured once, covering Sam."""
    automation = create_automation(
        db,
        name=f"Weekly Smart Reorder #{next(_RUN)}",
        kind=AutomationKind.NUDGE.value,
        channel=Channel.SMS.value,
        manual_customer_ids=[sam.id],
        message_template=(
            "Hi #first_name#, ready for another #product#? "
            "GIMME's ready when you are. Reply STOP to opt out."
        ),
    )
    approve(db, automation, user_id=1)
    activate(db, automation, now=to_utc_naive(_local(4, "21:00")))
    from app.automations import nudge

    nudge.enroll(db, automation, now=to_utc_naive(_local(4, "21:00")))
    return automation


def _queue_one(db, automation, *, now: datetime) -> ScheduledMessage:
    queue.build_queue(db, automation, now=now)
    message = queue.open_message_for(
        db,
        automation_id=automation.id,
        customer_id=db.execute(
            select(Customer.id).where(Customer.external_id == "SMART-REORDER-TEST-001")
        ).scalar_one(),
    )
    assert message is not None, "no individual message was written for Sam"
    return message


NOW = to_utc_naive(_local(4, "21:00"))  # just after his fifth order


# ==========================================================================
# 1. Prediction
# ==========================================================================
def test_1_the_engine_learns_sams_own_routine(db, sam):
    """Wednesday, about 7:37 PM, every seven days — from his rows, not ours.

    The time is a circular mean rather than the most recent order's clock:
    picking the last one would make the whole prediction hostage to a single
    late night.
    """
    from app.services.reorder_timing import plan_for_customer

    plan = plan_for_customer(db, sam.id, now=NOW)
    prediction = plan.prediction

    assert plan.has_plan, plan.reason
    assert prediction.preferred_weekday_name == "Wednesday"
    assert prediction.preferred_hour == 19
    # The five clock times average to 7:37 PM. Allowed a couple of minutes
    # rather than pinned, because the mean is over a circle.
    assert 33 <= prediction.preferred_minute <= 41, prediction.preferred_time_label
    assert prediction.intervals is not None
    assert 6.5 <= prediction.intervals.median_days <= 7.5
    assert prediction.overall_confidence >= 70, "a five-week routine should be confident"

    # The prediction is carried in the customer's own local time, which is the
    # only clock the question "when do they usually order" has an answer on.
    predicted = plan.predicted_local
    assert predicted.weekday() == 2  # Wednesday
    assert predicted.hour == 19
    assert to_utc_naive(predicted) > NOW, "the prediction must be a moment still to come"


# ==========================================================================
# 2. Message creation
# ==========================================================================
def test_2_an_individual_message_is_written_for_sam(db, sam, campaign):
    """A row of his own, with his name and his product already in it.

    Rendered at scheduling time rather than at send time on purpose: a queue
    that shows a template is a queue nobody can check.
    """
    message = _queue_one(db, campaign, now=NOW)

    assert message.customer_id == sam.id
    assert message.rendered_message.startswith("Hi Sam, ready for another")
    assert TEST_PRODUCT in message.rendered_message
    assert "#first_name#" not in message.rendered_message
    # The campaign's own copy is untouched — one template, many messages.
    assert "#first_name#" in campaign.message_template
    assert message.template == campaign.message_template


# ==========================================================================
# 3. Schedule
# ==========================================================================
def test_3_it_is_scheduled_for_his_minute_less_the_offset(db, sam, campaign):
    """Predicted order time minus the configured reminder offset.

    Asserted as arithmetic against the prediction rather than a literal
    clock time, so the test still means something when the routine changes.
    """
    message = _queue_one(db, campaign, now=NOW)

    assert message.status == ScheduledMessageStatus.SCHEDULED.value
    assert message.predicted_order_at is not None
    gap_minutes = (
        message.predicted_order_at - message.scheduled_at
    ).total_seconds() / 60.0
    # 30 minutes before is the default. The send window may pull it earlier
    # still — a 7:09 PM reminder is past the 7 PM close — and the row says so.
    if message.moved_for_send_window:
        assert gap_minutes > message.offset_minutes
    else:
        assert gap_minutes == message.offset_minutes == 30


def test_3b_two_customers_get_two_different_times(db, sam, campaign, bootstrapped):
    """The claim the whole feature rests on.

    One campaign, two customers, two schedules. A mass send would give these
    two the same minute, which is exactly the implementation the brief
    rejects — so it is asserted rather than assumed.
    """
    from app.automations import nudge

    # A Saturday-afternoon customer, nothing like Sam.
    other = Customer(
        external_id=f"SMART-REORDER-SAT-{next(_RUN)}",
        email="sat@smartreorder.test",
        phone="+64211000778",
        first_name="Hine",
        last_name="Walker",
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        signup_date=to_utc_naive(_local(0, "14:00")),
    )
    db.add(other)
    db.flush()
    for week in range(5):
        day = date(2026, 8, 8) + timedelta(weeks=week)  # Saturdays
        db.add(
            Order(
                external_id=f"{other.external_id}-ORD-{week}",
                customer_id=other.id,
                ordered_at=to_utc_naive(datetime(day.year, day.month, day.day, 14, 15)),
                status=OrderStatus.COMPLETED.value,
                total_amount=42.0,
            )
        )
    campaign.manual_customer_ids = [sam.id, other.id]
    db.commit()
    from app.services.intelligence import refresh_customer

    refresh_customer(db, other)
    nudge.enroll(db, campaign, now=NOW)

    queue.build_queue(db, campaign, now=NOW)
    sams = queue.open_message_for(db, automation_id=campaign.id, customer_id=sam.id)
    hines = queue.open_message_for(db, automation_id=campaign.id, customer_id=other.id)

    assert sams is not None and hines is not None
    assert sams.scheduled_at != hines.scheduled_at, "both customers got the same send time"
    assert to_local(sams.scheduled_at).weekday() == 2  # Wednesday
    assert to_local(hines.scheduled_at).weekday() == 5  # Saturday


# ==========================================================================
# 4. Preview
# ==========================================================================
def test_4_sam_appears_in_the_upcoming_queue(db, sam, campaign):
    """The operational screen: what is about to be sent, to whom, when."""
    _queue_one(db, campaign, now=NOW)
    rows = [queue.as_view(db, m) for m in queue.upcoming(db, automation_id=campaign.id)]

    mine = next(r for r in rows if r["customer_id"] == sam.id)
    assert mine["customer_name"] == "Sam Rangi"
    assert mine["status"] == ScheduledMessageStatus.SCHEDULED.value
    assert mine["channel"] == Channel.SMS.value
    assert TEST_PRODUCT in mine["message"]
    assert mine["confidence"] >= 70
    assert mine["usual_day"] == "Wednesday"
    # The contact is masked: an operations screen is looked at over shoulders.
    assert mine["to"] and mine["to"] != sam.phone


def test_4b_a_dry_run_shows_the_same_thing_and_writes_nothing(db, sam, campaign):
    """A preview that is the rules with the writes turned off, not a mock-up."""
    report = queue.build_queue(db, campaign, now=NOW, dry_run=True)

    assert report.scheduled >= 1
    sams = next(m for m in report.messages if m["customer_id"] == sam.id)
    assert TEST_PRODUCT in sams["message"]
    assert sams["status"] == "WOULD_SCHEDULE"
    assert (
        queue.open_message_for(db, automation_id=campaign.id, customer_id=sam.id) is None
    ), "a dry run created a real scheduled message"


# ==========================================================================
# 5. Duplication
# ==========================================================================
def test_5_running_the_scheduler_twice_sends_once(db, sam, campaign):
    """The property that cannot be promised by "the job never overlaps".

    The claim is a conditional update, so the second dispatcher finds the row
    already PROCESSING and does nothing. Asserted by dispatching the same due
    message twice and counting the sends.
    """
    message = _queue_one(db, campaign, now=NOW)
    due = message.scheduled_at

    first = queue.dispatch_due(db, now=due)
    second = queue.dispatch_due(db, now=due)

    assert first["sent"] == 1, first
    assert second["considered"] == 0, "the message was still due after being sent"
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.SENT.value
    assert message.attempts == 1


def test_5b_building_the_queue_twice_makes_one_message(db, sam, campaign):
    """A rebuild updates the row it already wrote rather than adding another."""
    queue.build_queue(db, campaign, now=NOW)
    second = queue.build_queue(db, campaign, now=NOW)

    rows = (
        db.execute(
            select(ScheduledMessage).where(
                ScheduledMessage.automation_id == campaign.id,
                ScheduledMessage.customer_id == sam.id,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, f"{len(rows)} messages queued for one customer"
    assert second.unchanged == 1 and second.scheduled == 0


# ==========================================================================
# 6. Already ordered
# ==========================================================================
def test_6_a_customer_who_ordered_first_is_not_reminded(db, sam, campaign):
    """The message this feature must never send.

    "Ready for your usual?" twenty minutes after they ordered says plainly
    that nothing was checked between scheduling and sending — which is the
    entire risk of writing the message down in advance.
    """
    message = _queue_one(db, campaign, now=NOW)
    due = message.scheduled_at

    db.add(
        Order(
            external_id="SMART-REORDER-EARLY",
            customer_id=sam.id,
            ordered_at=due - timedelta(minutes=15),
            status=OrderStatus.COMPLETED.value,
            total_amount=71.0,
        )
    )
    db.commit()

    stats = queue.dispatch_due(db, now=due)

    assert stats["sent"] == 0, "texted a customer who had already ordered"
    assert stats["cancelled"] == 1
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.CANCELLED.value
    assert message.cancellation_reason == CancellationReason.CUSTOMER_ALREADY_ORDERED.value


# ==========================================================================
# 7. Normal send
# ==========================================================================
def test_7_an_untouched_customer_is_sent_their_reminder(db, sam, campaign):
    """The happy path, through the real pipeline in mock mode."""
    message = _queue_one(db, campaign, now=NOW)

    stats = queue.dispatch_due(db, now=message.scheduled_at)

    assert stats["sent"] == 1, stats
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.SENT.value
    assert message.sent_at is not None
    assert message.provider
    assert TEST_PRODUCT in message.rendered_message


def test_7b_a_paused_campaign_sends_nothing(db, sam, campaign):
    """Pausing has to reach the messages already scheduled.

    A campaign is paused because somebody wants it to stop now, and thousands
    of messages already written down would otherwise keep going out.
    """
    message = _queue_one(db, campaign, now=NOW)
    campaign.status = AutomationStatus.PAUSED.value
    db.commit()

    stats = queue.dispatch_due(db, now=message.scheduled_at)

    assert stats["considered"] == 0, "a paused campaign was still dispatching"
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.SCHEDULED.value


# ==========================================================================
# 8. Conversion
# ==========================================================================
def test_8_an_order_after_the_reminder_is_credited_to_it(db, sam, campaign):
    """What the message was for, recorded against the message itself.

    The prediction error is kept signed: consistently early means the
    reminder arrives after the decision is made, consistently late means the
    interval is too short, and they call for opposite fixes.
    """
    message = _queue_one(db, campaign, now=NOW)
    queue.dispatch_due(db, now=message.scheduled_at)
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.SENT.value

    order = Order(
        external_id="SMART-REORDER-CONVERTED",
        customer_id=sam.id,
        ordered_at=message.sent_at + timedelta(minutes=27),
        status=OrderStatus.COMPLETED.value,
        total_amount=74.0,
    )
    db.add(order)
    db.commit()

    credited = queue.credit_conversion(db, order, commit=True)

    assert credited is not None, "the reminder that earned this order was not credited"
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.CONVERTED.value
    assert message.converted_order_id == order.id
    detail = message.context["conversion"]
    assert detail["hours_to_conversion"] == pytest.approx(0.45, abs=0.05)
    assert detail["order_amount"] == 74.0
    assert detail["prediction_error_minutes"] is not None


def test_8b_an_order_outside_the_window_is_not_credited(db, sam, campaign):
    """Attribution has an edge, or every order looks like a success."""
    message = _queue_one(db, campaign, now=NOW)
    queue.dispatch_due(db, now=message.scheduled_at)
    db.refresh(message)

    order = Order(
        external_id="SMART-REORDER-LATE",
        customer_id=sam.id,
        ordered_at=message.sent_at + timedelta(days=9),
        status=OrderStatus.COMPLETED.value,
        total_amount=74.0,
    )
    db.add(order)
    db.commit()

    assert queue.credit_conversion(db, order, commit=True) is None
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.SENT.value


# ==========================================================================
# 9. The next prediction
# ==========================================================================
def test_9_a_new_order_replaces_the_old_prediction(db, sam, campaign):
    """The loop closing: the order is both the answer and the new evidence.

    An order on Tuesday means the Wednesday prediction was wrong, and
    continuing to use it would aim the next reminder at a routine the
    customer has stopped keeping.
    """
    original = _queue_one(db, campaign, now=NOW)
    original_slot = original.scheduled_at

    # He orders unexpectedly on the Tuesday, a day before his usual.
    surprise_at = to_utc_naive(_local(5, "18:55") - timedelta(days=1))
    order = Order(
        external_id="SMART-REORDER-SURPRISE",
        customer_id=sam.id,
        ordered_at=surprise_at,
        status=OrderStatus.COMPLETED.value,
        total_amount=59.0,
    )
    db.add(order)
    db.commit()
    from app.services.intelligence import refresh_customer

    refresh_customer(db, sam)

    result = queue.refresh_for_customer(db, sam.id, now=surprise_at + timedelta(minutes=5))

    db.refresh(original)
    assert original.status == ScheduledMessageStatus.CANCELLED.value
    assert original.cancellation_reason == CancellationReason.PREDICTION_SUPERSEDED.value
    assert result["cancelled"] == 1

    replacement = queue.open_message_for(
        db, automation_id=campaign.id, customer_id=sam.id
    )
    assert replacement is not None, "no new reminder was planned from the new order"
    assert replacement.id != original.id
    assert replacement.scheduled_at != original_slot
    assert replacement.scheduled_at > surprise_at


# ==========================================================================
# Operator actions on one customer's message
# ==========================================================================
def test_an_operator_can_edit_one_customers_copy(db, sam, campaign):
    """Editing one message must not touch the campaign or anybody else.

    And a later rebuild must not quietly overwrite the edit, or the feature
    is a lie told once per refresh.
    """
    message = _queue_one(db, campaign, now=NOW)
    queue.edit(db, message.id, "Hi Sam, your usual is waiting. Reply STOP to opt out.")

    queue.build_queue(db, campaign, now=NOW)
    db.refresh(message)

    assert message.rendered_message.startswith("Hi Sam, your usual is waiting")
    assert message.edited_by_operator is True
    assert "#first_name#" in campaign.message_template


def test_an_operator_can_move_or_cancel_one_reminder(db, sam, campaign):
    message = _queue_one(db, campaign, now=NOW)
    moved_to = message.scheduled_at + timedelta(hours=1)

    queue.reschedule(db, message.id, moved_to)
    db.refresh(message)
    assert message.scheduled_at == moved_to

    queue.cancel_by_id(db, message.id, detail="Not this week.")
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.CANCELLED.value
    assert message.cancellation_reason == CancellationReason.OPERATOR_CANCELLED.value

    with pytest.raises(queue.QueueError):
        queue.cancel_by_id(db, message.id)


def test_a_reminder_whose_moment_passed_is_not_sent_late(db, sam, campaign):
    """"It's about your usual Wednesday evening", delivered Friday.

    Worse than silence: it says plainly that nothing was watching.
    """
    message = _queue_one(db, campaign, now=NOW)

    stats = queue.dispatch_due(
        db, now=message.scheduled_at + timedelta(minutes=queue.STALE_AFTER_MINUTES + 30)
    )

    assert stats["sent"] == 0
    assert stats["expired"] == 1
    db.refresh(message)
    assert message.status == ScheduledMessageStatus.EXPIRED.value
    assert message.cancellation_reason == CancellationReason.WINDOW_MISSED.value


def test_a_dashboard_counts_what_actually_happened(db, sam, campaign):
    """Counted off the message rows, not recomputed from order history."""
    message = _queue_one(db, campaign, now=NOW)
    queue.dispatch_due(db, now=message.scheduled_at)

    stats = queue.dashboard(db, campaign)

    assert stats["messages_sent"] == 1
    assert stats["messages_scheduled"] == 0
    assert stats["automation_name"] == campaign.name


# ==========================================================================
# The API the operational screens read
# ==========================================================================
def test_the_queue_endpoint_lists_individual_messages(db, sam, campaign, client, auth_headers):
    """What the Upcoming Messages screen reads.

    Asserted through HTTP rather than the service, because a screen that
    cannot get the data is a feature that does not exist however good the
    engine behind it is.
    """
    _queue_one(db, campaign, now=NOW)

    payload = client.get(
        f"/api/v1/smart-reorder/queue?automation_id={campaign.id}", headers=auth_headers
    ).json()

    mine = next(m for m in payload["messages"] if m["customer_id"] == sam.id)
    assert mine["customer_name"] == "Sam Rangi"
    assert TEST_PRODUCT in mine["message"]
    assert mine["status"] == ScheduledMessageStatus.SCHEDULED.value
    assert mine["scheduled_at_local"] and mine["predicted_order_at_local"]
    # The send time is before the order it is aimed at, on the same clock.
    assert mine["scheduled_at_local"] < mine["predicted_order_at_local"]


def test_the_dry_run_endpoint_schedules_nothing(db, sam, campaign, client, auth_headers):
    response = client.post(
        f"/api/v1/smart-reorder/{campaign.id}/dry-run", headers=auth_headers
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["dry_run"] is True
    assert payload["messages_scheduled"] >= 1
    assert any(m["customer_id"] == sam.id for m in payload["messages"])
    assert queue.open_message_for(db, automation_id=campaign.id, customer_id=sam.id) is None


def test_an_operator_can_cancel_one_message_over_the_api(db, sam, campaign, client, auth_headers):
    message = _queue_one(db, campaign, now=NOW)

    response = client.post(
        f"/api/v1/smart-reorder/queue/{message.id}/cancel",
        headers=auth_headers,
        json={"reason": "Sam asked us to hold off."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == ScheduledMessageStatus.CANCELLED.value
    # And a second attempt says why it cannot, rather than silently succeeding.
    assert client.post(
        f"/api/v1/smart-reorder/queue/{message.id}/cancel", headers=auth_headers
    ).status_code == 400


def test_edited_copy_is_still_held_to_the_merge_tag_whitelist(
    db, sam, campaign, client, auth_headers
):
    """An edit is a send path like any other, so it gets the same gate.

    Otherwise the one place copy bypasses validation is the place a person
    types it in a hurry.
    """
    message = _queue_one(db, campaign, now=NOW)

    bad = client.post(
        f"/api/v1/smart-reorder/queue/{message.id}/edit",
        headers=auth_headers,
        json={"body": "Hi #first_name#, use #discont_code#. Reply STOP to opt out."},
    )
    assert bad.status_code == 400
    assert "#discont_code#" in bad.json()["detail"]

    good = client.post(
        f"/api/v1/smart-reorder/queue/{message.id}/edit",
        headers=auth_headers,
        json={"body": "Hi Sam, your usual is a tap away. Reply STOP to opt out."},
    )
    assert good.status_code == 200
    assert good.json()["edited"] is True


def test_the_dashboard_endpoint_reports_the_campaign(db, sam, campaign, client, auth_headers):
    message = _queue_one(db, campaign, now=NOW)
    queue.dispatch_due(db, now=message.scheduled_at)

    payload = client.get(
        f"/api/v1/smart-reorder/{campaign.id}/dashboard", headers=auth_headers
    ).json()

    assert payload["messages_sent"] == 1
    assert payload["automation_id"] == campaign.id


def test_a_customers_own_last_order_does_not_cancel_their_reminder(db, sam, campaign):
    """The already-ordered window starts after the order it was built from.

    Found by running the engine against real imported customers: every
    reminder was cancelled as ALREADY_ORDERED before it could send. The
    window was being worked back from the predicted moment minus the typical
    interval, which lands an hour or two earlier than the customer's own last
    order — so the order the prediction was *derived from* looked like a
    purchase that had beaten us to it, and every perfectly good reminder was
    called off.

    The anchor is now stored on the message when it is written, which is the
    only place the exact value is known.
    """
    message = _queue_one(db, campaign, now=NOW)

    assert message.cycle_start_at is not None
    last_order_at = db.execute(
        select(Order.ordered_at)
        .where(Order.customer_id == sam.id, Order.status == OrderStatus.COMPLETED.value)
        .order_by(Order.ordered_at.desc())
    ).scalars().first()
    assert message.cycle_start_at > last_order_at, (
        "the window starts before the customer's own last order, so their "
        "history would read as 'already ordered'"
    )

    stats = queue.dispatch_due(db, now=message.scheduled_at)
    assert stats["cancelled"] == 0, "cancelled a reminder because of the order it was built from"
    assert stats["sent"] == 1
