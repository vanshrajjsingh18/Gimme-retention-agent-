"""AI Copilot: tools, planning, confirmation, receipts and the acceptance flow.

Everything runs on the deterministic offline planner or on a scripted
provider, so no test depends on an external model API.
"""
from __future__ import annotations

import time
from datetime import date, time as clock, timedelta
from itertools import count

import pytest
from sqlalchemy import func, select

from app.copilot.engine import CopilotEngine, CopilotError
from app.copilot.mock import MockAIProvider
from app.copilot.providers import AIProvider, AIResponse, ToolCall
from app.copilot.registry import Risk, registry
from app.core.enums import CancellationReason, ScheduledMessageStatus, UserRole
from app.core.security import hash_password
from app.core.timezones import combine_local, local_date
from app.models.base import utcnow
from app.models.entities import (
    AuditLog,
    Automation,
    BrandSettings,
    Campaign,
    CopilotAction,
    CopilotConversation,
    CopilotExecution,
    CouponVariant,
    Customer,
    CustomerCouponAssignment,
    Order,
    OrderItem,
    ScheduledMessage,
    Segment,
    User,
)

_RUN = count(int(time.time() * 1000))


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------
@pytest.fixture()
def admin(db, seeded) -> User:
    return db.execute(select(User).where(User.email == "admin@gimmedelivery.co.nz")).scalar_one()


@pytest.fixture()
def viewer(db, seeded) -> User:
    email = f"viewer-{next(_RUN)}@example.test"
    user = User(email=email, full_name="View Only", hashed_password=hash_password("Viewer123!"),
                role=UserRole.VIEWER.value)
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def beer_regular(db, seeded) -> Customer:
    """Orders Steinlager every three weeks on the same evening; last order 15 days ago.

    Lapsed by the acceptance test's 14-day rule, with a confident prediction
    a few days out — so the Smart Reorder dry run has a real row to show.
    """
    from app.services.intelligence import refresh_customer

    now = utcnow()
    tag = next(_RUN)
    customer = Customer(
        external_id=f"COPILOT-BEER-{tag}",
        first_name="Ngaio",
        last_name=f"Beerfan{tag}",
        phone="+64211234567",
        email=f"ngaio{tag}@example.test",
        city="Wellington",
        date_of_birth=date(1988, 1, 1),
        age_verified=True,
        marketing_consent=True,
        sms_consent=True,
        signup_date=now - timedelta(days=300),
    )
    db.add(customer)
    db.flush()
    today = local_date(now)
    for i in range(6):
        order = Order(
            external_id=f"{customer.external_id}-{i}",
            customer_id=customer.id,
            ordered_at=combine_local(today - timedelta(days=15 + 21 * i), clock(19, 30)),
            status="COMPLETED",
            total_amount=55.0,
        )
        db.add(order)
        db.flush()
        db.add(OrderItem(external_id=f"{order.external_id}-1", order_id=order.id, sku="BEER-STE-12",
                         product_name="Steinlager Classic 12pk", category="Beer", brand="Steinlager",
                         quantity=1, unit_price=55.0, line_total=55.0))
    db.commit()
    refresh_customer(db, customer, now=now)
    db.commit()
    return customer


