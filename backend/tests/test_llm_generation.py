from __future__ import annotations

import json

import pytest

from app.compliance.engine import ComplianceConfig
from app.core.enums import Channel, NextBestAction
from app.llm.factory import get_llm_provider
from app.llm.mock_provider import MockLLMProvider
from app.llm.prompts import GroundingContext, build_system_prompt, build_user_prompt
from app.llm.validator import parse_llm_output, validate_message

RESPONSIBLE = "Please enjoy responsibly."
AGE_STATEMENT = "You must be 18 or over to purchase alcohol."


def ctx(**overrides) -> GroundingContext:
    base = {
        "customer_first_name": "Sam",
        "city": "Auckland",
        "lifecycle_stage": "AT_RISK",
        "total_completed_orders": 7,
        "lifetime_revenue": 640.0,
        "average_order_value": 91.43,
        "days_since_last_order": 62,
        "expected_cycle_days": 30.0,
        "preferred_categories": ["Beer"],
        "preferred_brands": ["Steinlager"],
        "top_products": [
            {"product_name": "Steinlager Classic 12pk", "quantity": 9},
            {"product_name": "Garage Project Hazy IPA 6pk", "quantity": 4},
        ],
        "churn_score": 58.0,
        "churn_risk_band": "HIGH",
        "churn_explanation": "62 days since their last order against an expected 30-day cycle.",
        "recommended_action": NextBestAction.REORDER_REMINDER.value,
        "recommendation_explanation": "A timely reorder reminder can pull them back.",
        "brand_voice": "Friendly, direct and genuinely useful.",
        "tone": "Warm and conversational",
        "email_signature": "The GIMME Team",
        "delivery_promise": "Delivered in 60 minutes across central Auckland",
        "responsible_drinking_statement": RESPONSIBLE,
        "age_restriction_statement": AGE_STATEMENT,
        "website": "gimmedelivery.co.nz",
        "customer_service_email": "help@gimmedelivery.co.nz",
        "campaign_objective": "REORDER",
        "channel": Channel.EMAIL.value,
    }
    base.update(overrides)
    return GroundingContext(**base)


def config(**overrides) -> ComplianceConfig:
    base = {
        "allowed_coupon_codes": [],
        "allowed_promotions": [],
        "delivery_promise": "Delivered in 60 minutes across central Auckland",
        "responsible_drinking_statement": RESPONSIBLE,
        "age_restriction_statement": AGE_STATEMENT,
    }
    base.update(overrides)
    return ComplianceConfig(**base)


def generate(context: GroundingContext, variation: str = "default") -> tuple[str, str]:
    provider = MockLLMProvider()
    system = build_system_prompt(context)
    user = build_user_prompt(context, variation=variation)
    return parse_llm_output(provider.complete(system, user).text)


def error_codes(result) -> set[str]:
    return {f.code for f in result.findings}


# ==========================================================================
# Prompt construction
# ==========================================================================
def test_system_prompt_states_grounding_rules():
    prompt = build_system_prompt(ctx())
    for phrase in [
        "ONLY state facts",
        "NEVER invent",
        "coupon or promo codes",
        "NEVER invent facts about the customer",
        "improves health",
        "excessive",
        "legal purchase age",
    ]:
        assert phrase in prompt, phrase


def test_user_prompt_contains_only_verified_facts():
    prompt = build_user_prompt(ctx())
    assert "Steinlager Classic 12pk" in prompt
    assert "AT_RISK" in prompt
    assert '"days_since_last_order": 62' in prompt
    assert "VERIFIED PROMOTIONS" in prompt


def test_user_prompt_declares_empty_promotions_when_none_configured():
    prompt = build_user_prompt(ctx())
    marketing_section = prompt.split("VERIFIED PROMOTIONS")[1]
    assert '"promotions": []' in marketing_section
    assert '"coupon_codes": []' in marketing_section


def test_user_prompt_channel_specific_instructions():
    assert "Write an email" in build_user_prompt(ctx(channel=Channel.EMAIL.value))
    assert "single SMS" in build_user_prompt(ctx(channel=Channel.SMS.value))
    assert "WhatsApp message" in build_user_prompt(ctx(channel=Channel.WHATSAPP.value))
    assert "push notification" in build_user_prompt(ctx(channel=Channel.PUSH.value))


def test_variation_instruction_included():
    prompt = build_user_prompt(ctx(), variation="shorter")
    assert "noticeably shorter" in prompt


