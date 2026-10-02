"""Segments (rule-based audiences) and cohorts (purchase-history audiences)."""
from __future__ import annotations

from sqlalchemy import select

from app.copilot.registry import Risk, ToolContext, ToolError, ToolResult, tool
from app.copilot.tools._common import result_set, segment_for, segment_snapshot
from app.core.enums import LifecycleStage, SegmentStatus, SegmentType
from app.models.entities import AuditLog, CustomerSegment, Segment
from app.segmentation.rules import RuleError, describe_rule, field_catalog, validate_rule
from app.services.purchase_cohorts import (
    CohortCriteria,
    CohortError,
    create_cohort_segment,
    find_customers,
)
from app.services.segments import preview_rule, refresh_segment_membership

GROUP = "segments"

RULE_DOC = (
    "A rule tree: {\"op\": \"AND\"|\"OR\", \"conditions\": [{\"field\", \"operator\", \"value\"}, ...]}. "
    "Fields and operators come from get_segment_fields — never invent a field."
)

COHORT_PROPS = {
    "categories": {"type": "array", "items": {"type": "string"}, "description": "Order-line categories, e.g. Beer."},
    "brands": {"type": "array", "items": {"type": "string"}},
    "products": {"type": "array", "items": {"type": "string"}, "description": "Substrings of product names."},
    "min_matching_orders": {"type": "integer", "description": "Orders containing a match. Default 1."},
    "matching_within_days": {"type": "integer", "description": "Only count matching orders in this window."},
    "min_days_since_last_order": {"type": "integer", "description": "Lapsed: no order for at least N days."},
    "max_days_since_last_order": {"type": "integer", "description": "Active: ordered within N days."},
    "min_lifetime_revenue": {"type": "number"},
    "min_average_order_value": {"type": "number"},
    "lifecycle_stages": {"type": "array", "items": {"type": "string", "enum": [s.value for s in LifecycleStage]}},
}


def _criteria(args: dict) -> CohortCriteria:
    try:
        return CohortCriteria.from_dict({k: v for k, v in args.items() if k in COHORT_PROPS})
    except (CohortError, TypeError) as exc:
        raise ToolError(str(exc)) from exc


@tool(
    "list_segments",
    group=GROUP,
    risk=Risk.READ,
    description="Every active segment with its member count. 'VIP', 'high value', 'lapsed' etc. map onto these.",
    properties={"include_archived": {"type": "boolean"}},
)
def list_segments(ctx: ToolContext, include_archived: bool = False) -> ToolResult:
    query = select(Segment).order_by(Segment.is_system.desc(), Segment.name)
    if not include_archived:
        query = query.where(Segment.status == SegmentStatus.ACTIVE.value)
    segments = ctx.db.execute(query).scalars().all()
    return ToolResult.ok(
        [
            {
                **{k: v for k, v in segment_snapshot(s).items() if k not in ("rule",)},
                "definition": describe_rule(s.rule_definition)
                if s.segment_type == SegmentType.DYNAMIC.value
                else (s.description or "Manual list"),
            }
            for s in segments
        ]
    )


@tool(
    "get_segment",
    group=GROUP,
    risk=Risk.READ,
    description="One segment's definition, size and a sample of members.",
    properties={"segment_id": {"type": "integer"}, "name": {"type": "string"}},
)
def get_segment(ctx: ToolContext, segment_id: int | None = None, name: str | None = None) -> ToolResult:
    from app.services.segments import evaluate_segment

    segment = segment_for(ctx, segment_id, name)
    members = evaluate_segment(ctx.db, segment)
    data = segment_snapshot(segment)
    data["definition"] = (
        describe_rule(segment.rule_definition)
        if segment.segment_type == SegmentType.DYNAMIC.value
        else segment.description
    )
    data["current_members"] = len(members)
    data["sample"] = [
        {"id": m["id"], "name": f"{m['first_name']} {m['last_name']}".strip(), "lifecycle_stage": m.get("lifecycle_stage")}
        for m in members[:8]
    ]
    return ToolResult.ok(
        data,
        focus=("segment", segment.id),
        result_set=result_set("customers", [m["id"] for m in members], f"Members of segment '{segment.name}'"),
    )


@tool(
    "get_segment_fields",
    group=GROUP,
    risk=Risk.READ,
    description="The fields and operators segment rules can use.",
)
def get_segment_fields(ctx: ToolContext) -> ToolResult:
    return ToolResult.ok(field_catalog())


