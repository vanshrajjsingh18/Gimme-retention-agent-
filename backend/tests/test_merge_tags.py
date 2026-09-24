"""Personalisation: what a merge tag is allowed to mean, and what it must not.

A campaign body is written once and delivered to everyone in the audience, so
a tag that resolves wrongly is not one bad message — it is the whole send. The
tests here are about the three ways that goes wrong:

* the tag resolves to the wrong thing, or to nothing, and a sentence ships
  with a gap in it;
* the tag resolves to something it was never meant to reach, which is the
  security question: a merge tag is a field lookup, not an expression;
* the tag does not resolve at all and is delivered verbatim, which is the one
  failure a person reading the copy could have caught, so it has to be caught
  before the send rather than after it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.automations.runtime import AutomationError
from app.automations.service import activate, approve as approve_automation, create_automation
from app.campaigns.service import (
    CampaignError,
    approve_campaign,
    personalise,
    run_campaign,
)
from app.core.enums import (
    AutomationKind,
    CampaignCopyMode,
    CampaignStatus,
    Channel,
    RecipientStatus,
)
from app.core.timezones import to_local, to_utc_naive
from app.models.base import utcnow
from app.models.entities import (
    Campaign,
    CampaignRecipient,
    Customer,
    CustomerMetrics,
    Message,
)
from app.services.merge_tags import (
    ALLOWED_TOKENS,
    MESSAGE_FIELDS,
    render_template,
    resolve_message_template,
    unknown_tags,
)

#: Every tag the composer offers for a customer, in one body. Written as one
#: message rather than seventeen so that a tag which swallows the one after it
#: — a regex that runs on — is visible.
#: A fixed mid-morning send. Left to the wall clock, a send test passes all
#: day and fails after 7pm, when quiet hours correctly hold the message.
MORNING = to_utc_naive(datetime(2026, 9, 24, 11, 0))

EVERY_TAG = (
    "#first_name# #last_name# #full_name# #email# #phone# #product# "
    "#product_name# #category# #brand# #last_order_date# #last_order_amount# "
    "#average_order_value# #order_count# #preferred_category# "
    "#preferred_brand# #preferred_order_day# #preferred_order_time#"
)


def _customer(db, suffix: str, **overrides) -> Customer:
    """A customer with a full record, so every tag has something to find."""
    stamp = f"{utcnow().timestamp()}-{suffix}"
    fields = {
        "external_id": f"TAG-{stamp}",
        "email": "aroha@example.test",
        "phone": "+64211234567",
        "first_name": "Aroha",
        "last_name": "Ngata",
        "city": "Wellington",
        "age_verified": True,
        "marketing_consent": True,
        "email_consent": True,
        "sms_consent": True,
        "signup_date": utcnow() - timedelta(days=300),
    }
    fields.update(overrides)
    customer = Customer(**fields)
    db.add(customer)
    db.flush()
    db.add(
        CustomerMetrics(
            customer_id=customer.id,
            total_orders=9,
            completed_orders=9,
            average_order_value=62.40,
            last_order_amount=68.50,
            last_order_at=utcnow() - timedelta(days=6),
            days_since_last_order=6,
            preferred_brands=["Steinlager"],
            preferred_categories=["Beer"],
            top_products=[{"product_name": "Steinlager Classic 12pk", "quantity": 9}],
            typical_order_weekday="Wednesday",
            typical_order_hour=19,
            typical_order_minute=39,
        )
    )
    db.commit()
    return customer


def _campaign(db, body: str, *, subject: str = "", status=CampaignStatus.APPROVED) -> Campaign:
    campaign = Campaign(
        name=f"Tags {utcnow().timestamp()}",
        objective="RETENTION",
        channel=Channel.EMAIL.value,
        status=status.value,
        subject=subject,
        body=body,
        copy_mode=CampaignCopyMode.WRITTEN.value,
        compliance_result={"passed": True, "blocking_count": 0, "findings": []},
    )
    db.add(campaign)
    db.commit()
    return campaign


# ==========================================================================
# 1. The mapping
# ==========================================================================
def test_every_offered_tag_resolves_to_that_customers_own_detail(db, bootstrapped):
    """The menu may not offer a tag the sender cannot fill.

    Asserted tag by tag rather than on the whole string, because a resolver
    that returns the same value for two tokens produces a sentence that still
    reads fine and is still wrong.
    """
    customer = _customer(db, "all")
    product = "Steinlager Classic 12pk"
    expected = {
        "first_name": "Aroha",
        "last_name": "Ngata",
        "full_name": "Aroha Ngata",
        "email": "aroha@example.test",
        "phone": "+64211234567",
        "product": product,
        "product_name": product,
        "category": "Beer",
        "brand": "Steinlager",
        "last_order_amount": "$68.50",
        "average_order_value": "$62.40",
        "order_count": "9",
        "preferred_category": "Beer",
        "preferred_brand": "Steinlager",
        "preferred_order_day": "Wednesday",
        "preferred_order_time": "7:39 pm",
    }
    for token, value in expected.items():
        assert resolve_message_template(db, f"#{token}#", customer.id).text == value, token

    # The date moves with the fixture, so it is checked against the record
    # rather than against a literal — in the customer's own local day.
    assert resolve_message_template(db, "#last_order_date#", customer.id).text == (
        f"{to_local(customer.metrics.last_order_at):%-d %b}"
    )

    # And all of them at once, so a resolver that runs on past its own token
    # and swallows the next one is visible.
    resolved = resolve_message_template(db, EVERY_TAG, customer.id)
    assert not resolved.missing_fields
    assert not resolved.unknown_tags
    assert "#" not in resolved.text


def test_a_tag_resolves_the_same_way_every_time(db, bootstrapped):
    """Deterministic, and nothing is asked of a model.

    The whole point of a merge tag over generated copy is that an operator can
    predict what goes out. Two renders of the same template for the same
    customer must be the same string.
    """
    customer = _customer(db, "stable")
    first = resolve_message_template(db, EVERY_TAG, customer.id).text
    second = resolve_message_template(db, EVERY_TAG, customer.id).text
    assert first == second


def test_tags_resolve_wherever_they_sit_in_the_sentence(db, bootstrapped):
    """Start, middle and end, and the same tag used twice."""
    customer = _customer(db, "positions")
    resolved = resolve_message_template(
        db, "#first_name#, your #brand# is back. See you Friday, #first_name#", customer.id
    )
    assert resolved.text == "Aroha, your Steinlager is back. See you Friday, Aroha"


def test_typing_a_tag_by_hand_works_whatever_the_case(db, bootstrapped):
    """Nobody types #first_name# with the shift key in the right place.

    Rejecting #First_Name# would be technically defensible and would mean an
    operator's campaign fails validation for a capital letter.
    """
    customer = _customer(db, "case")
    assert resolve_message_template(db, "Hi #First_Name#", customer.id).text == "Hi Aroha"
    assert not unknown_tags("Hi #FIRST_NAME#")


def test_copy_with_no_tags_in_it_is_left_exactly_as_written(db, bootstrapped):
    """Personalisation is opt-in. Untagged copy is not reformatted."""
    customer = _customer(db, "plain")
    body = "Kia ora.  We are open late tonight.\n\nReply STOP to opt out."
    assert resolve_message_template(db, body, customer.id).text == body


# ==========================================================================
# 2. Missing values
# ==========================================================================
def test_a_customer_with_no_first_name_is_greeted_not_left_hanging(db, bootstrapped):
    """"Hi ," is the message this feature must never send."""
    customer = _customer(db, "nameless", first_name="", last_name="")
    resolved = resolve_message_template(db, "Hi #first_name#, we are open late.", customer.id)

    assert resolved.text == "Hi there, we are open late."
    assert "first_name" in resolved.missing_fields
    assert "first_name" in resolved.fallbacks_used


def test_the_older_spellings_fall_back_the_same_way(db, bootstrapped):
    """{favourite_brand} and #brand# are the same field, so they must agree.

    They briefly did not: the new resolver carried a fallback table covering
    only the fields it listed, so copy written last year against the older
    spellings rendered a bare gap — "your is one tap away". Two tables is how
    that happens; this is the test that there is one.
    """
    customer = _customer(db, "legacy", first_name="")
    customer.metrics.preferred_brands = []
    customer.metrics.preferred_categories = []
    customer.metrics.top_products = []
    db.commit()

    resolved = resolve_message_template(
        db, "Kia ora {name}, your {favourite_brand} is ready.", customer.id
    )
    assert resolved.text == "Kia ora there, your your favourites is ready."


def test_a_missing_number_is_left_out_rather_than_invented(db, bootstrapped):
    """A fallback may supply a greeting. It may not supply a figure.

    "$0.00" and "0 orders" are statements about a customer's account, and a
    made-up one is worse than the gap the tidy-up closes. So these two tags
    are recorded as missing with no fallback offered.
    """
    customer = _customer(db, "newbie")
    customer.metrics.last_order_amount = 0.0
    customer.metrics.completed_orders = 0
    db.commit()

    resolved = resolve_message_template(
        db, "You have spent #last_order_amount# over #order_count# orders.", customer.id
    )
    assert "$" not in resolved.text and "0" not in resolved.text
    assert set(resolved.missing_fields) == {"last_order_amount", "order_count"}
    assert not resolved.fallbacks_used


# ==========================================================================
# 3. A tag is a field lookup, not an expression
# ==========================================================================
@pytest.mark.parametrize(
    "hostile",
    [
        "#customer.password#",
        "#user.is_admin#",
        "{{ 7 * 7 }}",
        "{{ config.SECRET_KEY }}",
        "#__class__#",
        "#metrics.lifetime_revenue#",
    ],
)
def test_a_tag_can_only_name_a_whitelisted_field(db, bootstrapped, hostile):
    """Nothing here evaluates. It looks a name up in a table or it does not.

    The check is that the hostile string comes back unchanged: no attribute
    was walked, no expression was run, and no value that is not on the
    whitelist appears in the output.
    """
    customer = _customer(db, "hostile")
    resolved = resolve_message_template(db, f"Hi #first_name#. {hostile}", customer.id)

    assert hostile in resolved.text, "the input was interpreted rather than left alone"
    assert "Aroha" in resolved.text, "a hostile token stopped the legitimate one resolving"


def test_a_tag_that_is_not_a_field_is_reported_rather_than_silently_kept(db):
    """Left in the text *and* named, which are two different jobs.

    Deleting it would ship a broken sentence quietly. Keeping it without
    telling anybody would ship "#discont_code#" to the whole audience. It is
    kept so the message is legible, and reported so the send is blocked.
    """
    assert unknown_tags("Use #discont_code# today") == ["discont_code"]
    assert unknown_tags("Hi #first_name#") == []
    # Two hashes around a number is a sentence, not a tag.
    assert unknown_tags("We are rated #1 #2 in Auckland") == []


def test_a_resolved_value_is_never_read_back_as_a_tag(db, bootstrapped):
    """Substitution is one pass, and values are stripped of tag delimiters.

    A customer whose name is "#last_order_amount#" is not a realistic record;
    it is the shape of an injection, and the test is that the second tag never
    gets a chance to resolve out of the first one's value.
    """
    customer = _customer(db, "injected", first_name="#last_order_amount#")
    resolved = resolve_message_template(db, "Hi #first_name#.", customer.id)

    assert "$68.50" not in resolved.text
    assert "#" not in resolved.text


def test_a_value_cannot_carry_a_newline_into_a_subject_line(db, bootstrapped):
    """Header injection, and SMS segmentation, start the same way."""
    customer = _customer(db, "multiline", first_name="Aroha\nBcc: someone@example.test")
    resolved = resolve_message_template(db, "A note for #first_name#", customer.id)
    assert "\n" not in resolved.text


def test_script_tags_are_carried_as_the_plain_text_they_are(db, bootstrapped):
    """Templates are plain text. No channel here renders markup.

    So the correct behaviour is not to escape the angle brackets into entities
    — that would deliver "&lt;script&gt;" to an SMS — but to leave the text
    alone and never treat it as a template.
    """
    customer = _customer(db, "script")
    resolved = resolve_message_template(db, "<script>alert(1)</script> #first_name#", customer.id)
    assert resolved.text == "<script>alert(1)</script> Aroha"


def test_the_whitelist_is_the_menu(db):
    """Every field offered is resolvable, and nothing else is allowed.

    These drifting apart is how a composer ends up promising a value the
    sender does not fill, which is the thing this table exists to prevent.
    """
    for spec_tag in (
        "first_name last_name full_name email phone product product_name category brand "
        "last_order_date last_order_amount average_order_value order_count "
        "preferred_category preferred_brand preferred_order_day preferred_order_time"
    ).split():
        assert spec_tag in ALLOWED_TOKENS, f"#{spec_tag}# is not on the whitelist"
        assert any(f.token == spec_tag for f in MESSAGE_FIELDS), f"#{spec_tag}# is not offered"


# ==========================================================================
# 4. Validation before anything is sent
# ==========================================================================
def test_a_campaign_cannot_be_approved_with_a_tag_that_is_not_a_field(db, bootstrapped):
    """Caught while somebody is still looking at the copy.

    Approval is the moment a person vouches for the message. A tag that
    cannot be filled would be delivered exactly as typed, so it belongs in
    the same gate as every other reason copy is not sendable.
    """
    campaign = _campaign(
        db,
        "Kia ora #first_name#, here is #discont_code#. Reply STOP to opt out.",
        status=CampaignStatus.AWAITING_APPROVAL,
    )
    with pytest.raises(CampaignError) as raised:
        approve_campaign(db, campaign, user_id=1)

    assert "#discont_code#" in str(raised.value)
    assert campaign.status != CampaignStatus.APPROVED.value
    codes = [f["code"] for f in (campaign.compliance_result or {}).get("findings", [])]
    assert "UNKNOWN_MERGE_TAG" in codes


def test_the_send_refuses_a_tag_that_is_not_a_field(db, bootstrapped):
    """Checked again at the last moment, not trusted from the approval.

    A compliance report is a snapshot of copy at a moment. This is the point
    past which the text is out of anybody's hands.
    """
    campaign = _campaign(db, "Kia ora #first_name#, use #mystery_field# now.")
    with pytest.raises(CampaignError) as raised:
        run_campaign(db, campaign, simulate_engagement=False, now=MORNING)
    assert "#mystery_field#" in str(raised.value)


def test_an_automation_cannot_be_activated_with_a_tag_that_is_not_a_field(db, bootstrapped):
    """An automation sends on its own for as long as it is switched on.

    So a bad tag is not one message — it is every message it will ever send,
    with nobody watching. Activation is the last moment a person is.
    """
    automation = create_automation(
        db,
        name=f"Bad tag {utcnow().timestamp()}",
        kind=AutomationKind.NUDGE.value,
        channel=Channel.SMS.value,
        manual_customer_ids=[_customer(db, "activate").id],
        message_template="Hi #first_name#, your #discont_code# awaits. Reply STOP to opt out.",
    )
    approve_automation(db, automation, user_id=1)

    with pytest.raises(AutomationError) as raised:
        activate(db, automation)
    assert "#discont_code#" in str(raised.value)


def test_smart_reorder_copy_uses_the_same_fields(db, bootstrapped):
    """One resolver, so the two surfaces cannot disagree about a tag.

    Smart Reorder's reminder is the message most worth personalising — it is
    about this customer's own routine — and it renders through the same
    whitelist as a campaign, plus the routine values only it knows.
    """
    customer = _customer(db, "reorder")
    automation = create_automation(
        db,
        name=f"Reorder tags {utcnow().timestamp()}",
        kind=AutomationKind.NUDGE.value,
        channel=Channel.SMS.value,
        manual_customer_ids=[customer.id],
        message_template=(
            "Hi #first_name#, your usual #brand# on a #preferred_order_day# — "
            "ready when you are. Reply STOP to opt out."
        ),
    )
    approve_automation(db, automation, user_id=1)
    activate(db, automation)  # the tags are known, so this is allowed

    resolved = resolve_message_template(db, automation.message_template, customer.id)
    assert "Hi Aroha, your usual Steinlager on a Wednesday" in resolved.text


# ==========================================================================
# 5. The template survives the send
# ==========================================================================
def test_the_stored_template_is_never_overwritten_by_a_send(db, bootstrapped):
    """Resolution happens on the way out.

    If the send wrote the resolved text back, the second recipient would be
    greeted by the first one's name, and the campaign would no longer hold
    the copy that was approved.
    """
    customer = _customer(db, "template")
    template = "Kia ora #first_name#, we are open late. Reply STOP to opt out."
    campaign = _campaign(db, template)
    db.add(
        CampaignRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            status=RecipientStatus.ELIGIBLE.value,
        )
    )
    db.commit()

    run_campaign(db, campaign, simulate_engagement=False, now=MORNING)
    db.refresh(campaign)

    assert campaign.body == template, "the send rewrote the campaign's own copy"


def test_the_send_records_what_was_filled_in_for_each_person(db, bootstrapped):
    """The log that answers "why did this one read oddly?".

    The resolved text alone cannot answer it: "Hi there" is indistinguishable
    from a customer actually called There unless the fallback was recorded at
    the moment it was used.
    """
    customer = _customer(db, "audit", first_name="")
    campaign = _campaign(
        db,
        "Kia ora #first_name#, your #brand# awaits. Reply STOP to opt out.",
        subject="A note for #first_name#",
    )
    db.add(
        CampaignRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            status=RecipientStatus.ELIGIBLE.value,
        )
    )
    db.commit()

    run_campaign(db, campaign, simulate_engagement=False, now=MORNING)

    message = db.execute(
        select(Message).where(
            Message.campaign_id == campaign.id, Message.customer_id == customer.id
        )
    ).scalar_one()
    audit = message.generation_context["personalisation"]

    assert message.customer_id == customer.id  # recipient
    assert message.campaign_id == campaign.id  # campaign
    assert audit["template_body"] == campaign.body  # template used
    assert "Kia ora there" in message.body  # resolved message
    assert audit["missing_fields"] == ["first_name"]  # what was missing
    assert audit["fallbacks_used"] == ["first_name"]  # whether a fallback ran
    assert message.status == "SENT"  # send status
    # And the template is kept beside the resolved text rather than instead
    # of it: "what did we send this person" and "what was the copy" are
    # different questions.
    assert message.original_body == campaign.body
    assert message.body != message.original_body


def test_a_hole_in_the_sentence_is_not_sent(db, bootstrapped):
    """A gap no fallback can close means no message, not a broken one.

    "You have spent over orders" reads as a broken system to the one customer
    who was already the least engaged. The recipient is recorded as failed
    with the field that was missing named, so the audience count and the
    reason agree — a send that silently comes back one short is worse than
    one that says why.
    """
    customer = _customer(db, "hole")
    customer.metrics.completed_orders = 0
    db.commit()

    campaign = _campaign(
        db, "Kia ora #first_name#, that is #order_count# orders now. Reply STOP to opt out."
    )
    db.add(
        CampaignRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            status=RecipientStatus.ELIGIBLE.value,
        )
    )
    db.commit()

    stats = run_campaign(db, campaign, simulate_engagement=False, now=MORNING)

    assert stats["sent"] == 0
    assert stats["missing_personalisation"] == 1
    recipient = db.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    ).scalars().one()
    assert recipient.status == RecipientStatus.FAILED.value
    assert "#order_count#" in recipient.exclusion_reason


def test_a_gap_a_fallback_can_close_is_sent(db, bootstrapped):
    """The other side of the same rule, or it would block half the audience.

    A missing first name is not a missing message — that is what the fallback
    is for, and refusing to send would turn a greeting into a blocker.
    """
    customer = _customer(db, "greeting", first_name="")
    campaign = _campaign(db, "Kia ora #first_name#, we are open late. Reply STOP to opt out.")
    db.add(
        CampaignRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            status=RecipientStatus.ELIGIBLE.value,
        )
    )
    db.commit()

    stats = run_campaign(db, campaign, simulate_engagement=False, now=MORNING)
    assert stats["sent"] == 1
    assert stats["missing_personalisation"] == 0


def test_two_recipients_get_their_own_details_from_one_template(db, bootstrapped):
    """The claim the composer makes, asserted across more than one person."""
    template = "Kia ora #first_name#, your #brand# awaits."
    one = _customer(db, "one", first_name="Aroha")
    two = _customer(db, "two", first_name="Hemi")
    two.metrics.preferred_brands = ["Tui"]
    db.commit()

    campaign = _campaign(db, template)
    assert personalise(db, campaign, one)[1] == "Kia ora Aroha, your Steinlager awaits."
    assert personalise(db, campaign, two)[1] == "Kia ora Hemi, your Tui awaits."


def test_you_can_read_your_own_copy_in_the_evening(db, bootstrapped):
    """Quiet hours are a question about *when*, not about *who*.

    Between 7pm and 9am every recipient is correctly excluded, which emptied
    the copy preview — so an operator sitting down after dinner to write
    tomorrow's campaign saw no message at all and no reason given. The
    audience count stays honest; the samples fall back to the segment, and
    the screen says which it is showing.
    """
    from app.campaigns.service import preview_copy

    _customer(db, "evening")
    # SMS, because quiet hours are an SMS and WhatsApp rule — email at night
    # is not intrusive, so it is not the channel this bug shows up on.
    campaign = _campaign(db, "Kia ora #first_name#, your #brand# awaits. Reply STOP to opt out.")
    campaign.channel = Channel.SMS.value
    # 9:30pm their time: inside quiet hours, so nobody is eligible to receive.
    campaign.scheduled_at = to_utc_naive(datetime(2026, 9, 24, 21, 30))
    db.commit()

    preview = preview_copy(db, campaign)

    assert preview["eligible_count"] == 0, "the audience count should stay honest"
    assert preview["outside_send_window"] is True
    assert preview["samples"], "an operator could not read their own copy"
    body = preview["samples"][0]["body"]
    assert body.startswith("Kia ora ") and "#" not in body


def test_a_preview_with_nobody_attached_reads_as_a_message(db, bootstrapped):
    """A test send before an audience exists should not show raw tokens."""
    campaign = _campaign(db, "Kia ora #first_name#, your #brand# awaits.")
    assert personalise(db, campaign, None)[1] == "Kia ora Sarah, your Corona awaits."


# ==========================================================================
# 6. The API the composer uses
# ==========================================================================
def test_the_field_list_is_served_from_the_whitelist(client, auth_headers):
    payload = client.get("/api/v1/message-fields", headers=auth_headers).json()
    tokens = {f["token"] for f in payload["fields"]}

    assert "first_name" in tokens and "preferred_order_time" in tokens
    assert payload["syntax"] == "#field_name#"
    # Spelled out by the server so the composer never builds the syntax
    # itself and gets it subtly wrong.
    assert all(f["tag"] == f"#{f['token']}#" for f in payload["fields"])


def test_the_preview_endpoint_renders_unsaved_copy_for_a_chosen_customer(
    db, client, auth_headers, bootstrapped
):
    """The question an operator is asking is about the words in front of them.

    Those are usually unsaved, so the template arrives as text rather than as
    a campaign id — and it goes through the same resolver the send uses, so a
    preview cannot flatter the real thing.
    """
    customer = _customer(db, "preview")
    response = client.post(
        "/api/v1/message-fields/preview",
        headers=auth_headers,
        json={"template": "Hi #first_name#, your #brand#. #nope#", "customer_id": customer.id},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["text"].startswith("Hi Aroha, your Steinlager.")
    assert payload["unknown_tags"] == ["nope"]
    assert payload["customer_name"] == "Aroha Ngata"


def test_the_preview_falls_back_to_a_sample_customer(client, auth_headers):
    payload = client.post(
        "/api/v1/message-fields/preview",
        headers=auth_headers,
        json={"template": "Hi #first_name#"},
    ).json()
    assert payload["text"] == "Hi Sarah"
    assert payload["customer_id"] is None


def test_an_empty_template_is_not_an_error(client, auth_headers):
    """The composer previews as you type, which starts from nothing."""
    payload = client.post(
        "/api/v1/message-fields/preview", headers=auth_headers, json={"template": ""}
    ).json()
    assert payload["text"] == ""
    assert payload["unknown_tags"] == []


def test_rendering_needs_no_database_at_all(db):
    """The resolver splits cleanly: reading values, and filling a template.

    Worth holding onto — it is what lets the same code render a preview for a
    customer who does not exist and a real send for one who does.
    """
    resolved = render_template("Hi #first_name#, #brand#.", {"first_name": "Kim", "brand": ""})
    assert resolved.text == "Hi Kim, your favourites."
    assert resolved.missing_fields == ["brand"]
