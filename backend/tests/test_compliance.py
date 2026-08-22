from __future__ import annotations

from datetime import datetime, time

import pytest

from app.compliance.engine import (
    ComplianceConfig,
    RecipientView,
    check_campaign,
    check_content,
    check_recipient,
    check_targeting,
    in_quiet_hours,
)
from app.core.enums import Channel, ComplianceSeverity, RecipientStatus

RESPONSIBLE = "Please enjoy responsibly."
AGE_STATEMENT = "You must be 18 or over to purchase alcohol."


def config(**overrides) -> ComplianceConfig:
    base = {
        "allowed_coupon_codes": ["GIMME10"],
        "allowed_promotions": ["10% off your next order", "Free delivery on orders over $80"],
        "delivery_promise": "Delivered in 60 minutes across central Auckland",
        "responsible_drinking_statement": RESPONSIBLE,
        "age_restriction_statement": AGE_STATEMENT,
    }
    base.update(overrides)
    return ComplianceConfig(**base)


def compliant_email_body(extra: str = "") -> str:
    return f"Hi Sam, your usual Steinlager is ready to reorder. {extra}\n\n{RESPONSIBLE}\n{AGE_STATEMENT}"


def recipient(**overrides) -> RecipientView:
    base = {
        "customer_id": 1,
        "age": 35,
        "age_verified": True,
        "marketing_consent": True,
        "email_consent": True,
        "sms_consent": True,
        "whatsapp_consent": True,
        "email": "sam@example.com",
        "phone": "+64211234567",
    }
    base.update(overrides)
    return RecipientView(**base)


def codes(findings) -> set[str]:
    return {f.code for f in findings}


# ==========================================================================
# Recipient eligibility
# ==========================================================================
def test_eligible_recipient_passes():
    status, reason = check_recipient(recipient(), Channel.EMAIL, config())
    assert status == RecipientStatus.ELIGIBLE
    assert reason is None


def test_unverified_age_blocked():
    status, reason = check_recipient(
        recipient(age_verified=False), Channel.EMAIL, config()
    )
    assert status == RecipientStatus.EXCLUDED_AGE
    assert "Age has not been verified" in reason


def test_underage_customer_blocked():
    status, _ = check_recipient(recipient(age=16), Channel.EMAIL, config())
    assert status == RecipientStatus.EXCLUDED_AGE


def test_suppressed_customer_blocked():
    status, reason = check_recipient(recipient(is_suppressed=True), Channel.EMAIL, config())
    assert status == RecipientStatus.EXCLUDED_SUPPRESSED
    assert "suppression list" in reason


def test_channel_suppression_blocks_only_that_channel():
    r = recipient(suppressed_channels={"SMS"})
    assert check_recipient(r, Channel.SMS, config())[0] == RecipientStatus.EXCLUDED_SUPPRESSED
    assert check_recipient(r, Channel.EMAIL, config())[0] == RecipientStatus.ELIGIBLE


def test_all_channel_suppression_blocks_everything():
    r = recipient(suppressed_channels={"ALL"})
    for channel in (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP):
        assert check_recipient(r, channel, config())[0] == RecipientStatus.EXCLUDED_SUPPRESSED


def test_no_marketing_consent_blocked():
    status, reason = check_recipient(
        recipient(marketing_consent=False), Channel.EMAIL, config()
    )
    assert status == RecipientStatus.EXCLUDED_NO_CONSENT
    assert "No marketing consent" in reason


def test_channel_consent_enforced_independently():
    r = recipient(sms_consent=False)
    assert check_recipient(r, Channel.EMAIL, config())[0] == RecipientStatus.ELIGIBLE
    status, reason = check_recipient(r, Channel.SMS, config())
    assert status == RecipientStatus.EXCLUDED_NO_CONSENT
    assert "SMS channel consent" in reason


def test_missing_contact_details_blocked():
    assert (
        check_recipient(recipient(email=None), Channel.EMAIL, config())[0]
        == RecipientStatus.EXCLUDED_MISSING_CONTACT
    )
    assert (
        check_recipient(recipient(phone=None), Channel.SMS, config())[0]
        == RecipientStatus.EXCLUDED_MISSING_CONTACT
    )


def test_30_day_frequency_cap_enforced():
    status, reason = check_recipient(
        recipient(messages_last_30d=4), Channel.EMAIL, config(frequency_cap_30d=4)
    )
    assert status == RecipientStatus.EXCLUDED_FREQUENCY_CAP
    assert "30 days" in reason


def test_7_day_frequency_cap_enforced():
    status, reason = check_recipient(
        recipient(messages_last_7d=2), Channel.EMAIL, config(frequency_cap_7d=2)
    )
    assert status == RecipientStatus.EXCLUDED_FREQUENCY_CAP
    assert "7 days" in reason


