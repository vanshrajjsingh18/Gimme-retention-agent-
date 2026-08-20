"""Segment CRUD, rule preview and membership."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_write
from app.core.database import get_db
from app.core.enums import SegmentStatus, SegmentType
from app.models.entities import AuditLog, CustomerSegment, Segment, User
from app.schemas.common import OperationResult
from app.schemas.models import (
    SegmentCreate,
    SegmentOut,
    SegmentPreviewRequest,
    SegmentUpdate,
)
from app.segmentation.rules import RuleError, describe_rule, field_catalog, validate_rule
from app.services.segments import (
    evaluate_segment,
    preview_rule,
    refresh_all_segments,
    refresh_segment_membership,
)

router = APIRouter()


def _out(segment: Segment) -> SegmentOut:
    data = SegmentOut.model_validate(segment)
    data.rule_description = describe_rule(segment.rule_definition or {})
    return data


@router.get("/segments/fields", tags=["segments"])
def list_fields(_: User = Depends(get_current_user)) -> dict:
    """Field catalogue powering the visual rule builder."""
    catalog = field_catalog()
    groups: dict[str, list[dict]] = {}
    for entry in catalog:
        groups.setdefault(entry["group"], []).append(entry)
    return {"fields": catalog, "groups": groups}


@router.get("/segments", response_model=list[SegmentOut], tags=["segments"])
def list_segments(
    include_archived: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[SegmentOut]:
    stmt = select(Segment).order_by(Segment.is_system.desc(), Segment.name)
    if not include_archived:
        stmt = stmt.where(Segment.status == SegmentStatus.ACTIVE.value)
    return [_out(s) for s in db.execute(stmt).scalars().all()]


@router.post("/segments/preview", tags=["segments"])
def preview(
    payload: SegmentPreviewRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Count and sample the customers a candidate rule matches."""
    try:
        return preview_rule(db, payload.rule_definition, limit=payload.limit)
    except RuleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/segments", response_model=SegmentOut, status_code=201, tags=["segments"])