@tool(
    "preview_segment",
    group=GROUP,
    risk=Risk.READ,
    description=f"Count and sample the customers a segment rule would match, without saving it. {RULE_DOC}",
    properties={"rule": {"type": "object"}},
    required=["rule"],
)
def preview_segment(ctx: ToolContext, rule: dict) -> ToolResult:
    try:
        validate_rule(rule)
    except RuleError as exc:
        raise ToolError(f"Invalid rule: {exc}") from exc
    preview = preview_rule(ctx.db, rule, limit=10)
    preview["definition"] = describe_rule(rule)
    return ToolResult.ok(preview, metadata={"count": preview["matched_customers"]})


def _create_summary(args: dict) -> str:
    return f"Create segment '{args.get('name')}'"


@tool(
    "create_segment",
    group=GROUP,
    risk=Risk.WRITE,
    creates=True,
    summarize=_create_summary,
    description=(
        "Create a segment from a rule, or (use_last_result=true) from the customer list the "
        f"previous search returned. {RULE_DOC}"
    ),
    properties={
        "name": {"type": "string"},
        "description": {"type": "string"},
        "rule": {"type": "object"},
        "use_last_result": {"type": "boolean"},
    },
    required=["name"],
)
def create_segment(
    ctx: ToolContext,
    name: str,
    description: str = "",
    rule: dict | None = None,
    use_last_result: bool = False,
) -> ToolResult:
    db = ctx.db
    name = name.strip()
    if db.execute(select(Segment.id).where(Segment.name == name)).first():
        raise ToolError(f"A segment named '{name}' already exists.")
    if bool(rule) == bool(use_last_result):
        raise ToolError("Give either a rule or use_last_result=true, not both or neither.")

    if use_last_result:
        last = ctx.state.get("last_result_set") or {}
        ids = last.get("ids") or []
        if last.get("kind") != "customers" or not ids:
            raise ToolError("There is no customer list in this conversation to build a segment from.")
        segment = Segment(
            name=name,
            description=description or last.get("description", ""),
            segment_type=SegmentType.MANUAL.value,
            rule_definition={},
            status=SegmentStatus.ACTIVE.value,
        )
        db.add(segment)
        db.flush()
        for customer_id in ids:
            db.add(CustomerSegment(segment_id=segment.id, customer_id=customer_id, source="copilot"))
        db.flush()
    else:
        try:
            validate_rule(rule)
        except RuleError as exc:
            raise ToolError(f"Invalid rule: {exc}") from exc
        segment = Segment(
            name=name,
            description=description or describe_rule(rule),
            segment_type=SegmentType.DYNAMIC.value,
            rule_definition=rule,
            status=SegmentStatus.ACTIVE.value,
        )
        db.add(segment)
        db.flush()

    refresh_segment_membership(db, segment, commit=False)
    db.add(
        AuditLog(
            actor=ctx.user.email,
            action="SEGMENT_CREATED",
            entity_type="segment",
            entity_id=str(segment.id),
            detail={"name": segment.name, "via": "copilot"},
        )
    )
    db.commit()
    snapshot = segment_snapshot(segment)
    return ToolResult.ok(
        snapshot,
        focus=("segment", segment.id),
        target_type="segment",
        target_id=segment.id,
        after=snapshot,
    )