def test_under_frequency_cap_passes():
    status, _ = check_recipient(
        recipient(messages_last_30d=3, messages_last_7d=1), Channel.EMAIL, config()
    )
    assert status == RecipientStatus.ELIGIBLE


def local_config(**overrides) -> ComplianceConfig:
    """Config that reads send times as wall-clock, for testing the rule itself.

    The timezone conversion is exercised separately below; these cases are
    about the window comparison, so they skip it.
    """
    overrides.setdefault("use_business_timezone", False)
    return config(**overrides)


def test_quiet_hours_block_sms_but_not_email():
    late = datetime(2025, 6, 1, 23, 30)
    assert (
        check_recipient(recipient(), Channel.SMS, local_config(), send_time=late)[0]
        == RecipientStatus.EXCLUDED_QUIET_HOURS
    )
    assert (
        check_recipient(recipient(), Channel.EMAIL, local_config(), send_time=late)[0]
        == RecipientStatus.ELIGIBLE
    )


def test_quiet_hours_wrap_midnight():
    cfg = local_config()
    assert in_quiet_hours(datetime(2025, 6, 1, 22, 0), cfg)
    assert in_quiet_hours(datetime(2025, 6, 1, 3, 0), cfg)
    assert not in_quiet_hours(datetime(2025, 6, 1, 12, 0), cfg)
    assert not in_quiet_hours(datetime(2025, 6, 1, 18, 59), cfg)


def test_quiet_hours_start_is_closed_at_seven_pm():
    """The allowed window is 09:00-19:00 local, so 19:00 itself is blocked."""
    cfg = local_config()
    assert in_quiet_hours(datetime(2025, 6, 1, 19, 0), cfg)
    assert not in_quiet_hours(datetime(2025, 6, 1, 9, 0), cfg)


def test_non_wrapping_quiet_hours_supported():
    cfg = local_config(quiet_hours_start=time(1, 0), quiet_hours_end=time(6, 0))
    assert in_quiet_hours(datetime(2025, 6, 1, 3, 0), cfg)
    assert not in_quiet_hours(datetime(2025, 6, 1, 23, 0), cfg)


def test_daytime_sms_allowed():
    status, _ = check_recipient(
        recipient(), Channel.SMS, local_config(), send_time=datetime(2025, 6, 1, 14, 0)
    )
    assert status == RecipientStatus.ELIGIBLE


def test_quiet_hours_judged_in_business_timezone():
    """Stored times are naive UTC; the customer's clock is what matters.

    A job running at 02:00 UTC in June is reaching an Auckland customer at
    14:00 their time — fine. The same job at 09:00 UTC reaches them at
    21:00, which is not.
    """
    cfg = config()  # use_business_timezone left on
    assert not in_quiet_hours(datetime(2025, 6, 1, 2, 0), cfg)
    assert in_quiet_hours(datetime(2025, 6, 1, 9, 0), cfg)

    blocked, reason = check_recipient(
        recipient(), Channel.SMS, cfg, send_time=datetime(2025, 6, 1, 9, 0)
    )
    assert blocked == RecipientStatus.EXCLUDED_QUIET_HOURS
    # The reason quotes the customer's local time, not the UTC one.
    assert "21:00 local" in reason


def test_age_gate_takes_precedence_over_consent():
    """Reported reason must be the most severe rule, not the first checked."""
    r = recipient(age_verified=False, marketing_consent=False, is_suppressed=True)
    assert check_recipient(r, Channel.EMAIL, config())[0] == RecipientStatus.EXCLUDED_AGE


# ==========================================================================
# Prohibited content
# ==========================================================================
@pytest.mark.parametrize(
    "text,expected_code",
    [
        ("A glass of red is good for your heart.", "HEALTH_CLAIM"),
        ("Wine has real health benefits.", "HEALTH_CLAIM"),
        ("Drown your sorrows with our new IPA.", "EMOTIONAL_WELLBEING_CLAIM"),
        ("Had a rough week? Feel better with a drink.", "EMOTIONAL_WELLBEING_CLAIM"),
        ("Be the life of the party this weekend.", "SOCIAL_SUCCESS_CLAIM"),
        ("Impress your friends with a bottle of this.", "SOCIAL_SUCCESS_CLAIM"),
        ("This one will make you irresistible.", "SEXUAL_SUCCESS_CLAIM"),
        ("Close the deal over a drink.", "PROFESSIONAL_SUCCESS_CLAIM"),
        ("Get wasted this Friday.", "EXCESSIVE_CONSUMPTION"),
        ("Bottoms up!", "EXCESSIVE_CONSUMPTION"),
        ("Drink until you drop.", "EXCESSIVE_CONSUMPTION"),
        ("Perfect for the kids' birthday party.", "UNDERAGE_APPEAL"),
        ("Grab one for the road.", "DRINK_DRIVING"),
    ],
)
def test_prohibited_claims_are_blocking(text, expected_code):
    findings = check_content(text, config(), is_full_message=False)
    assert expected_code in codes(findings)
    blocking = [f for f in findings if f.code == expected_code]
    assert blocking[0].blocks_send
    assert blocking[0].severity == ComplianceSeverity.CRITICAL
    assert blocking[0].excerpt


