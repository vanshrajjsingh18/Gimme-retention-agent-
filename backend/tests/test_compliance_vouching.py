"""What a human reviewer may sign off, and what they may not.

The compliance engine holds no pricing data, no catalogue and no coupon list
beyond the handful in Brand settings. So when hand-written copy says "$10 off
with FIRST10", the engine is not detecting a lie — it is reporting that it has
nothing to check the claim against. Treating that as a veto makes the engine's
ignorance outrank the knowledge of the person who typed it, and the only way
past it is to stop writing real offers.

These tests draw the line: claims the engine cannot verify are a reviewer's to
confirm; claims it can judge are not.
"""
from __future__ import annotations

import pytest

from app.campaigns.service import CampaignError, approve_campaign, run_compliance_check
from app.compliance.engine import COUPON_PATTERN, VOUCHABLE_RULES
from app.core.enums import CampaignCopyMode, CampaignStatus, Channel
from app.models.base import utcnow
from app.models.entities import AuditLog, Campaign
from sqlalchemy import select

REAL_OFFER = (
    "Hi #first_name#, GIMME's got $10 off your next order with FIRST10. "
    "Delivery from $1. Please enjoy responsibly. Reply STOP to opt out."
)


def _campaign(db, body: str, status=CampaignStatus.AWAITING_APPROVAL) -> Campaign:
    campaign = Campaign(
        name=f"Vouching {utcnow().timestamp()}",
        objective="REORDER",
        channel=Channel.SMS.value,
        status=status.value,
        body=body,
        copy_mode=CampaignCopyMode.WRITTEN.value,
    )
    db.add(campaign)
    db.commit()
    return campaign


def test_the_company_name_is_not_a_coupon_code():
    """The false positive that made this urgent.

    The digit lookahead scanned the whole rest of the message rather than the
    token, so "GIMME" was reported as an unverified coupon code in every
    message that also mentioned a number — which is most of them. The brand's
    own name was blocking its own campaigns.
    """
    assert COUPON_PATTERN.findall("GIMME has 10% off") == []
    assert COUPON_PATTERN.findall("GIMME is here") == []
    # And a code that really is one is still caught.
    assert COUPON_PATTERN.findall("use FIRST10 today") == ["FIRST10"]
    assert COUPON_PATTERN.findall("use SUMMER24 today") == ["SUMMER24"]


def test_an_unverifiable_claim_is_flagged_but_a_reviewer_can_confirm_it(db, bootstrapped):
    """The whole point: "you cannot send this" becomes "confirm it and you can".

    The finding does not disappear. It stays in the report at full severity,
    now carrying the name of the person who took responsibility — which is a
    better record than the claim never having been questioned.
    """
    campaign = _campaign(db, REAL_OFFER)
    report = run_compliance_check(db, campaign)

    assert not report.passed, "a real offer should still be questioned"
    codes = {f.code for f in report.needs_vouching}
    assert "UNVERIFIED_COUPON_CODE" in codes
    assert "UNVERIFIED_PROMOTION" in codes
    assert "UNVERIFIED_PRICE_CLAIM" in codes
    assert not report.hard_blocking, "nothing here is beyond a reviewer's authority"

    campaign.status = CampaignStatus.AWAITING_APPROVAL.value
    db.commit()
    approve_campaign(
        db, campaign, user_id=1, vouch_for=sorted(codes), reviewer="Alex Taylor"
    )

    assert campaign.status == CampaignStatus.APPROVED.value
    result = campaign.compliance_result
    assert result["passed"] is True
    vouched = [f for f in result["findings"] if f["vouched_by"]]
    assert {f["code"] for f in vouched} == codes
    assert all("Confirmed by Alex Taylor" in f["message"] for f in vouched)
    # Still reported, not erased — the record is that somebody vouched.
    assert all(f["severity"] == "CRITICAL" for f in vouched)