def create_segment(
    payload: SegmentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> SegmentOut:
    existing = db.execute(select(Segment).where(Segment.name == payload.name)).first()
    if existing:
        raise HTTPException(
            status_code=409, detail=f"A segment named '{payload.name}' already exists."
        )
    if payload.segment_type == SegmentType.DYNAMIC.value:
        try:
            validate_rule(payload.rule_definition)
        except RuleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    segment = Segment(
        name=payload.name,
        description=payload.description,
        segment_type=payload.segment_type,
        rule_definition=payload.rule_definition,
        status=SegmentStatus.ACTIVE.value,
        is_system=False,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)

    refresh_segment_membership(db, segment)
    db.add(
        AuditLog(
            actor=user.email,
            action="SEGMENT_CREATED",
            entity_type="segment",
            entity_id=str(segment.id),
            detail={"name": segment.name},
        )
    )
    db.commit()
    db.refresh(segment)
    return _out(segment)


@router.get("/segments/{segment_id}", response_model=SegmentOut, tags=["segments"])
def get_segment(
    segment_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> SegmentOut:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found.")
    return _out(segment)


@router.patch("/segments/{segment_id}", response_model=SegmentOut, tags=["segments"])
def update_segment(
    segment_id: int,
    payload: SegmentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> SegmentOut:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found.")
    if segment.is_system and payload.rule_definition is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Built-in segments have fixed rules. Duplicate this segment to create an "
                "editable copy."
            ),
        )

    changes = payload.model_dump(exclude_none=True)
    if "rule_definition" in changes:
        try:
            validate_rule(changes["rule_definition"])
        except RuleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    for key, value in changes.items():
        setattr(segment, key, value)

    db.add(
        AuditLog(
            actor=user.email,
            action="SEGMENT_UPDATED",
            entity_type="segment",
            entity_id=str(segment_id),
            detail={"fields": sorted(changes)},
        )
    )
    db.commit()
    refresh_segment_membership(db, segment)
    db.refresh(segment)
    return _out(segment)


@router.post("/segments/{segment_id}/duplicate", response_model=SegmentOut, status_code=201, tags=["segments"])
def duplicate_segment(
    segment_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> SegmentOut:
    original = db.get(Segment, segment_id)
    if original is None:
        raise HTTPException(status_code=404, detail="Segment not found.")

    base = f"{original.name} (copy)"
    name, suffix = base, 2
    while db.execute(select(Segment.id).where(Segment.name == name)).first():
        name = f"{base} {suffix}"
        suffix += 1

    clone = Segment(
        name=name,
        description=original.description,
        segment_type=original.segment_type,
        rule_definition=dict(original.rule_definition or {}),
        status=SegmentStatus.ACTIVE.value,
        is_system=False,
    )
    db.add(clone)
    db.commit()
    db.refresh(clone)
    refresh_segment_membership(db, clone)
    db.refresh(clone)
    return _out(clone)


@router.post("/segments/{segment_id}/archive", response_model=SegmentOut, tags=["segments"])
def archive_segment(
    segment_id: int, db: Session = Depends(get_db), user: User = Depends(require_write)
) -> SegmentOut:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found.")
    segment.status = SegmentStatus.ARCHIVED.value
    db.add(
        AuditLog(
            actor=user.email,
            action="SEGMENT_ARCHIVED",
            entity_type="segment",
            entity_id=str(segment_id),
        )
    )
    db.commit()
    db.refresh(segment)
    return _out(segment)


@router.post("/segments/{segment_id}/refresh", response_model=SegmentOut, tags=["segments"])
def refresh_segment(
    segment_id: int, db: Session = Depends(get_db), _: User = Depends(require_write)
) -> SegmentOut:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found.")
    refresh_segment_membership(db, segment)
    db.refresh(segment)
    return _out(segment)


@router.post("/segments/refresh-all", tags=["segments"])
def refresh_all(db: Session = Depends(get_db), _: User = Depends(require_write)) -> dict:
    return {"segments": refresh_all_segments(db)}


@router.get("/segments/{segment_id}/members", tags=["segments"])
def segment_members(
    segment_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found.")
    views = evaluate_segment(db, segment)
    return {
        "segment_id": segment_id,
        "name": segment.name,
        "member_count": len(views),
        "members": [
            {
                "id": v["id"],
                "external_id": v["external_id"],
                "full_name": v["full_name"],
                "email": v.get("email"),
                "lifecycle_stage": v.get("lifecycle_stage"),
                "lifetime_revenue": v.get("lifetime_revenue"),
                "churn_score": v.get("churn_score"),
                "churn_risk_band": v.get("churn_risk_band"),
                "days_since_last_order": v.get("days_since_last_order"),
            }
            for v in views[:limit]
        ],
    }


@router.get("/segments/{segment_id}/export.csv", tags=["segments"])
def export_segment(
    segment_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> PlainTextResponse:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found.")

    views = evaluate_segment(db, segment)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "external_id", "first_name", "last_name", "email", "city", "lifecycle_stage",
            "completed_orders", "lifetime_revenue", "days_since_last_order", "churn_score",
            "churn_risk_band", "rfm_segment", "recommended_action", "marketing_consent",
        ]
    )
    for v in views:
        writer.writerow(
            [
                v["external_id"], v["first_name"], v["last_name"], v.get("email", ""),
                v.get("city", ""), v.get("lifecycle_stage", ""), v.get("completed_orders", 0),
                v.get("lifetime_revenue", 0), v.get("days_since_last_order", ""),
                v.get("churn_score", 0), v.get("churn_risk_band", ""),
                v.get("rfm_segment", ""), v.get("recommended_action", ""),
                v.get("marketing_consent", False),
            ]
        )

    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in segment.name).lower()
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="segment-{safe_name}.csv"'},
    )


@router.post("/segments/{segment_id}/members/{customer_id}", response_model=OperationResult, tags=["segments"])
def add_member(
    segment_id: int,
    customer_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_write),
) -> OperationResult:
    """Add a customer to a manual segment."""
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found.")
    if segment.segment_type != SegmentType.MANUAL.value:
        raise HTTPException(
            status_code=400,
            detail="Membership of a dynamic segment is determined by its rule, not by hand.",
        )

    exists = db.execute(
        select(CustomerSegment.id).where(
            CustomerSegment.segment_id == segment_id,
            CustomerSegment.customer_id == customer_id,
        )
    ).first()
    if not exists:
        db.add(
            CustomerSegment(
                segment_id=segment_id, customer_id=customer_id, source="manual"
            )
        )
        db.commit()
    refresh_segment_membership(db, segment)
    return OperationResult(message="Customer added to segment.")
