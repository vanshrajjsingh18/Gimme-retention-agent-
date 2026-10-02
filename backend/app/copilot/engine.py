"""The Copilot's command loop: request → tools → plan → preview → confirm → execute.

The model decides which approved tools to call. This module decides what
actually happens:

* READ tools run immediately, inside a sandbox transaction that is always
  rolled back, so no read can change data;
* WRITE tools are never executed when the model calls them. They are validated,
  run against the real engine inside the sandbox to build a preview, and stored
  as a pending action bound to an id;
* only ``confirm(action_id)`` — a person pressing Confirm on that card —
  executes a write, once, with a receipt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.copilot import tools as _tools  # noqa: F401 - registers every tool
from app.copilot.prompts import SYSTEM_PROMPT, build_context, pending_action, render_context
from app.copilot.providers import AIProvider, AIProviderError, get_ai_provider
from app.copilot.registry import Risk, ToolContext, ToolError, ToolResult, registry, validate_arguments
from app.copilot.sandbox import sandbox_session
from app.core.config import settings
from app.core.enums import UserRole
from app.models.base import utcnow
from app.models.entities import (
    AuditLog,
    CopilotAction,
    CopilotConversation,
    CopilotExecution,
    CopilotMessage,
    User,
)

logger = logging.getLogger("app.copilot")

#: Characters of a tool result handed back to the model. The full result is
#: stored on the message for the UI.
MODEL_RESULT_LIMIT = 14_000
HISTORY_TURNS = 12


class CopilotError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _canonical(args: dict) -> str:
    return json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))


class CopilotEngine:
    def __init__(self, db: Session, user: User, provider: AIProvider | None = None) -> None:
        self.db = db
        self.user = user
        self.provider = provider or get_ai_provider()

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------
    def conversation(self, conversation_id: int | None) -> CopilotConversation:
        if conversation_id is None:
            conversation = CopilotConversation(user_id=self.user.id, working_state={})
            self.db.add(conversation)
            self.db.flush()
            return conversation
        conversation = self.db.get(CopilotConversation, conversation_id)
        if conversation is None or conversation.user_id != self.user.id:
            raise CopilotError("That conversation does not exist.", 404)
        return conversation

    def _user_text(self, conversation: CopilotConversation) -> str:
        return "\n".join(m.content for m in conversation.messages if m.role == "user")

    def _expire_stale(self, conversation: CopilotConversation) -> None:
        now = utcnow()
        for action in self.db.execute(
            select(CopilotAction).where(
                CopilotAction.conversation_id == conversation.id,
                CopilotAction.status == "PENDING",
                CopilotAction.expires_at < now,
            )
        ).scalars():
            action.status = "EXPIRED"
            action.resolved_at = now
            if conversation.pending_action_id == action.id:
                conversation.pending_action_id = None

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def chat(self, conversation_id: int | None, text: str) -> tuple[CopilotConversation, list[CopilotMessage]]:
        text = (text or "").strip()
        if not text:
            raise CopilotError("Type a request first.")
        if len(text) > 8000:
            raise CopilotError("That message is too long (8,000 characters maximum).")

        conversation = self.conversation(conversation_id)
        self._expire_stale(conversation)
        if not conversation.messages:
            conversation.title = text[:80] + ("…" if len(text) > 80 else "")
        now = utcnow()
        context = build_context(self.db, conversation, now=now)
        user_message = CopilotMessage(
            conversation_id=conversation.id,
            role="user",
            content=text,
            meta={"context_text": render_context(context)},
        )
        self.db.add(user_message)
        conversation.updated_at = now
        self.db.commit()
        first_new_id = user_message.id
        started = time.perf_counter()
        logger.info("copilot.request conversation=%s user=%s chars=%s provider=%s",
                    conversation.id, self.user.id, len(text), self.provider.name)

        planned_write = False
        tools_used: list[str] = []
        # Captured once: what "it" and "them" meant when the operator spoke.
        # A tool that moves the focus mid-turn must not change the request.
        context["user_message"] = text
        for step in range(settings.AI_MAX_TOOL_STEPS):
            try:
                response = self.provider.chat(
                    system=SYSTEM_PROMPT,
                    messages=self._transcript(conversation),
                    tools=registry.schemas(),
                    context=context,
                )
            except AIProviderError as exc:
                logger.warning("copilot.llm_error conversation=%s error=%s", conversation.id, exc)
                self._say(conversation, f"I couldn't reach the AI model: {exc} Nothing was changed.",
                          meta={"error": str(exc)})
                break
            except Exception:  # noqa: BLE001 - a provider bug must not lose the turn
                logger.exception("copilot.llm_crash conversation=%s", conversation.id)
                self._say(conversation, "The AI model failed unexpectedly. Nothing was changed.",
                          meta={"error": "provider_exception"})
                break

            assistant = CopilotMessage(
                conversation_id=conversation.id,
                role="assistant",
                content=response.text,
                tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls],
                meta=_json_safe({
                    "provider": response.provider,
                    "model": response.model,
                    "usage": response.usage,
                    "latency_ms": round(response.latency_ms, 1),
                    "stop_reason": response.stop_reason,
                    "raw": response.raw,
                    "step": step,
                }),
            )
            self.db.add(assistant)
            self.db.commit()
            logger.info("copilot.model conversation=%s step=%s tools=%s usage=%s latency_ms=%.0f",
                        conversation.id, step, [c.name for c in response.tool_calls],
                        response.usage, response.latency_ms)
            if not response.tool_calls:
                break

            for call in response.tool_calls:
                tools_used.append(call.name)
                payload, action_id, risk = self._run_call(conversation, call.name, call.arguments,
                                                          allow_write=not planned_write)
                if action_id is not None:
                    planned_write = True
                self.db.add(
                    CopilotMessage(
                        conversation_id=conversation.id,
                        role="tool",
                        content="",
                        tool_results=_json_safe({
                            "tool_call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                            "risk": risk,
                            "result": payload,
                        }),
                        action_id=action_id,
                    )
                )
                self.db.commit()
        else:
            self._say(conversation, "I stopped after the maximum number of steps for one request. "
                                    "Here is where things stand; ask me to continue if needed.")

        logger.info("copilot.done conversation=%s tools=%s planned_write=%s latency_ms=%.0f",
                    conversation.id, tools_used, planned_write, (time.perf_counter() - started) * 1000)
        new = [m for m in conversation.messages if m.id >= first_new_id]
        return conversation, new

    def _say(self, conversation: CopilotConversation, text: str, *, meta: dict | None = None) -> None:
        self.db.add(CopilotMessage(conversation_id=conversation.id, role="assistant", content=text, meta=meta or {}))
        self.db.commit()

    def _transcript(self, conversation: CopilotConversation) -> list[dict]:
        """The last few turns, provider-neutral, starting on a user message."""
        messages = list(conversation.messages)
        user_indexes = [i for i, m in enumerate(messages) if m.role == "user"]
        if len(user_indexes) > HISTORY_TURNS:
            messages = messages[user_indexes[-HISTORY_TURNS]:]
        out: list[dict] = []
        for m in messages:
            if m.role == "user":
                context_text = (m.meta or {}).get("context_text", "")
                out.append({"role": "user", "content": f"{context_text}\n\n{m.content}" if context_text else m.content})
            elif m.role == "assistant":
                meta = m.meta or {}
                out.append({
                    "role": "assistant",
                    "content": m.content,
                    "tool_calls": m.tool_calls or [],
                    "raw": meta.get("raw"),
                    "raw_provider": meta.get("provider"),
                    "raw_model": meta.get("model"),
                })
            elif m.role == "tool":
                result = (m.tool_results or {}).get("result")
                content = compact_for_model(result)
                out.append({
                    "role": "tool",
                    "tool_call_id": m.tool_results.get("tool_call_id"),
                    "name": m.tool_results.get("name"),
                    "content": content,
                    "is_error": not (isinstance(result, dict) and result.get("success", True)),
                })
            elif m.role == "event":
                out.append({"role": "user", "content": f"[Engine note — not from the operator] {m.content}"})
        return out

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------
    def _context(self, db: Session, conversation: CopilotConversation | None, *, previewing: bool = False) -> ToolContext:
        return ToolContext(
            db=db,
            user=self.user,
            conversation=conversation,
            now=utcnow(),
            previewing=previewing,
            user_text=self._user_text(conversation) if conversation else "",
        )

    def _run_call(self, conversation: CopilotConversation, name: str, raw_args: Any, *,
                  allow_write: bool) -> tuple[dict, int | None, str | None]:
        tool_def = registry.get(name)
        if tool_def is None:
            return ToolResult.fail(f"There is no tool called '{name}'.").as_dict(), None, None
        try:
            args = validate_arguments(tool_def, raw_args)
        except ToolError as exc:
            return ToolResult.fail(str(exc)).as_dict(), None, tool_def.risk.value
        if tool_def.risk == Risk.READ:
            return self.run_read(conversation, name, args), None, tool_def.risk.value
        if not allow_write:
            return (
                ToolResult.fail("One change at a time: a change is already awaiting confirmation in this "
                                "turn. Ask the operator to confirm or cancel it first.").as_dict(),
                None,
                tool_def.risk.value,
            )
        payload, action = self.plan_write(conversation, name, args)
        return payload, (action.id if action else None), tool_def.risk.value

    def run_read(self, conversation: CopilotConversation | None, name: str, args: dict) -> dict:
        tool_def = registry.get(name)
        started = time.perf_counter()
        result: ToolResult | None = None
        for attempt in range(2):
            try:
                with sandbox_session() as sandbox:
                    result = tool_def.handler(self._context(sandbox, conversation), **args)
                break
            except ToolError as exc:
                result = ToolResult.fail(str(exc))
                break
            except OperationalError as exc:
                # A locked database is worth one retry for a read; anything
                # else is reported, not retried.
                if attempt == 0 and "locked" in str(exc).lower():
                    time.sleep(0.3)
                    continue
                logger.exception("copilot.tool_error tool=%s", name)
                result = ToolResult.fail(f"{name} failed with a database error.")
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception("copilot.tool_error tool=%s", name)
                result = ToolResult.fail(f"{name} failed unexpectedly ({exc.__class__.__name__}).")
                break
        logger.info("copilot.tool tool=%s risk=READ success=%s latency_ms=%.0f",
                    name, result.success, (time.perf_counter() - started) * 1000)
        if conversation is not None and result.success:
            self._apply_state(conversation, result)
        return _json_safe(result.as_dict())

    def _apply_state(self, conversation: CopilotConversation, result: ToolResult) -> None:
        state = dict(conversation.working_state or {})
        if result.focus:
            conversation.active_entity_type, conversation.active_entity_id = result.focus[0], int(result.focus[1])
        if result.result_set:
            state["last_result_set"] = result.result_set
        conversation.working_state = state
        self.db.commit()

    def plan_write(self, conversation: CopilotConversation, name: str, args: dict) -> tuple[dict, CopilotAction | None]:
        tool_def = registry.get(name)
        if self.user.role == UserRole.VIEWER.value:
            return ToolResult.fail("This account is read-only, so I can't prepare changes.").as_dict(), None
        key = hashlib.sha256(f"{conversation.id}|{name}|{_canonical(args)}".encode()).hexdigest()

        existing = self.db.execute(
            select(CopilotAction).where(
                CopilotAction.conversation_id == conversation.id,
                CopilotAction.idempotency_key == key,
                CopilotAction.status.in_(["PENDING", "EXECUTING", "EXECUTED"]),
            ).order_by(CopilotAction.id.desc())
        ).scalars().first()
        if existing is not None and existing.status in ("PENDING", "EXECUTING"):
            return self._pending_payload(existing, reused=True), existing
        if existing is not None and tool_def.creates:
            receipt = self.db.get(CopilotExecution, existing.execution_id) if existing.execution_id else None
            if receipt is not None and receipt.success:
                return ToolResult.ok(
                    {
                        "status": "ALREADY_DONE",
                        "message": f"This exact change already ran as action #{existing.id}; nothing was duplicated.",
                        "execution_id": receipt.id,
                        "target": {"type": receipt.target_type, "id": receipt.target_id},
                    }
                ).as_dict(), None

        started = time.perf_counter()
        summary = tool_def.summarize(args) if tool_def.summarize else name.replace("_", " ").capitalize()
        try:
            with sandbox_session() as sandbox:
                ctx = self._context(sandbox, conversation, previewing=True)
                result = tool_def.handler(ctx, **args)
                extra = tool_def.previewer(ctx, args, result) if tool_def.previewer else {}
        except ToolError as exc:
            logger.info("copilot.plan_refused tool=%s reason=%s", name, exc)
            return ToolResult.fail(f"This change can't be made: {exc}").as_dict(), None
        except Exception as exc:  # noqa: BLE001
            logger.exception("copilot.plan_error tool=%s", name)
            return ToolResult.fail(f"Preparing {name} failed unexpectedly ({exc.__class__.__name__}).").as_dict(), None

        preview = _json_safe({
            "summary": summary,
            "risk": tool_def.risk.value,
            "target": {"type": result.target_type, "id": result.target_id},
            "before": result.before,
            "after": result.after,
            "result": result.data,
            **(extra or {}),
        })
        now = utcnow()
        for other in self.db.execute(
            select(CopilotAction).where(
                CopilotAction.conversation_id == conversation.id, CopilotAction.status == "PENDING"
            )
        ).scalars():
            other.status = "SUPERSEDED"
            other.resolved_at = now
        action = CopilotAction(
            conversation_id=conversation.id,
            user_id=self.user.id,
            tool_name=name,
            risk=tool_def.risk.value,
            arguments=_json_safe(args),
            summary=summary[:500],
            preview=preview,
            status="PENDING",
            idempotency_key=key,
            expires_at=now + timedelta(minutes=settings.AI_ACTION_TTL_MINUTES),
        )
        self.db.add(action)
        self.db.flush()
        conversation.pending_action_id = action.id
        self.db.commit()
        logger.info("copilot.planned tool=%s risk=%s action=%s latency_ms=%.0f",
                    name, tool_def.risk.value, action.id, (time.perf_counter() - started) * 1000)
        return self._pending_payload(action), action

    @staticmethod
    def _pending_payload(action: CopilotAction, *, reused: bool = False) -> dict:
        return ToolResult.ok(
            {
                "status": "PENDING_CONFIRMATION",
                "action_id": action.id,
                "summary": action.summary,
                "risk": action.risk,
                "reused_existing_pending_action": reused,
                "preview": action.preview,
                "note": "Nothing has changed yet. The operator must press Confirm on this action card.",
            }
        ).as_dict()

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------
    def _action(self, action_id: int) -> CopilotAction:
        action = self.db.get(CopilotAction, action_id)
        if action is None:
            raise CopilotError("That action does not exist.", 404)
        conversation = self.db.get(CopilotConversation, action.conversation_id)
        if conversation is None or conversation.user_id != self.user.id:
            raise CopilotError("That action does not belong to you.", 403)
        return action

    def confirm(self, action_id: int, *, retry: bool = False) -> dict:
        action = self._action(action_id)
        conversation = self.db.get(CopilotConversation, action.conversation_id)
        if self.user.role == UserRole.VIEWER.value:
            raise CopilotError("Your account has read-only access.", 403)
        if action.status == "EXECUTED" and not retry:
            # A second click, or a replayed request: report, don't repeat.
            return self._outcome(action, duplicate=True)
        expected = "FAILED" if retry else "PENDING"
        if action.status != expected:
            raise CopilotError(
                f"Action #{action.id} is {action.status.lower()} and cannot be "
                f"{'retried' if retry else 'confirmed'}.", 409
            )
        if action.expires_at and action.expires_at < utcnow():
            action.status = "EXPIRED"
            action.resolved_at = utcnow()
            self.db.commit()
            raise CopilotError("This plan expired — the data may have changed. Ask again for a fresh preview.", 409)

        claimed = self.db.execute(
            update(CopilotAction)
            .where(CopilotAction.id == action.id, CopilotAction.status == expected)
            .values(status="EXECUTING")
        ).rowcount
        self.db.commit()
        if claimed != 1:
            raise CopilotError("This action is already being executed.", 409)

        tool_def = registry.get(action.tool_name)
        started = time.perf_counter()
        ctx = self._context(self.db, conversation)
        error = None
        result: ToolResult | None = None
        try:
            result = tool_def.handler(ctx, **dict(action.arguments))
        except ToolError as exc:
            self.db.rollback()
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            self.db.rollback()
            logger.exception("copilot.execute_error action=%s tool=%s", action.id, action.tool_name)
            error = f"The engine raised an unexpected error ({exc.__class__.__name__}). Nothing was confirmed as done."
        duration = (time.perf_counter() - started) * 1000
        success = error is None and result is not None and result.success
        if result is not None and not result.success and error is None:
            error = "; ".join(result.errors) or "The tool reported failure."

        action = self.db.get(CopilotAction, action_id)
        execution = CopilotExecution(
            conversation_id=action.conversation_id,
            user_id=self.user.id,
            action_id=action.id,
            tool_name=action.tool_name,
            action_type=action.risk,
            target_type=result.target_type if result else None,
            target_id=str(result.target_id) if result and result.target_id is not None else None,
            arguments=action.arguments,
            before_state=_json_safe(result.before) if result else {},
            after_state=_json_safe(result.after) if result else {},
            result=_json_safe(result.as_dict()) if result else {},
            confirmation_required=True,
            confirmation_received=True,
            success=success,
            error=error,
            duration_ms=round(duration, 1),
        )
        self.db.add(execution)
        self.db.flush()
        action.status = "EXECUTED" if success else "FAILED"
        action.error = error
        action.resolved_at = utcnow()
        action.execution_id = execution.id
        conversation = self.db.get(CopilotConversation, action.conversation_id)
        if conversation.pending_action_id == action.id:
            conversation.pending_action_id = None
        if success and result.focus:
            conversation.active_entity_type, conversation.active_entity_id = result.focus[0], int(result.focus[1])
        note = (
            f"Action #{action.id} \"{action.summary}\" was confirmed by {self.user.email} and executed "
            f"successfully. Result: {json.dumps(_brief(result.data), default=str)[:1500]}"
            if success
            else f"Action #{action.id} \"{action.summary}\" was confirmed but FAILED: {error}"
        )
        self.db.add(CopilotMessage(conversation_id=conversation.id, role="event", content=note, action_id=action.id))
        self.db.add(
            AuditLog(
                actor=self.user.email,
                action="COPILOT_ACTION_EXECUTED" if success else "COPILOT_ACTION_FAILED",
                entity_type=execution.target_type or "copilot",
                entity_id=execution.target_id or str(action.id),
                detail={"tool": action.tool_name, "action_id": action.id, "execution_id": execution.id,
                        "risk": action.risk, "error": error},
            )
        )
        self.db.commit()
        logger.info("copilot.executed action=%s tool=%s success=%s latency_ms=%.0f",
                    action.id, action.tool_name, success, duration)
        return self._outcome(action)

    def cancel(self, action_id: int) -> dict:
        action = self._action(action_id)
        if action.status != "PENDING":
            raise CopilotError(f"Action #{action.id} is {action.status.lower()}, so there is nothing to cancel.", 409)
        action.status = "CANCELLED"
        action.resolved_at = utcnow()
        conversation = self.db.get(CopilotConversation, action.conversation_id)
        if conversation.pending_action_id == action.id:
            conversation.pending_action_id = None
        self.db.add(CopilotMessage(conversation_id=conversation.id, role="event",
                                   content=f"Action #{action.id} \"{action.summary}\" was cancelled by the operator. Nothing changed.",
                                   action_id=action.id))
        self.db.commit()
        logger.info("copilot.cancelled action=%s", action.id)
        return self._outcome(action)

    def _outcome(self, action: CopilotAction, *, duplicate: bool = False) -> dict:
        execution = self.db.get(CopilotExecution, action.execution_id) if action.execution_id else None
        return {
            "action": action_view(action),
            "execution": execution_view(execution) if execution else None,
            "duplicate": duplicate,
        }

    def dry_run(self, conversation_id: int | None, automation_id: int | None) -> dict:
        conversation = self.conversation(conversation_id) if conversation_id else None
        if automation_id is None and conversation is not None and conversation.active_entity_type == "automation":
            automation_id = conversation.active_entity_id
        if automation_id is None:
            raise CopilotError("Which campaign? Pass automation_id or select one in the conversation.")
        return self.run_read(conversation, "preview_smart_reorder", {"automation_id": automation_id})


def _shrink(value: Any, items: int) -> Any:
    if isinstance(value, list):
        kept = [_shrink(v, items) for v in value[:items]]
        if len(value) > items:
            kept.append(f"… {len(value) - items} more not shown")
        return kept
    if isinstance(value, dict):
        return {k: _shrink(v, items) for k, v in value.items()}
    if isinstance(value, str) and len(value) > 600:
        return value[:600] + "…"
    return value


def compact_for_model(result: Any) -> str:
    """A tool result as JSON the model can read, always valid, within the limit.

    Lists are shortened (saying how many were left out) rather than the text
    being cut mid-object, so a large result degrades into a smaller true one
    instead of an unreadable one.
    """
    content = json.dumps(result, default=str)
    for items in (25, 10, 5, 2):
        if len(content) <= MODEL_RESULT_LIMIT:
            return content
        content = json.dumps(_shrink(result, items), default=str)
    if len(content) <= MODEL_RESULT_LIMIT:
        return content
    summary = {k: result.get(k) for k in ("success", "errors", "metadata")} if isinstance(result, dict) else {}
    summary["note"] = "Result too large to show; ask a narrower question (fewer rows, one campaign, shorter period)."
    return json.dumps(summary, default=str)


def _brief(data: Any) -> Any:
    """A compact version of a result for the engine note the model reads."""
    if isinstance(data, dict):
        keep = ("id", "name", "status", "channel", "kind_label", "audience", "coupons", "timing", "members",
                "affected", "pending_messages_cancelled", "approval_withdrawn", "coupon_codes_added_to_brand_settings",
                "campaign", "segment", "download")
        return {k: _brief(v) if k == "campaign" else v for k, v in data.items() if k in keep}
    return data


def action_view(action: CopilotAction) -> dict:
    return {
        "id": action.id,
        "conversation_id": action.conversation_id,
        "tool_name": action.tool_name,
        "risk": action.risk,
        "summary": action.summary,
        "arguments": action.arguments,
        "preview": action.preview,
        "status": action.status,
        "error": action.error,
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "expires_at": action.expires_at.isoformat() if action.expires_at else None,
        "resolved_at": action.resolved_at.isoformat() if action.resolved_at else None,
        "execution_id": action.execution_id,
    }


def execution_view(execution: CopilotExecution) -> dict:
    return {
        "id": execution.id,
        "conversation_id": execution.conversation_id,
        "user_id": execution.user_id,
        "action_id": execution.action_id,
        "tool_name": execution.tool_name,
        "action_type": execution.action_type,
        "target_type": execution.target_type,
        "target_id": execution.target_id,
        "arguments": execution.arguments,
        "before_state": execution.before_state,
        "after_state": execution.after_state,
        "result": execution.result,
        "confirmation_required": execution.confirmation_required,
        "confirmation_received": execution.confirmation_received,
        "success": execution.success,
        "error": execution.error,
        "duration_ms": execution.duration_ms,
        "timestamp": execution.created_at.isoformat() if execution.created_at else None,
    }


def message_view(message: CopilotMessage, actions: dict[int, CopilotAction]) -> dict:
    view = {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }
    if message.tool_calls:
        view["tool_calls"] = message.tool_calls
    if message.role == "tool":
        tr = message.tool_results or {}
        result = tr.get("result") or {}
        view["tool"] = {
            "name": tr.get("name"),
            "risk": tr.get("risk"),
            "arguments": tr.get("arguments"),
            "success": result.get("success"),
            "errors": result.get("errors"),
            "metadata": result.get("metadata"),
            "data": result.get("data"),
        }
    if message.role == "assistant" and message.meta:
        view["meta"] = {k: message.meta.get(k) for k in ("provider", "model", "latency_ms", "usage", "error")}
    if message.action_id and message.action_id in actions:
        view["action"] = action_view(actions[message.action_id])
    return view


def conversation_view(db: Session, conversation: CopilotConversation, *,
                      messages: list[CopilotMessage] | None = None) -> dict:
    from app.copilot.providers import provider_info

    action_ids = {m.action_id for m in conversation.messages if m.action_id}
    actions = {
        a.id: a
        for a in db.execute(select(CopilotAction).where(CopilotAction.id.in_(action_ids or {0}))).scalars()
    }
    executions = db.execute(
        select(CopilotExecution)
        .where(CopilotExecution.conversation_id == conversation.id)
        .order_by(CopilotExecution.id.desc())
        .limit(20)
    ).scalars().all()
    context = build_context(db, conversation, now=utcnow())
    pending = pending_action(db, conversation)
    return {
        "id": conversation.id,
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
        "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else None,
        "context": {
            "active": context["active"],
            "pending_action": action_view(pending) if pending else None,
            "last_result_set": context["last_result_set"],
            "now_local": context["now_local"],
        },
        "messages": [message_view(m, actions) for m in (messages if messages is not None else conversation.messages)],
        "recent_actions": [execution_view(e) for e in executions],
        "provider": provider_info(),
    }
