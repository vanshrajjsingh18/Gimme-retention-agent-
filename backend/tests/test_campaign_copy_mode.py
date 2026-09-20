"""Who writes a campaign's copy, and whether the send agrees.

A campaign has two honest ways to produce a message: send the copy somebody
wrote, or draft each recipient's own at send time. It used to have neither.
Drafting was a parameter on the send call that defaulted to true, so the
dashboard's send button and the scheduler both replaced approved copy with a
generated message — including for campaigns whose whole body was hand-written
copy, and including the merge tags the composer invites you to insert.

The choice now lives on the campaign, because it decides what the person
approving is approving.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.campaigns.service import preview_copy, run_campaign, send_test_message
from app.core.enums import CampaignCopyMode, CampaignStatus, Channel, RecipientStatus
from app.models.base import utcnow
from app.models.entities import (
    Campaign,
    CampaignRecipient,
    Customer,
    CustomerMetrics,
    Message,
)

WRITTEN_BODY = (
    "Kia ora #name#, your #favourite_brand# is one tap away at #link#.\n"
    "Please enjoy responsibly. You must be 18 or over to purchase alcohol.\n"
    "Reply STOP to opt out."
)


@pytest.fixture()
def campaign_with_recipient(db, bootstrapped):
    """One eligible, approved recipient with a history worth drafting from."""
    stamp = utcnow().timestamp()
    customer = Customer(
        external_id=f"COPY-{stamp}",
        email="copy@example.test",
        phone="+64211234567",
        first_name="Mere",
        last_name="Hohepa",
        city="Auckland",
        age_verified=True,
        marketing_consent=True,
        email_consent=True,
        sms_consent=True,
        signup_date=utcnow() - timedelta(days=200),
    )
    db.add(customer)
    db.flush()
    db.add(
        CustomerMetrics(
            customer_id=customer.id,
            total_orders=6,
            completed_orders=6,
            last_order_at=utcnow() - timedelta(days=40),
            days_since_last_order=40,
            preferred_brands=["Steinlager"],
            preferred_categories=["Beer"],
            top_products=[{"product_name": "Steinlager Classic 12pk", "quantity": 6}],
        )
    )

    campaign = Campaign(
        name=f"Copy mode {stamp}",
        objective="RETENTION",
        channel=Channel.EMAIL.value,
        status=CampaignStatus.APPROVED.value,
        subject="A note for #name#",
        body=WRITTEN_BODY,
        copy_mode=CampaignCopyMode.WRITTEN.value,
        compliance_result={"passed": True, "blocking_count": 0, "findings": []},
    )
    db.add(campaign)
    db.flush()
    db.add(
        CampaignRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            status=RecipientStatus.ELIGIBLE.value,
        )
    )
    db.commit()
    return campaign, customer


def _sent(db, campaign, customer) -> Message:
    return db.execute(
        select(Message).where(
            Message.campaign_id == campaign.id,
            Message.customer_id == customer.id,
            Message.is_test.is_(False),
        )
    ).scalar_one()


# ==========================================================================
# What the send actually sends
# ==========================================================================
def test_written_copy_is_what_goes_out(db, campaign_with_recipient):
    """The defect this exists to stop: approving one message and sending another."""
    campaign, customer = campaign_with_recipient
    run_campaign(db, campaign, simulate_engagement=False)

    body = _sent(db, campaign, customer).body
    assert body.startswith("Kia ora Mere, your Steinlager is one tap away")
    assert "#name#" not in body


def test_drafted_copy_is_written_per_recipient(db, campaign_with_recipient):
    campaign, customer = campaign_with_recipient
    campaign.copy_mode = CampaignCopyMode.DRAFTED.value
    db.commit()

    run_campaign(db, campaign, simulate_engagement=False)

    body = _sent(db, campaign, customer).body
    assert body != WRITTEN_BODY
    assert "Mere" in body  # still that customer's message, just not this text


def test_the_send_reports_which_mode_it_used(db, campaign_with_recipient):
    campaign, _ = campaign_with_recipient
    assert run_campaign(db, campaign, simulate_engagement=False)["copy_mode"] == "WRITTEN"


def test_the_scheduler_sends_what_the_campaign_says(db, campaign_with_recipient):
    """A scheduled send takes the same path with no caller to pass a flag.

    This is where the old default did its quietest damage: nobody chose it,
    nobody saw it, and an approved campaign went out as something else.
    """
    campaign, customer = campaign_with_recipient
    campaign.status = CampaignStatus.SCHEDULED.value
    campaign.scheduled_at = utcnow() - timedelta(minutes=1)
    db.commit()

    from app.campaigns.service import due_scheduled_campaigns

    due = due_scheduled_campaigns(db)
    assert campaign.id in [c.id for c in due]
    run_campaign(db, campaign)

    assert _sent(db, campaign, customer).body.startswith("Kia ora Mere,")


# ==========================================================================
# The test send shows the real thing
# ==========================================================================
def test_a_test_send_of_written_copy_shows_the_written_copy(db, campaign_with_recipient):
    """A test send is how an operator checks their own words before sending."""
    campaign, customer = campaign_with_recipient
    result = send_test_message(
        db, campaign, to="marketing@example.test", customer_id=customer.id
    )

    assert result["body"].startswith("Kia ora Mere,")
    assert result["copy_mode"] == "WRITTEN"


def test_a_test_send_of_drafted_copy_shows_a_draft(db, campaign_with_recipient):
    campaign, customer = campaign_with_recipient
    campaign.copy_mode = CampaignCopyMode.DRAFTED.value
    db.commit()

    result = send_test_message(
        db, campaign, to="marketing@example.test", customer_id=customer.id
    )
    assert result["body"] != WRITTEN_BODY
    assert result["copy_mode"] == "DRAFTED"


# ==========================================================================
# Reading it before approving it
# ==========================================================================
def test_the_preview_shows_written_copy_filled_in(db, campaign_with_recipient):
    campaign, _ = campaign_with_recipient
    preview = preview_copy(db, campaign)

    assert preview["copy_mode"] == "WRITTEN"
    assert preview["samples"]
    for sample in preview["samples"]:
        # Each is the approved copy as that person will read it — their own
        # name and brand in it, and no token left showing.
        assert sample["body"].startswith("Kia ora ")
        assert "#name#" not in sample["body"]
        assert "#favourite_brand#" not in sample["body"]
        assert sample["validation_failed"] is False


def test_the_preview_drafts_for_a_drafted_campaign(db, campaign_with_recipient):
    campaign, _ = campaign_with_recipient
    campaign.copy_mode = CampaignCopyMode.DRAFTED.value
    db.commit()

    sample = preview_copy(db, campaign)["samples"][0]
    assert sample["body"] != WRITTEN_BODY


def test_previewing_persists_nothing(db, campaign_with_recipient):
    """Reading what would be sent must not count as having sent it."""
    campaign, _ = campaign_with_recipient
    campaign.copy_mode = CampaignCopyMode.DRAFTED.value
    db.commit()

    before = db.execute(select(Message)).scalars().all()
    preview_copy(db, campaign)
    after = db.execute(select(Message)).scalars().all()

    assert len(after) == len(before)
    assert campaign.messages_sent == 0


def test_the_preview_says_when_a_draft_would_not_send(db, campaign_with_recipient, monkeypatch):
    """A draft that fails grounding is skipped at send time, not fallen back on."""
    from app.services import messaging

    real_generate = messaging.generate_message

    def poisoned(db_session, customer, **kwargs):
        message = real_generate(db_session, customer, **kwargs)
        message.body = "Take 60% off with code FAKE99! Only 1 left in stock."
        messaging.revalidate_message(db_session, message)
        return message

    monkeypatch.setattr("app.campaigns.service.generate_message", poisoned)

    campaign, _ = campaign_with_recipient
    campaign.copy_mode = CampaignCopyMode.DRAFTED.value
    db.commit()

    sample = preview_copy(db, campaign)["samples"][0]
    assert sample["validation_failed"] is True


# ==========================================================================
# Approval follows the choice
# ==========================================================================
def test_the_compliance_report_says_the_copy_is_not_the_message(db, campaign_with_recipient):
    from app.campaigns.service import run_compliance_check

    campaign, _ = campaign_with_recipient
    campaign.status = CampaignStatus.DRAFT.value
    campaign.copy_mode = CampaignCopyMode.DRAFTED.value
    db.commit()

    report = run_compliance_check(db, campaign)
    codes = {f.code for f in report.findings}
    assert "COPY_DRAFTED_PER_RECIPIENT" in codes
    # An advisory, not a veto: the fallback still has to be compliant, and so
    # does every draft, which is checked again as it is written.
    assert report.passed is True


def test_written_copy_gets_no_drafting_notice(db, campaign_with_recipient):
    from app.campaigns.service import run_compliance_check

    campaign, _ = campaign_with_recipient
    campaign.status = CampaignStatus.DRAFT.value
    db.commit()

    report = run_compliance_check(db, campaign)
    assert "COPY_DRAFTED_PER_RECIPIENT" not in {f.code for f in report.findings}


def test_changing_the_mode_withdraws_approval(client, auth_headers, db, campaign_with_recipient):
    """Approval was given for a message. Switching who writes it is a new message."""
    campaign, _ = campaign_with_recipient
    campaign.status = CampaignStatus.AWAITING_APPROVAL.value
    db.commit()

    response = client.patch(
        f"/api/v1/campaigns/{campaign.id}",
        json={"copy_mode": "DRAFTED"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["copy_mode"] == "DRAFTED"
    assert response.json()["status"] == "DRAFT"
    assert response.json()["approved_at"] is None


# ==========================================================================
# The old flag
# ==========================================================================
def test_the_old_send_time_flag_is_refused_rather_than_ignored(
    client, auth_headers, campaign_with_recipient
):
    """Ignoring it would be the same defect in reverse: asked for one thing, given another."""
    campaign, _ = campaign_with_recipient
    response = client.post(
        f"/api/v1/campaigns/{campaign.id}/run",
        json={"generate_per_customer": True, "simulate_engagement": False},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "copy_mode" in response.json()["detail"]


def test_a_send_without_the_flag_still_works(client, auth_headers, campaign_with_recipient):
    campaign, _ = campaign_with_recipient
    response = client.post(
        f"/api/v1/campaigns/{campaign.id}/run",
        json={"simulate_engagement": False},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["copy_mode"] == "WRITTEN"


# ==========================================================================
# Databases that predate the column
# ==========================================================================
def test_campaigns_that_predate_the_column_keep_drafting(tmp_path, monkeypatch):
    """An existing approved campaign must not silently change what it sends.

    New campaigns default to WRITTEN. Campaigns that already exist have been
    drafting every time they sent, so they are marked for what they do.
    """
    from sqlalchemy import create_engine, text

    from app.services import bootstrap

    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE campaigns (id INTEGER PRIMARY KEY, copy_mode TEXT)"))
        connection.execute(text("INSERT INTO campaigns (id, copy_mode) VALUES (1, 'WRITTEN')"))
        connection.execute(text("INSERT INTO campaigns (id, copy_mode) VALUES (2, 'WRITTEN')"))

    monkeypatch.setattr(bootstrap, "engine", engine)
    bootstrap._preserve_existing_copy_mode({"campaigns": ["copy_mode"]})

    with engine.begin() as connection:
        modes = connection.execute(text("SELECT copy_mode FROM campaigns")).scalars().all()
    assert modes == ["DRAFTED", "DRAFTED"]


def test_nothing_is_rewritten_when_the_column_was_already_there(tmp_path, monkeypatch):
    """The backfill is a one-off, not something that runs on every startup."""
    from sqlalchemy import create_engine, text

    from app.services import bootstrap

    engine = create_engine(f"sqlite:///{tmp_path/'current.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE campaigns (id INTEGER PRIMARY KEY, copy_mode TEXT)"))
        connection.execute(text("INSERT INTO campaigns (id, copy_mode) VALUES (1, 'WRITTEN')"))

    monkeypatch.setattr(bootstrap, "engine", engine)
    bootstrap._preserve_existing_copy_mode({})

    with engine.begin() as connection:
        assert connection.execute(text("SELECT copy_mode FROM campaigns")).scalar_one() == "WRITTEN"
