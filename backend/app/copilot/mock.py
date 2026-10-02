"""Deterministic offline planner: the Copilot without a language model.

It plays the model's part in the same loop — reads the operator's message and
the tool results so far, and returns either the next tool call or the final
answer — so the whole engine (tools, previews, confirmation, receipts) runs
and is testable with no API key and no network.

It understands the common command shapes by pattern, not by language
understanding. Anything it does not recognise gets an honest "I didn't follow"
with examples. Its answers are composed only from tool results.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.copilot.providers import AIProvider, AIResponse, ToolCall

NUMBER_WORDS = {
    "once": 1, "one": 1, "twice": 2, "two": 2, "thrice": 3, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
PERIODS = [
    (r"last week|previous week", "last_week"),
    (r"this week", "this_week"),
    (r"yesterday", "yesterday"),
    (r"tonight|this evening", "tonight"),
    (r"tomorrow", "tomorrow"),
    (r"next 24 ?h|next 24 hours", "next_24h"),
    (r"next (7|seven) days|next week", "next_7_days"),
    (r"last (7|seven) days|past (7|seven) days|past week", "last_7_days"),
    (r"last (30|thirty) days|last month|past month|past (30|thirty) days", "last_30_days"),
    (r"last (90|ninety) days|last quarter|past (90|ninety) days|past quarter", "last_90_days"),
    (r"\btoday\b|today's", "today"),
]
CONFIRM_WORDS = re.compile(
    r"^(yes|yep|yeah|y|ok|okay|sure|confirm(ed)?|go ahead|do it|approve[d]?|proceed|create it|"
    r"activate it now|ship it|yes,? (please|do it|go ahead|create it))[.! ]*$"
)
COUPON_TOKEN = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z][A-Z0-9]{2,19}\b")


class NeedTool(Exception):
    def __init__(self, name: str, args: dict) -> None:
        self.name, self.arguments = name, args


class ToolFailed(Exception):
    def __init__(self, name: str, errors: list[str]) -> None:
        self.name, self.errors = name, errors


class MockAIProvider(AIProvider):
    name = "mock"
    model = "gimme-copilot-planner-1"
    mode = "mock"

    def chat(self, *, system: str, messages: list[dict], tools: list[dict], context: dict) -> AIResponse:
        text = context.get("user_message") or _last_user(messages)
        planner = Planner(text, context, _steps_since_user(messages))
        step = planner.next()
        if isinstance(step, NeedTool):
            call = ToolCall(id=f"mock_{len(planner.done) + 1}_{step.name}", name=step.name, arguments=step.arguments)
            return AIResponse(text="", tool_calls=[call], provider=self.name, model=self.model,
                              stop_reason="tool_use", usage={"intent": planner.intent})
        return AIResponse(text=step, provider=self.name, model=self.model, stop_reason="end_turn",
                          usage={"intent": planner.intent})


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "user" and not m["content"].startswith("[Engine note"):
            return m["content"].split("\n\n", 1)[-1] if m["content"].startswith("[OPERATIONAL CONTEXT") else m["content"]
    return ""


def _steps_since_user(messages: list[dict]) -> list[dict]:
    """(name, args, result) for every tool call since the operator last spoke."""
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m["role"] == "user" and not m["content"].startswith("[Engine note"):
            start = i + 1
            break
    calls: dict[str, dict] = {}
    steps: list[dict] = []
    for m in messages[start:]:
        if m["role"] == "assistant":
            for c in m.get("tool_calls") or []:
                calls[c["id"]] = c
        elif m["role"] == "tool":
            call = calls.get(m["tool_call_id"], {})
            try:
                result = json.loads(m["content"])
            except ValueError:
                result = {"success": False, "errors": ["unreadable tool result"]}
            steps.append({"name": call.get("name"), "args": call.get("arguments"), "result": result})
    return steps


# --------------------------------------------------------------------------
# Copy the offline planner "writes"
# --------------------------------------------------------------------------
def compose_copy(*, tones: set[str], coupon: bool, objective: str = "reorder") -> str:
    code = "Code #coupon_code# is yours if you fancy a restock. " if coupon else ""
    if "kiwi" in tones or "dry" in tones:
        code = "#coupon_code# if you're keen. " if coupon else ""
        return f"Kia ora #first_name#, your #product# reckons it's been a while. {code}Reply STOP to opt out."
    if "cheeky" in tones or "playful" in tones:
        return f"Hey #first_name#, your #product# misses you. {code}Reply STOP to opt out."
    if objective == "reactivation":
        code = "Here's a code for your next order: #coupon_code#. " if coupon else ""
        return f"Hi #first_name#, it's been a while. Your usual #product# is a few taps away at #link#. {code}Reply STOP to opt out."
    code = "Use #coupon_code# at checkout. " if coupon else ""
    return f"Hi #first_name#, it's about time for your usual #product#. {code}Order at #link#. Reply STOP to opt out."


def _tones(t: str) -> set[str]:
    found = set()
    for word, tone in (("cheek", "cheeky"), ("playful", "playful"), ("kiwi", "kiwi"), ("dry", "dry"),
                       ("friendl", "friendly"), ("formal", "formal"), ("short", "short")):
        if word in t:
            found.add(tone)
    return found


# --------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------
class Planner:
    def __init__(self, text: str, context: dict, done: list[dict]) -> None:
        self.raw = text.strip()
        self.t = re.sub(r"\s+", " ", self.raw.lower())
        self.ctx = context
        self.done = done
        self.cursor = 0
        self.intent = "unknown"

    # -- plumbing ---------------------------------------------------------
    def call(self, name: str, args: dict | None = None, *, allow_fail: bool = False) -> dict:
        args = {k: v for k, v in (args or {}).items() if v not in (None, [], "")}
        if self.cursor < len(self.done):
            step = self.done[self.cursor]
            self.cursor += 1
            result = step["result"]
            if not result.get("success") and not allow_fail:
                raise ToolFailed(name, result.get("errors") or ["unknown error"])
            return result
        raise NeedTool(name, args)

    def next(self):
        try:
            return self.route()()
        except NeedTool as need:
            return need
        except ToolFailed as failed:
            return f"I couldn't complete that — `{failed.name}` reported: {'; '.join(failed.errors)}"

    @property
    def active(self) -> dict | None:
        return self.ctx.get("active")

    def active_is(self, kind: str) -> bool:
        a = self.active
        return bool(a) and a.get("type") == "automation" and (kind == "any" or a.get("kind") == kind)

    def has(self, *patterns: str) -> bool:
        return any(re.search(p, self.t) for p in patterns)

    # -- parsing ----------------------------------------------------------
    def period(self, default: str) -> str:
        for pattern, name in PERIODS:
            if re.search(pattern, self.t):
                return name
        return default

    def number(self, token: str) -> int | None:
        token = token.strip()
        if token.isdigit():
            return int(token)
        return NUMBER_WORDS.get(token)

    def min_orders(self) -> int | None:
        m = re.search(r"at least (\w+)(?: times)?", self.t)
        if m and (n := self.number(m.group(1))):
            return n
        m = re.search(r"more than (\w+) times", self.t)
        if m and (n := self.number(m.group(1))):
            return n + 1
        m = re.search(r"(\w+) or more times|(\w+)\+ times", self.t)
        if m and (n := self.number(m.group(1) or m.group(2))):
            return n
        if re.search(r"ordered \w+ (twice|three times)", self.t):
            return 2 if "twice" in self.t else 3
        return None

    def lapsed_days(self) -> int | None:
        m = re.search(
            r"(?:haven'?t|have not|hasn'?t|has not|not|no orders?|didn'?t|without)\s+(?:ordered|order(?:ing)?|bought|purchased)?"
            r"\s*(?:anything\s+)?(?:in|for|within|during)?\s*(?:the\s+)?(?:last\s+|past\s+)?(\d+)\s+days", self.t)
        if m:
            return int(m.group(1))
        m = re.search(r"lapsed (?:for )?(\d+)\s*days|(\d+)\+? days (?:since|without)", self.t)
        if m:
            return int(m.group(1) or m.group(2))
        return None

    def recent_exclusion_days(self) -> int | None:
        m = re.search(r"exclude (?:customers|people|anyone) who (?:bought|ordered|purchased) in the (?:last|past) (\d+) days", self.t)
        return int(m.group(1)) if m else None

    def categories(self) -> list[str]:
        found = []
        for category in self.ctx.get("categories", []):
            stem = category.lower().rstrip("s")
            if re.search(rf"\b{re.escape(stem)}s?\b", self.t):
                found.append(category)
        return found

    def brands(self) -> list[str]:
        return [b for b in self.ctx.get("brands", []) if len(b) >= 4 and re.search(rf"\b{re.escape(b.lower())}\b", self.t)]

    def money_after(self, *phrases: str) -> float | None:
        for phrase in phrases:
            m = re.search(phrase + r"\s*(?:is\s+)?(?:above|over|more than|greater than|at least|>=?)?\s*\$\s?(\d+(?:\.\d+)?)", self.t)
            if m:
                return float(m.group(1))
        return None

    def channel(self) -> str | None:
        if "whatsapp" in self.t:
            return "WHATSAPP"
        if re.search(r"\bemail\b", self.t):
            return "EMAIL"
        if re.search(r"\bsms\b|\btext message", self.t):
            return "SMS"
        return None

    def minutes(self) -> int | None:
        m = re.search(r"(\d+)\s*(?:min|mins|minutes)\s*(before|after|prior|ahead)", self.t)
        if not m:
            return None
        return -int(m.group(1)) if m.group(2) == "after" else int(m.group(1))

    def coupon_codes(self) -> list[str]:
        """Codes named after the word coupon/code, never ones that are part of a campaign name."""
        m = re.search(r"\b(?:coupons?|codes?)\b(.*?)(?:[.;!?](?:\s|$)|$)", self.raw, re.IGNORECASE | re.DOTALL)
        if not m:
            return []
        name = re.search(r"(?:called|named)\s+[\"“]?(.+?)(?=[\"”]|\s+(?:for|targeting|that|which|to|with|using|from)\b|[.,]|$)",
                         self.raw, re.IGNORECASE)
        excluded = set(COUPON_TOKEN.findall(name.group(1))) if name else set()
        return [c for c in dict.fromkeys(COUPON_TOKEN.findall(m.group(1))) if c not in excluded and c != "SMS"]

    def percentages(self) -> list[float]:
        found = re.findall(r"(\d{1,3}(?:\.\d+)?)\s*%", self.t)
        if found:
            return [float(x) for x in found]
        m = re.search(r"\b(\d{1,3}(?:\s*/\s*\d{1,3})+)\b", self.t)
        return [float(x) for x in re.split(r"\s*/\s*", m.group(1))] if m else []

    def quoted_copy(self) -> str | None:
        m = re.search(r"[\"“](.+?)[\"”]", self.raw)
        if m and "#" in m.group(1):
            return m.group(1).strip()
        m = re.search(r"(?:message|copy|text|sms)\s+to\s*:\s*(.+)$", self.raw, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip().strip('"“”')
        return None

    def cohort(self) -> dict:
        criteria: dict[str, Any] = {}
        if cats := self.categories():
            criteria["categories"] = cats
        if brands := self.brands():
            criteria["brands"] = brands
        if (n := self.min_orders()) is not None:
            criteria["min_matching_orders"] = n
        if (d := self.lapsed_days()) is not None:
            criteria["min_days_since_last_order"] = d
        elif self.has(r"haven'?t ordered recently|lapsed|disappear"):
            criteria["min_days_since_last_order"] = 30
        if (v := self.money_after("lifetime (?:spend|value|revenue)", "spent")) is not None:
            criteria["min_lifetime_revenue"] = v
        if (v := self.money_after("average order(?: value)?", "historically ordered", "aov")) is not None:
            criteria["min_average_order_value"] = v
        if self.has(r"high[- ]value|vip"):
            criteria["lifecycle_stages"] = ["HIGH_VALUE", "VIP"]
        return criteria

    def segment_named(self) -> dict | None:
        for keyword, needle in (("vip", "vip"), ("high value", "high value"), ("high-value", "high value"),
                                ("at risk", "at risk"), ("dormant", "dormant"), ("churned", "churned"),
                                ("regulars", "regular"), ("new customers", "new customers")):
            if keyword in self.t:
                for segment in self.ctx.get("segments", []):
                    if needle in segment["name"].lower():
                        return segment
        return None

    # -- routing ----------------------------------------------------------
    def route(self):
        t = self.t
        if not t or t in ("help", "?") or self.has(r"^what can you do"):
            self.intent = "help"
            return self.help
        if CONFIRM_WORDS.match(t):
            self.intent = "typed_confirmation"
            return self.typed_confirmation
        if self.has(r"\bwhy\b") and self.has(r"receive|get |got|sent|send|cancel|reminder|message"):
            self.intent = "diagnose"
            return self.diagnose
        if self.has(r"dry[- ]run", r"preview (this|the|my)? ?(campaign|it|draft)", r"final preview", r"show me (a |the )?preview"):
            self.intent = "dry_run"
            return self.dry_run
        if self.has(r"\b(pause|resume|cancel|stop)\b.*\ball\b", r"\ball\b.*\b(campaigns?)\b.*\bto (whatsapp|sms|email)\b",
                    r"change all"):
            self.intent = "bulk"
            return self.bulk
        if self.has(r"\bactivate\b", r"go live", r"switch (it )?on", r"turn (it )?on", r"\blaunch\b", r"start sending"):
            self.intent = "activate"
            return self.activate
        if self.has(r"^(please )?(pause|resume|unpause|cancel|archive|stop)\b") and not self.has(r"message #?\d+"):
            self.intent = "lifecycle"
            return self.lifecycle
        if self.has(r"cancel (scheduled )?message #?(\d+)"):
            self.intent = "cancel_message"
            return self.cancel_message
        if self.has(r"\b(show|open|select|use|load|work on|switch to)\b (the )?campaign #?[ac]?\d+"):
            self.intent = "open_campaign"
            return self.open_campaign
        creating = self.has(r"\b(create|set up|setup|build|start|new)\b", r"\bmake (a|an|me|us)\b")
        if (self.has(r"coupon|split|allocation") and (self.percentages() or self.coupon_codes())
                and not (creating and self.has(r"campaign"))):
            self.intent = "coupons"
            return self.coupons
        if self.has(r"\b(message|copy|wording|sms text)\b") and self.has(r"\b(change|make|rewrite|update|edit|use|more|less)\b") \
                and not (creating and self.has(r"campaign")):
            self.intent = "copy"
            return self.copy
        if not creating and (self.minutes() is not None or self.has(r"low[- ]confidence")):
            self.intent = "timing"
            return self.timing
        if not creating and self.has(r"instead of|switch to|change (the )?channel|use (whatsapp|sms|email)"):
            self.intent = "channel"
            return self.change_channel
        if not creating and self.has(r"make the audience|only customers|exclude customers|audience to") and self.active_is("NUDGE"):
            self.intent = "audience"
            return self.audience
        if creating and self.has(r"smart reorder|reorder (reminder|campaign)|reminder campaign"):
            self.intent = "create_smart_reorder"
            return self.create_smart_reorder
        if creating and self.has(r"\bsegment\b"):
            self.intent = "create_segment"
            return self.create_segment
        if creating and self.has(r"\bcohort\b"):
            self.intent = "create_cohort"
            return self.create_cohort
        if creating and self.has(r"campaign"):
            self.intent = "create_campaign"
            return self.create_campaign
        if self.has(r"\bexport\b"):
            self.intent = "export"
            return self.export
        if self.has(r"disappear|dropping off|drifting away|falling off"):
            self.intent = "investigate"
            return self.investigate
        if self.has(r"predicted|likely to (re)?order|expected to order|predicted reorders|going to order"):
            self.intent = "predictions"
            return self.predictions
        if self.has(r"scheduled|smart reorder messages|in the queue|reminders (today|tonight)"):
            self.intent = "scheduled"
            return self.scheduled
        if self.has(r"revenue|reactivated|coupon (revenue|performance)|converted|conversion|losing money|"
                    r"performance|accuracy|retention|delivery (rate|analytics)|how (are|is) .* doing"):
            self.intent = "analytics"
            return self.analytics
        if self.has(r"\bcampaigns?\b") and self.has(r"running|active|list|show|which|ending|zero|draft|what"):
            self.intent = "list_campaigns"
            return self.list_campaigns
        if self.has(r"\bsegments?\b"):
            self.intent = "segments"
            return self.segments
        if self.has(r"\bproducts?\b|repeat purchase"):
            self.intent = "products"
            return self.products
        if self.has(r"system status|errors?|scheduler|background jobs|health"):
            self.intent = "system"
            return self.system
        if self.has(r"customers?|people|who are|vip|lapsed"):
            self.intent = "search_customers"
            return self.search_customers
        return self.unknown

    # -- intents ----------------------------------------------------------
    def help(self) -> str:
        return (
            "I can inspect and operate the Retention Engine. For example:\n"
            "- **Smart Reorder:** \"Show today's predicted reorders\", \"Create a Smart Reorder campaign for customers who "
            "ordered beer at least twice and haven't ordered in 14 days\", \"Show me a dry run\", \"Activate it\"\n"
            "- **Customers:** \"Find my highest-value lapsed customers\", \"Why didn't Sarah Patel get her reminder?\"\n"
            "- **Segments:** \"Create a segment for customers whose average order value is above $80\", \"Export this segment\"\n"
            "- **Analytics:** \"How much revenue did Smart Reorder generate last week?\", \"Show campaign performance\"\n"
            "Changes are always shown as a preview first and only happen when you press **Confirm**."
        )

    def typed_confirmation(self) -> str:
        pending = self.ctx.get("pending_action")
        if pending:
            return (f"I can't act on a typed approval. To go ahead with **#{pending['id']} {pending['summary']}**, "
                    "press **Confirm** on its card (or Cancel).")
        return "There's nothing waiting for confirmation right now. Tell me what you'd like to do."

    def unknown(self) -> str:
        return ("I'm running on the offline planner (no AI model is configured) and didn't recognise that request, "
                "so I haven't done anything. " + self.help())

    # Customers -----------------------------------------------------------
    def search_customers(self) -> str:
        if self.segment_named() and not self.lapsed_days() and not self.money_after("average order(?: value)?", "historically ordered"):
            segment = self.segment_named()
            res = self.call("get_segment", {"segment_id": segment["id"]})
            data = res["data"]
            rows = "\n".join(f"| {s['id']} | {s['name']} | {s.get('lifecycle_stage') or ''} |" for s in data["sample"])
            return (f"**{data['name']}** has **{data['current_members']}** members ({data['definition']}).\n\n"
                    f"| ID | Customer | Stage |\n|---|---|---|\n{rows}\n\nSay \"create a campaign for them\" to target them.")
        args: dict[str, Any] = {}
        notes = []
        if (d := self.lapsed_days()) is not None:
            args["min_days_since_last_order"] = d
        elif self.has(r"haven'?t ordered recently|lapsed"):
            args["min_days_since_last_order"] = 30
            notes.append("I read \"lapsed / not recently\" as no order for 30+ days.")
        if (v := self.money_after("historically ordered", "average order(?: value)?", "aov", "spend(?:s)? per order")) is not None:
            args["min_average_order_value"] = v
            if "historically" in self.t:
                notes.append(f"I read \"historically ordered more than ${v:g}\" as an average order value of ${v:g}+.")
        if (v := self.money_after("lifetime (?:spend|value|revenue)", "spent")) is not None:
            args["min_lifetime_revenue"] = v
        if self.has(r"highest[- ]value|most valuable|top|best") :
            args["sort_by"] = "lifetime_revenue"
        m = re.search(r"\b(\d{1,3})\s+(?:highest|most|top|best)", self.t) or re.search(r"top (\d{1,3})", self.t)
        args["limit"] = min(int(m.group(1)), 100) if m else 10
        if cats := self.categories():
            args["preferred_category"] = cats[0]
        res = self.call("search_customers", args)
        rows = res["data"]
        meta = res["metadata"]
        table = "\n".join(
            f"| {r['id']} | {r['name']} | ${r['lifetime_revenue']:,.2f} | ${r['average_order_value']:,.2f} | "
            f"{r['days_since_last_order'] if r['days_since_last_order'] is not None else '–'} |"
            for r in rows
        )
        filters = ", ".join(meta.get("filters") or []) or "no filters"
        return (
            f"**{meta['count']}** customers match ({filters}).\n\n"
            + (" ".join(notes) + "\n\n" if notes else "")
            + ("| ID | Customer | Lifetime | Avg order | Days since order |\n|---|---|---|---|---|\n" + table if rows else "")
            + ("\n\nSay \"create a campaign for them\" or \"create a segment from them\" to act on this list." if rows else "")
        )

    def diagnose(self) -> str:
        m = re.search(
            r"why (?:did|didn'?t|did not|hasn'?t|has not|wasn'?t|was|doesn'?t|isn'?t|haven'?t|won'?t)\s+"
            r"([^\W\d_][\w'-]*(?:\s+[^\W\d_][\w'-]*)?)",
            self.raw, re.IGNORECASE)
        name = m.group(1) if m else None
        if name and name.lower().split()[0] in ("this", "the", "my", "that", "it", "they", "he", "she", "a"):
            name = None
        if name:
            name = re.sub(r"\s+(receive|get|got|not|have|been|ever)$", "", name, flags=re.IGNORECASE)
        customer = name
        if not customer:
            active = self.active
            if active and active.get("type") == "customer":
                customer = str(active["id"])
            else:
                return "Which customer? Give me their name, email or customer id."
        period = self.period("last_7_days")
        res = self.call("diagnose_customer_delivery", {"customer": customer, "period": period})
        data = res["data"]
        lines = [f"**{data['customer']['name']}** — {data['period']}:"]
        lines += [f"- {f}" for f in data["findings"]]
        orders = data.get("orders_in_period") or []
        if orders:
            lines.append("\nOrders in that window: " + ", ".join(f"{o['ordered_at_local']} ({o['status']})" for o in orders))
        pred = data.get("prediction") or {}
        if pred.get("predicted_next_order_local"):
            lines.append(f"Current prediction: next order {pred['predicted_next_order_local']} (confidence {pred['confidence']}).")
        return "\n".join(lines)

    # Smart Reorder -------------------------------------------------------
    def predictions(self) -> str:
        period = self.period("today")
        res = self.call("get_smart_reorder_predictions", {"period": period})
        rows, meta = res["data"], res["metadata"]
        table = "\n".join(f"| {r['name']} | {r['predicted_order_local']} | {r['confidence']} | {r.get('last_order_product') or '–'} |" for r in rows)
        return (f"**{meta['count']}** contactable customers are predicted to order {meta['period']} "
                f"(confidence ≥ {meta['min_confidence']}).\n\n"
                + ("| Customer | Predicted | Confidence | Last product |\n|---|---|---|---|\n" + table if rows else "")
                + ("\n\nSay \"create a reminder campaign for them\" to act on this." if rows else ""))

    def scheduled(self) -> str:
        period = self.period("today")
        args = {"period": period}
        if self.active_is("NUDGE") and self.has(r"this campaign|its "):
            args["automation_id"] = self.active["id"]
        res = self.call("get_scheduled_messages", args)
        data, meta = res["data"], res["metadata"]
        counts = ", ".join(f"{k.lower()}: {v}" for k, v in data["by_status"].items()) or "none"
        rows = "\n".join(f"| {m['customer_name']} | {m['scheduled_at_local_label']} | {m['status']} | {m.get('cancellation_reason') or ''} |"
                         for m in data["messages"][:15])
        return (f"Smart Reorder messages for {meta['period']}: **{data['total']}** ({counts}).\n\n"
                + ("| Customer | Send time | Status | Cancelled because |\n|---|---|---|---|\n" + rows if rows else ""))

    def dry_run(self) -> str:
        if self.active_is("NUDGE"):
            res = self.call("preview_smart_reorder", {"automation_id": self.active["id"]})
            return self.render_dry_run(res["data"], "DRY RUN")
        a = self.active
        if a and a.get("type") in ("automation", "campaign"):
            key = "automation_id" if a["type"] == "automation" else "campaign_id"
            res = self.call("preview_campaign", {key: a["id"]})
            data = res["data"]
            audience = data.get("audience") or {}
            if audience:
                return (f"**Preview of {a['name']}** — eligible {audience.get('eligible_count')} of "
                        f"{audience.get('audience_size')}; excluded: {audience.get('excluded_by_reason')}.")
            return f"**Preview of {a['name']}**:\n```\n{json.dumps(data, indent=1, default=str)[:1500]}\n```"
        return "Which campaign should I dry-run? Open one first (e.g. \"show campaign 12\")."

    @staticmethod
    def render_dry_run(d: dict, title: str) -> str:
        c = d["campaign"]
        timing = c.get("timing") or {}
        excluded = "\n".join(f"- {k}: {v}" for k, v in d["excluded"].items()) or "- none"
        coupons = "\n".join(f"- {k}: {v}" for k, v in d["coupon_allocation"].items()) or "- no coupon codes"
        sample = "\n".join(
            f"| {s['customer']} | {s['send_at_local']} | "
            f"{'moved into send window' if s.get('moved_for_send_window') else str(s['minutes_before']) + ' min before'} | "
            f"{s.get('coupon_code') or '–'} | {s.get('product') or '–'} |"
            for s in d["schedule_sample"]
        )
        check = d["message_check"]
        compliance = "passes compliance" if check["sendable_without_review"] else (
            "**needs attention**: " + "; ".join(check["blocking"] + check["needs_confirmation"]))
        sms = check.get("sms") or {}
        return (
            f"**{title} — {c['name']}** ({c['status']}, {c['channel']}, "
            f"{timing.get('minutes_before_predicted_order')} min before predicted order, min confidence {timing.get('min_confidence')})\n\n"
            f"Customers analysed: **{d['analysed']}**\n\nExcluded:\n{excluded}\n\n"
            f"Final audience (next {d['horizon_days']} days): **{d['final_audience']}**  ·  "
            f"messages {d['today_label']}: **{d['messages_today']}**, tonight: **{d['messages_tonight']}**\n\n"
            f"Coupon allocation:\n{coupons}\n\n"
            + ("| Customer | Send | Timing | Coupon | Product |\n|---|---|---|---|---|\n" + sample + "\n\n" if sample else "")
            + f"Copy {compliance}" + (f" · {sms.get('characters')} chars, {sms.get('segments')} SMS segment(s)" if sms else "")
            + ("\n\nWarnings: " + "; ".join(check["warnings"]) if check["warnings"] else "")
        )

    def create_smart_reorder(self) -> str:
        cohort = self.cohort()
        segment = self.segment_named() if not cohort.get("categories") and not cohort.get("brands") else None
        codes = self.coupon_codes()
        if not cohort and not segment and not self.has(r"for (them|those|these)"):
            return ("I can set that up. Who should it target? For example: \"customers who ordered beer at least twice "
                    "and haven't ordered in 14 days\", the VIP segment, or a list I've just found. Unless you say "
                    "otherwise I'll use SMS, 30 minutes before each customer's predicted reorder, minimum confidence 70, "
                    "and at most one reminder per 7 days.")
        self.call("get_smart_reorder_overview")
        audience: dict[str, Any]
        if self.has(r"for (them|those|these)") and self.ctx.get("last_result_set"):
            audience = {"use_last_result": True}
            label = self.ctx["last_result_set"]["description"]
        elif segment and not cohort.get("min_matching_orders"):
            audience = {"segment_id": segment["id"]}
            label = f"segment {segment['name']}"
        else:
            preview = self.call("preview_cohort", cohort)
            audience = {"cohort": cohort}
            label = preview["data"]["definition"]
        copy = self.quoted_copy() or compose_copy(tones=_tones(self.t), coupon=bool(codes) or "coupon" in self.t)
        channel = self.channel() or "SMS"
        validation = self.call("validate_message_template", {"template": copy, "channel": channel, "coupon_codes": codes})
        if not validation["data"]["valid"]:
            problems = validation["data"]["unknown_tags"] + [f["message"] for f in validation["data"]["blocking_findings"]]
            return "The message wouldn't pass checks, so I haven't prepared the campaign: " + "; ".join(problems)
        allocation = self.percentages()
        coupons = [{"code": c} for c in codes]
        if coupons and len(allocation) == len(coupons) and not self.has(r"equal"):
            coupons = [{"code": c, "allocation": a} for c, a in zip(codes, allocation)]
        name = self.campaign_name(label)
        args = {
            "name": name,
            **audience,
            "channel": channel,
            "minutes_before": self.minutes() if self.minutes() is not None else 30,
            "message_template": copy,
            "coupons": coupons,
        }
        res = self.call("create_smart_reorder_campaign", args)
        return self.render_pending(res, intro=f"Here's the plan for **{name}** — audience: {label}.\n\nMessage: \"{copy}\"")

    def campaign_name(self, label: str) -> str:
        m = re.search(r"(?:called|named)\s+[\"“]?(.+?)(?=[\"”]|\s+(?:for|targeting|that|which|to|with|using|from)\b|[.,]|$)", self.raw, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        cats = self.categories() or self.brands()
        bits = ["Smart Reorder"] + ([cats[0]] if cats else []) + ([f"lapsed {self.lapsed_days()}d"] if self.lapsed_days() else [])
        return " — ".join([bits[0], " ".join(bits[1:])]) if len(bits) > 1 else f"Smart Reorder — {label[:40]}"

    def render_pending(self, res: dict, *, intro: str = "") -> str:
        data = res["data"]
        if data.get("status") == "ALREADY_DONE":
            return data["message"]
        preview = data["preview"]
        parts = [intro] if intro else []
        if preview.get("dry_run"):
            d = preview["dry_run"]
            top = ", ".join(f"{k.lower()} ({v})" for k, v in list(d["excluded"].items())[:3]) or "none"
            split = ", ".join(f"{k} {v}" for k, v in d["coupon_allocation"].items())
            check = d["message_check"]
            parts.append(
                f"Engine preview: **{d['final_audience']}** customers would get a reminder in the next "
                f"{d['horizon_days']} days (of {d['analysed']} analysed) — **{d['messages_today']}** {d['today_label']}. "
                f"Main exclusions: {top}."
                + (f" Coupons: {split}." if split else "")
                + (" The copy passes compliance." if check["sendable_without_review"]
                   else " Compliance: " + "; ".join(check["blocking"] + check["needs_confirmation"]) + ".")
            )
        after = preview.get("after") or {}
        before = preview.get("before") or {}
        if before and after:
            changed = [k for k in after if before.get(k) != after.get(k) and k not in ("dry_run",)]
            for key in changed[:6]:
                parts.append(f"- **{key}**: {json.dumps(before.get(key), default=str)} → {json.dumps(after.get(key), default=str)}")
        result = preview.get("result") or {}
        if result.get("coupon_codes_added_to_brand_settings"):
            parts.append("Your coupon codes " + ", ".join(result["coupon_codes_added_to_brand_settings"])
                         + " will be added to Brand Settings' verified list so compliance accepts them.")
        if data["risk"] == "HIGH_RISK_WRITE":
            parts.append(f"⚠️ **{data['summary']}**. This needs your explicit confirmation.")
        parts.append(f"Nothing has changed yet — review action **#{data['action_id']}** and press **Confirm** to go ahead, or Cancel.")
        return "\n\n".join(p for p in parts if p)

    def target_args(self) -> dict | None:
        a = self.active
        if not a or a.get("type") not in ("automation", "campaign"):
            return None
        return {"automation_id": a["id"]} if a["type"] == "automation" else {"campaign_id": a["id"]}

    def activate(self) -> str:
        target = self.target_args()
        if target is None:
            return "Which campaign should I activate? Open it first (e.g. \"show campaign 12\")."
        if self.active_is("NUDGE"):
            res = self.call("activate_smart_reorder_campaign", target, allow_fail=True)
        else:
            res = self.call("activate_campaign", target, allow_fail=True)
        if not res.get("success"):
            return "I can't activate it yet: " + "; ".join(res.get("errors") or [])
        return self.render_pending(res, intro=f"Activating **{self.active['name']}**.")

    def lifecycle(self) -> str:
        verb = re.match(r"(?:please )?(\w+)", self.t).group(1)
        target = self.target_args()
        m = re.search(r"campaign #?(\d+)", self.t)
        if m:
            target = {"automation_id": int(m.group(1))}
        if target is None:
            return f"Which campaign should I {verb}? Name it or open it first."
        tool_name = {"pause": "pause_campaign", "stop": "pause_campaign", "resume": "resume_campaign",
                     "unpause": "resume_campaign", "cancel": "cancel_campaign", "archive": "cancel_campaign"}[verb]
        if tool_name == "resume_campaign":
            target = {"automation_id": target.get("automation_id")}
        res = self.call(tool_name, target, allow_fail=True)
        if not res.get("success"):
            return f"I can't {verb} it: " + "; ".join(res.get("errors") or [])
        return self.render_pending(res)

    def bulk(self) -> str:
        action = "pause" if "pause" in self.t else "resume" if "resume" in self.t else "cancel" if self.has(r"cancel|stop") else "set_channel"
        filters: dict[str, Any] = {}
        if "smart reorder" in self.t:
            filters["kind"] = "smart_reorder"
        if "active" in self.t or action == "pause":
            filters["status"] = "ACTIVE"
        if "draft" in self.t:
            filters["status"] = "DRAFT"
        if action == "resume":
            filters["status"] = "PAUSED"
        args = {"action": action, **filters}
        if action == "set_channel":
            m = re.search(r"\b(sms|whatsapp|email)\b.*\bto (whatsapp|sms|email)\b", self.t)
            if not m:
                return "Which channel should they move to?"
            args["channel"] = m.group(1).upper()
            args["new_channel"] = m.group(2).upper()
        res = self.call("bulk_update_campaigns", args, allow_fail=True)
        if not res.get("success"):
            return "Nothing to do: " + "; ".join(res.get("errors") or [])
        matched = res["data"]["preview"]["before"]["matched"]
        listing = "\n".join(f"- {m['name']} ({m['kind']}, {m['status']}, {m['channel']})" for m in matched)
        return (f"This will modify **{len(matched)}** campaign(s):\n{listing}\n\n"
                + self.render_pending(res))

    def cancel_message(self) -> str:
        m = re.search(r"message #?(\d+)", self.t)
        res = self.call("cancel_scheduled_message", {"message_id": int(m.group(1))}, allow_fail=True)
        if not res.get("success"):
            return "I can't cancel it: " + "; ".join(res.get("errors") or [])
        return self.render_pending(res)

    def coupons(self) -> str:
        if not self.active_is("any"):
            return "Which campaign's coupons? Open the campaign first."
        current = [c for c in self.active.get("coupons", []) if c.get("enabled")]
        codes = self.coupon_codes()
        pcts = self.percentages()
        if codes:
            coupons = [{"code": c} for c in codes]
            if len(pcts) == len(codes):
                coupons = [{"code": c, "allocation": p} for c, p in zip(codes, pcts)]
        elif pcts:
            if len(pcts) != len(current):
                return (f"The campaign has {len(current)} coupon code(s) ({', '.join(c['code'] for c in current) or 'none'}), "
                        f"but you gave {len(pcts)} percentages. Which split goes with which code?")
            coupons = [{"code": c["code"], "allocation": p} for c, p in zip(current, pcts)]
        else:
            return "What should the coupon codes or split be?"
        res = self.call("update_coupon_allocation", {"automation_id": self.active["id"], "coupons": coupons}, allow_fail=True)
        if not res.get("success"):
            return "I can't make that change: " + "; ".join(res.get("errors") or [])
        split = ", ".join(f"{c['code']} {c.get('allocation', 'equal')}{'%' if c.get('allocation') else ''}" for c in coupons)
        return self.render_pending(res, intro=f"New coupon split: {split}.")

    def copy(self) -> str:
        target = self.target_args()
        if target is None:
            return "Which campaign's message? Open the campaign first."
        explicit = self.quoted_copy()
        current = (self.active or {}).get("message_template") or (self.active or {}).get("body") or ""
        has_coupon = "coupon_code" in current or bool([c for c in (self.active or {}).get("coupons", []) if c.get("enabled")])
        copy = explicit or compose_copy(tones=_tones(self.t) or {"default"}, coupon=has_coupon)
        channel = (self.active or {}).get("channel", "SMS")
        validation = self.call("validate_message_template", {"template": copy, "channel": channel,
                                                              **({"automation_id": target["automation_id"]} if "automation_id" in target else {})})
        v = validation["data"]
        if not v["valid"]:
            return "That copy won't pass checks, so I haven't changed anything: " + "; ".join(
                v["unknown_tags"] + [f["message"] for f in v["blocking_findings"]])
        res = self.call("update_message_template", {"template": copy, **target}, allow_fail=True)
        if not res.get("success"):
            return "I can't change the message: " + "; ".join(res.get("errors") or [])
        sms = v.get("sms") or {}
        return self.render_pending(
            res,
            intro=f"New copy: \"{copy}\"\n\nSample: \"{v['sample_render']}\""
            + (f" ({sms.get('characters')} chars, {sms.get('segments')} segment(s))" if sms else ""),
        )

    def open_campaign(self) -> str:
        m = re.search(r"campaign #?([ac]?)(\d+)", self.t)
        key = "campaign_id" if m.group(1) == "c" else "automation_id"
        res = self.call("get_campaign", {key: int(m.group(2))})
        d = res["data"]
        details = [f"**{d['name']}** — {d.get('kind_label', 'One-off')} campaign, {d['status']}, {d['channel']}."]
        if d.get("timing"):
            t = d["timing"]
            details.append(f"Sends {t['minutes_before_predicted_order']} min before each predicted order; "
                           f"minimum confidence {t['min_confidence']}; at most one reminder per {t['min_gap_days']} days.")
        if d.get("coupons"):
            details.append("Coupons: " + ", ".join(f"{c['code']} {c['allocation']:g}%" for c in d["coupons"] if c["enabled"]))
        if d.get("message_template") or d.get("body"):
            details.append(f"Copy: \"{d.get('message_template') or d.get('body')}\"")
        details.append("I'm now working on this campaign — ask me to change it, dry-run it or activate it.")
        return "\n\n".join(details)

    def timing(self) -> str:
        if not self.active_is("NUDGE"):
            return "Which Smart Reorder campaign? Open it first (e.g. \"show campaign 12\")."
        args: dict[str, Any] = {"automation_id": self.active["id"]}
        if (m := self.minutes()) is not None:
            args["minutes_before"] = m
        if self.has(r"low[- ]confidence"):
            current = (self.active.get("timing") or {}).get("min_confidence") or 70
            args["min_confidence"] = max(current, 80)
        res = self.call("update_smart_reorder_campaign", args, allow_fail=True)
        if not res.get("success"):
            return "I can't make that change: " + "; ".join(res.get("errors") or [])
        return self.render_pending(res)

    def change_channel(self) -> str:
        target = self.target_args()
        channel = None
        m = re.search(r"(?:use|switch to|change (?:the )?channel to)\s+(whatsapp|sms|email)", self.t)
        if m:
            channel = m.group(1).upper()
        if target is None or channel is None:
            return "Which campaign, and which channel (SMS, WhatsApp or email)?"
        tool_name = "update_smart_reorder_campaign" if self.active_is("NUDGE") else "update_campaign"
        res = self.call(tool_name, {**target, "channel": channel}, allow_fail=True)
        if not res.get("success"):
            return "I can't switch the channel: " + "; ".join(res.get("errors") or [])
        return self.render_pending(res)

    def audience(self) -> str:
        current = dict((self.active.get("audience") or {}).get("cohort_query") or {})
        criteria = {**current, **self.cohort()}
        if (d := self.recent_exclusion_days()) is not None:
            criteria["min_days_since_last_order"] = max(criteria.get("min_days_since_last_order") or 0, d)
        res = self.call("update_smart_reorder_campaign", {"automation_id": self.active["id"], "cohort": criteria}, allow_fail=True)
        if not res.get("success"):
            return "I can't change the audience: " + "; ".join(res.get("errors") or [])
        return self.render_pending(res, intro="Updated audience criteria: " + json.dumps(criteria))

    # Segments / cohorts / campaigns --------------------------------------
    def create_segment(self) -> str:
        name_m = re.search(r"(?:called|named)\s+[\"“]?(.+?)(?=[\"”]|\s+(?:for|targeting|that|which|to|with|using|from)\b|[.,]|$)", self.raw, re.IGNORECASE)
        if self.has(r"from (them|those|these|this list)|for (them|those|these)"):
            name = name_m.group(1).strip() if name_m else f"Copilot list — {(self.ctx.get('last_result_set') or {}).get('description', 'customers')[:60]}"
            res = self.call("create_segment", {"name": name, "use_last_result": True}, allow_fail=True)
            if not res.get("success"):
                return "I can't create it: " + "; ".join(res.get("errors") or [])
            return self.render_pending(res, intro=f"Segment **{name}** from the last list.")
        conditions = []
        if (v := self.money_after("average order(?: value)?", "aov")) is not None:
            conditions.append({"field": "average_order_value", "operator": "gt", "value": v})
        if (v := self.money_after("lifetime (?:spend|value|revenue)", "spent", "bought")) is not None:
            if self.has(r"last \d+ days|past \d+ days"):
                return ("Spend within a time window isn't a field segments can filter on yet. I can do lifetime spend "
                        "or average order value — which would you like?")
            conditions.append({"field": "lifetime_revenue", "operator": "gt", "value": v})
        if (d := self.lapsed_days()) is not None:
            conditions.append({"field": "days_since_last_order", "operator": "gte", "value": d})
        if not conditions:
            return "What should define the segment? e.g. \"average order value above $80\" or \"no order in 30 days\"."
        rule = {"op": "AND", "conditions": conditions}
        preview = self.call("preview_segment", {"rule": rule})
        name = name_m.group(1).strip() if name_m else preview["data"]["definition"][:80]
        res = self.call("create_segment", {"name": name, "rule": rule}, allow_fail=True)
        if not res.get("success"):
            return "I can't create it: " + "; ".join(res.get("errors") or [])
        return self.render_pending(
            res, intro=f"**{name}** — {preview['data']['definition']}: currently **{preview['data']['matched_customers']}** customers.")

    def create_cohort(self) -> str:
        criteria = self.cohort()
        if not criteria:
            return "What should the cohort be based on — a category or brand, how many orders, how recent?"
        preview = self.call("preview_cohort", criteria)
        name = preview["data"]["definition"][:80]
        res = self.call("create_cohort", {"name": name, **criteria}, allow_fail=True)
        if not res.get("success"):
            return "I can't create it: " + "; ".join(res.get("errors") or [])
        return self.render_pending(res, intro=f"Cohort **{name}**: {preview['data']['matched']} customers today.")

    def create_campaign(self) -> str:
        objective = "REACTIVATION" if self.has(r"reactivat|bring back|win ?back|lapsed") else "RETENTION"
        last = self.ctx.get("last_result_set")
        if self.has(r"for (them|those|these)") and last:
            audience = {"use_last_result": True}
            label = last["description"]
        else:
            cohort = self.cohort()
            segment = self.segment_named()
            if cohort.get("categories") or cohort.get("brands") or cohort.get("min_days_since_last_order"):
                audience, label = {"cohort": cohort}, "a purchase cohort"
            elif segment:
                audience, label = {"segment_id": segment["id"]}, f"segment {segment['name']}"
            else:
                return ("I can create that. I need:\n- **Target audience** (a segment, or e.g. \"lapsed beer customers\")\n"
                        "- **Products / category**\n- **Offer** (only one you give me, e.g. a coupon code)\n"
                        "- **Channel** (SMS, WhatsApp or email)\n- **Start / end date**\n\n"
                        "Or say \"use defaults\" for SMS, no offer, and a draft you can schedule later.")
        channel = self.channel() or "SMS"
        codes = self.coupon_codes()
        body = self.quoted_copy() or compose_copy(tones=_tones(self.t), coupon=bool(codes),
                                                  objective="reactivation" if objective == "REACTIVATION" else "reorder")
        if codes and "#coupon_code#" in body:
            body = body.replace("#coupon_code#", codes[0])
        validation = self.call("validate_message_template", {"template": body, "channel": channel})
        if not validation["data"]["valid"]:
            return "The message wouldn't pass checks: " + "; ".join(
                validation["data"]["unknown_tags"] + [f["message"] for f in validation["data"]["blocking_findings"]])
        name_m = re.search(r"(?:called|named)\s+[\"“]?(.+?)(?=[\"”]|\s+(?:for|targeting|that|which|to|with|using|from)\b|[.,]|$)", self.raw, re.IGNORECASE)
        short = label if len(label) <= 40 else label[:40].rsplit(" ", 1)[0] + "…"
        name = name_m.group(1).strip() if name_m else f"{objective.title()} — {short}"
        res = self.call("create_campaign", {"name": name, "objective": objective, "channel": channel,
                                            "body": body, **audience}, allow_fail=True)
        if not res.get("success"):
            return "I can't create it: " + "; ".join(res.get("errors") or [])
        return self.render_pending(res, intro=f"Draft **{name}** for {label} via {channel}.\n\nMessage: \"{body}\"")

    def export(self) -> str:
        segment = self.segment_named()
        if segment:
            res = self.call("export_segment", {"segment_id": segment["id"]})
        elif self.active and self.active.get("type") == "segment":
            res = self.call("export_segment", {"segment_id": self.active["id"]})
        else:
            return "Which segment should I export?"
        d = res["data"]
        return f"The export of **{d['segment']}** ({d['members']} customers) is ready — use the download button below."

    def investigate(self) -> str:
        base = self.cohort()
        base.pop("min_days_since_last_order", None)
        if not (base.get("brands") or base.get("categories")):
            m = re.search(r"(?:ordered|bought|buy|buying)\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)", self.raw)
            if not m:
                return "Which product, brand or category are you worried about?"
            word = m.group(1)
            found = self.call("search_products", {"query": word})
            if not found["data"]:
                return (f"No product, brand or category called \"{word}\" appears in the order history, "
                        "so there's no data to investigate. Check the spelling, or name a category such as Beer.")
            base["products"] = [word]
        everyone = self.call("preview_cohort", base)
        lapsed = self.call("preview_cohort", {**base, "min_days_since_last_order": 30})
        total, gone = everyone["data"]["matched"], lapsed["data"]["matched"]
        share = f"{gone / total:.0%}" if total else "n/a"
        return (f"{everyone['data']['definition']}: **{total}** customers. Of those, **{gone}** ({share}) haven't ordered "
                f"anything in 30+ days. That's the order-history picture; it doesn't say why they stopped.\n\n"
                "Say \"create a reactivation campaign for those customers\" to target the lapsed group.")

    def list_campaigns(self) -> str:
        filters: dict[str, Any] = {}
        if self.has(r"running|active|live"):
            filters["status"] = "ACTIVE"
        if self.has(r"draft"):
            filters["status"] = "DRAFT"
        if "smart reorder" in self.t:
            filters["kind"] = "smart_reorder"
        if self.has(r"ending this week|ending soon"):
            filters["ending_within_days"] = 7
        if self.has(r"zero conversions|no conversions"):
            filters["zero_conversions"] = True
        res = self.call("list_campaigns", filters)
        rows = res["data"]
        table = "\n".join(f"| {r['type'][0].upper()}{r['id']} | {r['name']} | {r['kind']} | {r['status']} | {r['channel']} | "
                          f"{r['messages_sent']} | {r['conversions']} | ${r['attributed_revenue']:,.2f} |" for r in rows)
        return (f"**{len(rows)}** campaign(s) match.\n\n"
                + ("| Ref | Name | Type | Status | Channel | Sent | Conv. | Revenue |\n|---|---|---|---|---|---|---|---|\n" + table if rows else ""))

    def segments(self) -> str:
        if (segment := self.segment_named()):
            res = self.call("get_segment", {"segment_id": segment["id"]})
            d = res["data"]
            return f"**{d['name']}**: {d['current_members']} members — {d['definition']}."
        res = self.call("list_segments", {})
        rows = "\n".join(f"| {s['id']} | {s['name']} | {s['members']} | {s['segment_type']} |" for s in res["data"])
        return f"| ID | Segment | Members | Type |\n|---|---|---|---|\n{rows}"

    def products(self) -> str:
        period = self.period("last_30_days")
        by = "repeat_rate" if self.has(r"repeat") else "units" if self.has(r"units|volume") else "revenue"
        cats = self.categories()
        res = self.call("get_top_products", {"by": by, "period": period, **({"category": cats[0]} if cats else {})})
        def rate(r: dict) -> str:
            value = r.get("repeat_purchase_rate")
            return f"{value:.0%}" if value is not None else "–"

        rows = "\n".join(
            f"| {r['product']} | {r['category']} | {r['units']} | ${r['revenue']:,.2f} | {rate(r)} |"
            for r in res["data"])
        return (f"Top products by {by.replace('_', ' ')} — {res['metadata']['period']}:\n\n"
                f"| Product | Category | Units | Revenue | Repeat rate |\n|---|---|---|---|---|\n{rows}\n\n_{res['metadata']['note']}_")

    def system(self) -> str:
        if self.has(r"error"):
            res = self.call("get_recent_errors", {})
            d = res["data"]
            lines = [f"- {e['at_local']} {e['level']} {e['source']}: {e['message']}" for e in d["log_entries"][:10]]
            return "Recent errors/warnings:\n" + ("\n".join(lines) or "- none") + f"\n\nFailed Smart Reorder messages: {len(d['failed_messages'])}."
        res = self.call("get_system_status", {})
        d = res["data"]
        integrations = ", ".join(f"{i['channel']} {i['provider']} ({i['mode']}, {i['status']})" for i in d["integrations"])
        return (f"Environment **{d['environment']}**, business time {d['business_timezone']}, send window {d['send_window']}.\n"
                f"- Scheduler running: {d['scheduler'].get('running')}\n- Integrations: {integrations or 'none'}\n"
                f"- AI Copilot: {d['ai_copilot']['provider']} ({d['ai_copilot']['mode']})\n- Data: {d['data']}")

    def analytics(self) -> str:
        if self.has(r"accuracy"):
            res = self.call("get_prediction_accuracy", {})
            d = res["data"]
            return (f"Prediction accuracy over {res['metadata']['period']}: {json.dumps({k: d[k] for k in list(d)[:8]}, default=str)}")
        if self.has(r"retention"):
            res = self.call("get_retention_overview", {})
            d = res["data"]
            keys = [k for k in d if not isinstance(d[k], (list, dict))][:12]
            return "Retention overview (now):\n" + "\n".join(f"- {k.replace('_', ' ')}: {d[k]}" for k in keys)
        if self.has(r"campaign performance|campaigns? (doing|perform)|losing money|performance"):
            res = self.call("get_campaign_analytics", {})
            d = res["data"]
            text = f"Campaign analytics ({res['metadata']['period']}):\n```\n{json.dumps(d, default=str)[:1800]}\n```"
            if self.has(r"losing money"):
                text += ("\n\nThe engine tracks attributed revenue but not campaign costs (message fees, discounts given), "
                         "so I can't say which campaigns lose money — only which earn little.")
            return text
        if self.has(r"delivery"):
            res = self.call("get_message_delivery_analytics", {"period": self.period("last_7_days")})
            return f"Delivery ({res['metadata']['period']}): {json.dumps(res['data'], default=str)[:1500]}"
        if self.has(r"segment converted|which segment"):
            return ("Conversions are attributed to campaigns, not segments, so the engine can't rank segments by "
                    "conversion directly. I can show campaign performance (each campaign targets one segment) — "
                    "say \"show campaign performance\".")
        source = "smart_reorder" if "smart reorder" in self.t else "all"
        period = self.period("last_30_days")
        res = self.call("get_revenue_analytics", {"period": period, "source": source})
        d, meta = res["data"], res["metadata"]
        lines = [
            f"**{meta['period']}** — total completed-order revenue **${d['total_revenue']:,.2f}** ({d['total_orders']} orders).",
            f"Attributed to {'Smart Reorder' if source == 'smart_reorder' else 'campaigns'}: **${d['attributed_revenue']:,.2f}** "
            f"from {d['attributed_orders']} orders ({d['attribution_model']}); reactivations: {d['reactivations']}.",
        ]
        if self.has(r"coupon") and d["coupon_revenue"]:
            lines.append("Coupon revenue: " + ", ".join(f"{c['coupon_code']} ${c['revenue']:,.2f} ({c['orders']})" for c in d["coupon_revenue"]))
        return "\n".join(lines)
