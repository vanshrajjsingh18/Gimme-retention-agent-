"""Merge tags, copy validation and message changes.

Validation is the existing machinery, not a second copy of it: tags are
checked against the merge-tag whitelist, the copy is resolved with the same
resolver campaigns use, and the result is run through the compliance engine
with the brand's own configuration.
"""
from __future__ import annotations

import math

from app.copilot.registry import Risk, ToolContext, ToolError, ToolResult, tool
from app.copilot.tools._common import (
    automation_for,
    automation_snapshot,
    campaign_for,
    campaign_snapshot,
    coupon_rows,
)
from app.compliance.engine import check_content
from app.core.enums import CampaignStatus, Channel
from app.models.entities import AuditLog
from app.services.brand import build_compliance_config
from app.services.merge_tags import (
    LEGACY_TOKENS,
    context_for,
    field_catalog,
    render_template,
    unknown_tags,
)

GROUP = "messaging"

GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€"
)


def sms_segments(text: str) -> dict:
    gsm = all(ch in GSM7 for ch in text)
    length = len(text)
    if gsm:
        segments = 1 if length <= 160 else math.ceil(length / 153)
    else:
        segments = 1 if length <= 70 else math.ceil(length / 67)
    return {"characters": length, "encoding": "GSM-7" if gsm else "UCS-2", "segments": segments}


def validate_copy(
    ctx: ToolContext,
    template: str,
    channel: str,
    *,
    coupon_codes: list[str] | None = None,
) -> dict:
    """Everything that would stop this copy being sent, or make it read badly."""
    db = ctx.db
    unknown = unknown_tags(template)
    uses_coupon = "coupon_code" in template
    extra = {"coupon_code": (coupon_codes or [""])[0]} if uses_coupon else None
    resolved = render_template(template, context_for(db, None, extra=extra))
    config = build_compliance_config(db)
    findings = check_content(resolved.text, config, channel=Channel(channel))

    warnings: list[str] = []
    if uses_coupon and not coupon_codes:
        warnings.append(
            "#coupon_code# is used but this campaign has no coupon codes, so it would render empty."
        )
    if resolved.unfillable:
        warnings.append(
            "These tags have no fallback and render empty when a customer lacks the value: "
            + ", ".join(f"#{t}#" for t in resolved.unfillable)
        )
    hard = [f.as_dict() for f in findings if f.blocks_send and not f.vouchable]
    vouchable = [f.as_dict() for f in findings if f.blocks_send and f.vouchable]
    advisory = [f.as_dict() for f in findings if not f.blocks_send]
    report = {
        "valid": not unknown and not hard,
        "sendable_without_review": not unknown and not hard and not vouchable,
        "unknown_tags": [f"#{t}#" for t in unknown],
        "tags_used": [f"#{t}#" for t in resolved.valid_tags],
        "sample_render": resolved.text,
        "blocking_findings": hard,
        "needs_confirmation_findings": vouchable,
        "advisory_findings": advisory,
        "warnings": warnings,
    }
    if channel == Channel.SMS.value:
        report["sms"] = sms_segments(resolved.text)
        if report["sms"]["segments"] > 2:
            warnings.append(
                f"The sample renders to {report['sms']['segments']} SMS segments; each is billed."
            )
    return report


@tool(
    "get_message_fields",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "The merge tags message copy may use (#first_name#, #product#, …) with fallbacks. "
        "#coupon_code# is filled from the campaign's coupon variants."
    ),
)
def get_message_fields(ctx: ToolContext) -> ToolResult:
    return ToolResult.ok(
        {"fields": field_catalog(), "also_accepted": sorted(f"#{t}#" for t in LEGACY_TOKENS)}
    )


@tool(
    "validate_message_template",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Check copy before using it: unknown merge tags, compliance (alcohol marketing rules, "
        "opt-out wording, unverified offers/codes), SMS length and segments, and a sample "
        "render. Always run this on copy you write before proposing it."
    ),
    properties={
        "template": {"type": "string"},
        "channel": {"type": "string", "enum": [c.value for c in Channel]},
        "coupon_codes": {"type": "array", "items": {"type": "string"}},
        "automation_id": {"type": "integer", "description": "Use this campaign's coupon codes."},
    },
    required=["template"],
)
def validate_message_template(
    ctx: ToolContext,
    template: str,
    channel: str = "SMS",
    coupon_codes: list[str] | None = None,
    automation_id: int | None = None,
) -> ToolResult:
    if automation_id and not coupon_codes:
        coupon_codes = [v.coupon_code for v in coupon_rows(ctx.db, automation_id) if v.enabled]
    return ToolResult.ok(validate_copy(ctx, template, channel, coupon_codes=coupon_codes))


