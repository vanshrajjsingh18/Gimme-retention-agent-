"""All ORM entities for the GIMME Retention Engine."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.enums import (
    CampaignObjective,
    CampaignStatus,
    Channel,
    ChurnRiskBand,
    IngestionStatus,
    JourneyExecutionStatus,
    JourneyStatus,
    LifecycleStage,
    MessageStatus,
    NextBestAction,
    OrderStatus,
    RecipientStatus,
    SegmentStatus,
    SegmentType,
    UserRole,
)
from app.models.base import TimestampMixin, utcnow


# --------------------------------------------------------------------------
# Identity & access
# --------------------------------------------------------------------------
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=UserRole.ADMIN.value)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False, default="ingest")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApiRequestLog(Base):
    __tablename__ = "api_request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True, index=True
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )


# --------------------------------------------------------------------------
# Customer domain
# --------------------------------------------------------------------------
class Customer(Base, TimestampMixin):
    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_email_lower", "email"),
        Index("ix_customers_lifecycle", "lifecycle_stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    first_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    age_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postcode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str] = mapped_column(String(60), nullable=False, default="New Zealand")
    signup_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    acquisition_source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    preferred_channel: Mapped[str] = mapped_column(
        String(20), nullable=False, default=Channel.EMAIL.value
    )

    # Consent (current state; consent_events holds the audit trail)
    marketing_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sms_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    whatsapp_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    lifecycle_stage: Mapped[str] = mapped_column(
        String(30), nullable=False, default=LifecycleStage.NEW.value
    )
    lifecycle_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    orders: Mapped[list["Order"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    metrics: Mapped["CustomerMetrics | None"] = relationship(
        back_populates="customer", cascade="all, delete-orphan", uselist=False
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_customer_date", "customer_id", "ordered_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OrderStatus.COMPLETED.value
    )
    total_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    delivery_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="NZD")
    channel: Mapped[str | None] = mapped_column(String(40), nullable=True)
    coupon_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    delivery_city: Mapped[str | None] = mapped_column(String(120), nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base, TimestampMixin):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    brand: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    line_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    order: Mapped["Order"] = relationship(back_populates="items")


class CustomerMetrics(Base, TimestampMixin):
    """Derived behavioural metrics; recomputed by the metrics service."""

    __tablename__ = "customer_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    total_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lifetime_revenue: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    average_order_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_order_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_order_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    days_since_last_order: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    days_since_first_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    average_purchase_interval_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_purchase_interval_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    purchase_frequency_per_month: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_dependency: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    orders_last_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_last_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_prev_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_last_365d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue_last_90d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    revenue_prev_90d: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    spend_trend: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    frequency_trend: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    preferred_categories: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    preferred_brands: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    top_products: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    typical_order_weekday: Mapped[str | None] = mapped_column(String(12), nullable=True)
    typical_order_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_ltv: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    engagement_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    messages_received_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_opened_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_sent_90d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    customer: Mapped["Customer"] = relationship(back_populates="metrics")


class CustomerLifecycleHistory(Base):
    __tablename__ = "customer_lifecycle_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_stage: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )


class RfmScore(Base, TimestampMixin):
    __tablename__ = "rfm_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    recency_score: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    frequency_score: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    monetary_score: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rfm_cell: Mapped[str] = mapped_column(String(8), nullable=False, default="111")
    rfm_total: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    rfm_segment: Mapped[str] = mapped_column(String(40), nullable=False, default="Others", index=True)
    recency_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frequency_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monetary_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ChurnScore(Base, TimestampMixin):
    __tablename__ = "churn_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    risk_band: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ChurnRiskBand.LOW.value, index=True
    )
    factors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    revenue_at_risk: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    previous_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Recommendation(Base, TimestampMixin):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(
        String(40), nullable=False, default=NextBestAction.NO_ACTION.value, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recommended_channel: Mapped[str] = mapped_column(
        String(20), nullable=False, default=Channel.EMAIL.value
    )
    suggested_products: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class CustomerEvent(Base):
    """Normalized behavioural events (orders, signup, custom)."""

    __tablename__ = "customer_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_customer_events_idempotency"),
        Index("ix_customer_events_customer_type", "customer_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="system")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ConsentEvent(Base):
    __tablename__ = "consent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consent_type: Mapped[str] = mapped_column(String(20), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(60), nullable=False, default="import")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class SuppressionList(Base, TimestampMixin):
    __tablename__ = "suppression_lists"
    __table_args__ = (
        UniqueConstraint("customer_id", "channel", name="uq_suppression_customer_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="ALL")
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# --------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------
class Segment(Base, TimestampMixin):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    segment_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SegmentType.DYNAMIC.value
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SegmentStatus.ACTIVE.value, index=True
    )
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rule_definition: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    members: Mapped[list["CustomerSegment"]] = relationship(
        back_populates="segment", cascade="all, delete-orphan"
    )


class CustomerSegment(Base):
    __tablename__ = "customer_segments"
    __table_args__ = (
        UniqueConstraint("customer_id", "segment_id", name="uq_customer_segment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    segment_id: Mapped[int] = mapped_column(
        ForeignKey("segments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="dynamic")

    segment: Mapped["Segment"] = relationship(back_populates="members")


# --------------------------------------------------------------------------
# Brand, compliance, integrations
# --------------------------------------------------------------------------
class BrandSettings(Base, TimestampMixin):
    """Single-row table (id=1) holding brand + grounding configuration."""

    __tablename__ = "brand_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(160), nullable=False, default="GIMME")
    company_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    brand_voice: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tone: Mapped[str] = mapped_column(String(120), nullable=False, default="Friendly and direct")
    communication_principles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    preferred_vocabulary: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    words_to_avoid: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    emoji_usage: Mapped[str] = mapped_column(String(40), nullable=False, default="sparing")
    max_email_words: Mapped[int] = mapped_column(Integer, nullable=False, default=140)
    max_sms_characters: Mapped[int] = mapped_column(Integer, nullable=False, default=320)
    max_whatsapp_characters: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    email_signature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    whatsapp_closing: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sms_style: Mapped[str] = mapped_column(Text, nullable=False, default="")
    customer_service_phone: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    customer_service_email: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    website: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    delivery_areas: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    delivery_promise: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mission_statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    responsible_drinking_statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    legal_disclaimer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    age_restriction_statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prohibited_claims: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allowed_promotions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    active_coupon_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    verified_products: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    minimum_age: Mapped[int] = mapped_column(Integer, nullable=False, default=18)


class ComplianceRule(Base, TimestampMixin):
    __tablename__ = "compliance_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="CRITICAL")
    blocks_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Integration(Base, TimestampMixin):
    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="mock")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Credentials never leave the backend; API responses expose masked values only.
    credentials: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="NOT_CONFIGURED")
    status_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------
# Messaging & campaigns
# --------------------------------------------------------------------------
class MessageTemplate(Base, TimestampMixin):
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    objective: Mapped[str] = mapped_column(String(40), nullable=False, default="RETENTION")
    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Campaign(Base, TimestampMixin):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    objective: Mapped[str] = mapped_column(
        String(40), nullable=False, default=CampaignObjective.RETENTION.value
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default=Channel.EMAIL.value)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=CampaignStatus.DRAFT.value, index=True
    )
    segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("segments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sending_strategy: Mapped[str] = mapped_column(String(30), nullable=False, default="IMMEDIATE")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    attribution_window_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=72)

    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    audience_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    compliance_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Rolling metrics maintained by the send + event pipeline
    total_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_delivered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_opened: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_clicked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_replied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unsubscribes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conversions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attributed_revenue: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    segment: Mapped["Segment | None"] = relationship()
    recipients: Mapped[list["CampaignRecipient"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    variants: Mapped[list["CampaignVariant"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignVariant(Base, TimestampMixin):
    __tablename__ = "campaign_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(16), nullable=False, default="A")
    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_opened: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_clicked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conversions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attributed_revenue: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    campaign: Mapped["Campaign"] = relationship(back_populates="variants")


class CampaignRecipient(Base, TimestampMixin):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "customer_id", name="uq_campaign_recipient"),
        Index("ix_campaign_recipients_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_variants.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=RecipientStatus.ELIGIBLE.value, index=True
    )
    exclusion_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    campaign: Mapped["Campaign"] = relationship(back_populates="recipients")


class Message(Base, TimestampMixin):
    """A generated / sent message. Also used for Message Studio drafts."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=True, index=True
    )
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_recipients.id", ondelete="CASCADE"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default=Channel.EMAIL.value)
    objective: Mapped[str] = mapped_column(String(40), nullable=False, default="RETENTION")
    subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    original_subject: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    original_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=MessageStatus.DRAFT.value, index=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="mock")
    provider_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # LLM provenance
    llm_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="mock")
    llm_model: Mapped[str] = mapped_column(String(80), nullable=False, default="mock-1")
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False, default="v1")
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    generation_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    validation_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    was_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class CommunicationEvent(Base):
    """Provider-normalized delivery / engagement events."""

    __tablename__ = "communication_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_communication_events_idempotency"),
        Index("ix_comm_events_customer_time", "customer_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=True, index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default=Channel.EMAIL.value)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="mock")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AttributionRecord(Base):
    __tablename__ = "attribution_records"
    __table_args__ = (UniqueConstraint("order_id", name="uq_attribution_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    touch_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("communication_events.id", ondelete="SET NULL"), nullable=True
    )
    model: Mapped[str] = mapped_column(String(30), nullable=False, default="LAST_TOUCH")
    window_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=72)
    hours_since_touch: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    revenue: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_reactivation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )


