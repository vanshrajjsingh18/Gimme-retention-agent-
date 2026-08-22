"""Campaign automations: the shared send pipeline and all three campaign types.

Most of these assert on what the system *refused* to do. The valuable
properties here are negative ones — consent honoured at send time, one message
per customer per day, nothing sent at 3am, a dry run that touches no provider
— and each is checked against the ledger rather than a return value, because
the ledger is what an operator would audit after the fact.
"""
from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.automations import cohort, nudge, sequences
from app.automations.runtime import (
    AutomationError,
    Candidate,
    execute_candidates,
    resolve_send_time,
)
from app.automations.service import (
    activate,
    approve,
    automation_stats,
    create_automation,
    preview,
    run_automation,
)
from app.automations.templates import build_context, render, sign_off
from app.core.enums import (
    AutomationKind,
    AutomationStatus,
    Channel,
    EnrollmentMode,
    EnrollmentStatus,
    OrderStatus,
    RecurrenceKind,
    SendStatus,
    SkipReason,
)
from app.core.timezones import combine_local, to_local
from app.models.entities import (
    Automation,
    AutomationEnrollment,
    AutomationSend,
    Customer,
    Order,
    Segment,
)
from app.services.optout import apply_global_opt_out, handle_inbound_reply

# A Monday inside the send window: 22:00 UTC on Sunday is 10:00 Monday in
# Auckland, which is when a real run would happen.
MONDAY_10AM = combine_local(date(2026, 6, 15), datetime(2026, 6, 15, 10, 0).time())


#: Monotonic across the whole module: object ids get recycled, so using one as
#: a uniqueness suffix produces collisions on names and external ids.
_SEQ = itertools.count(1)


@pytest.fixture()
def make_customer(db):
    def _make(**overrides) -> Customer:
        n = next(_SEQ)
        base = {
            "external_id": f"auto-test-{n}",
            "email": f"auto{n}@example.test",
            "phone": f"+6421000{n:04d}",
            "first_name": f"Test{n}",
            "last_name": "Customer",
            "city": "Auckland",
            "date_of_birth": date(1990, 1, 1),
            "age_verified": True,
            "marketing_consent": True,
            "email_consent": True,
            "sms_consent": True,
            "whatsapp_consent": True,
        }
        base.update(overrides)
        customer = Customer(**base)
        db.add(customer)
        db.commit()
        return customer

    return _make


@pytest.fixture()
def make_automation(db, bootstrapped):
    def _make(**overrides) -> Automation:
        base = {
            "name": f"Test automation {next(_SEQ)}",
            "kind": AutomationKind.COHORT_BULK.value,
            "channel": Channel.SMS.value,
            "message_template": "Hi {first_name}, your usual is a tap away. Reply STOP to opt out.",
        }
        base.update(overrides)
        automation = create_automation(db, **base)
        approve(db, automation, user_id=1)
        activate(db, automation, now=MONDAY_10AM)
        return automation

    return _make


def sends_for(db, automation) -> list[AutomationSend]:
    return list(
        db.execute(
            select(AutomationSend)
            .where(AutomationSend.automation_id == automation.id)
            .order_by(AutomationSend.id)
        )
        .scalars()
        .all()
    )


# ==========================================================================
# Consent, checked at send time
# ==========================================================================
class TestConsentAtSendTime:
    def test_customer_without_sms_consent_is_never_sent_to(
        self, db, make_customer, make_automation
    ):
        customer = make_customer(sms_consent=False)
        automation = make_automation(manual_customer_ids=[customer.id])

        report = run_automation(db, automation, now=MONDAY_10AM)

        assert report.sent == 0
        assert report.skipped == 1
        assert report.results[0].skip_reason == SkipReason.NO_CONSENT

    def test_consent_revoked_after_creation_is_honoured(
        self, db, make_customer, make_automation
    ):
        """The point of a send-time check: the audience was valid when built."""
        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])

        # The preview, taken while consent stood, says this customer is in.
        assert preview(db, automation, now=MONDAY_10AM)["previewed"] == 1

        customer.sms_consent = False
        db.commit()

        report = run_automation(db, automation, now=MONDAY_10AM)
        assert report.sent == 0
        assert report.results[0].skip_reason == SkipReason.NO_CONSENT

    def test_suppressed_customer_is_skipped(self, db, make_customer, make_automation):
        customer = make_customer(is_suppressed=True)
        automation = make_automation(manual_customer_ids=[customer.id])
        report = run_automation(db, automation, now=MONDAY_10AM)
        assert report.results[0].skip_reason == SkipReason.SUPPRESSED

    def test_skip_is_written_to_the_ledger_with_its_reason(
        self, db, make_customer, make_automation
    ):
        customer = make_customer(sms_consent=False)
        automation = make_automation(manual_customer_ids=[customer.id])
        run_automation(db, automation, now=MONDAY_10AM)

        rows = sends_for(db, automation)
        assert len(rows) == 1
        assert rows[0].status == SendStatus.SKIPPED.value
        assert rows[0].skip_reason == SkipReason.NO_CONSENT.value
        assert rows[0].skip_detail  # the operator can see *why*