@tool(
    "render_message_preview",
    group=GROUP,
    risk=Risk.READ,
    description=(
        "Render a template for real customers. With a Smart Reorder campaign, renders exactly "
        "what its dry run would schedule (send time, product, coupon) for the first few customers."
    ),
    properties={
        "template": {"type": "string", "description": "Omit to use the campaign's current copy."},
        "automation_id": {"type": "integer"},
        "customer": {"type": "string", "description": "Render for one specific customer."},
        "count": {"type": "integer"},
    },
)
def render_message_preview(
    ctx: ToolContext,
    template: str | None = None,
    automation_id: int | None = None,
    customer: str | None = None,
    count: int = 3,
) -> ToolResult:
    from app.copilot.tools._common import find_customer
    from app.services.coupon_assignment import get_or_assign_coupon

    count = max(1, min(count, 10))
    automation = None
    if automation_id is not None or (ctx.active[0] == "automation" and not customer):
        automation = automation_for(ctx, automation_id)

    if automation is not None and automation.kind == "NUDGE" and not customer and template is None:
        from app.services.smart_reorder_queue import build_queue

        report = build_queue(ctx.db, automation, now=ctx.now, dry_run=True, limit=count)
        ctx.db.rollback()
        return ToolResult.ok(
            {"automation": automation.name, "previews": report.messages},
            metadata={"source": "smart_reorder_dry_run"},
        )

    copy = template or (automation.message_template if automation else None)
    if not copy:
        raise ToolError("Give the copy to render, or select a campaign.")
    targets = []
    if customer:
        targets = [find_customer(ctx.db, customer)]
    elif automation is not None:
        from app.automations.cohort import resolve_audience
        from app.models.entities import Customer

        ids = resolve_audience(ctx.db, automation, now=ctx.now)[:count]
        targets = [ctx.db.get(Customer, i) for i in ids]
    previews = []
    for target in targets:
        extra = {}
        if automation is not None:
            extra["coupon_code"] = get_or_assign_coupon(ctx.db, target.id, automation.id, persist=False) or ""
        resolved = render_template(copy, context_for(ctx.db, target, extra=extra))
        previews.append(
            {
                "customer_id": target.id,
                "customer": target.full_name,
                "message": resolved.text,
                "fallbacks_used": resolved.fallbacks_used,
                "unknown_tags": resolved.unknown_tags,
            }
        )
    if not previews:
        previews.append({"customer": "Sample", "message": render_template(copy, context_for(ctx.db, None)).text})
    return ToolResult.ok({"template": copy, "previews": previews})


@tool(
    "update_message_template",
    group=GROUP,
    risk=Risk.WRITE,
    summarize=lambda a: "Change message copy",
    description=(
        "Replace the message copy of an automation (incl. Smart Reorder) or a one-off campaign. "
        "Validated first; copy with unknown merge tags or prohibited claims is refused. "
        "Changing copy withdraws any existing approval."
    ),
    properties={
        "template": {"type": "string"},
        "automation_id": {"type": "integer"},
        "campaign_id": {"type": "integer"},
    },
    required=["template"],
)
def update_message_template(
    ctx: ToolContext, template: str, automation_id: int | None = None, campaign_id: int | None = None
) -> ToolResult:
    from app.automations.service import apply_update

    db = ctx.db
    use_campaign = campaign_id is not None or (automation_id is None and ctx.active[0] == "campaign")
    if use_campaign:
        campaign = campaign_for(ctx, campaign_id)
        report = validate_copy(ctx, template, campaign.channel)
        _refuse_invalid(report)
        if campaign.status not in (
            CampaignStatus.DRAFT.value,
            CampaignStatus.AI_GENERATED.value,
            CampaignStatus.VALIDATED.value,
            CampaignStatus.COMPLIANCE_CHECKED.value,
            CampaignStatus.AWAITING_APPROVAL.value,
            CampaignStatus.APPROVED.value,
        ):
            raise ToolError(f"A campaign in status {campaign.status} cannot be edited.")
        before = campaign_snapshot(db, campaign)
        campaign.body = template
        campaign.status = CampaignStatus.DRAFT.value
        campaign.compliance_result = {}
        campaign.approved_by_id = None
        campaign.approved_at = None
        db.add(
            AuditLog(
                actor=ctx.user.email,
                action="CAMPAIGN_UPDATED",
                entity_type="campaign",
                entity_id=str(campaign.id),
                detail={"fields": ["body"], "via": "copilot"},
            )
        )
        db.commit()
        after = campaign_snapshot(db, campaign)
        return ToolResult.ok(
            {"campaign": after, "validation": report},
            focus=("campaign", campaign.id),
            target_type="campaign",
            target_id=campaign.id,
            before=before,
            after=after,
        )

    automation = automation_for(ctx, automation_id)
    codes = [v.coupon_code for v in coupon_rows(db, automation.id) if v.enabled]
    report = validate_copy(ctx, template, automation.channel, coupon_codes=codes)
    _refuse_invalid(report)
    before = automation_snapshot(db, automation)
    summary = apply_update(db, automation, {"message_template": template}, actor=ctx.user.email)
    if automation.campaign_id:
        from app.models.entities import Campaign

        backing = db.get(Campaign, automation.campaign_id)
        if backing is not None:
            backing.body = template
            db.commit()
    after = automation_snapshot(db, automation)
    return ToolResult.ok(
        {"automation": after, "validation": report, "approval_withdrawn": summary["approval_revoked"]},
        focus=("automation", automation.id),
        target_type="automation",
        target_id=automation.id,
        before=before,
        after=after,
    )


def _refuse_invalid(report: dict) -> None:
    problems = list(report["unknown_tags"] and [f"unknown merge tags {', '.join(report['unknown_tags'])}"] or [])
    problems += [f["message"] for f in report["blocking_findings"]]
    if problems:
        raise ToolError("This copy cannot be used: " + "; ".join(problems))
