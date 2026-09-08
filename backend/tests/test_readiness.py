"""Go-live readiness: what has to be true before this texts real people."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.enums import AutomationStatus, Channel
from app.models.entities import Automation, Customer, Integration
from app.services.readiness import integration_readiness


@pytest.fixture()
def sms(db, seeded):
    integration = db.execute(
        select(Integration).where(Integration.channel == Channel.SMS.value)
    ).scalar_one()
    before = (integration.mode, dict(integration.credentials or {}), integration.status)
    integration.credentials = {
        "auth_token": "token",
        "sender": "GIMME",
        "webhook_secret": "shh",
    }
    integration.status = "OK"
    integration.status_message = "Connected."
    db.commit()
    yield integration
    integration.mode, integration.credentials, integration.status = before
    db.commit()


def by_key(report):
    return {check.key: check for check in report.checks}


def test_a_fully_configured_integration_reports_ready(db, sms):
    report = integration_readiness(db, sms)
    assert report.ready is True
    assert report.blockers == []


def test_a_missing_webhook_secret_blocks_go_live(db, sms):
    sms.credentials = {"auth_token": "token", "sender": "GIMME"}
    db.commit()

    report = integration_readiness(db, sms)
    assert report.ready is False
    check = by_key(report)["webhook_secret"]
    assert check.passed is False and check.blocking is True
    # The remedy has to say why it matters, not just what to type.
    assert "START" in check.remedy


def test_missing_credentials_block_and_name_what_is_missing(db, sms):
    sms.credentials = {"webhook_secret": "shh"}
    db.commit()

    check = by_key(integration_readiness(db, sms))["credentials"]
    assert check.passed is False
    assert "auth_token" in check.detail and "sender" in check.detail


def test_an_untested_connection_blocks_go_live(db, sms):
    sms.status = "NOT_CHECKED"
    db.commit()
    assert by_key(integration_readiness(db, sms))["connection"].passed is False


def test_an_over_long_alphanumeric_sender_is_caught_before_the_carrier_rejects_it(db, sms):
    sms.credentials = {**sms.credentials, "sender": "GIMMEDELIVERYNZ"}
    db.commit()

    check = by_key(integration_readiness(db, sms))["sender"]
    assert check.passed is False
    assert "11" in check.detail


def test_an_alphanumeric_sender_is_flagged_as_unable_to_receive_replies(db, sms):
    # It passes — it is a legitimate configuration — but an operator relying on
    # STOP replies needs to know they will not arrive.
    check = by_key(integration_readiness(db, sms))["sender"]
    assert check.passed is True
    assert "cannot receive replies" in check.detail


def test_a_numeric_sender_reports_no_such_caveat(db, sms):
    sms.credentials = {**sms.credentials, "sender": "+6421000000"}
    db.commit()
    check = by_key(integration_readiness(db, sms))["sender"]
    assert check.passed is True
    assert "cannot receive replies" not in check.detail


def test_unreachable_numbers_are_counted_rather_than_assumed_fine(db, sms):
    check = by_key(integration_readiness(db, sms))["reachable_audience"]
    # The seeded data is all valid NZ mobiles, so this is a clean baseline.
    assert check.passed is True
    assert "can reach" in check.detail


def test_a_list_full_of_landlines_is_reported_as_a_data_problem(db, sms):
    consenting = db.execute(
        select(Customer).where(
            Customer.sms_consent.is_(True), Customer.is_suppressed.is_(False)
        )
    ).scalars().all()
    original = [(c.id, c.phone) for c in consenting]
    for customer in consenting:
        customer.phone = "093661234"  # a real Auckland landline
    db.commit()
    try:
        check = by_key(integration_readiness(db, sms))["reachable_audience"]
        assert check.passed is False
        # Advisory, not a veto: the sends are skipped safely either way.
        assert check.blocking is False
        assert "E.164" in check.remedy
    finally:
        for customer_id, phone in original:
            db.get(Customer, customer_id).phone = phone
        db.commit()


def test_an_active_but_unapproved_automation_blocks_go_live(db, sms, make_unapproved_active):
    check = by_key(integration_readiness(db, sms))["unapproved_active"]
    assert check.passed is False
    assert check.blocking is True
    assert make_unapproved_active.name in check.detail


@pytest.fixture()
def make_unapproved_active(db, seeded):
    """An automation that is live-but-unapproved: harmless in mock, not in live."""
    automation = Automation(
        name="Unapproved and active",
        kind="COHORT_BULK",
        channel=Channel.SMS.value,
        status=AutomationStatus.ACTIVE.value,
        require_approval=True,
        approved_at=None,
        message_template="Hi. Reply STOP to opt out.",
    )
    db.add(automation)
    db.commit()
    yield automation
    db.delete(automation)
    db.commit()


def test_the_report_says_what_would_start_sending_immediately(db, sms):
    check = by_key(integration_readiness(db, sms))["pending_volume"]
    assert check.blocking is False
    assert "automation" in check.detail


def test_a_send_window_that_two_places_disagree_about_is_surfaced(db, sms):
    """The mismatch that started as 21:00 in compliance and 19:00 in settings."""
    from app.core.enums import ComplianceSeverity
    from app.models.entities import ComplianceRule
    from sqlalchemy import select as sa_select

    rule = db.execute(
        sa_select(ComplianceRule).where(ComplianceRule.code == "QUIET_HOURS")
    ).scalar_one()
    before = dict(rule.config or {})
    rule.config = {**before, "start": "21:00"}
    db.commit()
    try:
        check = by_key(integration_readiness(db, sms))["quiet_hours"]
        assert check.passed is False
        assert "21:00" in check.detail and "19:00" in check.detail
        # Advisory: the tighter window still wins for automations, so nothing
        # unsafe happens — but a campaign can send in the gap.
        assert check.blocking is False
        assert "campaign can send in the gap" in check.detail
    finally:
        rule.config = before
        db.commit()


def test_matching_windows_report_one_number(db, sms):
    check = by_key(integration_readiness(db, sms))["quiet_hours"]
    assert check.passed is True
    assert "both the campaign and automation paths" in check.detail