# ==========================================================================
# Global opt-out
# ==========================================================================
class TestGlobalOptOut:
    def test_stop_reply_clears_every_consent_flag(self, db, make_customer):
        customer = make_customer()
        handle_inbound_reply(db, customer=customer, body="STOP", channel=Channel.SMS)

        db.refresh(customer)
        assert customer.is_suppressed is True
        assert not any(
            (
                customer.marketing_consent,
                customer.email_consent,
                customer.sms_consent,
                customer.whatsapp_consent,
            )
        )

    @pytest.mark.parametrize(
        "reply", ["STOP", "stop", "Stop.", "STOP!", "unsubscribe", "opt out", "QUIT"]
    )
    def test_common_opt_out_variants_are_recognised(self, db, make_customer, reply):
        customer = make_customer()
        assert handle_inbound_reply(db, customer=customer, body=reply) is not None

    def test_a_sentence_containing_stop_is_not_an_opt_out(self, db, make_customer):
        """Narrow by design: the keyword must be the whole message."""
        customer = make_customer()
        result = handle_inbound_reply(
            db, customer=customer, body="I couldn't stop drinking that IPA"
        )
        assert result is None
        db.refresh(customer)
        assert customer.sms_consent is True

    def test_opt_out_stops_every_automation_not_just_the_one_replied_to(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        first = make_automation(manual_customer_ids=[customer.id])
        second = make_automation(
            kind=AutomationKind.SEQUENCE.value,
            manual_customer_ids=[customer.id],
            steps=[{"offset_days": 0, "message_template": "Step one. Reply STOP to opt out."}],
        )
        sequences.enroll(db, second, now=MONDAY_10AM)
        assert len(sequences.active_enrollments(db, second)) == 1

        apply_global_opt_out(db, customer, channel=Channel.SMS)

        assert sequences.active_enrollments(db, second) == []
        # And the unrelated cohort automation will not reach them either.
        report = run_automation(db, first, now=MONDAY_10AM)
        assert report.sent == 0

    def test_opt_in_restores_messaging(self, db, make_customer, make_automation):
        customer = make_customer()
        handle_inbound_reply(db, customer=customer, body="STOP")
        handle_inbound_reply(db, customer=customer, body="START")

        automation = make_automation(manual_customer_ids=[customer.id])
        report = run_automation(db, automation, now=MONDAY_10AM)
        assert report.sent == 1


# ==========================================================================
# Quiet hours
# ==========================================================================
class TestQuietHours:
    def test_send_inside_the_window_is_not_moved(self):
        when, deferred = resolve_send_time(MONDAY_10AM)
        assert when == MONDAY_10AM
        assert deferred is False

    def test_late_night_send_is_deferred_to_the_morning(self):
        late = combine_local(date(2026, 6, 15), datetime(2026, 6, 15, 23, 0).time())
        when, deferred = resolve_send_time(late)
        assert deferred is True
        local = to_local(when)
        assert (local.hour, local.date()) == (9, date(2026, 6, 16))

    def test_a_three_am_job_still_sends_during_business_hours(
        self, db, make_customer, make_automation
    ):
        """The job's run time must not become the customer's receipt time."""
        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        three_am_local = combine_local(date(2026, 6, 16), datetime(2026, 6, 16, 3, 0).time())

        report = run_automation(db, automation, now=three_am_local)

        assert report.sent == 1
        local = to_local(report.results[0].scheduled_for)
        assert 9 <= local.hour < 19


# ==========================================================================
# Dedup and priority
# ==========================================================================
class TestDedup:
    def test_two_automations_cannot_message_the_same_customer_on_one_day(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        first = make_automation(manual_customer_ids=[customer.id])
        second = make_automation(manual_customer_ids=[customer.id])

        assert run_automation(db, first, now=MONDAY_10AM).sent == 1
        second_report = run_automation(db, second, now=MONDAY_10AM)

        assert second_report.sent == 0
        assert second_report.results[0].skip_reason == SkipReason.DEDUPED

    def test_the_same_customer_can_be_messaged_again_the_next_day(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        first = make_automation(manual_customer_ids=[customer.id])
        second = make_automation(manual_customer_ids=[customer.id])

        run_automation(db, first, now=MONDAY_10AM)
        tomorrow = MONDAY_10AM + timedelta(days=1)
        assert run_automation(db, second, now=tomorrow).sent == 1

    def test_a_sent_message_cannot_be_recalled_by_a_higher_priority_one(
        self, db, make_customer, make_automation
    ):
        """Priority orders a contest; it does not un-send yesterday's SMS."""
        customer = make_customer()
        bulk = make_automation(manual_customer_ids=[customer.id])
        run_automation(db, bulk, now=MONDAY_10AM)

        nudge_automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=[customer.id]
        )
        report = execute_candidates(
            db,
            nudge_automation,
            [Candidate(customer_id=customer.id, scheduled_for=MONDAY_10AM, body="Nudge copy.")],
            now=MONDAY_10AM,
        )
        assert report.results[0].skip_reason == SkipReason.DEDUPED
        assert "already received" in report.results[0].skip_detail

    def test_dedup_uses_the_customers_local_day_not_the_utc_day(
        self, db, make_customer, make_automation
    ):
        """Two sends either side of UTC midnight are the same NZ evening."""
        customer = make_customer()
        first = make_automation(manual_customer_ids=[customer.id])
        second = make_automation(manual_customer_ids=[customer.id])

        # 06:00 and 18:00 UTC on 15 June are 18:00 on the 15th and 06:00 on the
        # 16th locally — different local days despite one UTC day.
        morning_local = combine_local(date(2026, 6, 16), datetime(2026, 6, 16, 10, 0).time())
        run_automation(db, first, now=morning_local)
        rows = sends_for(db, first)
        assert rows[0].local_date == date(2026, 6, 16)

        report = run_automation(db, second, now=morning_local)
        assert report.results[0].skip_reason == SkipReason.DEDUPED

    def test_one_automation_cannot_double_send_within_a_single_batch(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        report = execute_candidates(
            db,
            automation,
            [
                Candidate(customer_id=customer.id, scheduled_for=MONDAY_10AM, body="First."),
                Candidate(customer_id=customer.id, scheduled_for=MONDAY_10AM, body="Second."),
            ],
            now=MONDAY_10AM,
        )
        assert report.sent == 1
        assert report.skipped == 1


# ==========================================================================
# Dry run
# ==========================================================================
class TestDryRun:
    def test_dry_run_sends_nothing(self, db, make_customer, make_automation):
        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])

        result = preview(db, automation, now=MONDAY_10AM)

        assert result["dry_run"] is True
        assert result["previewed"] == 1
        assert result["sent"] == 0
        live_rows = [r for r in sends_for(db, automation) if not r.is_dry_run]
        assert live_rows == []

    def test_dry_run_shows_the_exact_copy_and_local_time(
        self, db, make_customer, make_automation
    ):
        customer = make_customer(first_name="Ana")
        automation = make_automation(manual_customer_ids=[customer.id])

        recipient = preview(db, automation, now=MONDAY_10AM)["recipients"][0]

        assert "Ana" in recipient["body"]
        assert recipient["scheduled_for_local"].startswith("2026-06-15T10:00")

    def test_dry_run_reports_who_would_be_excluded_and_why(
        self, db, make_customer, make_automation
    ):
        allowed = make_customer()
        blocked = make_customer(sms_consent=False)
        automation = make_automation(manual_customer_ids=[allowed.id, blocked.id])

        result = preview(db, automation, now=MONDAY_10AM)

        assert result["previewed"] == 1
        assert result["skips_by_reason"] == {SkipReason.NO_CONSENT.value: 1}

    def test_a_dry_run_does_not_reserve_the_day(self, db, make_customer, make_automation):
        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        preview(db, automation, now=MONDAY_10AM)
        assert run_automation(db, automation, now=MONDAY_10AM).sent == 1

    def test_previews_mask_contact_details(self, db, make_customer, make_automation):
        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        shown = preview(db, automation, now=MONDAY_10AM)["recipients"][0]["to"]
        assert shown != customer.phone
        assert "***" in shown


# ==========================================================================
# Approval gate
# ==========================================================================
class TestApprovalGate:
    def test_an_unapproved_automation_cannot_send(self, db, make_customer):
        customer = make_customer()
        automation = create_automation(
            db,
            name=f"Unapproved {next(_SEQ)}",
            kind=AutomationKind.COHORT_BULK.value,
            manual_customer_ids=[customer.id],
            message_template="Hi {first_name}. Reply STOP to opt out.",
        )
        with pytest.raises(AutomationError, match="approved"):
            activate(db, automation, now=MONDAY_10AM)

    def test_a_draft_automation_cannot_send_but_can_be_previewed(self, db, make_customer):
        customer = make_customer()
        automation = create_automation(
            db,
            name=f"Draft {next(_SEQ)}",
            kind=AutomationKind.COHORT_BULK.value,
            manual_customer_ids=[customer.id],
            message_template="Hi {first_name}. Reply STOP to opt out.",
            require_approval=False,
        )
        with pytest.raises(AutomationError, match="ACTIVE"):
            run_automation(db, automation, now=MONDAY_10AM)
        assert preview(db, automation, now=MONDAY_10AM)["previewed"] == 1


# ==========================================================================
# Feature 3 — cohort bulk
# ==========================================================================
class TestCohortCampaigns:
    def test_audience_is_re_evaluated_at_send_time(
        self, db, make_customer, make_automation, bootstrapped
    ):
        """A recurring cohort send must track the segment, not a stale list."""
        segment = db.execute(
            select(Segment).where(Segment.name == "New Customers")
        ).scalar_one()
        joiner = make_customer(lifecycle_stage="REGULAR")
        automation = make_automation(segment_id=segment.id)

        assert joiner.id not in cohort.resolve_audience(db, automation)

        joiner.lifecycle_stage = "NEW"
        db.commit()
        assert joiner.id in cohort.resolve_audience(db, automation)

    def test_copy_defaults_to_the_segments_tone(self, db, make_automation, bootstrapped):
        second_order = db.execute(
            select(Segment).where(Segment.name == "Needs Second Order")
        ).scalar_one()
        lapsed = db.execute(select(Segment).where(Segment.name == "Dormant")).scalar_one()

        first = make_automation(segment_id=second_order.id, message_template="")
        second = make_automation(segment_id=lapsed.id, message_template="")

        assert "round two" in cohort.template_for(db, first)
        assert "been a while" in cohort.template_for(db, second)

    def test_explicit_copy_overrides_the_segment_default(
        self, db, make_automation, bootstrapped
    ):
        segment = db.execute(select(Segment).where(Segment.name == "Dormant")).scalar_one()
        automation = make_automation(
            segment_id=segment.id, message_template="Our own words. Reply STOP to opt out."
        )
        assert cohort.template_for(db, automation) == automation.message_template

    def test_a_one_off_send_completes(self, db, make_customer, make_automation):
        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        run_automation(db, automation, now=MONDAY_10AM)
        assert automation.status == AutomationStatus.COMPLETED.value
        assert automation.next_run_at is None

    def test_a_weekly_send_schedules_the_next_monday(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        automation = make_automation(
            manual_customer_ids=[customer.id],
            recurrence=RecurrenceKind.WEEKLY.value,
            recurrence_day=0,  # Monday
            send_time_local="10:00",
        )
        run_automation(db, automation, now=MONDAY_10AM)

        assert automation.status == AutomationStatus.ACTIVE.value
        local = to_local(automation.next_run_at)
        assert local.weekday() == 0
        assert (local.hour, local.date()) == (10, date(2026, 6, 22))

    def test_recurrence_stops_at_the_end_date(self, db, make_customer, make_automation):
        customer = make_customer()
        automation = make_automation(
            manual_customer_ids=[customer.id],
            recurrence=RecurrenceKind.WEEKLY.value,
            recurrence_day=0,
            ends_at=MONDAY_10AM + timedelta(days=3),
        )
        run_automation(db, automation, now=MONDAY_10AM)
        assert automation.next_run_at is None
        assert automation.status == AutomationStatus.COMPLETED.value

    def test_monthly_recurrence_survives_a_short_month(self, db, make_automation):
        automation = make_automation(
            manual_customer_ids=[1],
            recurrence=RecurrenceKind.MONTHLY.value,
            recurrence_day=31,
        )
        after = combine_local(date(2026, 3, 31), datetime(2026, 3, 31, 12, 0).time())
        following = cohort.next_occurrence(automation, after=after)
        assert to_local(following).date() == date(2026, 4, 30)


# ==========================================================================
# Feature 1 — sequences
# ==========================================================================
STEPS = [
    {"name": "Day 0", "offset_days": 0, "message_template": "Day zero. Reply STOP to opt out."},
    {"name": "Day 7", "offset_days": 7, "message_template": "Day seven. Reply STOP to opt out."},
    {"name": "Day 14", "offset_days": 14, "message_template": "Day fourteen. Reply STOP to opt out."},
]


@pytest.fixture()
def sequence(db, make_automation):
    def _make(customer_ids, **overrides):
        return make_automation(
            kind=AutomationKind.SEQUENCE.value,
            manual_customer_ids=list(customer_ids),
            steps=STEPS,
            **overrides,
        )

    return _make


class TestSequences:
    def test_steps_are_timed_from_enrollment_not_the_calendar(
        self, db, make_customer, sequence
    ):
        customer = make_customer()
        automation = sequence([customer.id])

        assert run_automation(db, automation, now=MONDAY_10AM).sent == 1

        enrollment = sequences.active_enrollments(db, automation)[0]
        assert enrollment.current_step == 1
        # Day 7 is seven days after *this* customer joined.
        due = sequences.step_due_at(
            automation, sequences.steps_for(db, automation)[1], enrollment
        )
        assert to_local(due).date() == to_local(enrollment.enrolled_at).date() + timedelta(days=7)

    def test_a_later_step_is_not_sent_early(self, db, make_customer, sequence):
        customer = make_customer()
        automation = sequence([customer.id])
        run_automation(db, automation, now=MONDAY_10AM)

        next_day = MONDAY_10AM + timedelta(days=1)
        assert run_automation(db, automation, now=next_day).sent == 0

    def test_the_sequence_advances_one_step_per_run(self, db, make_customer, sequence):
        customer = make_customer()
        automation = sequence([customer.id])

        bodies = []
        for offset in (0, 7, 14):
            report = run_automation(db, automation, now=MONDAY_10AM + timedelta(days=offset))
            assert report.sent == 1, f"day {offset}"
            bodies.append(report.results[0].body)

        assert bodies == ["Day zero. Reply STOP to opt out.",
                          "Day seven. Reply STOP to opt out.",
                          "Day fourteen. Reply STOP to opt out."]

    def test_a_customer_who_completes_every_step_leaves_the_sequence(
        self, db, make_customer, sequence
    ):
        customer = make_customer()
        automation = sequence([customer.id])
        for offset in (0, 7, 14):
            run_automation(db, automation, now=MONDAY_10AM + timedelta(days=offset))

        enrollment = db.execute(
            select(AutomationEnrollment).where(
                AutomationEnrollment.automation_id == automation.id
            )
        ).scalar_one()
        assert enrollment.status == EnrollmentStatus.COMPLETED.value

    def test_a_skipped_step_is_retried_rather_than_consumed(
        self, db, make_customer, sequence
    ):
        """A dedup loss must not silently swallow a message."""
        customer = make_customer()
        blocker = make_automation_for_same_day(db, customer)
        automation = sequence([customer.id])

        run_automation(db, blocker, now=MONDAY_10AM)
        first = run_automation(db, automation, now=MONDAY_10AM)
        assert first.sent == 0
        assert first.results[0].skip_reason == SkipReason.DEDUPED

        enrollment = sequences.active_enrollments(db, automation)[0]
        assert enrollment.current_step == 0  # not advanced

        later = run_automation(db, automation, now=MONDAY_10AM + timedelta(days=1))
        assert later.sent == 1
        assert later.results[0].body.startswith("Day zero")

    # -- stop conditions ---------------------------------------------------
    def test_placing_an_order_stops_the_sequence(self, db, make_customer, sequence):
        customer = make_customer()
        automation = sequence([customer.id])
        run_automation(db, automation, now=MONDAY_10AM)

        db.add(
            Order(
                external_id=f"ord-{customer.id}-goal",
                customer_id=customer.id,
                ordered_at=MONDAY_10AM + timedelta(days=1),
                status=OrderStatus.COMPLETED.value,
                total_amount=80.0,
            )
        )
        db.commit()

        report = run_automation(db, automation, now=MONDAY_10AM + timedelta(days=7))
        assert report.sent == 0

        enrollment = db.execute(
            select(AutomationEnrollment).where(
                AutomationEnrollment.automation_id == automation.id
            )
        ).scalar_one()
        assert enrollment.status == EnrollmentStatus.STOPPED.value
        assert enrollment.stop_reason == sequences.STOP_ORDERED

    def test_a_cancelled_order_does_not_count_as_the_goal(
        self, db, make_customer, sequence
    ):
        customer = make_customer()
        automation = sequence([customer.id])
        run_automation(db, automation, now=MONDAY_10AM)
        db.add(
            Order(
                external_id=f"ord-{customer.id}-cancelled",
                customer_id=customer.id,
                ordered_at=MONDAY_10AM + timedelta(days=1),
                status=OrderStatus.CANCELLED.value,
                total_amount=80.0,
            )
        )
        db.commit()

        assert run_automation(db, automation, now=MONDAY_10AM + timedelta(days=7)).sent == 1

    def test_opting_out_stops_the_sequence(self, db, make_customer, sequence):
        customer = make_customer()
        automation = sequence([customer.id])
        run_automation(db, automation, now=MONDAY_10AM)

        handle_inbound_reply(db, customer=customer, body="STOP")

        assert run_automation(db, automation, now=MONDAY_10AM + timedelta(days=7)).sent == 0
        assert sequences.active_enrollments(db, automation) == []

    def test_the_end_date_stops_remaining_steps(self, db, make_customer, sequence):
        customer = make_customer()
        automation = sequence([customer.id], ends_at=MONDAY_10AM + timedelta(days=3))
        run_automation(db, automation, now=MONDAY_10AM)

        assert run_automation(db, automation, now=MONDAY_10AM + timedelta(days=7)).sent == 0
        assert automation.status == AutomationStatus.COMPLETED.value

    # -- enrollment modes --------------------------------------------------
    def test_rolling_enrollment_lets_new_customers_join_mid_flight(
        self, db, make_customer, sequence
    ):
        first = make_customer()
        automation = sequence([first.id], enrollment_mode=EnrollmentMode.ROLLING.value)
        run_automation(db, automation, now=MONDAY_10AM)

        latecomer = make_customer()
        automation.manual_customer_ids = [first.id, latecomer.id]
        db.commit()

        report = run_automation(db, automation, now=MONDAY_10AM + timedelta(days=1))
        assert [r.customer_id for r in report.results if r.status == SendStatus.SENT] == [
            latecomer.id
        ]

    def test_a_late_joiner_starts_at_day_zero_not_mid_sequence(
        self, db, make_customer, sequence
    ):
        first = make_customer()
        automation = sequence([first.id])
        run_automation(db, automation, now=MONDAY_10AM)

        latecomer = make_customer()
        automation.manual_customer_ids = [first.id, latecomer.id]
        db.commit()
        report = run_automation(db, automation, now=MONDAY_10AM + timedelta(days=1))

        assert report.results[0].body.startswith("Day zero")

    def test_fixed_cohort_locks_the_audience_at_launch(self, db, make_customer, sequence):
        first = make_customer()
        automation = sequence([first.id], enrollment_mode=EnrollmentMode.FIXED_COHORT.value)
        run_automation(db, automation, now=MONDAY_10AM)

        latecomer = make_customer()
        automation.manual_customer_ids = [first.id, latecomer.id]
        db.commit()

        run_automation(db, automation, now=MONDAY_10AM + timedelta(days=1))
        enrolled = db.execute(
            select(AutomationEnrollment.customer_id).where(
                AutomationEnrollment.automation_id == automation.id
            )
        ).scalars().all()
        assert list(enrolled) == [first.id]

    def test_a_sequence_needs_at_least_one_step(self, db, make_customer):
        customer = make_customer()
        with pytest.raises(AutomationError, match="at least one step"):
            create_automation(
                db,
                name=f"Stepless {next(_SEQ)}",
                kind=AutomationKind.SEQUENCE.value,
                manual_customer_ids=[customer.id],
            )


def make_automation_for_same_day(db, customer) -> Automation:
    """A separate cohort automation used to occupy a customer's day."""
    automation = create_automation(
        db,
        name=f"Blocker {customer.id}-{next(_SEQ)}",
        kind=AutomationKind.COHORT_BULK.value,
        manual_customer_ids=[customer.id],
        message_template="Blocking send. Reply STOP to opt out.",
    )
    approve(db, automation, user_id=1)
    return activate(db, automation, now=MONDAY_10AM)


# ==========================================================================
# Feature 2 — behavioural nudge
# ==========================================================================
def friday_orders(customer_id: int, count: int, *, hour: int = 18) -> list[Order]:
    """`count` weekly Friday-evening orders ending shortly before the fixture date."""
    last = datetime(2026, 6, 12, hour, 0)  # a Friday
    return [
        Order(
            external_id=f"ord-{customer_id}-{i}",
            customer_id=customer_id,
            ordered_at=last - timedelta(weeks=i),
            status=OrderStatus.COMPLETED.value,
            total_amount=75.0,
        )
        for i in range(count)
    ]


class TestBehaviouralNudge:
    def test_a_customer_with_a_pattern_is_enrolled(self, db, make_customer, make_automation):
        customer = make_customer()
        for order in friday_orders(customer.id, 5):
            db.add(order)
        db.commit()

        automation = make_automation(
            kind=AutomationKind.NUDGE.value,
            manual_customer_ids=[customer.id],
            message_template="Hi {first_name}, your usual {usual_day}? Reply STOP to opt out.",
        )
        result = nudge.enroll(db, automation, now=MONDAY_10AM)

        assert result["enrolled"] == 1
        enrollment = nudge._active(db, automation)[0]
        assert enrollment.pattern["weekday_name"] == "Friday"

    def test_a_customer_with_too_few_orders_is_not_enrolled(
        self, db, make_customer, make_automation
    ):
        """No pattern means no nudge — a guess timed by coincidence is worse."""
        customer = make_customer()
        for order in friday_orders(customer.id, 2):
            db.add(order)
        db.commit()

        automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=[customer.id]
        )
        result = nudge.enroll(db, automation, now=MONDAY_10AM)

        assert result["enrolled"] == 0
        assert result["skipped_no_pattern"] == 1

    def test_the_nudge_is_scheduled_for_the_customers_own_day(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        for order in friday_orders(customer.id, 6):
            db.add(order)
        db.commit()

        automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=[customer.id]
        )
        nudge.enroll(db, automation, now=MONDAY_10AM)

        enrollment = nudge._active(db, automation)[0]
        assert to_local(enrollment.next_due_at).weekday() == 4  # Friday

    def test_a_customer_with_an_order_in_flight_is_not_nudged(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        for order in friday_orders(customer.id, 5):
            db.add(order)
        db.add(
            Order(
                external_id=f"ord-{customer.id}-pending",
                customer_id=customer.id,
                ordered_at=MONDAY_10AM,
                status=OrderStatus.PENDING.value,
                total_amount=60.0,
            )
        )
        db.commit()

        automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=[customer.id]
        )
        nudge.enroll(db, automation, now=MONDAY_10AM)
        due = MONDAY_10AM + timedelta(days=14)
        report = run_automation(db, automation, now=due)

        assert report.sent == 0
        assert any(r.skip_reason == SkipReason.PENDING_ORDER for r in report.results)

    def test_the_nudge_keeps_running_after_a_send(self, db, make_customer, make_automation):
        """A standing automation, not a campaign that finishes."""
        customer = make_customer()
        for order in friday_orders(customer.id, 6):
            db.add(order)
        db.commit()

        automation = make_automation(
            kind=AutomationKind.NUDGE.value,
            manual_customer_ids=[customer.id],
            message_template="Hi {first_name}, your usual? Reply STOP to opt out.",
        )
        nudge.enroll(db, automation, now=MONDAY_10AM)
        report = run_automation(db, automation, now=MONDAY_10AM + timedelta(days=14))

        assert report.sent == 1
        assert automation.status == AutomationStatus.ACTIVE.value
        enrollment = nudge._active(db, automation)[0]
        assert enrollment.next_due_at > MONDAY_10AM + timedelta(days=14)

    def test_a_stale_pattern_is_recomputed(self, db, make_customer, make_automation):
        customer = make_customer()
        for order in friday_orders(customer.id, 5):
            db.add(order)
        db.commit()

        automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=[customer.id]
        )
        nudge.enroll(db, automation, now=MONDAY_10AM)
        result = nudge.refresh_patterns(db, automation, now=MONDAY_10AM + timedelta(days=45))
        assert result["refreshed"] == 1

    def test_a_customer_whose_pattern_disappears_is_dropped(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        orders = friday_orders(customer.id, 4)
        for order in orders:
            db.add(order)
        db.commit()
        automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=[customer.id]
        )
        nudge.enroll(db, automation, now=MONDAY_10AM)

        for order in orders[:3]:
            db.delete(order)
        db.commit()

        result = nudge.refresh_patterns(db, automation, now=MONDAY_10AM, force=True)
        assert result["dropped"] == 1
        assert nudge._active(db, automation) == []

    # -- timing the nudge into business hours ------------------------------
    def test_a_late_evening_buyer_is_nudged_earlier_the_same_day(self):
        """Not pushed to tomorrow morning — that is after they would have ordered."""
        from app.automations.nudge import clamp_to_window

        saturday_9pm = datetime(2026, 8, 22, 21, 0)
        assert clamp_to_window(saturday_9pm) == datetime(2026, 8, 22, 18, 0)

    def test_an_overnight_buyer_is_nudged_the_evening_before(self):
        from app.automations.nudge import clamp_to_window

        assert clamp_to_window(datetime(2026, 8, 22, 3, 0)) == datetime(2026, 8, 21, 18, 0)

    def test_a_pattern_already_inside_business_hours_is_left_alone(self):
        from app.automations.nudge import clamp_to_window

        for hour in (9, 12, 17, 18):
            moment = datetime(2026, 8, 22, hour, 0)
            assert clamp_to_window(moment) == moment

    def test_every_scheduled_nudge_lands_inside_the_send_window(
        self, db, make_customer, make_automation
    ):
        """Whatever hour a customer buys at, the nudge itself is in business hours."""
        ids = []
        for hour in (2, 8, 11, 15, 18, 21, 23):
            customer = make_customer()
            for order in friday_orders(customer.id, 5, hour=hour):
                db.add(order)
            ids.append(customer.id)
        db.commit()
        automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=ids
        )

        nudge.enroll(db, automation, now=MONDAY_10AM)

        due = [e.next_due_at for e in nudge._active(db, automation) if e.next_due_at]
        assert len(due) == len(ids)
        for moment in due:
            local = to_local(moment)
            assert 9 <= local.hour < 19, f"{local} is outside business hours"
            assert moment > MONDAY_10AM, f"{local} is already in the past"

    # -- previewing before anyone is enrolled -------------------------------
    def test_a_nudge_can_be_previewed_before_anyone_is_enrolled(
        self, db, make_customer, make_automation
    ):
        """Enrollment happens on a live run, so a naive preview would be empty
        — and an operator deciding whether to approve would learn nothing."""
        customer = make_customer()
        for order in friday_orders(customer.id, 6):
            db.add(order)
        db.commit()
        automation = make_automation(
            kind=AutomationKind.NUDGE.value,
            manual_customer_ids=[customer.id],
            message_template="Hi {first_name}, your usual {usual_day}? Reply STOP to opt out.",
        )

        assert nudge._active(db, automation) == []  # nobody enrolled yet

        result = preview(db, automation, now=MONDAY_10AM)

        assert result["previewed"] == 1
        recipient = result["recipients"][0]
        assert recipient["customer_id"] == customer.id
        assert "Friday" in recipient["body"]
        assert recipient["context"]["usual_day"] == "Friday"

    def test_previewing_a_nudge_enrolls_nobody(self, db, make_customer, make_automation):
        customer = make_customer()
        for order in friday_orders(customer.id, 6):
            db.add(order)
        db.commit()
        automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=[customer.id]
        )

        preview(db, automation, now=MONDAY_10AM)

        assert nudge._active(db, automation) == []
        assert automation.status == AutomationStatus.ACTIVE.value

    def test_a_preview_shows_the_whole_audience_not_just_todays_slot(
        self, db, make_customer, make_automation
    ):
        """A live run sends only what is due; a preview shows the standing set."""
        ids = []
        for _ in range(3):
            customer = make_customer()
            for order in friday_orders(customer.id, 5):
                db.add(order)
            ids.append(customer.id)
        db.commit()
        automation = make_automation(
            kind=AutomationKind.NUDGE.value, manual_customer_ids=ids
        )

        # MONDAY_10AM is nowhere near a Friday slot, so a live run sends none.
        assert run_automation(db, automation, now=MONDAY_10AM).sent == 0
        # ...but the preview still accounts for all three customers.
        assert preview(db, automation, now=MONDAY_10AM)["candidates"] == 3

    def test_a_customer_without_a_pattern_is_absent_from_the_preview_too(
        self, db, make_customer, make_automation
    ):
        """The preview count must match what a live run would actually enrol."""
        with_pattern = make_customer()
        for order in friday_orders(with_pattern.id, 5):
            db.add(order)
        too_few = make_customer()
        for order in friday_orders(too_few.id, 2):
            db.add(order)
        db.commit()

        automation = make_automation(
            kind=AutomationKind.NUDGE.value,
            manual_customer_ids=[with_pattern.id, too_few.id],
        )
        result = preview(db, automation, now=MONDAY_10AM)

        assert result["candidates"] == 1
        assert result["recipients"][0]["customer_id"] == with_pattern.id

    # -- offers ------------------------------------------------------------
    def test_no_discount_for_a_customer_who_buys_at_full_price(
        self, db, make_customer, bootstrapped
    ):
        customer = make_customer()
        decision = nudge.offer_for(db, customer.id)
        assert decision.include_discount is False

    def test_a_discount_responsive_customer_gets_an_approved_promotion(
        self, db, make_customer, bootstrapped
    ):
        from app.models.entities import CustomerMetrics
        from app.services.brand import get_brand_settings

        customer = make_customer()
        db.add(CustomerMetrics(customer_id=customer.id, discount_dependency=0.75))
        db.commit()
        brand = get_brand_settings(db)

        decision = nudge.offer_for(db, customer.id)
        if brand.allowed_promotions:
            assert decision.include_discount is True
            assert decision.promotion in brand.allowed_promotions
        else:
            assert decision.include_discount is False