# --------------------------------------------------------------------------
# Journeys
# --------------------------------------------------------------------------
class Journey(Base, TimestampMixin):
    __tablename__ = "journeys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JourneyStatus.DRAFT.value, index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(60), nullable=False)
    trigger_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    allow_reentry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_entered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    nodes: Mapped[list["JourneyNode"]] = relationship(
        back_populates="journey", cascade="all, delete-orphan", order_by="JourneyNode.position"
    )


class JourneyNode(Base, TimestampMixin):
    __tablename__ = "journey_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    journey_id: Mapped[int] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    node_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subtype: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    journey: Mapped["Journey"] = relationship(back_populates="nodes")


class JourneyExecution(Base):
    __tablename__ = "journey_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    journey_id: Mapped[int] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[int | None] = mapped_column(
        ForeignKey("journey_nodes.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(30), nullable=False, default="OK")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )


class JourneyCustomerState(Base, TimestampMixin):
    __tablename__ = "journey_customer_states"
    __table_args__ = (
        UniqueConstraint("journey_id", "customer_id", name="uq_journey_customer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    journey_id: Mapped[int] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=JourneyExecutionStatus.ACTIVE.value, index=True
    )
    current_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resume_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    entered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------
class IngestionJob(Base, TimestampMixin):
    __tablename__ = "ingestion_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="csv_upload")
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IngestionStatus.PENDING.value, index=True
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(160), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO", index=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="app")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