@tool(
    "update_segment",
    group=GROUP,
    risk=Risk.WRITE,
    summarize=lambda a: f"Update segment {a.get('segment_id') or '(current)'}",
    description=f"Rename a segment or change its rule. Campaigns using it pick up the change. {RULE_DOC}",
    properties={
        "segment_id": {"type": "integer"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "rule": {"type": "object"},
    },
)
def update_segment(
    ctx: ToolContext,
    segment_id: int | None = None,
    name: str | None = None,
    description: str | None = None,
    rule: dict | None = None,
) -> ToolResult:
    segment = segment_for(ctx, segment_id)
    if segment.is_system and rule is not None:
        raise ToolError(f"'{segment.name}' is a system segment; its rule cannot be changed. Duplicate it instead.")
    before = segment_snapshot(segment)
    if name:
        segment.name = name.strip()
    if description is not None:
        segment.description = description
    if rule is not None:
        if segment.segment_type != SegmentType.DYNAMIC.value:
            raise ToolError("Only a rule-based segment has a rule to change.")
        try:
            validate_rule(rule)
        except RuleError as exc:
            raise ToolError(f"Invalid rule: {exc}") from exc
        segment.rule_definition = rule
    refresh_segment_membership(ctx.db, segment, commit=False)
    ctx.db.add(
        AuditLog(
            actor=ctx.user.email,
            action="SEGMENT_UPDATED",
            entity_type="segment",
            entity_id=str(segment.id),
            detail={"via": "copilot"},
        )
    )
    ctx.db.commit()
    after = segment_snapshot(segment)
    return ToolResult.ok(
        after, focus=("segment", segment.id), target_type="segment", target_id=segment.id, before=before, after=after
    )


@tool(
    "export_segment",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Prepare a CSV export of a segment's members. Returns a download link the operator "
        "clicks; the Copilot never reads the exported contact details itself."
    ),
    properties={"segment_id": {"type": "integer"}, "name": {"type": "string"}},
)
def export_segment(ctx: ToolContext, segment_id: int | None = None, name: str | None = None) -> ToolResult:
    segment = segment_for(ctx, segment_id, name)
    return ToolResult.ok(
        {
            "segment_id": segment.id,
            "segment": segment.name,
            "members": segment.member_count,
            "download": {
                "path": f"/api/v1/segments/{segment.id}/export.csv",
                "filename": f"segment-{segment.id}.csv",
            },
        },
        focus=("segment", segment.id),
    )


@tool(
    "list_cohorts",
    group=GROUP,
    risk=Risk.READ,
    description="Saved purchase cohorts, plus monthly acquisition-cohort retention from analytics.",
)
def list_cohorts(ctx: ToolContext) -> ToolResult:
    from app.analytics.dashboards import cohort_analytics
    from app.services.purchase_cohorts import is_cohort_segment

    saved = [
        segment_snapshot(s)
        for s in ctx.db.execute(select(Segment).where(Segment.segment_type == SegmentType.MANUAL.value))
        .scalars()
        .all()
        if is_cohort_segment(s)
    ]
    return ToolResult.ok(
        {"purchase_cohorts": saved, "acquisition_cohorts": cohort_analytics(ctx.db, months=6, now=ctx.now)}
    )


@tool(
    "preview_cohort",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Count customers by what they bought and when — e.g. ordered Beer at least twice and "
        "no order in 14 days. Nothing is saved; the matched list is remembered for follow-ups."
    ),
    properties=COHORT_PROPS,
)
def preview_cohort(ctx: ToolContext, **args) -> ToolResult:
    criteria = _criteria(args)
    rows = find_customers(ctx.db, criteria, now=ctx.now)
    contactable = sum(1 for r in rows if r["marketing_consent"])
    return ToolResult.ok(
        {
            "definition": criteria.describe(),
            "criteria": criteria.as_dict(),
            "matched": len(rows),
            "with_marketing_consent": contactable,
            "sample": rows[:10],
        },
        metadata={"count": len(rows)},
        result_set=result_set("customers", [r["id"] for r in rows], criteria.describe()),
    )


@tool(
    "create_cohort",
    group=GROUP,
    risk=Risk.WRITE,
    creates=True,
    summarize=lambda a: f"Create cohort '{a.get('name')}'",
    description="Save a purchase cohort as a segment campaigns can target. It re-evaluates on every segment refresh.",
    properties={"name": {"type": "string"}, "description": {"type": "string"}, **COHORT_PROPS},
    required=["name"],
)
def create_cohort(ctx: ToolContext, name: str, description: str = "", **args) -> ToolResult:
    criteria = _criteria(args)
    try:
        segment = create_cohort_segment(
            ctx.db, name=name.strip(), criteria=criteria, description=description, now=ctx.now, commit=False
        )
    except CohortError as exc:
        raise ToolError(str(exc)) from exc
    ctx.db.add(
        AuditLog(
            actor=ctx.user.email,
            action="SEGMENT_CREATED",
            entity_type="segment",
            entity_id=str(segment.id),
            detail={"name": segment.name, "via": "copilot", "cohort": criteria.as_dict()},
        )
    )
    ctx.db.commit()
    snapshot = segment_snapshot(segment)
    return ToolResult.ok(
        snapshot, focus=("segment", segment.id), target_type="segment", target_id=segment.id, after=snapshot
    )
