"""Go-live readiness for a messaging integration.

Switching an integration from mock to live is the moment this system starts
texting real people who can complain, opt out, or report it. The failures that
matter then are mostly not code failures — an unset webhook secret, a sender ID
the carrier will reject, customer records whose phone numbers no SMS can reach.
None of those show up in a connection test, and all of them are cheaper to find
before the first send than after it.

So this module answers one question: *if I switch this to live right now, what
happens?* Each check is independent, names what is wrong and what to do about
it, and says whether it blocks go-live or is merely worth knowing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import AutomationStatus, Channel
from app.core.phone import is_sendable
from app.integrations.registry import (
    LIVE_ADAPTERS,
    get_integration,
    webhook_secret,
)
from app.models.entities import Automation, Customer, Integration
from app.services.brand import build_compliance_config

#: TNZ rejects an alphanumeric sender longer than this.
MAX_ALPHANUMERIC_SENDER = 11


@dataclass
class Check:
    """One readiness question, already answered."""

    key: str
    label: str
    passed: bool
    #: A failed blocking check means "do not go live yet". A failed advisory
    #: check is something to know, not a veto — a fraction of unreachable
    #: numbers is normal in imported data.
    blocking: bool
    detail: str
    #: What to do about it, when there is something to do.
    remedy: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "blocking": self.blocking,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class ReadinessReport:
    provider: str
    channel: str
    mode: str
    checks: list[Check] = field(default_factory=list)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and not c.passed]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.blocking and not c.passed]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "channel": self.channel,
            "mode": self.mode,
            "ready": self.ready,
            "blocking_count": len(self.blockers),
            "warning_count": len(self.warnings),
            "checks": [c.as_dict() for c in self.checks],
        }


def _credentials_check(integration: Integration) -> Check:
    adapter_cls = LIVE_ADAPTERS.get(Channel(integration.channel))
    required = list(adapter_cls.required_credentials) if adapter_cls else []
    creds = integration.credentials or {}
    missing = [key for key in required if not str(creds.get(key) or "").strip()]
    return Check(
        key="credentials",
        label="Provider credentials",
        passed=not missing,
        blocking=True,
        detail=(
            "All required credentials are set."
            if not missing
            else f"Missing: {', '.join(missing)}."
        ),
        remedy="" if not missing else "Add them under this integration's settings.",
    )


def _webhook_secret_check(integration: Integration) -> Check:
    secret = webhook_secret(integration)
    return Check(
        key="webhook_secret",
        label="Webhook secret",
        passed=bool(secret),
        blocking=True,
        detail=(
            "Set, so inbound delivery receipts and replies are authenticated."
            if secret
            else "Not set. Inbound webhooks are refused while it is missing."
        ),
        remedy=(
            ""
            if secret
            else "Generate a secret, save it here, and configure the same value "
            "on the provider's webhook. Without it anyone who knows a customer's "
            "number could forge a STOP — or a START that restores consent they "
            "withdrew."
        ),
    )


def _sender_check(integration: Integration) -> Check:
    sender = str((integration.credentials or {}).get("sender") or "").strip()
    if not sender:
        return Check(
            key="sender",
            label="Sender ID",
            passed=False,
            blocking=True,
            detail="No sender configured.",
            remedy="Set the sender ID or number TNZ has registered for GIMME.",
        )
    # A numeric sender is a real number and has its own length rules; an
    # alphanumeric sender ID is capped and cannot receive replies.
    numeric = sender.lstrip("+").isdigit()
    if not numeric and len(sender) > MAX_ALPHANUMERIC_SENDER:
        return Check(
            key="sender",
            label="Sender ID",
            passed=False,
            blocking=True,
            detail=f"'{sender}' is {len(sender)} characters; the limit is {MAX_ALPHANUMERIC_SENDER}.",
            remedy="Shorten the sender ID, or use a registered number instead.",
        )
    return Check(
        key="sender",
        label="Sender ID",
        passed=True,
        blocking=True,
        detail=(
            f"Sending from {sender}."
            if numeric
            else f"Sending from '{sender}'. An alphanumeric sender cannot receive replies, "
            "so STOP would not reach this system — use a number if you rely on it."
        ),
    )


def _connection_check(integration: Integration) -> Check:
    status = (integration.status or "").upper()
    if status == "MOCK":
        # Testing the mock proves nothing about TNZ. It always succeeds.
        return Check(
            key="connection",
            label="Connection test",
            passed=False,
            blocking=True,
            detail="The last test ran against the mock adapter, which cannot fail.",
            remedy=(
                "Enter the TNZ credentials, switch this integration to Live, and test "
                "again — that is the first call that actually reaches TNZ."
            ),
        )
    passed = status == "OK"
    if passed:
        return Check(
            key="connection",
            label="Connection test",
            passed=True,
            blocking=True,
            detail=integration.status_message or "Connected.",
        )
    if not status:
        return Check(
            key="connection",
            label="Connection test",
            passed=False,
            blocking=True,
            detail="Never run.",
            remedy="Run a connection test from this integration's settings.",
        )
    # The failure is already recorded; repeating "run a test" when one just ran
    # and said why is not a remedy, it is a shrug.
    return Check(
        key="connection",
        label="Connection test",
        passed=False,
        blocking=True,
        detail=integration.status_message or f"Last result: {status}.",
        remedy="Resolve what the test reports, then run it again.",
    )


def _reachability_check(db: Session) -> Check:
    """How many consenting customers we could actually text."""
    rows = db.execute(
        select(Customer.phone).where(
            Customer.sms_consent.is_(True),
            Customer.is_suppressed.is_(False),
        )
    ).scalars().all()
    total = len(rows)
    reachable = sum(1 for phone in rows if is_sendable(phone))
    unreachable = total - reachable

    if total == 0:
        return Check(
            key="reachable_audience",
            label="Reachable audience",
            passed=False,
            blocking=True,
            detail="No customer has SMS consent, so a live run would send nothing.",
            remedy="Import consent data, or check that consent is being recorded.",
        )

    share = unreachable / total
    return Check(
        key="reachable_audience",
        label="Reachable audience",
        # A handful of bad numbers is ordinary; a fifth of the list is a data
        # problem worth fixing before spending money on sends.
        passed=share <= 0.2,
        blocking=False,
        detail=(
            f"{reachable} of {total} consenting customers have a mobile number an SMS "
            f"can reach ({unreachable} cannot)."
        ),
        remedy=(
            ""
            if share <= 0.2
            else "Re-import the affected customers with mobile numbers in E.164 "
            "(+64…) form. They are skipped rather than sent to, so nothing is lost — "
            "but they are also never reached."
        ),
    )


def _opt_out_check(db: Session) -> Check:
    config = build_compliance_config(db)
    enabled = getattr(config, "require_sms_opt_out", False)
    return Check(
        key="sms_opt_out",
        label="Opt-out enforcement",
        passed=bool(enabled),
        blocking=True,
        detail=(
            "Every SMS is checked for an opt-out instruction before it sends."
            if enabled
            else "Disabled. An SMS with no way to opt out could go out."
        ),
        remedy="" if enabled else "Re-enable require_sms_opt_out in compliance settings.",
    )


def _quiet_hours_check(db: Session) -> Check:
    config = build_compliance_config(db)
    if not config.enforce_quiet_hours:
        return Check(
            key="quiet_hours",
            label="Send window",
            passed=False,
            blocking=False,
            detail="Not enforced, so a campaign could text somebody overnight.",
            remedy="Re-enable quiet hours in compliance settings.",
        )

    # Two places decide when a message may go out: the compliance rule blocks a
    # recipient, and SEND_WINDOW defers an automation candidate. When they
    # disagree the tighter one silently wins for automations only, so a
    # campaign can send in the gap that an automation would have held — and no
    # screen shows a number both halves agree on.
    window_start, window_end = settings.send_window
    if (config.quiet_hours_end, config.quiet_hours_start) != (window_start, window_end):
        return Check(
            key="quiet_hours",
            label="Send window",
            passed=False,
            blocking=False,
            detail=(
                f"Compliance allows {config.quiet_hours_end:%H:%M}–"
                f"{config.quiet_hours_start:%H:%M}, but automations are held to "
                f"{window_start:%H:%M}–{window_end:%H:%M}. A campaign can send in "
                "the gap; an automation cannot."
            ),
            remedy=(
                "Set the QUIET_HOURS rule to start at "
                f"{window_end:%H:%M} and end at {window_start:%H:%M}, or change "
                "SEND_WINDOW to match the rule — whichever is the window you mean."
            ),
        )

    return Check(
        key="quiet_hours",
        label="Send window",
        passed=True,
        blocking=False,
        detail=(
            f"Sends are confined to {window_start:%H:%M}–{window_end:%H:%M} NZ time, "
            "on both the campaign and automation paths."
        ),
    )


def _unapproved_automations_check(db: Session) -> Check:
    """Anything already ACTIVE that has not been approved.

    Harmless in mock mode. The moment the integration goes live, an active
    automation sends for real on its next scheduled run.
    """
    rows = db.execute(
        select(Automation.name).where(
            Automation.status == AutomationStatus.ACTIVE.value,
            Automation.require_approval.is_(True),
            Automation.approved_at.is_(None),
        )
    ).scalars().all()
    return Check(
        key="unapproved_active",
        label="Active automations are approved",
        passed=not rows,
        blocking=True,
        detail=(
            "Every active automation has approved copy."
            if not rows
            else f"Active but unapproved: {', '.join(rows)}."
        ),
        remedy="" if not rows else "Approve the copy, or pause these before going live.",
    )


def _pending_send_volume(db: Session) -> Check:
    """What the first live day would actually cost, in messages."""
    active = db.execute(
        select(func.count(Automation.id)).where(
            Automation.status == AutomationStatus.ACTIVE.value
        )
    ).scalar_one()
    return Check(
        key="pending_volume",
        label="What goes live immediately",
        passed=True,
        blocking=False,
        detail=(
            f"{active} automation(s) are active and will send for real on their next run."
            if active
            else "No automation is active, so nothing sends until you activate one."
        ),
        remedy=(
            "Dry-run each one first — the preview is produced by the same code path "
            "as a live send."
            if active
            else ""
        ),
    )


def integration_readiness(db: Session, integration: Integration) -> ReadinessReport:
    """Everything that decides whether this integration can safely go live."""
    checks = [
        _credentials_check(integration),
        _connection_check(integration),
        _webhook_secret_check(integration),
    ]
    if Channel(integration.channel) in (Channel.SMS, Channel.WHATSAPP):
        checks.append(_sender_check(integration))
        checks.append(_reachability_check(db))
        checks.append(_opt_out_check(db))
    checks.extend(
        [
            _quiet_hours_check(db),
            _unapproved_automations_check(db),
            _pending_send_volume(db),
        ]
    )
    return ReadinessReport(
        provider=integration.provider,
        channel=integration.channel,
        mode=integration.mode or "mock",
        checks=checks,
    )


def sms_readiness(db: Session) -> ReadinessReport | None:
    """Readiness for the SMS integration, which is the TNZ one."""
    integration = get_integration(db, Channel.SMS)
    return integration_readiness(db, integration) if integration else None