# ==========================================================================
# Templates
# ==========================================================================
class TestTemplates:
    def test_placeholders_are_filled_from_the_customer_record(
        self, db, make_customer, bootstrapped
    ):
        from app.services.brand import get_brand_settings

        customer = make_customer(first_name="Wiremu", city="Wellington")
        body = render(
            "Hi {first_name} in {city}.", build_context(customer, get_brand_settings(db))
        )
        assert body == "Hi Wiremu in Wellington."

    def test_a_missing_first_name_reads_naturally(self, db, make_customer, bootstrapped):
        from app.services.brand import get_brand_settings

        customer = make_customer(first_name="")
        body = render("Hi {first_name}, ready to reorder?", build_context(customer, get_brand_settings(db)))
        assert body == "Hi there, ready to reorder?"

    def test_an_unknown_token_is_left_visible_rather_than_deleted(
        self, db, make_customer, bootstrapped
    ):
        """It must fail the compliance placeholder check, not ship a gap."""
        from app.automations.templates import unresolved_tokens
        from app.services.brand import get_brand_settings

        customer = make_customer()
        body = render("Hi {first_name}, {mystery_field}.", build_context(customer, get_brand_settings(db)))
        assert unresolved_tokens(body) == ["mystery_field"]

    def test_the_sign_off_is_omitted_until_a_real_name_is_configured(
        self, db, bootstrapped
    ):
        from app.services.brand import get_brand_settings

        brand = get_brand_settings(db)
        original = brand.signatory_name
        brand.signatory_name = ""
        assert sign_off(brand) == ""

        brand.signatory_name = "Alex Tui"
        brand.signatory_title = "Founder"
        assert sign_off(brand) == "Alex Tui, Founder"
        brand.signatory_name = original