# ==========================================================================
# Mock provider
# ==========================================================================
def test_mock_provider_returns_parseable_json():
    provider = MockLLMProvider()
    response = provider.complete(build_system_prompt(ctx()), build_user_prompt(ctx()))
    assert response.is_mock
    data = json.loads(response.text)
    assert set(data) == {"subject", "body"}


def test_mock_generation_is_deterministic():
    a = generate(ctx())
    b = generate(ctx())
    assert a == b


def test_mock_generation_differs_between_customers():
    a = generate(ctx(customer_first_name="Sam"))
    b = generate(ctx(customer_first_name="Alex", days_since_last_order=120))
    assert a != b


def test_mock_email_uses_real_customer_name_and_products():
    subject, body = generate(ctx())
    assert "Sam" in subject or "Sam" in body
    assert "Steinlager Classic 12pk" in body


def test_mock_email_includes_required_statements():
    _, body = generate(ctx())
    assert RESPONSIBLE in body
    assert AGE_STATEMENT in body
    assert "The GIMME Team" in body


def test_mock_output_has_no_unresolved_placeholders():
    for channel in (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP, Channel.PUSH):
        subject, body = generate(ctx(channel=channel.value))
        assert "{{" not in body and "{{" not in subject
        assert "[NAME]" not in body


def test_mock_sms_respects_length_limit():
    _, body = generate(ctx(channel=Channel.SMS.value))
    assert 0 < len(body) <= 320


def test_mock_whatsapp_respects_length_limit():
    _, body = generate(ctx(channel=Channel.WHATSAPP.value))
    assert 0 < len(body) <= 600


def test_mock_never_invents_a_promotion():
    """With no verified promotions, generated copy must contain no offer."""
    for action in [a.value for a in NextBestAction]:
        _, body = generate(ctx(recommended_action=action))
        assert "%" not in body
        assert "code" not in body.lower() or "code" in RESPONSIBLE.lower()


def test_mock_uses_verified_promotion_when_supplied():
    context = ctx(
        recommended_action=NextBestAction.WIN_BACK.value,
        verified_promotions=["10% off your next order"],
        verified_coupon_codes=["GIMME10"],
    )
    _, body = generate(context)
    assert "10% off your next order" in body
    assert "GIMME10" in body


def test_shorter_variation_produces_shorter_body():
    _, normal = generate(ctx())
    _, shorter = generate(ctx(), variation="shorter")
    assert len(shorter) <= len(normal)


def test_factory_defaults_to_mock_without_api_key():
    assert isinstance(get_llm_provider(), MockLLMProvider)
    assert isinstance(get_llm_provider("openai"), MockLLMProvider)


def test_mock_health_reports_mock_mode():
    health = MockLLMProvider().health()
    assert health["status"] == "OK"
    assert health["mode"] == "mock"


# ==========================================================================
# Output parsing
# ==========================================================================
def test_parse_plain_json():
    assert parse_llm_output('{"subject": "Hi", "body": "There"}') == ("Hi", "There")


def test_parse_fenced_json():
    raw = '```json\n{"subject": "Hi", "body": "There"}\n```'
    assert parse_llm_output(raw) == ("Hi", "There")


def test_parse_json_with_leading_prose():
    raw = 'Sure! Here is the message:\n{"subject": "Hi", "body": "There"}'
    assert parse_llm_output(raw) == ("Hi", "There")


def test_parse_non_json_falls_back_to_body():
    subject, body = parse_llm_output("Just some text.")
    assert subject == ""
    assert body == "Just some text."


def test_parse_empty_response():
    assert parse_llm_output("") == ("", "")


# ==========================================================================
# Validation
# ==========================================================================
def test_mock_output_passes_validation_for_every_channel():
    for channel in (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP):
        context = ctx(channel=channel.value)
        subject, body = generate(context)
        result = validate_message(
            subject=subject, body=body, channel=channel, context=context, config=config()
        )
        assert result.valid, (channel, [f.as_dict() for f in result.findings])


def test_mock_output_passes_validation_for_every_action():
    for action in [a.value for a in NextBestAction]:
        context = ctx(recommended_action=action)
        subject, body = generate(context)
        result = validate_message(
            subject=subject, body=body, channel=Channel.EMAIL, context=context, config=config()
        )
        assert result.valid, (action, [f.as_dict() for f in result.findings])