def test_the_confirmation_is_written_to_the_audit_log(db, bootstrapped):
    """Who said the coupon was real, and what the copy said at the time.

    A sign-off that leaves no trace is indistinguishable from the rule never
    having existed.
    """
    campaign = _campaign(db, REAL_OFFER)
    run_compliance_check(db, campaign)
    campaign.status = CampaignStatus.AWAITING_APPROVAL.value
    db.commit()

    approve_campaign(
        db,
        campaign,
        user_id=1,
        vouch_for=[
            "UNVERIFIED_COUPON_CODE",
            "UNVERIFIED_PROMOTION",
            "UNVERIFIED_PRICE_CLAIM",
        ],
        reviewer="Alex Taylor",
    )

    entry = db.execute(
        select(AuditLog)
        .where(AuditLog.action == "COMPLIANCE_VOUCHED", AuditLog.entity_id == str(campaign.id))
        .order_by(AuditLog.id.desc())
    ).scalars().first()

    assert entry is not None
    assert entry.actor == "1"
    assert entry.detail["reviewer"] == "Alex Taylor"
    assert "UNVERIFIED_COUPON_CODE" in entry.detail["codes"]
    assert "FIRST10" in entry.detail["body"], "the copy as it stood is part of the record"


def test_confirming_some_findings_does_not_clear_the_rest(db, bootstrapped):
    """Sign-off is per claim, not a blanket override.

    Confirming the coupon code says nothing about the price, and a reviewer
    who ticks one box should not find they have waved through three.
    """
    campaign = _campaign(db, REAL_OFFER)
    run_compliance_check(db, campaign)
    campaign.status = CampaignStatus.AWAITING_APPROVAL.value
    db.commit()

    with pytest.raises(CampaignError) as raised:
        approve_campaign(
            db, campaign, user_id=1, vouch_for=["UNVERIFIED_COUPON_CODE"], reviewer="Alex"
        )

    message = str(raised.value)
    assert "UNVERIFIED_PRICE_CLAIM" in message
    assert campaign.status != CampaignStatus.APPROVED.value


def test_a_reviewer_cannot_sign_off_a_prohibited_claim(db, bootstrapped):
    """The line. Some findings are not a matter of opinion.

    A health claim in alcohol marketing is prohibited whoever approves it, and
    a merge tag that cannot be filled will be delivered as raw text however
    confident the reviewer is. Neither becomes acceptable by agreement.
    """
    campaign = _campaign(db, "Hi #first_name#, a beer a day is good for your heart. Reply STOP to opt out.")
    report = run_compliance_check(db, campaign)

    assert report.hard_blocking, "a health claim must stay blocking"
    assert "HEALTH_CLAIM" in {f.code for f in report.hard_blocking}
    assert "HEALTH_CLAIM" not in VOUCHABLE_RULES

    campaign.status = CampaignStatus.AWAITING_APPROVAL.value
    db.commit()
    with pytest.raises(CampaignError) as raised:
        approve_campaign(db, campaign, user_id=1, vouch_for=["HEALTH_CLAIM"])
    assert "cannot be signed off" in str(raised.value)


def test_a_confirmation_survives_a_re_check(db, bootstrapped):
    """Re-running the check must not quietly revoke the sign-off.

    Otherwise an approved campaign becomes blocked again the next time
    anything touches it, and nobody can tell why.
    """
    campaign = _campaign(db, REAL_OFFER)
    report = run_compliance_check(db, campaign)
    campaign.status = CampaignStatus.AWAITING_APPROVAL.value
    db.commit()
    approve_campaign(
        db,
        campaign,
        user_id=1,
        vouch_for=sorted({f.code for f in report.needs_vouching}),
        reviewer="Alex Taylor",
    )

    again = run_compliance_check(db, campaign)
    assert again.passed, "the sign-off did not survive a re-check"


def test_the_report_says_which_findings_a_reviewer_could_clear(db, bootstrapped):
    """So the screen can offer the button rather than just refusing."""
    campaign = _campaign(db, REAL_OFFER)
    payload = run_compliance_check(db, campaign).as_dict()

    assert payload["vouchable_codes"]
    assert payload["hard_blocking_count"] == 0
    assert all(f["vouchable"] for f in payload["findings"] if f["code"] in payload["vouchable_codes"])
