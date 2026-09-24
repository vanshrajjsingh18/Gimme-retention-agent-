"""Campaign copy is written once and sent to everybody.

Which makes the merge tags in it the only thing that distinguishes one
recipient's message from another's. The send path used to hand the stored body
straight to the provider, so a campaign written as "Hi #name#" delivered
exactly that — the personalisation the composer invites you to write went out
as punctuation.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.campaigns.service import run_campaign, send_test_message
from app.core.enums import CampaignStatus, Channel, RecipientStatus
from app.models.base import utcnow
from app.models.entities import (
    Campaign,
    CampaignRecipient,
    Customer,
    CustomerMetrics,
    Message,
)


@pytest.fixture()
def templated_campaign(db, bootstrapped):
    """One eligible recipient with a purchase history, and copy using tags."""
    stamp = utcnow().timestamp()
    customer = Customer(
        external_id=f"TAG-{stamp}",
        email="tag@example.test",
        phone="+64211234567",
        first_name="Wiremu",
        last_name="Tane",
        city="Wellington",
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
            preferred_brands=["Steinlager"],
            preferred_categories=["Beer"],
            top_products=[{"product_name": "Steinlager Classic 12pk", "quantity": 6}],
        )
    )

    campaign = Campaign(
        name=f"Tagged {stamp}",
        objective="RETENTION",
        channel=Channel.EMAIL.value,
        status=CampaignStatus.APPROVED.value,
        subject="A note for #name#",
        body="Hi #name#, #favourite_brand# is back. Order at #link#.",
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


def _sent_message(db, campaign, customer) -> Message:
    return db.execute(
        select(Message).where(
            Message.campaign_id == campaign.id, Message.customer_id == customer.id
        )
    ).scalar_one()


def test_a_send_fills_the_tags_from_the_recipient(db, templated_campaign):
    campaign, customer = templated_campaign
    run_campaign(db, campaign, simulate_engagement=False)

    message = _sent_message(db, campaign, customer)
    assert "Hi Wiremu, Steinlager is back." in message.body
    assert "#name#" not in message.body
    assert "#favourite_brand#" not in message.body


def test_the_subject_is_filled_as_well_as_the_body(db, templated_campaign):
    campaign, customer = templated_campaign
    run_campaign(db, campaign, simulate_engagement=False)

    assert _sent_message(db, campaign, customer).subject == "A note for Wiremu"


def test_copy_with_no_tags_is_delivered_exactly_as_written(db, templated_campaign):
    """The renderer tidies whitespace, so copy nobody templated must skip it."""
    campaign, customer = templated_campaign
    campaign.subject = "A note"
    campaign.body = "Hi there,  just checking in."
    db.commit()

    run_campaign(db, campaign, simulate_engagement=False)
    assert _sent_message(db, campaign, customer).body == "Hi there,  just checking in."


def test_a_test_send_with_nobody_attached_shows_stand_ins(db, templated_campaign):
    """Raw tokens in a test send read as broken rather than as a preview."""
    campaign, _ = templated_campaign
    result = send_test_message(db, campaign, to="preview@example.test")

    assert result["body"] == "Hi Sarah, Corona is back. Order at gimmedelivery.co.nz."
    assert result["subject"] == "A note for Sarah"
