"""Brand settings and compliance rule configuration."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_write
from app.compliance.engine import PROHIBITED_CLAIM_PATTERNS, check_content
from app.core.database import get_db
from app.core.enums import Channel
from app.models.entities import AuditLog, ComplianceRule, User
from app.schemas.models import (
    BrandSettingsOut,
    BrandSettingsUpdate,
    ComplianceRuleOut,
    ComplianceRuleUpdate,
    ContentCheckRequest,
)
from app.services.brand import build_compliance_config, get_brand_settings

router = APIRouter()


@router.get("/brand", response_model=BrandSettingsOut, tags=["brand"])
def read_brand(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> BrandSettingsOut:
    return BrandSettingsOut.model_validate(get_brand_settings(db))


@router.put("/brand", response_model=BrandSettingsOut, tags=["brand"])
def update_brand(
    payload: BrandSettingsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> BrandSettingsOut:
    """Update brand settings.

    These values ground every generated message and drive the content
    compliance rules, so a change here immediately narrows what the LLM may
    say and what the validator will accept.
    """
    brand = get_brand_settings(db)
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields supplied.")

    for key, value in changes.items():
        setattr(brand, key, value)

    db.add(
        AuditLog(
            actor=user.email,
            action="BRAND_UPDATED",
            entity_type="brand_settings",
            entity_id="1",
            detail={"fields": sorted(changes)},
        )
    )
    db.commit()
    db.refresh(brand)
    return BrandSettingsOut.model_validate(brand)


@router.get("/compliance/rules", response_model=list[ComplianceRuleOut], tags=["compliance"])
def list_rules(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[ComplianceRuleOut]:
    rules = db.execute(select(ComplianceRule).order_by(ComplianceRule.code)).scalars().all()
    return [ComplianceRuleOut.model_validate(r) for r in rules]


@router.patch(
    "/compliance/rules/{rule_id}", response_model=ComplianceRuleOut, tags=["compliance"]
)
def update_rule(
    rule_id: int,
    payload: ComplianceRuleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> ComplianceRuleOut:
    rule = db.get(ComplianceRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Compliance rule not found.")

    changes = payload.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(rule, key, value)

    # Disabling a rule that blocks sending is a consequential change; make it
    # explicit in the audit trail.
    db.add(
        AuditLog(
            actor=user.email,
            action="COMPLIANCE_RULE_UPDATED",
            entity_type="compliance_rule",
            entity_id=rule.code,
            detail={"changes": changes, "blocks_send": rule.blocks_send},
        )
    )
    db.commit()
    db.refresh(rule)
    return ComplianceRuleOut.model_validate(rule)


@router.get("/compliance/prohibited-claims", tags=["compliance"])
def prohibited_claims(_: User = Depends(get_current_user)) -> dict:
    """The built-in prohibited claim categories, for the compliance screen."""
    return {
        "claims": [
            {"code": code, "label": label} for code, label, _ in PROHIBITED_CLAIM_PATTERNS
        ]
    }


@router.post("/compliance/check-content", tags=["compliance"])
def check_message_content(
    payload: ContentCheckRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Run the content compliance rules over arbitrary copy."""
    config = build_compliance_config(db)
    combined = (
        f"{payload.subject}\n{payload.body}"
        if payload.channel == Channel.EMAIL
        else payload.body
    )
    findings = check_content(combined, config, channel=payload.channel)
    blocking = [f for f in findings if f.blocks_send]
    return {
        "passed": not blocking,
        "blocking_count": len(blocking),
        "findings": [f.as_dict() for f in findings],
    }


@router.get("/compliance/config", tags=["compliance"])
def read_config(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict:
    """The live enforcement configuration assembled from brand + rules."""
    config = build_compliance_config(db)
    return {
        "minimum_age": config.minimum_age,
        "require_age_verification": config.require_age_verification,
        "frequency_cap_30d": config.frequency_cap_30d,
        "frequency_cap_7d": config.frequency_cap_7d,
        "quiet_hours_start": config.quiet_hours_start.strftime("%H:%M"),
        "quiet_hours_end": config.quiet_hours_end.strftime("%H:%M"),
        "enforce_quiet_hours": config.enforce_quiet_hours,
        "require_responsible_drinking_statement": (
            config.require_responsible_drinking_statement
        ),
        "allowed_coupon_codes": config.allowed_coupon_codes,
        "allowed_promotions": config.allowed_promotions,
        "delivery_promise": config.delivery_promise,
        "disabled_rules": sorted(config.disabled_rules),
    }