class ScriptedProvider(AIProvider):
    """Returns pre-written model turns, for testing the engine without a planner."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, turns: list[AIResponse]) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def chat(self, *, system, messages, tools, context):
        self.seen.append(messages)
        if not self.turns:
            return AIResponse(text="Done.", provider=self.name, model=self.model)
        return self.turns.pop(0)


def call(tool_name: str, /, **arguments) -> AIResponse:
    return AIResponse(text="", tool_calls=[ToolCall(id=f"c{next(_RUN)}", name=tool_name, arguments=arguments)],
                      provider="scripted", model="scripted-1")


def say(engine: CopilotEngine, conversation_id: int | None, text: str):
    conversation, new = engine.chat(conversation_id, text)
    tools = [m.tool_results for m in new if m.role == "tool"]
    reply = "\n".join(m.content for m in new if m.role == "assistant" and m.content)
    return conversation, tools, reply


def pending(db, conversation: CopilotConversation) -> CopilotAction:
    db.refresh(conversation)
    action = db.get(CopilotAction, conversation.pending_action_id)
    assert action is not None and action.status == "PENDING"
    return action


def acceptance_request(name: str) -> str:
    return (
        f"Create a Smart Reorder campaign called {name} for customers who ordered beer at least twice and "
        "haven't ordered in 14 days. Send SMS 30 minutes before their predicted reorder time. Use three "
        "coupon codes FIRST7, LUCKY7 and COMEAGAIN7 with equal allocation. Write a short cheeky message "
        "using #first_name#, #product# and #coupon_code#."
    )


# --------------------------------------------------------------------------
# 1-3. Reads
# --------------------------------------------------------------------------
def test_simple_read_command_reports_engine_numbers(db, admin):
    from app.copilot.tools._common import resolve_window
    from app.models.entities import CustomerMetrics

    engine = CopilotEngine(db, admin, MockAIProvider())
    _, tools, reply = say(engine, None, "Show today's predicted reorders")
    assert [t["name"] for t in tools] == ["get_smart_reorder_predictions"]
    result = tools[0]["result"]
    assert result["success"] is True
    start, end, _ = resolve_window("today", utcnow())
    expected = db.execute(
        select(func.count()).select_from(CustomerMetrics).join(Customer, Customer.id == CustomerMetrics.customer_id)
        .where(CustomerMetrics.predicted_next_order_at >= start, CustomerMetrics.predicted_next_order_at < end,
               CustomerMetrics.prediction_confidence >= 70, Customer.marketing_consent.is_(True),
               Customer.is_suppressed.is_(False))
    ).scalar_one()
    assert result["metadata"]["count"] == expected
    assert f"**{expected}**" in reply
    assert "today" in result["metadata"]["period"]


def test_customer_lookup_masks_contact_details_and_refuses_ambiguity(db, admin):
    customer = db.execute(select(Customer).order_by(Customer.id)).scalars().first()
    twin = Customer(external_id=f"TWIN-{next(_RUN)}", first_name=customer.first_name, last_name="Zzwin", phone="+64210000001")
    db.add(twin)
    db.commit()
    provider = ScriptedProvider([call("get_customer", customer=str(customer.id)),
                                 call("get_customer", customer=customer.first_name)])
    engine = CopilotEngine(db, admin, provider)
    _, tools, _ = say(engine, None, "look them up")
    exact, ambiguous = tools[0]["result"], tools[1]["result"]
    assert exact["success"] and exact["data"]["id"] == customer.id
    if customer.phone:
        assert exact["data"]["phone"] != customer.phone and "***" in exact["data"]["phone"]
    assert ambiguous["success"] is False
    assert "matches" in ambiguous["errors"][0] and "which one" in ambiguous["errors"][0].lower()


def test_segment_lookup_focuses_the_segment(db, admin):
    vip = db.execute(select(Segment).where(Segment.name.ilike("%vip%"))).scalars().first()
    engine = CopilotEngine(db, admin, MockAIProvider())
    conversation, tools, reply = say(engine, None, "Show me my VIP customers")
    assert tools[0]["name"] == "get_segment"
    assert tools[0]["result"]["data"]["id"] == vip.id
    db.refresh(conversation)
    assert (conversation.active_entity_type, conversation.active_entity_id) == ("segment", vip.id)
    assert conversation.working_state["last_result_set"]["kind"] == "customers"


# --------------------------------------------------------------------------
# 4-9. Create, modify, preview, confirm, cancel, protect activation
# --------------------------------------------------------------------------
def test_campaign_creation_is_planned_then_created_as_draft_only_on_confirm(db, admin, beer_regular):
    name = f"Beer Lapsed {next(_RUN)}"
    engine = CopilotEngine(db, admin, MockAIProvider())
    before = db.execute(select(func.count(Automation.id))).scalar_one()
    conversation, tools, reply = say(engine, None, acceptance_request(name))

    assert [t["name"] for t in tools] == [
        "get_smart_reorder_overview", "preview_cohort", "validate_message_template", "create_smart_reorder_campaign",
    ]
    assert db.execute(select(func.count(Automation.id))).scalar_one() == before, "planning must not write"
    assert db.execute(select(Segment.id).where(Segment.name == f"{name} — audience")).first() is None

    action = pending(db, conversation)
    assert action.risk == Risk.WRITE.value
    dry = action.preview["dry_run"]
    assert dry["campaign"]["status"] == "DRAFT"
    assert dry["final_audience"] >= 1
    assert beer_regular.id in [s["customer_id"] for s in dry["schedule_sample"]] or dry["final_audience"] > 5
    assert sum(dry["coupon_allocation"].values()) == dry["final_audience"]
    assert set(dry["coupon_allocation"]) <= {"FIRST7", "LUCKY7", "COMEAGAIN7"}
    assert "Confirm" in reply

    outcome = engine.confirm(action.id)
    assert outcome["action"]["status"] == "EXECUTED"
    automation = db.execute(select(Automation).where(Automation.name == name)).scalar_one()
    assert automation.status == "DRAFT" and automation.kind == "NUDGE" and automation.channel == "SMS"
    assert automation.config["custom_offset_minutes"] == 30
    variants = db.execute(select(CouponVariant).where(CouponVariant.automation_id == automation.id)).scalars().all()
    assert sorted(v.coupon_code for v in variants) == ["COMEAGAIN7", "FIRST7", "LUCKY7"]
    assert abs(sum(v.allocation_percentage for v in variants) - 100) < 0.01
    brand = db.get(BrandSettings, 1)
    db.refresh(brand)
    assert {"FIRST7", "LUCKY7", "COMEAGAIN7"} <= set(brand.active_coupon_codes)
    db.refresh(conversation)
    assert (conversation.active_entity_type, conversation.active_entity_id) == ("automation", automation.id)


@pytest.fixture()
def draft(db, admin, beer_regular):
    """A conversation with a confirmed acceptance-test draft in focus."""
    engine = CopilotEngine(db, admin, MockAIProvider())
    name = f"Beer Draft {next(_RUN)}"
    conversation, _, _ = say(engine, None, acceptance_request(name))
    engine.confirm(pending(db, conversation).id)
    automation = db.execute(select(Automation).where(Automation.name == name)).scalar_one()
    return engine, conversation, automation


def test_campaign_modification_coupon_split(db, draft):
    engine, conversation, automation = draft
    _, tools, reply = say(engine, conversation.id, "Change the coupon split to 50%, 25%, 25%.")
    assert tools[-1]["name"] == "update_coupon_allocation"
    action = pending(db, conversation)
    assert action.preview["before"]["coupons"][0]["allocation"] == pytest.approx(33.33, abs=0.01)
    engine.confirm(action.id)
    split = {v.coupon_code: v.allocation_percentage
             for v in db.execute(select(CouponVariant).where(CouponVariant.automation_id == automation.id)).scalars()}
    assert split == {"FIRST7": 50.0, "LUCKY7": 25.0, "COMEAGAIN7": 25.0}


def test_campaign_preview_writes_nothing(db, draft):
    engine, conversation, automation = draft
    counts = lambda: (  # noqa: E731
        db.execute(select(func.count(ScheduledMessage.id))).scalar_one(),
        db.execute(select(func.count(CustomerCouponAssignment.id))).scalar_one(),
    )
    before = counts()
    _, tools, reply = say(engine, conversation.id, "Show me a dry run.")
    assert tools[0]["name"] == "preview_smart_reorder"
    data = tools[0]["result"]["data"]
    assert data["final_audience"] >= 1
    assert data["excluded"], "a dry run reports who was left out and why"
    assert all(s["coupon_code"] in ("FIRST7", "LUCKY7", "COMEAGAIN7") for s in data["schedule_sample"])
    assert "DRY RUN" in reply
    assert counts() == before


def test_confirmation_creates_receipt_and_audit_entry(db, draft):
    engine, conversation, automation = draft
    say(engine, conversation.id, "Change the reminder to 20 minutes before predicted order.")
    action = pending(db, conversation)
    outcome = engine.confirm(action.id)
    receipt = db.get(CopilotExecution, outcome["execution"]["id"])
    assert receipt.success and receipt.confirmation_received and receipt.confirmation_required
    assert receipt.tool_name == "update_smart_reorder_campaign"
    assert receipt.target_type == "automation" and receipt.target_id == str(automation.id)
    assert receipt.before_state["timing"]["minutes_before_predicted_order"] == 30
    assert receipt.after_state["timing"]["minutes_before_predicted_order"] == 20
    db.refresh(automation)
    assert automation.config["custom_offset_minutes"] == 20
    assert db.execute(select(AuditLog).where(AuditLog.action == "COPILOT_ACTION_EXECUTED",
                                             AuditLog.detail["execution_id"].as_integer() == receipt.id)).first()


def test_cancellation_discards_the_plan(db, draft):
    engine, conversation, automation = draft
    say(engine, conversation.id, "Use WhatsApp instead of SMS.")
    action = pending(db, conversation)
    engine.cancel(action.id)
    db.refresh(automation)
    assert automation.channel == "SMS"
    with pytest.raises(CopilotError) as exc:
        engine.confirm(action.id)
    assert exc.value.status == 409


def test_activation_needs_explicit_confirmation(db, draft):
    engine, conversation, automation = draft
    _, tools, reply = say(engine, conversation.id, "Activate it.")
    action = pending(db, conversation)
    assert action.risk == Risk.HIGH_RISK_WRITE.value
    db.refresh(automation)
    assert automation.status == "DRAFT" and automation.approved_at is None

    _, tools, reply = say(engine, conversation.id, "yes go ahead")
    assert tools == [] and "Confirm" in reply
    db.refresh(automation)
    assert automation.status == "DRAFT", "typed approval must never execute"

    engine.confirm(action.id)
    db.refresh(automation)
    assert automation.status == "ACTIVE" and automation.approved_at is not None


# --------------------------------------------------------------------------
# 10-12. Smart Reorder settings, coupons, merge tags
# --------------------------------------------------------------------------
def test_smart_reorder_low_confidence_and_channel(db, draft):
    engine, conversation, automation = draft
    say(engine, conversation.id, "Exclude low-confidence predictions.")
    engine.confirm(pending(db, conversation).id)
    db.refresh(automation)
    assert automation.config["min_confidence"] == 80


def test_coupon_codes_are_never_invented_and_must_total_100(db, draft):
    engine, conversation, automation = draft
    provider = ScriptedProvider([
        call("update_coupon_allocation", automation_id=automation.id,
             coupons=[{"code": "FIRST7", "allocation": 60}, {"code": "LUCKY7", "allocation": 60}]),
        call("update_coupon_allocation", automation_id=automation.id,
             coupons=[{"code": "SECRET50"}]),
    ])
    engine.provider = provider
    _, tools, _ = say(engine, conversation.id, "adjust the coupons")
    over, invented = tools[0]["result"], tools[1]["result"]
    assert over["success"] is False and "100" in over["errors"][0]
    assert invented["success"] is False and "never be invented" in invented["errors"][0]


def test_merge_tags_are_validated(db, draft):
    engine, conversation, automation = draft
    provider = ScriptedProvider([
        call("validate_message_template", template="Hi #first_name#, use #discont_code#. Reply STOP to opt out."),
        call("update_message_template", automation_id=automation.id,
             template="Hi #frist_name#, your #product# awaits. Reply STOP to opt out."),
    ])
    engine.provider = provider
    _, tools, _ = say(engine, conversation.id, "check this copy")
    report, update = tools[0]["result"], tools[1]["result"]
    assert report["data"]["valid"] is False and "#discont_code#" in report["data"]["unknown_tags"]
    assert update["success"] is False and "frist_name" in update["errors"][0]
    db.refresh(automation)
    assert "frist_name" not in automation.message_template


# --------------------------------------------------------------------------
# 13-14. Analytics and customer debugging
# --------------------------------------------------------------------------
def test_analytics_query_uses_real_figures_and_names_the_period(db, admin):
    from app.copilot.tools._common import resolve_window

    engine = CopilotEngine(db, admin, MockAIProvider())
    _, tools, reply = say(engine, None, "How much revenue did we make last week?")
    result = tools[0]["result"]
    assert tools[0]["name"] == "get_revenue_analytics"
    start, end, label = resolve_window("last_week", utcnow())
    total = db.execute(select(func.coalesce(func.sum(Order.total_amount), 0.0)).where(
        Order.status == "COMPLETED", Order.ordered_at >= start, Order.ordered_at < end)).scalar_one()
    assert result["data"]["total_revenue"] == pytest.approx(round(total, 2))
    assert result["metadata"]["period"] == label
    assert label in reply


def test_customer_debugging_reports_the_recorded_reason(db, draft, beer_regular):
    engine, conversation, automation = draft
    now = utcnow()
    db.add(ScheduledMessage(
        customer_id=beer_regular.id, automation_id=automation.id, scheduled_at=now - timedelta(hours=2),
        predicted_order_at=now - timedelta(hours=1, minutes=30), channel="SMS", template="t", rendered_message="m",
        status=ScheduledMessageStatus.CANCELLED.value,
        cancellation_reason=CancellationReason.CUSTOMER_ALREADY_ORDERED.value,
        cancellation_detail="They ordered at 6:58 PM, before the reminder was due.",
        cancelled_at=now - timedelta(hours=2, minutes=10),
    ))
    db.commit()
    _, tools, reply = say(engine, conversation.id, f"Why didn't {beer_regular.full_name} receive the Smart Reorder message today?")
    assert tools[0]["name"] == "diagnose_customer_delivery"
    entry = next(a for a in tools[0]["result"]["data"]["automations"] if a["automation_id"] == automation.id)
    assert "CUSTOMER_ALREADY_ORDERED" in entry["conclusion"]
    assert "6:58 PM" in entry["conclusion"]
    assert "CUSTOMER_ALREADY_ORDERED" in reply


# --------------------------------------------------------------------------
# 15-16. Failures and idempotency
# --------------------------------------------------------------------------
def test_tool_failure_is_reported_not_hidden(db, admin, monkeypatch):
    tool = registry.get("get_retention_overview")

    def boom(ctx, **_):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(tool, "handler", boom)
    provider = ScriptedProvider([call("get_retention_overview"), call("no_such_tool")])
    engine = CopilotEngine(db, admin, provider)
    _, tools, _ = say(engine, None, "how are we doing")
    assert tools[0]["result"]["success"] is False
    assert "failed unexpectedly" in tools[0]["result"]["errors"][0]
    assert tools[1]["result"]["success"] is False and "no tool" in tools[1]["result"]["errors"][0]
    # The model is told the call failed.
    tool_messages = [m for m in provider.seen[-1] if m["role"] == "tool"]
    assert all(m["is_error"] for m in tool_messages)


def test_provider_failure_is_honest(db, admin):
    from app.copilot.providers import AIProviderError

    class Down(AIProvider):
        name = "down"

        def chat(self, **_):
            raise AIProviderError("The AI provider returned HTTP 503.")

    engine = CopilotEngine(db, admin, Down())
    _, tools, reply = say(engine, None, "Create a segment for AOV above $80")
    assert tools == [] and "503" in reply and "Nothing was changed" in reply


def test_idempotency_repeated_create_and_double_confirm(db, admin, beer_regular):
    engine = CopilotEngine(db, admin, MockAIProvider())
    name = f"Beer Idem {next(_RUN)}"
    conversation, _, _ = say(engine, None, acceptance_request(name))
    first = pending(db, conversation)
    _, tools, _ = say(engine, conversation.id, acceptance_request(name))
    assert tools[-1]["result"]["data"]["action_id"] == first.id
    assert tools[-1]["result"]["data"]["reused_existing_pending_action"] is True

    engine.confirm(first.id)
    again = engine.confirm(first.id)
    assert again["duplicate"] is True
    assert db.execute(select(func.count(CopilotExecution.id)).where(CopilotExecution.action_id == first.id)).scalar_one() == 1

    _, tools, _ = say(engine, conversation.id, acceptance_request(name))
    assert tools[-1]["result"]["data"]["status"] == "ALREADY_DONE"
    assert db.execute(select(func.count(Automation.id)).where(Automation.name == name)).scalar_one() == 1


# --------------------------------------------------------------------------
# 17-18. Conversation and working state
# --------------------------------------------------------------------------
def test_conversation_state_persists_and_follow_ups_use_it(db, admin):
    engine = CopilotEngine(db, admin, MockAIProvider())
    conversation, _, _ = say(engine, None, "Show me customers who haven't ordered for 30 days but historically ordered more than $70.")
    db.refresh(conversation)
    found = conversation.working_state["last_result_set"]
    assert found["count"] > 0 and found["ids"]
    name = f"Lapsed spenders {next(_RUN)}"
    _, tools, _ = say(engine, conversation.id, f"Create a segment from them called {name}")
    assert tools[-1]["result"]["data"]["preview"]["result"]["members"] == found["count"]
    engine.confirm(pending(db, conversation).id)
    segment = db.execute(select(Segment).where(Segment.name == name)).scalar_one()
    assert segment.member_count == found["count"]
    db.refresh(conversation)
    assert conversation.active_entity_type == "segment" and conversation.active_entity_id == segment.id
    roles = [m.role for m in conversation.messages]
    assert roles.count("user") == 2 and "tool" in roles and "event" in roles


def test_working_draft_follows_the_campaign_in_focus(db, draft):
    engine, conversation, automation = draft
    other_conversation, _, _ = say(engine, None, f"show campaign {automation.id}")
    db.refresh(other_conversation)
    assert other_conversation.active_entity_id == automation.id
    _, tools, _ = say(engine, other_conversation.id, "Change the coupon split to 50/30/20")
    assert tools[-1]["arguments"]["automation_id"] == automation.id
    assert [c["allocation"] for c in tools[-1]["arguments"]["coupons"]] == [50.0, 30.0, 20.0]


# --------------------------------------------------------------------------
# 19-20. Bulk confirmation and compliance
# --------------------------------------------------------------------------
def test_bulk_action_shows_affected_count_before_changing_anything(db, admin):
    tag = f"BULK{next(_RUN)}"
    for i in range(2):
        db.add(Campaign(name=f"{tag} draft {i}", channel="SMS", status="DRAFT", objective="RETENTION",
                        body="Hi #first_name#. Reply STOP to opt out."))
    db.commit()
    provider = ScriptedProvider([call("bulk_update_campaigns", action="set_channel", new_channel="WHATSAPP",
                                      status="DRAFT", channel="SMS", name_contains=tag)])
    engine = CopilotEngine(db, admin, provider)
    conversation, tools, _ = say(engine, None, "move them to whatsapp")
    action = pending(db, conversation)
    assert len(action.preview["before"]["matched"]) == 2
    assert action.risk == Risk.HIGH_RISK_WRITE.value
    channels = lambda: {c.channel for c in db.execute(select(Campaign).where(Campaign.name.startswith(tag))).scalars()}  # noqa: E731
    assert channels() == {"SMS"}
    engine.confirm(action.id)
    db.expire_all()
    assert channels() == {"WHATSAPP"}


def test_compliance_blocks_unsafe_alcohol_copy(db, draft):
    engine, conversation, automation = draft
    unsafe = "Hi #first_name#, drink more tonight, beer is good for your health! Reply STOP to opt out."
    provider = ScriptedProvider([
        call("validate_message_template", template=unsafe),
        call("update_message_template", automation_id=automation.id, template=unsafe),
        call("validate_message_template", template="Hi #first_name#, your #product# awaits."),
    ])
    engine.provider = provider
    _, tools, _ = say(engine, conversation.id, "use this copy")
    report, update, no_stop = (t["result"] for t in tools)
    assert report["data"]["valid"] is False and report["data"]["blocking_findings"]
    assert update["success"] is False
    assert any("opt out" in f["message"].lower() for f in no_stop["data"]["needs_confirmation_findings"])
    db.refresh(automation)
    assert "health" not in automation.message_template


# --------------------------------------------------------------------------
# Permissions, sandbox, providers
# --------------------------------------------------------------------------
def test_viewer_can_read_but_not_change(db, viewer, draft):
    _, _, automation = draft
    engine = CopilotEngine(db, viewer, ScriptedProvider([call("pause_campaign", automation_id=automation.id)]))
    conversation, tools, _ = say(engine, None, "pause it")
    assert tools[0]["result"]["success"] is False and "read-only" in tools[0]["result"]["errors"][0]
    assert conversation.pending_action_id is None


def test_write_previews_run_in_a_rolled_back_sandbox(db, admin):
    before = db.execute(select(func.count(Segment.id))).scalar_one()
    name = f"Sandbox check {next(_RUN)}"
    provider = ScriptedProvider([call("create_segment", name=name,
                                      rule={"op": "AND", "conditions": [{"field": "average_order_value", "operator": "gt", "value": 80}]})])
    engine = CopilotEngine(db, admin, provider)
    conversation, tools, _ = say(engine, None, "make it")
    preview = tools[0]["result"]["data"]["preview"]
    assert preview["result"]["name"] == name and preview["result"]["members"] >= 0
    assert db.execute(select(func.count(Segment.id))).scalar_one() == before


def test_every_tool_declares_risk_and_writes_are_never_read():
    names = {t.name for t in registry.all()}
    assert {"activate_smart_reorder_campaign", "activate_campaign", "resume_campaign", "cancel_campaign",
            "bulk_update_campaigns"} <= {t.name for t in registry.all() if t.risk == Risk.HIGH_RISK_WRITE}
    for name in ("search_customers", "get_campaign", "preview_smart_reorder", "get_revenue_analytics"):
        assert registry.get(name).risk == Risk.READ
    for name in ("create_campaign", "update_campaign", "create_segment", "update_coupon_allocation"):
        assert registry.get(name).risk == Risk.WRITE
    assert len(names) >= 60


def test_provider_selection_and_anthropic_transcript(monkeypatch):
    from app.copilot import providers
    from app.core.config import settings

    monkeypatch.setattr(settings, "AI_PROVIDER", "")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    assert providers.provider_info()["mode"] == "mock"
    monkeypatch.setattr(settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "AI_API_KEY", "test-key-not-real")
    provider = providers.get_ai_provider()
    assert provider.name == "anthropic" and provider.model == "claude-opus-5-5"
    wire = provider._wire_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "x", "arguments": {}},
                                                             {"id": "b", "name": "y", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "a", "name": "x", "content": "{}"},
        {"role": "tool", "tool_call_id": "b", "name": "y", "content": "{}", "is_error": True},
    ])
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in wire[2]["content"]] == ["a", "b"]
    assert wire[2]["content"][1]["is_error"] is True


# --------------------------------------------------------------------------
# API and the end-to-end acceptance test
# --------------------------------------------------------------------------
def test_api_requires_auth_and_lists_tools(client, auth_headers, seeded):
    assert client.post("/api/v1/ai/chat", json={"message": "hi"}).status_code == 401
    response = client.get("/api/v1/ai/tools", headers=auth_headers)
    assert response.status_code == 200
    tools = {t["name"]: t for t in response.json()["tools"]}
    assert tools["activate_smart_reorder_campaign"]["risk"] == "HIGH_RISK_WRITE"
    assert tools["search_customers"]["requires_confirmation"] is False


def test_end_to_end_acceptance_via_api(client, auth_headers, db, beer_regular):
    name = f"Acceptance {next(_RUN)}"

    def chat(message, conversation_id=None):
        response = client.post("/api/v1/ai/chat", headers=auth_headers,
                               json={"message": message, "conversation_id": conversation_id})
        assert response.status_code == 200, response.text
        return response.json()

    def confirm(action_id):
        response = client.post("/api/v1/ai/confirm", headers=auth_headers, json={"action_id": action_id})
        assert response.status_code == 200, response.text
        return response.json()

    view = chat(acceptance_request(name))
    cid = view["id"]
    card = view["context"]["pending_action"]
    assert card["risk"] == "WRITE" and card["preview"]["dry_run"]["final_audience"] >= 1
    tool_names = [m["tool"]["name"] for m in view["messages"] if m["role"] == "tool"]
    assert tool_names[:3] == ["get_smart_reorder_overview", "preview_cohort", "validate_message_template"]

    outcome = confirm(card["id"])
    assert outcome["execution"]["success"] and outcome["conversation"]["context"]["active"]["status"] == "DRAFT"
    automation_id = outcome["conversation"]["context"]["active"]["id"]

    view = chat("Change the coupon split to 50%, 25%, 25%.", cid)
    confirm(view["context"]["pending_action"]["id"])
    view = chat("Change the message to be more Kiwi and dry.", cid)
    assert "Kia ora" in view["context"]["pending_action"]["preview"]["after"]["message_template"]
    confirm(view["context"]["pending_action"]["id"])

    view = chat("Show me a dry run.", cid)
    dry = next(m for m in view["messages"] if m["role"] == "tool" and m["tool"]["name"] == "preview_smart_reorder")
    data = dry["tool"]["data"]
    assert data["final_audience"] >= 1
    assert data["campaign"]["timing"]["minutes_before_predicted_order"] == 30
    assert all(s["coupon_code"] in ("FIRST7", "LUCKY7", "COMEAGAIN7") for s in data["schedule_sample"])
    assert all("Kia ora" in s["message"] for s in data["schedule_sample"])

    view = chat("Activate it.", cid)
    card = view["context"]["pending_action"]
    assert card["risk"] == "HIGH_RISK_WRITE"
    assert db.get(Automation, automation_id).status == "DRAFT"
    outcome = confirm(card["id"])
    assert outcome["execution"]["after_state"]["status"] == "ACTIVE"

    receipts = client.get(f"/api/v1/ai/executions?conversation_id={cid}", headers=auth_headers).json()
    assert [r["tool_name"] for r in receipts][::-1] == [
        "create_smart_reorder_campaign", "update_coupon_allocation", "update_message_template",
        "activate_smart_reorder_campaign",
    ]
    assert client.delete(f"/api/v1/ai/conversations/{cid}", headers=auth_headers).status_code == 200
    assert len(client.get(f"/api/v1/ai/executions?conversation_id={cid}", headers=auth_headers).json()) == 4


def test_oversized_tool_results_stay_valid_json_for_the_model():
    import json

    from app.copilot.engine import MODEL_RESULT_LIMIT, compact_for_model

    big = {"success": True, "errors": [], "metadata": {"count": 5000},
           "data": [{"id": i, "note": "x" * 200} for i in range(5000)]}
    text = compact_for_model(big)
    assert len(text) <= MODEL_RESULT_LIMIT
    parsed = json.loads(text)
    assert parsed["metadata"]["count"] == 5000
    assert any("more not shown" in str(row) for row in parsed["data"])
