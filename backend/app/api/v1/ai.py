"""AI Copilot endpoints.

Chatting needs a signed-in user; confirming, cancelling or retrying a planned
change needs write access, and the action must belong to the caller. Every
model call happens here on the server — no key or provider detail reaches the
browser.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_write
from app.copilot.engine import (
    CopilotEngine,
    CopilotError,
    action_view,
    conversation_view,
    execution_view,
)
from app.copilot.registry import registry
from app.core.database import get_db
from app.models.entities import CopilotAction, CopilotConversation, CopilotExecution, User

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: int | None = None


class ActionRequest(BaseModel):
    action_id: int


class DryRunRequest(BaseModel):
    conversation_id: int | None = None
    automation_id: int | None = None


def _raise(exc: CopilotError):
    raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/ai/chat", tags=["ai"])
def chat(payload: ChatRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    engine = CopilotEngine(db, user)
    try:
        conversation, new_messages = engine.chat(payload.conversation_id, payload.message)
    except CopilotError as exc:
        _raise(exc)
    view = conversation_view(db, conversation)
    view["new_message_ids"] = [m.id for m in new_messages]
    return view


@router.post("/ai/confirm", tags=["ai"])
def confirm(payload: ActionRequest, db: Session = Depends(get_db), user: User = Depends(require_write)) -> dict:
    engine = CopilotEngine(db, user)
    try:
        outcome = engine.confirm(payload.action_id)
    except CopilotError as exc:
        _raise(exc)
    conversation = db.get(CopilotConversation, outcome["action"]["conversation_id"])
    return {**outcome, "conversation": conversation_view(db, conversation)}


@router.post("/ai/retry", tags=["ai"])
def retry(payload: ActionRequest, db: Session = Depends(get_db), user: User = Depends(require_write)) -> dict:
    """Run a FAILED action again, with the arguments that were confirmed."""
    engine = CopilotEngine(db, user)
    try:
        outcome = engine.confirm(payload.action_id, retry=True)
    except CopilotError as exc:
        _raise(exc)
    conversation = db.get(CopilotConversation, outcome["action"]["conversation_id"])
    return {**outcome, "conversation": conversation_view(db, conversation)}


@router.post("/ai/cancel", tags=["ai"])
def cancel(payload: ActionRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    engine = CopilotEngine(db, user)
    try:
        outcome = engine.cancel(payload.action_id)
    except CopilotError as exc:
        _raise(exc)
    conversation = db.get(CopilotConversation, outcome["action"]["conversation_id"])
    return {**outcome, "conversation": conversation_view(db, conversation)}


@router.get("/ai/conversations", tags=["ai"])
def conversations(
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    rows = db.execute(
        select(CopilotConversation)
        .where(CopilotConversation.user_id == user.id)
        .order_by(CopilotConversation.updated_at.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "id": c.id,
            "title": c.title,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "active_entity_type": c.active_entity_type,
            "active_entity_id": c.active_entity_id,
        }
        for c in rows
    ]


def _own(db: Session, user: User, conversation_id: int) -> CopilotConversation:
    conversation = db.get(CopilotConversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=404, detail="That conversation does not exist.")
    return conversation


@router.get("/ai/conversations/{conversation_id}", tags=["ai"])
def get_conversation(conversation_id: int, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)) -> dict:
    return conversation_view(db, _own(db, user, conversation_id))


@router.delete("/ai/conversations/{conversation_id}", tags=["ai"])
def delete_conversation(conversation_id: int, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)) -> dict:
    """Removes the chat. Execution receipts are kept: they are the audit trail."""
    conversation = _own(db, user, conversation_id)
    for action in db.execute(
        select(CopilotAction).where(CopilotAction.conversation_id == conversation.id, CopilotAction.status == "PENDING")
    ).scalars():
        action.status = "CANCELLED"
    db.delete(conversation)
    db.commit()
    return {"deleted": True, "id": conversation_id}


@router.get("/ai/tools", tags=["ai"])
def tools(_: User = Depends(get_current_user)) -> dict:
    from app.copilot.providers import provider_info

    return {"provider": provider_info(), "tools": [t.describe() for t in registry.all()]}


@router.get("/ai/actions/{action_id}", tags=["ai"])
def get_action(action_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    action = db.get(CopilotAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="That action does not exist.")
    _own(db, user, action.conversation_id)
    return action_view(action)


@router.get("/ai/executions", tags=["ai"])
def executions(
    conversation_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    query = select(CopilotExecution).where(CopilotExecution.user_id == user.id)
    if conversation_id is not None:
        query = query.where(CopilotExecution.conversation_id == conversation_id)
    rows = db.execute(query.order_by(CopilotExecution.id.desc()).limit(limit)).scalars().all()
    return [execution_view(e) for e in rows]


@router.get("/ai/executions/{execution_id}", tags=["ai"])
def get_execution(execution_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    execution = db.get(CopilotExecution, execution_id)
    if execution is None or (execution.user_id != user.id and user.role != "ADMIN"):
        raise HTTPException(status_code=404, detail="That execution does not exist.")
    return execution_view(execution)


@router.post("/ai/dry-run", tags=["ai"])
def dry_run(payload: DryRunRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    engine = CopilotEngine(db, user)
    try:
        return engine.dry_run(payload.conversation_id, payload.automation_id)
    except CopilotError as exc:
        _raise(exc)
