"""Journey builder and runner API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_write
from app.core.database import get_db
from app.core.enums import JourneyStatus
from app.journeys.engine import (
    NODE_CATALOG,
    JourneyError,
    enrol_customers,
    run_all_journeys,
    run_journey,
    validate_journey,
)
from app.models.entities import (
    AuditLog,
    Customer,
    Journey,
    JourneyCustomerState,
    JourneyExecution,
    JourneyNode,
    User,
)
from app.schemas.models import JourneyCreate, JourneyOut, JourneyUpdate

router = APIRouter()


def _get(db: Session, journey_id: int) -> Journey:
    journey = db.get(Journey, journey_id)
    if journey is None:
        raise HTTPException(status_code=404, detail="Journey not found.")
    return journey


def _replace_nodes(db: Session, journey: Journey, nodes: list) -> None:
    for existing in list(journey.nodes):
        db.delete(existing)
    db.flush()
    for position, node in enumerate(nodes):
        data = node if isinstance(node, dict) else node.model_dump()
        db.add(
            JourneyNode(
                journey_id=journey.id,
                position=position,
                node_type=data["node_type"],
                subtype=data["subtype"],
                config=data.get("config") or {},
            )
        )


@router.get("/journeys/catalog", tags=["journeys"])
def catalog(_: User = Depends(get_current_user)) -> dict:
    """Available triggers, delays, conditions and actions."""
    return NODE_CATALOG


@router.get("/journeys", response_model=list[JourneyOut], tags=["journeys"])
def list_journeys(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[JourneyOut]:
    journeys = db.execute(select(Journey).order_by(Journey.created_at.desc())).scalars().all()
    return [JourneyOut.model_validate(j) for j in journeys]


@router.post("/journeys", response_model=JourneyOut, status_code=201, tags=["journeys"])
def create_journey(
    payload: JourneyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> JourneyOut:
    if db.execute(select(Journey.id).where(Journey.name == payload.name)).first():
        raise HTTPException(
            status_code=409, detail=f"A journey named '{payload.name}' already exists."
        )
    try:
        validate_journey(payload.trigger_type, [n.model_dump() for n in payload.nodes])
    except JourneyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    journey = Journey(
        name=payload.name,
        description=payload.description,
        trigger_type=payload.trigger_type,
        trigger_config=payload.trigger_config,
        allow_reentry=payload.allow_reentry,
        status=JourneyStatus.DRAFT.value,
    )
    db.add(journey)
    db.flush()
    _replace_nodes(db, journey, payload.nodes)
    db.add(
        AuditLog(
            actor=user.email,
            action="JOURNEY_CREATED",
            entity_type="journey",
            entity_id=payload.name,
        )
    )
    db.commit()
    db.refresh(journey)
    return JourneyOut.model_validate(journey)


@router.get("/journeys/{journey_id}", response_model=JourneyOut, tags=["journeys"])
def get_journey(
    journey_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> JourneyOut:
    return JourneyOut.model_validate(_get(db, journey_id))


@router.patch("/journeys/{journey_id}", response_model=JourneyOut, tags=["journeys"])
def update_journey(
    journey_id: int,
    payload: JourneyUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> JourneyOut:
    journey = _get(db, journey_id)
    changes = payload.model_dump(exclude_none=True)
    nodes = changes.pop("nodes", None)

    trigger = changes.get("trigger_type", journey.trigger_type)
    if nodes is not None:
        try:
            validate_journey(trigger, nodes)
        except JourneyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    for key, value in changes.items():
        setattr(journey, key, value)
    if nodes is not None:
        _replace_nodes(db, journey, nodes)

    db.add(
        AuditLog(
            actor=user.email,
            action="JOURNEY_UPDATED",
            entity_type="journey",
            entity_id=str(journey_id),
            detail={"fields": sorted(changes)},
        )
    )
    db.commit()
    db.refresh(journey)
    return JourneyOut.model_validate(journey)


@router.post("/journeys/{journey_id}/activate", response_model=JourneyOut, tags=["journeys"])
def activate(
    journey_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> JourneyOut:
    journey = _get(db, journey_id)
    try:
        validate_journey(
            journey.trigger_type,
            [
                {"node_type": n.node_type, "subtype": n.subtype, "config": n.config}
                for n in journey.nodes
            ],
        )
    except JourneyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    journey.status = JourneyStatus.ACTIVE.value
    db.add(
        AuditLog(
            actor=user.email,
            action="JOURNEY_ACTIVATED",
            entity_type="journey",
            entity_id=str(journey_id),
        )
    )
    db.commit()
    db.refresh(journey)
    return JourneyOut.model_validate(journey)


@router.post("/journeys/{journey_id}/pause", response_model=JourneyOut, tags=["journeys"])
def pause(
    journey_id: int, db: Session = Depends(get_db), _: User = Depends(require_write)
) -> JourneyOut:
    journey = _get(db, journey_id)
    journey.status = JourneyStatus.PAUSED.value
    db.commit()
    db.refresh(journey)
    return JourneyOut.model_validate(journey)


@router.post("/journeys/{journey_id}/enrol", tags=["journeys"])
def enrol(
    journey_id: int,
    limit: int | None = Query(default=None, ge=1, le=5000),
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> dict:
    journey = _get(db, journey_id)
    return {"enrolled": enrol_customers(db, journey, limit=limit)}


@router.post("/journeys/{journey_id}/run", tags=["journeys"])
def run(
    journey_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> dict:
    """Advance every active customer through the journey."""
    journey = _get(db, journey_id)
    stats = run_journey(db, journey)
    db.add(
        AuditLog(
            actor=user.email,
            action="JOURNEY_RUN",
            entity_type="journey",
            entity_id=str(journey_id),
            detail=stats,
        )
    )
    db.commit()
    return stats


@router.post("/journeys/run-all", tags=["journeys"])
def run_all(db: Session = Depends(get_db), _: User = Depends(require_write)) -> dict:
    return {"journeys": run_all_journeys(db)}


@router.get("/journeys/{journey_id}/executions", tags=["journeys"])
def executions(
    journey_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    _get(db, journey_id)
    rows = db.execute(
        select(JourneyExecution, Customer)
        .join(Customer, Customer.id == JourneyExecution.customer_id)
        .where(JourneyExecution.journey_id == journey_id)
        .order_by(JourneyExecution.executed_at.desc())
        .limit(limit)
    ).all()

    states = db.execute(
        select(JourneyCustomerState.status, JourneyCustomerState.id).where(
            JourneyCustomerState.journey_id == journey_id
        )
    ).all()
    status_counts: dict[str, int] = {}
    for status, _id in states:
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "status_counts": status_counts,
        "executions": [
            {
                "customer_id": c.id,
                "customer_name": c.full_name,
                "action": e.action,
                "outcome": e.outcome,
                "detail": e.detail,
                "executed_at": e.executed_at.isoformat(),
            }
            for e, c in rows
        ],
    }