# ==========================================================================
# Delivery tracking
# ==========================================================================
class TestDeliveryTracking:
    def test_a_send_is_recorded_with_its_provider_id(
        self, db, make_customer, make_automation
    ):
        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        run_automation(db, automation, now=MONDAY_10AM)

        row = sends_for(db, automation)[0]
        assert row.status == SendStatus.SENT.value
        assert row.provider
        assert row.message_id is not None
        assert row.body

    def test_a_delivery_receipt_advances_the_ledger(
        self, db, make_customer, make_automation
    ):
        from app.automations.delivery import apply_delivery_event
        from app.core.enums import EventType

        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        run_automation(db, automation, now=MONDAY_10AM)
        row = sends_for(db, automation)[0]

        apply_delivery_event(
            db,
            event_type=EventType.SMS_DELIVERED,
            message_id=row.message_id,
            occurred_at=MONDAY_10AM + timedelta(minutes=2),
        )
        db.commit()

        assert row.status == SendStatus.DELIVERED.value
        assert row.delivered_at is not None

    def test_a_late_sent_receipt_cannot_undo_a_delivery(
        self, db, make_customer, make_automation
    ):
        from app.automations.delivery import apply_delivery_event
        from app.core.enums import EventType

        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        run_automation(db, automation, now=MONDAY_10AM)
        row = sends_for(db, automation)[0]

        apply_delivery_event(db, event_type=EventType.SMS_DELIVERED, message_id=row.message_id)
        apply_delivery_event(db, event_type=EventType.SMS_SENT, message_id=row.message_id)
        db.commit()

        assert row.status == SendStatus.DELIVERED.value

    def test_stats_summarise_the_ledger(self, db, make_customer, make_automation):
        allowed = make_customer()
        blocked = make_customer(sms_consent=False)
        automation = make_automation(manual_customer_ids=[allowed.id, blocked.id])
        run_automation(db, automation, now=MONDAY_10AM)

        stats = automation_stats(db, automation)
        assert stats["total_sent"] == 1
        assert stats["total_skipped"] == 1
        assert stats["skips_by_reason"] == {SkipReason.NO_CONSENT.value: 1}