def test_ordinary_marketing_copy_is_not_flagged():
    clean = (
        "Hi Sam, your usual Steinlager Classic is back in your basket. "
        "We can have it at your door this evening. Cheers from the GIMME team."
    )
    findings = check_content(clean, config(), is_full_message=False)
    assert not [f for f in findings if f.blocks_send], codes(findings)


def test_brand_configured_prohibited_phrase_blocks():
    cfg = config(extra_prohibited_claims=["cheapest in town"])
    findings = check_content("We are the cheapest in town.", cfg, is_full_message=False)
    assert "BRAND_PROHIBITED_CLAIM" in codes(findings)


def test_unverified_coupon_code_blocks():
    findings = check_content("Use code SAVE50 today.", config(), is_full_message=False)
    assert "UNVERIFIED_COUPON_CODE" in codes(findings)


def test_verified_coupon_code_allowed():
    findings = check_content("Use code GIMME10 today.", config(), is_full_message=False)
    assert "UNVERIFIED_COUPON_CODE" not in codes(findings)


def test_coupon_pattern_does_not_flag_ordinary_words():
    findings = check_content(
        "GIMME delivers across Auckland. Cheers, The GIMME Team.", config(), is_full_message=False
    )
    assert "UNVERIFIED_COUPON_CODE" not in codes(findings)


def test_unapproved_discount_blocks():
    findings = check_content("Take 40% off everything!", config(), is_full_message=False)
    assert "UNVERIFIED_PROMOTION" in codes(findings)


def test_approved_discount_allowed():
    findings = check_content("Here is 10% off your next order.", config(), is_full_message=False)
    assert "UNVERIFIED_PROMOTION" not in codes(findings)


def test_invented_free_delivery_blocks_when_not_approved():
    cfg = config(allowed_promotions=["10% off your next order"])
    findings = check_content("Enjoy free delivery on us!", cfg, is_full_message=False)
    assert "UNVERIFIED_PROMOTION" in codes(findings)


def test_unverified_delivery_time_claim_blocks():
    findings = check_content("We deliver in 20 minutes.", config(), is_full_message=False)
    assert "UNVERIFIED_DELIVERY_CLAIM" in codes(findings)


def test_delivery_claim_matching_promise_allowed():
    findings = check_content(
        "We deliver in 60 minutes across central Auckland.", config(), is_full_message=False
    )
    assert "UNVERIFIED_DELIVERY_CLAIM" not in codes(findings)


def test_stock_claims_block():
    for text in ("Only 3 left in stock!", "While stocks last.", "Selling out fast."):
        findings = check_content(text, config(), is_full_message=False)
        assert "UNVERIFIED_STOCK_CLAIM" in codes(findings), text


def test_unresolved_placeholder_blocks():
    for text in ("Hi {{first_name}}, welcome.", "Hi [FIRST_NAME], welcome."):
        findings = check_content(text, config(), is_full_message=False)
        assert "UNRESOLVED_PLACEHOLDER" in codes(findings), text


def test_missing_responsible_drinking_statement_blocks_email():
    findings = check_content(
        "Hi Sam, your usual is ready to reorder.", config(), channel=Channel.EMAIL
    )
    assert "MISSING_RESPONSIBLE_DRINKING" in codes(findings)


def test_present_responsible_drinking_statement_passes():
    findings = check_content(compliant_email_body(), config(), channel=Channel.EMAIL)
    assert "MISSING_RESPONSIBLE_DRINKING" not in codes(findings)


def test_sms_exempt_from_mandatory_statements():
    findings = check_content("Your usual is ready to reorder.", config(), channel=Channel.SMS)
    assert "MISSING_RESPONSIBLE_DRINKING" not in codes(findings)


def test_missing_age_statement_warns_but_does_not_block():
    body = f"Hi Sam, your usual is ready.\n\n{RESPONSIBLE}"
    findings = check_content(body, config(), channel=Channel.EMAIL)
    age = [f for f in findings if f.code == "MISSING_AGE_STATEMENT"]
    assert age and not age[0].blocks_send
    assert age[0].severity == ComplianceSeverity.WARNING


