"""Request and response schemas for the API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.enums import (
    CampaignObjective,
    CampaignStatus,
    Channel,
    ConsentType,
    LifecycleStage,
    OrderStatus,
)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    user: "UserOut"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: str
    is_active: bool


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: str = "ingest"


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    scopes: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    #: Returned exactly once, at creation. Never stored in plaintext.
    api_key: str


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
class CustomerIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=120)
    # Deliberately a plain string rather than EmailStr: strict validators reject
    # reserved TLDs (.test, .invalid) and unusual-but-valid corporate domains,
    # which would drop real customers on import. Format is checked in the
    # ingestion service, which reports the row rather than failing the batch.
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = None
    first_name: str = ""
    last_name: str = ""
    date_of_birth: date | None = None
    age_verified: bool = False
    city: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str = "New Zealand"
    signup_date: datetime | None = None
    acquisition_source: str | None = None
    preferred_channel: Channel = Channel.EMAIL
    marketing_consent: bool = False
    email_consent: bool = False
    sms_consent: bool = False
    whatsapp_consent: bool = False


class OrderIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=120)
    customer_external_id: str = Field(min_length=1, max_length=120)
    ordered_at: datetime
    status: OrderStatus = OrderStatus.COMPLETED
    total_amount: float = Field(ge=0)
    discount_amount: float = Field(default=0.0, ge=0)
    delivery_fee: float = Field(default=0.0, ge=0)
    currency: str = "NZD"
    channel: str | None = None
    coupon_code: str | None = None
    delivery_city: str | None = None


class OrderItemIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=120)
    order_external_id: str = Field(min_length=1, max_length=120)
    sku: str = Field(min_length=1, max_length=80)
    product_name: str = Field(min_length=1, max_length=255)
    category: str = ""
    brand: str = ""
    quantity: int = Field(default=1, ge=1)
    unit_price: float = Field(default=0.0, ge=0)
    line_total: float | None = Field(default=None, ge=0)


class EventIn(BaseModel):
    customer_external_id: str
    event_type: str
    occurred_at: datetime | None = None
    source: str = "api"
    payload: dict = Field(default_factory=dict)


class ConsentEventIn(BaseModel):
    customer_external_id: str
    consent_type: ConsentType
    granted: bool
    source: str = "api"
    occurred_at: datetime | None = None


class IngestResponse(BaseModel):
    entity_type: str
    total_rows: int
    accepted_rows: int
    updated_rows: int
    rejected_rows: int
    duplicate_rows: int
    errors: list[dict]
    affected_customers: int


class IngestionJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    entity_type: str
    filename: str
    status: str
    total_rows: int
    accepted_rows: int
    updated_rows: int
    rejected_rows: int
    duplicate_rows: int
    errors: list
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
class CustomerSummary(BaseModel):
    id: int
    external_id: str
    full_name: str
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    lifecycle_stage: str
    total_orders: int = 0
    completed_orders: int = 0
    lifetime_revenue: float = 0.0
    average_order_value: float = 0.0
    days_since_last_order: int | None = None
    last_order_at: datetime | None = None
    churn_score: float = 0.0
    churn_risk_band: str = "LOW"
    rfm_segment: str | None = None
    rfm_cell: str | None = None
    estimated_ltv: float = 0.0
    engagement_score: float = 0.0
    recommended_action: str = "NO_ACTION"
    marketing_consent: bool = False
    is_suppressed: bool = False


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    external_id: str
    ordered_at: datetime
    status: str
    total_amount: float
    discount_amount: float
    delivery_fee: float
    coupon_code: str | None
    channel: str | None
    items: list["OrderItemOut"] = Field(default_factory=list)


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    product_name: str
    category: str
    brand: str
    quantity: int
    unit_price: float
    line_total: float


class LifecycleHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_stage: str | None
    to_stage: str
    reason: str
    changed_at: datetime


class CommunicationEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    channel: str
    provider: str
    campaign_id: int | None
    message_id: int | None
    occurred_at: datetime
    is_simulated: bool


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int | None
    campaign_id: int | None
    channel: str
    objective: str
    subject: str
    body: str
    status: str
    provider: str
    is_test: bool
    llm_provider: str
    llm_model: str
    prompt_version: str
    generated_at: datetime | None
    validation_result: dict
    was_edited: bool
    approved_at: datetime | None
    sent_at: datetime | None
    error_message: str | None
    created_at: datetime


class CustomerDetail(BaseModel):
    profile: dict
    orders: list[OrderOut]
    lifecycle_history: list[LifecycleHistoryOut]
    communication_events: list[CommunicationEventOut]
    messages: list[MessageOut]
    campaigns: list[dict]
    segments: list[dict]
    attribution: list[dict]


class SuppressRequest(BaseModel):
    channel: str = "ALL"
    reason: str = Field(default="", max_length=255)


class ConsentUpdateRequest(BaseModel):
    marketing_consent: bool | None = None
    email_consent: bool | None = None
    sms_consent: bool | None = None
    whatsapp_consent: bool | None = None


# --------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------
class SegmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    segment_type: Literal["DYNAMIC", "MANUAL"] = "DYNAMIC"
    rule_definition: dict = Field(default_factory=dict)


class SegmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    rule_definition: dict | None = None
    status: Literal["ACTIVE", "ARCHIVED"] | None = None


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    segment_type: str
    status: str
    is_system: bool
    rule_definition: dict
    member_count: int
    last_evaluated_at: datetime | None
    created_at: datetime
    updated_at: datetime
    rule_description: str = ""


class SegmentPreviewRequest(BaseModel):
    rule_definition: dict
    limit: int = Field(default=10, ge=1, le=100)


# --------------------------------------------------------------------------
# Brand
# --------------------------------------------------------------------------
class BrandSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_name: str
    company_description: str
    brand_voice: str
    tone: str
    communication_principles: list
    preferred_vocabulary: list
    words_to_avoid: list
    emoji_usage: str
    max_email_words: int
    max_sms_characters: int
    max_whatsapp_characters: int
    email_signature: str
    whatsapp_closing: str
    sms_style: str
    customer_service_phone: str
    customer_service_email: str
    website: str
    delivery_areas: list
    delivery_promise: str
    mission_statement: str
    responsible_drinking_statement: str
    legal_disclaimer: str
    age_restriction_statement: str
    prohibited_claims: list
    allowed_promotions: list
    active_coupon_codes: list
    verified_products: list
    minimum_age: int
    updated_at: datetime


class BrandSettingsUpdate(BaseModel):
    company_name: str | None = None
    company_description: str | None = None
    brand_voice: str | None = None
    tone: str | None = None
    communication_principles: list[str] | None = None
    preferred_vocabulary: list[str] | None = None
    words_to_avoid: list[str] | None = None
    emoji_usage: str | None = None
    max_email_words: int | None = Field(default=None, ge=20, le=1000)
    max_sms_characters: int | None = Field(default=None, ge=50, le=1600)
    max_whatsapp_characters: int | None = Field(default=None, ge=50, le=4000)
    email_signature: str | None = None
    whatsapp_closing: str | None = None
    sms_style: str | None = None
    customer_service_phone: str | None = None
    customer_service_email: str | None = None
    website: str | None = None
    delivery_areas: list[str] | None = None
    delivery_promise: str | None = None
    mission_statement: str | None = None
    responsible_drinking_statement: str | None = None
    legal_disclaimer: str | None = None
    age_restriction_statement: str | None = None
    prohibited_claims: list[str] | None = None
    allowed_promotions: list[str] | None = None
    active_coupon_codes: list[str] | None = None
    verified_products: list[dict] | None = None
    minimum_age: int | None = Field(default=None, ge=18, le=25)


# --------------------------------------------------------------------------
# Message studio
# --------------------------------------------------------------------------
class GenerateMessageRequest(BaseModel):
    customer_id: int
    channel: Channel = Channel.EMAIL
    objective: str = ""
    variation: str = "default"
    campaign_id: int | None = None

    @field_validator("variation")
    @classmethod
    def known_variation(cls, value: str) -> str:
        from app.llm.prompts import TONE_INSTRUCTIONS

        if value not in TONE_INSTRUCTIONS:
            raise ValueError(
                f"Unknown variation '{value}'. Choose one of: "
                f"{', '.join(sorted(TONE_INSTRUCTIONS))}."
            )
        return value


class MessageEditRequest(BaseModel):
    subject: str | None = None
    body: str | None = None


class SendTestRequest(BaseModel):
    to: str = Field(min_length=3)
    customer_id: int | None = None


# --------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------
class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    objective: CampaignObjective = CampaignObjective.RETENTION
    channel: Channel = Channel.EMAIL
    segment_id: int | None = None
    sending_strategy: Literal["IMMEDIATE", "SCHEDULED"] = "IMMEDIATE"
    scheduled_at: datetime | None = None
    attribution_window_hours: int = Field(default=72, ge=1, le=720)
    subject: str = ""
    body: str = ""


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    objective: CampaignObjective | None = None
    channel: Channel | None = None
    segment_id: int | None = None
    sending_strategy: Literal["IMMEDIATE", "SCHEDULED"] | None = None
    scheduled_at: datetime | None = None
    attribution_window_hours: int | None = Field(default=None, ge=1, le=720)
    subject: str | None = None
    body: str | None = None


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    objective: str
    channel: str
    status: str
    segment_id: int | None
    segment_name: str | None = None
    sending_strategy: str
    scheduled_at: datetime | None
    attribution_window_hours: int
    subject: str
    body: str
    audience_snapshot: dict
    compliance_result: dict
    approved_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    total_recipients: int
    messages_sent: int
    messages_delivered: int
    messages_opened: int
    messages_clicked: int
    messages_replied: int
    messages_failed: int
    unsubscribes: int
    conversions: int
    attributed_revenue: float
    created_at: datetime
    updated_at: datetime


class ScheduleRequest(BaseModel):
    scheduled_at: datetime


class RunCampaignRequest(BaseModel):
    generate_per_customer: bool = True
    simulate_engagement: bool = True
    limit: int | None = Field(default=None, ge=1, le=10000)


# --------------------------------------------------------------------------
# Integrations
# --------------------------------------------------------------------------
class IntegrationOut(BaseModel):
    id: int
    provider: str
    channel: str
    display_name: str
    mode: str
    enabled: bool
    status: str
    status_message: str
    last_checked_at: datetime | None
    config: dict
    #: Presence flags only — secret values never leave the backend.
    credentials: dict
    required_credentials: list[str]


class IntegrationUpdate(BaseModel):
    mode: Literal["mock", "live"] | None = None
    enabled: bool | None = None
    display_name: str | None = None
    config: dict | None = None
    credentials: dict[str, str] | None = None


class IntegrationTestMessage(BaseModel):
    to: str = Field(min_length=3)
    subject: str = "GIMME Retention Engine test message"
    body: str = "This is a test message from the GIMME Retention Engine."


# --------------------------------------------------------------------------
# Compliance
# --------------------------------------------------------------------------
class ComplianceRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str
    severity: str
    blocks_send: bool
    enabled: bool
    config: dict


class ComplianceRuleUpdate(BaseModel):
    enabled: bool | None = None
    config: dict | None = None


class ContentCheckRequest(BaseModel):
    subject: str = ""
    body: str
    channel: Channel = Channel.EMAIL


# --------------------------------------------------------------------------
# Journeys
# --------------------------------------------------------------------------
class JourneyNodeIn(BaseModel):
    node_type: Literal["TRIGGER", "DELAY", "CONDITION", "ACTION"]
    subtype: str
    config: dict = Field(default_factory=dict)


class JourneyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    trigger_type: str
    trigger_config: dict = Field(default_factory=dict)
    allow_reentry: bool = False
    nodes: list[JourneyNodeIn] = Field(default_factory=list)


class JourneyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: Literal["DRAFT", "ACTIVE", "PAUSED", "ARCHIVED"] | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None
    allow_reentry: bool | None = None
    nodes: list[JourneyNodeIn] | None = None


class JourneyNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    node_type: str
    subtype: str
    config: dict


class JourneyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    status: str
    trigger_type: str
    trigger_config: dict
    allow_reentry: bool
    total_entered: int
    total_completed: int
    nodes: list[JourneyNodeOut]
    created_at: datetime
    updated_at: datetime


OrderOut.model_rebuild()
TokenResponse.model_rebuild()