# ==========================================================================
# Integration with the existing campaign reporting
# ==========================================================================
class TestReportingIntegration:
    def test_an_automation_send_is_attributed_like_any_campaign(
        self, db, make_customer, make_automation
    ):
        """The reason every automation carries a backing campaign.

        Without it an automated send would be invisible to attribution,
        campaign analytics and the Customer 360 history — a parallel reporting
        world that inevitably disagrees with the first one.
        """
        from app.services.attribution import attribute_order
        from app.models.entities import Campaign, CommunicationEvent

        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        report = run_automation(db, automation, now=MONDAY_10AM)
        assert report.sent == 1

        # The send is a real communication event against the backing campaign.
        touch = db.execute(
            select(CommunicationEvent).where(
                CommunicationEvent.customer_id == customer.id,
                CommunicationEvent.campaign_id == automation.campaign_id,
            )
        ).scalars().first()
        assert touch is not None

        order = Order(
            external_id=f"ord-attrib-{customer.id}",
            customer_id=customer.id,
            ordered_at=MONDAY_10AM + timedelta(hours=6),
            status=OrderStatus.COMPLETED.value,
            total_amount=94.50,
        )
        db.add(order)
        db.commit()

        record = attribute_order(db, order)
        assert record is not None
        assert record.campaign_id == automation.campaign_id
        assert record.revenue == 94.50

        campaign = db.get(Campaign, automation.campaign_id)
        assert campaign.conversions == 1
        assert campaign.attributed_revenue == 94.50

    def test_a_skipped_customer_generates_no_touch_to_attribute(
        self, db, make_customer, make_automation
    ):
        """A message that was never sent must not earn credit for an order."""
        from app.services.attribution import attribute_order

        customer = make_customer(sms_consent=False)
        automation = make_automation(manual_customer_ids=[customer.id])
        run_automation(db, automation, now=MONDAY_10AM)

        order = Order(
            external_id=f"ord-noattrib-{customer.id}",
            customer_id=customer.id,
            ordered_at=MONDAY_10AM + timedelta(hours=6),
            status=OrderStatus.COMPLETED.value,
            total_amount=61.00,
        )
        db.add(order)
        db.commit()

        assert attribute_order(db, order) is None

    def test_a_dry_run_earns_no_attribution(self, db, make_customer, make_automation):
        from app.services.attribution import attribute_order

        customer = make_customer()
        automation = make_automation(manual_customer_ids=[customer.id])
        preview(db, automation, now=MONDAY_10AM)

        order = Order(
            external_id=f"ord-dryrun-{customer.id}",
            customer_id=customer.id,
            ordered_at=MONDAY_10AM + timedelta(hours=6),
            status=OrderStatus.COMPLETED.value,
            total_amount=55.00,
        )
        db.add(order)
        db.commit()

        assert attribute_order(db, order) is None