def test_invented_coupon_code_rejected():
    result = validate_message(
        subject="A treat",
        body=f"Use code FREE50 at checkout.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert not result.valid
    assert "UNVERIFIED_COUPON_CODE" in error_codes(result)


def test_invented_discount_rejected():
    result = validate_message(
        subject="A treat",
        body=f"Take 30% off today.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert "UNVERIFIED_PROMOTION" in error_codes(result)


def test_unverified_product_rejected():
    result = validate_message(
        subject="New in",
        body=f"Try the Corona Extra 24pk this week.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert not result.valid
    assert "UNVERIFIED_PRODUCT" in error_codes(result)


def test_product_from_purchase_history_accepted():
    result = validate_message(
        subject="Your usual",
        body=f"Your Steinlager Classic 12pk is ready to reorder.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert "UNVERIFIED_PRODUCT" not in error_codes(result)


def test_product_from_verified_catalogue_accepted():
    context = ctx(verified_products=[{"product_name": "Corona Extra 24pk", "price": "$42.00"}])
    result = validate_message(
        subject="New in",
        body=f"Try the Corona Extra 24pk this week.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=context,
        config=config(),
    )
    assert "UNVERIFIED_PRODUCT" not in error_codes(result)


def test_unverified_price_rejected():
    result = validate_message(
        subject="Deal",
        body=f"Grab it for $19.99 today.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert "UNVERIFIED_PRICE" in error_codes(result)


def test_verified_price_accepted():
    context = ctx(verified_products=[{"product_name": "Steinlager Classic 12pk", "price": "$32.00"}])
    result = validate_message(
        subject="Your usual",
        body=f"Your Steinlager Classic 12pk is $32.00.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=context,
        config=config(),
    )
    assert "UNVERIFIED_PRICE" not in error_codes(result)


@pytest.mark.parametrize(
    "sentence",
    [
        "Happy birthday from all of us!",
        "We know you're stressed, so here's a drink.",
        "You've had a rough week.",
        "Something for your family this weekend.",
    ],
)
def test_invented_customer_facts_rejected(sentence):
    result = validate_message(
        subject="Hello",
        body=f"{sentence}\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert not result.valid
    assert "INVENTED_CUSTOMER_FACT" in error_codes(result)


def test_health_claim_rejected():
    result = validate_message(
        subject="Good for you",
        body=f"A daily glass is good for your heart.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert "HEALTH_CLAIM" in error_codes(result)


def test_invented_delivery_claim_rejected():
    result = validate_message(
        subject="Fast",
        body=f"We'll be there in 10 minutes.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert "UNVERIFIED_DELIVERY_CLAIM" in error_codes(result)


def test_stock_claim_rejected():
    result = validate_message(
        subject="Hurry",
        body=f"Only 2 left in stock!\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert "UNVERIFIED_STOCK_CLAIM" in error_codes(result)


def test_missing_responsible_statement_rejected_on_email():
    result = validate_message(
        subject="Your usual",
        body="Your usual is ready to reorder.",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    assert "MISSING_RESPONSIBLE_DRINKING" in error_codes(result)


def test_oversized_sms_blocked():
    result = validate_message(
        subject="",
        body="x" * 400,
        channel=Channel.SMS,
        context=ctx(channel=Channel.SMS.value),
        config=config(),
    )
    assert not result.valid
    assert "BODY_EXCEEDS_CHANNEL_LIMIT" in error_codes(result)


def test_long_email_subject_warns_but_does_not_block():
    context = ctx()
    result = validate_message(
        subject="x" * 120,
        body=f"Hello Sam.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=context,
        config=config(),
    )
    assert result.valid
    assert "SUBJECT_TOO_LONG" in {w.code for w in result.warnings}


def test_words_to_avoid_warn_only():
    context = ctx(words_to_avoid=["cheap"])
    result = validate_message(
        subject="Your usual",
        body=f"Grab it cheap today.\n\n{RESPONSIBLE}\n{AGE_STATEMENT}",
        channel=Channel.EMAIL,
        context=context,
        config=config(),
    )
    assert result.valid
    assert "BRAND_WORD_TO_AVOID" in {w.code for w in result.warnings}


def test_empty_body_rejected():
    result = validate_message(
        subject="Hi", body="  ", channel=Channel.EMAIL, context=ctx(), config=config()
    )
    assert not result.valid
    assert "EMPTY_BODY" in error_codes(result)


def test_validation_result_serializes():
    result = validate_message(
        subject="Deal",
        body="Take 30% off with code FREE50.",
        channel=Channel.EMAIL,
        context=ctx(),
        config=config(),
    )
    data = result.as_dict()
    assert data["valid"] is False
    assert len(data["errors"]) >= 2
    assert "body_word_count" in data
