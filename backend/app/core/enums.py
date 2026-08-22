"""Domain enumerations shared across models, services and API schemas.

These are stored as plain strings in the database so that adding a value never
requires a destructive migration.
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class LifecycleStage(StrEnum):
    NEW = "NEW"
    ACTIVATING = "ACTIVATING"
    REGULAR = "REGULAR"
    HIGH_VALUE = "HIGH_VALUE"
    VIP = "VIP"
    AT_RISK = "AT_RISK"
    DORMANT = "DORMANT"
    CHURNED = "CHURNED"
    REACTIVATED = "REACTIVATED"


class ChurnRiskBand(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class NextBestAction(StrEnum):
    NO_ACTION = "NO_ACTION"
    WELCOME = "WELCOME"
    ENCOURAGE_SECOND_ORDER = "ENCOURAGE_SECOND_ORDER"
    REORDER_REMINDER = "REORDER_REMINDER"
    PERSONALIZED_RECOMMENDATION = "PERSONALIZED_RECOMMENDATION"
    CATEGORY_MESSAGE = "CATEGORY_MESSAGE"
    VIP_APPRECIATION = "VIP_APPRECIATION"
    LOYALTY_RECOGNITION = "LOYALTY_RECOGNITION"
    REACTIVATION = "REACTIVATION"
    WIN_BACK = "WIN_BACK"
    REQUEST_FEEDBACK = "REQUEST_FEEDBACK"
    SUPPRESS_COMMUNICATION = "SUPPRESS_COMMUNICATION"


class Channel(StrEnum):
    EMAIL = "EMAIL"
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    PUSH = "PUSH"


class CampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    AI_GENERATED = "AI_GENERATED"
    VALIDATED = "VALIDATED"
    COMPLIANCE_CHECKED = "COMPLIANCE_CHECKED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CampaignObjective(StrEnum):
    ACTIVATION = "ACTIVATION"
    SECOND_ORDER = "SECOND_ORDER"
    RETENTION = "RETENTION"
    REORDER = "REORDER"
    VIP_APPRECIATION = "VIP_APPRECIATION"
    REACTIVATION = "REACTIVATION"
    WIN_BACK = "WIN_BACK"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    FEEDBACK = "FEEDBACK"


class RecipientStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    EXCLUDED_NO_CONSENT = "EXCLUDED_NO_CONSENT"
    EXCLUDED_SUPPRESSED = "EXCLUDED_SUPPRESSED"
    EXCLUDED_FREQUENCY_CAP = "EXCLUDED_FREQUENCY_CAP"
    EXCLUDED_QUIET_HOURS = "EXCLUDED_QUIET_HOURS"
    EXCLUDED_AGE = "EXCLUDED_AGE"
    EXCLUDED_MISSING_CONTACT = "EXCLUDED_MISSING_CONTACT"
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CONVERTED = "CONVERTED"


class MessageStatus(StrEnum):
    DRAFT = "DRAFT"
    GENERATED = "GENERATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    EDITED = "EDITED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"
    FAILED = "FAILED"


class EventType(StrEnum):
    CUSTOMER_CREATED = "CUSTOMER_CREATED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_COMPLETED = "ORDER_COMPLETED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    EMAIL_SENT = "EMAIL_SENT"
    EMAIL_DELIVERED = "EMAIL_DELIVERED"
    EMAIL_OPENED = "EMAIL_OPENED"
    EMAIL_CLICKED = "EMAIL_CLICKED"
    EMAIL_BOUNCED = "EMAIL_BOUNCED"
    SMS_SENT = "SMS_SENT"
    SMS_DELIVERED = "SMS_DELIVERED"
    SMS_FAILED = "SMS_FAILED"
    WHATSAPP_SENT = "WHATSAPP_SENT"
    WHATSAPP_DELIVERED = "WHATSAPP_DELIVERED"
    WHATSAPP_READ = "WHATSAPP_READ"
    WHATSAPP_REPLIED = "WHATSAPP_REPLIED"
    MESSAGE_FAILED = "MESSAGE_FAILED"
    CUSTOMER_OPTED_OUT = "CUSTOMER_OPTED_OUT"
    CUSTOMER_REACTIVATED = "CUSTOMER_REACTIVATED"
    CAMPAIGN_CONVERSION = "CAMPAIGN_CONVERSION"


ENGAGEMENT_EVENTS = {
    EventType.EMAIL_OPENED,
    EventType.EMAIL_CLICKED,
    EventType.WHATSAPP_READ,
    EventType.WHATSAPP_REPLIED,
}

DELIVERY_EVENTS = {
    EventType.EMAIL_DELIVERED,
    EventType.SMS_DELIVERED,
    EventType.WHATSAPP_DELIVERED,
}


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class ConsentType(StrEnum):
    MARKETING = "MARKETING"
    EMAIL = "EMAIL"
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"


class SegmentType(StrEnum):
    DYNAMIC = "DYNAMIC"
    MANUAL = "MANUAL"


class SegmentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ComplianceSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class IngestionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JourneyStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class JourneyNodeType(StrEnum):
    TRIGGER = "TRIGGER"
    DELAY = "DELAY"
    CONDITION = "CONDITION"
    ACTION = "ACTION"


class JourneyExecutionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    EXITED = "EXITED"
    FAILED = "FAILED"


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    MARKETER = "MARKETER"
    VIEWER = "VIEWER"


# --------------------------------------------------------------------------
# Campaign automations (recurring sequences, behavioural nudges, cohort bulk)
# --------------------------------------------------------------------------
class AutomationKind(StrEnum):
    """The three campaign types, which share plumbing but differ in timing."""

    #: Feature 1 — an ordered series of steps on a per-customer clock.
    SEQUENCE = "SEQUENCE"
    #: Feature 2 — a standing per-customer nudge on their own order pattern.
    NUDGE = "NUDGE"
    #: Feature 3 — a one-off or recurring send to whoever matches a segment.
    COHORT_BULK = "COHORT_BULK"


class AutomationStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class EnrollmentMode(StrEnum):
    #: New matching customers join after the automation has started.
    ROLLING = "ROLLING"
    #: The audience is locked at launch.
    FIXED_COHORT = "FIXED_COHORT"


class EnrollmentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    #: Reached the end of the step list.
    COMPLETED = "COMPLETED"
    #: Left early — opted out, ordered, or fell out of the segment.
    STOPPED = "STOPPED"


class SendStatus(StrEnum):
    """Per-customer, per-message delivery state."""

    SCHEDULED = "SCHEDULED"
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    #: Deliberately not sent — consent, quiet hours, dedup, cap.
    SKIPPED = "SKIPPED"
    #: Produced by a dry run; never dispatched.
    PREVIEW = "PREVIEW"


class SkipReason(StrEnum):
    NO_CONSENT = "NO_CONSENT"
    SUPPRESSED = "SUPPRESSED"
    AGE_NOT_VERIFIED = "AGE_NOT_VERIFIED"
    MISSING_CONTACT = "MISSING_CONTACT"
    FREQUENCY_CAP = "FREQUENCY_CAP"
    QUIET_HOURS = "QUIET_HOURS"
    #: Lost a same-day contest to a higher-priority automation.
    DEDUPED = "DEDUPED"
    ALREADY_ORDERED = "ALREADY_ORDERED"
    PENDING_ORDER = "PENDING_ORDER"
    LEFT_SEGMENT = "LEFT_SEGMENT"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class RecurrenceKind(StrEnum):
    ONCE = "ONCE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


#: Dedup priority when two automations would message the same customer on the
#: same local day. Higher wins; the loser is skipped and logged.
AUTOMATION_PRIORITY: dict[str, int] = {
    AutomationKind.NUDGE.value: 30,
    AutomationKind.SEQUENCE.value: 20,
    AutomationKind.COHORT_BULK.value: 10,
}