# ==========================================================================
# Seeded examples
# ==========================================================================
class TestSeededExamples:
    def test_examples_cover_all_three_campaign_types(self, db, bootstrapped):
        from app.services.seed_automations import seed_automations

        seed_automations(db)
        kinds = set(
            db.execute(
                select(Automation.kind).where(
                    Automation.name.in_(
                        ["Weekly win-back", "Second-order series", "Reorder nudge"]
                    )
                )
            ).scalars()
        )
        assert kinds == {"COHORT_BULK", "SEQUENCE", "NUDGE"}

    def test_every_seeded_automation_is_an_unapproved_draft(self, db, bootstrapped):
        """Seed data must not be able to start texting customers by itself."""
        from app.services.seed_automations import EXAMPLES, seed_automations

        seed_automations(db)
        rows = (
            db.execute(
                select(Automation).where(
                    Automation.name.in_([spec["name"] for spec in EXAMPLES])
                )
            )
            .scalars()
            .all()
        )
        assert rows
        for row in rows:
            assert row.status == AutomationStatus.DRAFT.value
            assert row.require_approval is True
            assert row.approved_at is None
            assert row.next_run_at is None

    def test_seeding_twice_creates_nothing_new(self, db, bootstrapped):
        from app.services.seed_automations import seed_automations

        seed_automations(db)
        second = seed_automations(db)
        assert second["created"] == []
        assert second["skipped"]

    def test_the_seeded_sequence_uses_day_offsets(self, db, bootstrapped):
        from app.models.entities import AutomationStep
        from app.services.seed_automations import seed_automations

        seed_automations(db)
        automation = db.execute(
            select(Automation).where(Automation.name == "Second-order series")
        ).scalar_one()
        offsets = (
            db.execute(
                select(AutomationStep.offset_days)
                .where(AutomationStep.automation_id == automation.id)
                .order_by(AutomationStep.position)
            )
            .scalars()
            .all()
        )
        assert list(offsets) == [0, 7, 14]

    def test_seeded_copy_carries_an_opt_out_instruction(self, db, bootstrapped):
        from app.models.entities import AutomationStep
        from app.services.seed_automations import seed_automations

        seed_automations(db)
        bodies = db.execute(select(AutomationStep.message_template)).scalars().all()
        assert bodies
        for body in bodies:
            assert "STOP" in body