def test_disabled_rule_is_skipped():
    cfg = config(disabled_rules={"UNVERIFIED_STOCK_CLAIM"})
    findings = check_content("While stocks last.", cfg, is_full_message=False)
    assert "UNVERIFIED_STOCK_CLAIM" not in codes(findings)


# ==========================================================================
# Targeting
# ==========================================================================
def test_lifecycle_targeting_is_allowed():
    rule = {"field": "lifecycle_stage", "operator": "in", "value": ["AT_RISK"]}
    assert check_targeting(segment_rule=rule, objective="RETENTION", config=config()) == []


def test_discount_dependency_targeting_warns():
    rule = {"field": "discount_dependency", "operator": "gte", "value": 0.8}
    findings = check_targeting(segment_rule=rule, objective="RETENTION", config=config())
    assert "VULNERABILITY_TARGETING" in codes(findings)
    assert not findings[0].blocks_send


def test_heavy_consumption_targeting_blocks():
    rule = {"field": "purchase_frequency_per_month", "operator": "gte", "value": 15}
    findings = check_targeting(segment_rule=rule, objective="REORDER", config=config())
    assert "HEAVY_CONSUMPTION_TARGETING" in codes(findings)
    assert findings[0].blocks_send


def test_targeting_walks_nested_groups():
    rule = {
        "op": "AND",
        "conditions": [
            {"field": "lifecycle_stage", "operator": "eq", "value": "REGULAR"},
            {
                "op": "OR",
                "conditions": [
                    {"field": "purchase_frequency_per_month", "operator": "gt", "value": 20}
                ],
            },
        ],
    }
    findings = check_targeting(segment_rule=rule, objective="REORDER", config=config())
    assert "HEAVY_CONSUMPTION_TARGETING" in codes(findings)


# ==========================================================================
# Full campaign report
# ==========================================================================
def test_clean_campaign_passes_once_approved():
    report = check_campaign(
        subject="Your usual is ready",
        body=compliant_email_body(),
        channel=Channel.EMAIL,
        objective="REORDER",
        segment_rule={"field": "lifecycle_stage", "operator": "eq", "value": "REGULAR"},
        config=config(),
        approved_by_human=True,
    )
    assert report.passed, [f.as_dict() for f in report.blocking_findings]


def test_unapproved_campaign_reports_info_but_still_passes_content_checks():
    report = check_campaign(
        subject="Your usual is ready",
        body=compliant_email_body(),
        channel=Channel.EMAIL,
        objective="REORDER",
        segment_rule=None,
        config=config(),
        approved_by_human=False,
    )
    assert report.passed
    assert "REQUIRES_HUMAN_APPROVAL" in codes(report.findings)


def test_campaign_with_prohibited_claim_is_blocked():
    report = check_campaign(
        subject="Good for your heart",
        body=compliant_email_body("A daily glass is good for your heart."),
        channel=Channel.EMAIL,
        objective="RETENTION",
        segment_rule=None,
        config=config(),
        approved_by_human=True,
    )
    assert not report.passed
    assert "HEALTH_CLAIM" in codes(report.blocking_findings)


def test_empty_body_blocked():
    report = check_campaign(
        subject="Hello",
        body="   ",
        channel=Channel.EMAIL,
        objective="RETENTION",
        segment_rule=None,
        config=config(),
        approved_by_human=True,
    )
    assert not report.passed
    assert "EMPTY_MESSAGE" in codes(report.blocking_findings)


def test_empty_subject_blocked_for_email_only():
    email = check_campaign(
        subject="",
        body=compliant_email_body(),
        channel=Channel.EMAIL,
        objective="RETENTION",
        segment_rule=None,
        config=config(),
        approved_by_human=True,
    )
    assert "EMPTY_SUBJECT" in codes(email.blocking_findings)

    sms = check_campaign(
        subject="",
        body="Your usual is ready to reorder.",
        channel=Channel.SMS,
        objective="RETENTION",
        segment_rule=None,
        config=config(),
        approved_by_human=True,
    )
    assert "EMPTY_SUBJECT" not in codes(sms.findings)


def test_report_serializes_for_persistence():
    report = check_campaign(
        subject="Take 40% off",
        body="Take 40% off everything with code SAVE50!",
        channel=Channel.EMAIL,
        objective="RETENTION",
        segment_rule=None,
        config=config(),
        approved_by_human=True,
    )
    data = report.as_dict()
    assert data["passed"] is False
    assert data["blocking_count"] >= 2
    assert all({"code", "message", "severity", "blocks_send"} <= set(f) for f in data["findings"])
    assert data["checked_at"]
