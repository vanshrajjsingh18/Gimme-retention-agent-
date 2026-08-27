"""Deterministic synthetic demo data.

Generates a customer base with realistic behavioural diversity so every
lifecycle stage, churn band and RFM segment is represented. Uses a fixed seed
so the demo is reproducible and tests can assert against it.

No real customer data is used anywhere in this module.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    CampaignObjective,
    CampaignStatus,
    Channel,
    ConsentType,
    EventType,
    OrderStatus,
)
from app.models.base import utcnow
from app.models.entities import (
    AttributionRecord,
    Automation,
    Campaign,
    CampaignRecipient,
    ChurnScore,
    CommunicationEvent,
    ConsentEvent,
    Customer,
    CustomerEvent,
    CustomerLifecycleHistory,
    CustomerMetrics,
    CustomerSegment,
    IngestionJob,
    Message,
    Order,
    OrderItem,
    Recommendation,
    RfmScore,
    Segment,
    SuppressionList,
)

logger = logging.getLogger(__name__)

FIRST_NAMES = [
    "Aroha", "Ben", "Charlotte", "Daniel", "Emma", "Finn", "Grace", "Hemi", "Isla", "Jack",
    "Kiri", "Liam", "Maia", "Nikau", "Olivia", "Patrick", "Quinn", "Ruby", "Sione", "Tama",
    "Ana", "Blake", "Chloe", "Declan", "Ella", "Felix", "Georgia", "Harry", "Ivy", "James",
    "Kate", "Lachlan", "Mia", "Noah", "Ophelia", "Pania", "Riley", "Sophie", "Te Aroha", "Vince",
    "Wiremu", "Xanthe", "Yasmin", "Zach", "Ariana", "Brooke", "Cameron", "Dylan", "Eve", "Fraser",
]

LAST_NAMES = [
    "Anderson", "Beckett", "Chen", "Davies", "Edwards", "Fitzgerald", "Gray", "Harrison",
    "Ingram", "Jones", "Kaur", "Lawson", "Māhuta", "Nguyen", "O'Brien", "Patel", "Quinn",
    "Roberts", "Singh", "Taylor", "Ualesi", "Vermeulen", "Walker", "Xu", "Young", "Zhang",
    "Ngata", "Waititi", "Cooper", "Murphy", "Sullivan", "Wilson", "Thompson", "Reid",
]

CITIES = [
    ("Auckland", "Auckland", "1010"),
    ("Auckland", "Auckland", "1021"),
    ("Wellington", "Wellington", "6011"),
    ("Christchurch", "Canterbury", "8011"),
    ("Hamilton", "Waikato", "3204"),
    ("Tauranga", "Bay of Plenty", "3110"),
    ("Dunedin", "Otago", "9016"),
    ("Napier", "Hawke's Bay", "4110"),
]

ACQUISITION_SOURCES = [
    "Organic Search", "Instagram", "Facebook", "Referral", "Google Ads",
    "Word of Mouth", "Partner Promo", "TikTok",
]


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    brand: str
    price: float


CATALOGUE: list[Product] = [
    Product("BEER-STE-12", "Steinlager Classic 12pk", "Beer", "Steinlager", 28.99),
    Product("BEER-STE-24", "Steinlager Classic 24pk", "Beer", "Steinlager", 52.99),
    Product("BEER-GAR-06", "Garage Project Hazy Daze 6pk", "Beer", "Garage Project", 24.99),
    Product("BEER-EPI-06", "Epic Pale Ale 6pk", "Beer", "Epic", 22.99),
    Product("BEER-PAN-12", "Panhead Supercharger IPA 12pk", "Beer", "Panhead", 39.99),
    Product("BEER-EMR-12", "Emerson's Pilsner 12pk", "Beer", "Emerson's", 34.99),
    Product("WINE-VIL-SB", "Villa Maria Sauvignon Blanc", "Wine", "Villa Maria", 19.99),
    Product("WINE-CLO-SB", "Cloudy Bay Sauvignon Blanc", "Wine", "Cloudy Bay", 39.99),
    Product("WINE-OYS-CH", "Oyster Bay Chardonnay", "Wine", "Oyster Bay", 21.99),
    Product("WINE-FEL-PN", "Felton Road Pinot Noir", "Wine", "Felton Road", 74.99),
    Product("WINE-MTD-PG", "Mt Difficulty Pinot Gris", "Wine", "Mt Difficulty", 27.99),
    Product("WINE-BAB-RO", "Babich Rosé", "Wine", "Babich", 17.99),
    Product("SPIR-BRO-GN", "Broken Heart Gin 700ml", "Spirits", "Broken Heart", 79.99),
    Product("SPIR-42B-VK", "42 Below Vodka 1L", "Spirits", "42 Below", 54.99),
    Product("SPIR-SCA-WH", "Scapegrace Whisky 700ml", "Spirits", "Scapegrace", 89.99),
    Product("SPIR-BAC-RM", "Bacardi Carta Blanca 1L", "Spirits", "Bacardi", 49.99),
    Product("RTD-CDC-12", "Codys 7% 12pk", "RTDs", "Codys", 32.99),
    Product("RTD-LNS-10", "Long White Vodka 10pk", "RTDs", "Long White", 29.99),
    Product("RTD-PAL-06", "Pals Vodka Soda 6pk", "RTDs", "Pals", 21.99),
    Product("MIX-SCH-06", "Schweppes Tonic 6pk", "Mixers", "Schweppes", 9.99),
    Product("MIX-EAS-04", "East Imperial Ginger Beer 4pk", "Mixers", "East Imperial", 12.99),
    Product("NA-HEI-12", "Heineken 0.0 12pk", "Non-Alcoholic", "Heineken", 24.99),
    Product("NA-GIE-06", "Giesen 0% Sauvignon Blanc", "Non-Alcoholic", "Giesen", 18.99),
]

CATEGORY_AFFINITIES = ["Beer", "Wine", "Spirits", "RTDs", "Mixed"]


@dataclass
class Persona:
    """A behavioural archetype and the share of the base it represents."""

    key: str
    weight: float
    orders: tuple[int, int]  # (min, max) completed orders
    interval_days: tuple[int, int]  # typical days between orders
    last_order_days_ago: tuple[int, int]
    basket: tuple[float, float]  # multiplier on catalogue price totals
    discount_rate: float  # probability an order carries a discount
    consent_rate: float
    engagement: float  # 0..1, drives simulated open/click behaviour
    tenure_days: tuple[int, int] | None = None
    declining: bool = False


PERSONAS: list[Persona] = [
    Persona("brand_new_no_order", 0.05, (0, 0), (0, 0), (0, 0), (0, 0), 0.0, 0.85, 0.5,
            tenure_days=(1, 28)),
    Persona("new_one_order", 0.08, (1, 1), (0, 0), (2, 25), (0.8, 1.3), 0.35, 0.85, 0.55,
            tenure_days=(5, 29)),
    Persona("one_time_lapsed", 0.09, (1, 1), (0, 0), (150, 400), (0.7, 1.2), 0.55, 0.7, 0.2,
            tenure_days=(200, 500)),
    Persona("activating", 0.10, (2, 3), (20, 40), (5, 30), (0.8, 1.4), 0.3, 0.85, 0.6,
            tenure_days=(45, 130)),
    Persona("regular", 0.18, (4, 9), (21, 38), (3, 28), (0.8, 1.3), 0.2, 0.9, 0.6,
            tenure_days=(200, 400)),
    Persona("high_value", 0.09, (6, 12), (18, 32), (2, 25), (1.4, 2.0), 0.15, 0.92, 0.65,
            tenure_days=(240, 400)),
    Persona("vip", 0.06, (11, 22), (12, 24), (1, 18), (1.8, 2.6), 0.1, 0.95, 0.75,
            tenure_days=(300, 420)),
    Persona("discount_dependent", 0.07, (4, 9), (25, 45), (10, 40), (0.7, 1.1), 0.95, 0.85, 0.45,
            tenure_days=(220, 400)),
    Persona("declining", 0.08, (5, 10), (20, 35), (35, 70), (0.7, 1.4), 0.4, 0.85, 0.35,
            tenure_days=(260, 420), declining=True),
    Persona("at_risk", 0.07, (4, 8), (18, 30), (45, 85), (0.9, 1.6), 0.25, 0.85, 0.3,
            tenure_days=(250, 400)),
    Persona("dormant", 0.06, (3, 7), (25, 40), (130, 220), (0.8, 1.4), 0.3, 0.75, 0.15,
            tenure_days=(320, 420)),
    Persona("churned", 0.05, (3, 8), (22, 38), (260, 400), (0.8, 1.5), 0.35, 0.6, 0.1,
            tenure_days=(400, 430)),
    Persona("reactivated", 0.02, (4, 8), (20, 35), (2, 20), (0.9, 1.5), 0.3, 0.9, 0.55,
            tenure_days=(360, 425)),
]


def _weighted_personas(rng: random.Random, count: int) -> list[Persona]:
    """Assign personas by weight, guaranteeing at least one of each."""
    assigned = list(PERSONAS)  # one guaranteed of each
    remaining = max(count - len(assigned), 0)
    weights = [p.weight for p in PERSONAS]
    assigned += rng.choices(PERSONAS, weights=weights, k=remaining)
    rng.shuffle(assigned)
    return assigned[:count]


def _pick_products(rng: random.Random, affinity: str, n: int) -> list[Product]:
    if affinity == "Mixed":
        pool = CATALOGUE
    else:
        pool = [p for p in CATALOGUE if p.category == affinity] or CATALOGUE
    # Occasionally reach outside the usual category, as real customers do.
    picks = []
    for _ in range(n):
        if rng.random() < 0.15:
            picks.append(rng.choice(CATALOGUE))
        else:
            picks.append(rng.choice(pool))
    return picks


def clear_demo_data(db: Session) -> None:
    """Remove all transactional and customer data, keeping configuration.

    Brand settings, compliance rules, integrations, API keys and users are
    preserved so a reseed does not undo an operator's configuration.
    """
    for model in (
        AttributionRecord,
        CommunicationEvent,
        Message,
        CampaignRecipient,
        Campaign,
        CustomerSegment,
        CustomerEvent,
        ConsentEvent,
        SuppressionList,
        CustomerLifecycleHistory,
        Recommendation,
        ChurnScore,
        RfmScore,
        CustomerMetrics,
        OrderItem,
        Order,
        Customer,
        IngestionJob,
    ):
        db.execute(delete(model))
    # Dynamic segments survive; their membership was cleared above.
    for segment in db.execute(select(Segment)).scalars().all():
        segment.member_count = 0
        segment.last_evaluated_at = None
    db.commit()


def generate_customers(
    db: Session,
    *,
    count: int = 1000,
    seed: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Create synthetic customers, orders and order items."""
    rng = random.Random(seed if seed is not None else settings.MOCK_SEED)
    now = now or utcnow()
    personas = _weighted_personas(rng, count)

    customers_created = 0
    orders_created = 0
    items_created = 0
    consent_events = 0

    for index, persona in enumerate(personas, start=1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        city, region, postcode = rng.choice(CITIES)
        external_id = f"CUST-{index:05d}"
        affinity = rng.choice(CATEGORY_AFFINITIES)

        tenure = rng.randint(*persona.tenure_days) if persona.tenure_days else rng.randint(30, 400)
        signup = now - timedelta(days=tenure)

        # Age: everyone is over 18; a small share is unverified so the age gate
        # has something to exclude in the demo.
        age = rng.randint(19, 68)
        dob = date(now.year - age, rng.randint(1, 12), rng.randint(1, 28))
        age_verified = rng.random() < 0.93

        has_consent = rng.random() < persona.consent_rate
        email_consent = has_consent and rng.random() < 0.97
        sms_consent = has_consent and rng.random() < 0.45
        whatsapp_consent = has_consent and rng.random() < 0.30
        preferred = Channel.EMAIL
        if sms_consent and rng.random() < 0.2:
            preferred = Channel.SMS
        elif whatsapp_consent and rng.random() < 0.25:
            preferred = Channel.WHATSAPP

        customer = Customer(
            external_id=external_id,
            email=f"{first.lower().replace(' ', '')}.{last.lower().replace(chr(39), '')}"
            f"{index}@example.test",
            phone=f"+6421{rng.randint(1000000, 9999999)}",
            first_name=first,
            last_name=last,
            date_of_birth=dob,
            age_verified=age_verified,
            city=city,
            region=region,
            postcode=postcode,
            country="New Zealand",
            signup_date=signup,
            acquisition_source=rng.choice(ACQUISITION_SOURCES),
            preferred_channel=preferred.value,
            marketing_consent=has_consent,
            email_consent=email_consent,
            sms_consent=sms_consent,
            whatsapp_consent=whatsapp_consent,
            notes=f"Synthetic demo customer (persona: {persona.key}).",
        )
        db.add(customer)
        db.flush()
        customers_created += 1

        db.add(
            CustomerEvent(
                customer_id=customer.id,
                event_type=EventType.CUSTOMER_CREATED.value,
                occurred_at=signup,
                source="seed",
                payload={"persona": persona.key},
                idempotency_key=f"seed-created-{customer.id}",
            )
        )
        for consent_type, granted in (
            (ConsentType.MARKETING, has_consent),
            (ConsentType.EMAIL, email_consent),
            (ConsentType.SMS, sms_consent),
            (ConsentType.WHATSAPP, whatsapp_consent),
        ):
            db.add(
                ConsentEvent(
                    customer_id=customer.id,
                    consent_type=consent_type.value,
                    granted=granted,
                    source="signup",
                    occurred_at=signup,
                )
            )
            consent_events += 1

        # A small share of customers are suppressed by an operator.
        if rng.random() < 0.03:
            customer.is_suppressed = True
            db.add(
                SuppressionList(
                    customer_id=customer.id,
                    channel="ALL",
                    reason="Customer requested no further marketing.",
                    created_by="seed",
                    active=True,
                )
            )

        order_count = rng.randint(*persona.orders)
        if order_count == 0:
            continue

        last_gap = rng.randint(*persona.last_order_days_ago)
        interval = (
            rng.randint(*persona.interval_days) if persona.interval_days[1] > 0 else 30
        )

        # Build order dates backwards from the most recent order.
        order_dates: list[datetime] = []
        cursor = now - timedelta(days=last_gap)
        for position in range(order_count):
            order_dates.append(cursor)
            step = interval
            if persona.declining:
                # A declining customer's earlier orders were closer together.
                step = max(int(interval * (0.55 + 0.05 * position)), 5)
            jitter = rng.randint(-4, 6)
            cursor = cursor - timedelta(days=max(step + jitter, 2))
        order_dates.reverse()

        if persona.key == "reactivated":
            # Force a long gap before the most recent order.
            gap = rng.randint(120, 220)
            order_dates = [d - timedelta(days=gap) for d in order_dates[:-1]] + [order_dates[-1]]

        for position, ordered_at in enumerate(order_dates):
            if ordered_at < signup:
                ordered_at = signup + timedelta(days=rng.randint(0, 3))
            if ordered_at > now:
                continue

            # Give orders a plausible time of day, skewed to evenings.
            hour = rng.choices(
                [11, 14, 16, 17, 18, 19, 20, 21], weights=[3, 4, 6, 10, 14, 16, 12, 8]
            )[0]
            ordered_at = ordered_at.replace(hour=hour, minute=rng.randint(0, 59), second=0)

            # Baskets are sized to land the population AOV in the NZ drinks
            # delivery range (roughly $60-$95), so the value tiers stay meaningful.
            basket_multiplier = rng.uniform(*persona.basket)
            item_count = max(1, min(5, int(round(basket_multiplier * rng.choice([1, 1, 2])))))
            products = _pick_products(rng, affinity, item_count)

            status = OrderStatus.COMPLETED.value
            if rng.random() < 0.035:
                status = (
                    OrderStatus.CANCELLED.value if rng.random() < 0.7 else OrderStatus.REFUNDED.value
                )

            subtotal = 0.0
            line_rows = []
            for item_index, product in enumerate(products, start=1):
                quantity = rng.choices([1, 1, 1, 1, 2, 2], k=1)[0]
                line_total = round(product.price * quantity, 2)
                subtotal += line_total
                line_rows.append((item_index, product, quantity, line_total))

            discount = 0.0
            coupon = None
            if rng.random() < persona.discount_rate:
                discount = round(subtotal * rng.choice([0.10, 0.15, 0.20]), 2)
                coupon = rng.choice(["GIMME10", "WELCOME15", "RESTOCK20"])
            delivery_fee = 0.0 if subtotal >= 80 else 7.99
            total = round(max(subtotal - discount + delivery_fee, 0.0), 2)

            order = Order(
                external_id=f"ORD-{index:05d}-{position + 1:02d}",
                customer_id=customer.id,
                ordered_at=ordered_at,
                status=status,
                total_amount=total,
                discount_amount=discount,
                delivery_fee=delivery_fee,
                currency="NZD",
                channel=rng.choice(["web", "web", "web", "ios", "android"]),
                coupon_code=coupon,
                delivery_city=city,
            )
            db.add(order)
            db.flush()
            orders_created += 1

            for item_index, product, quantity, line_total in line_rows:
                db.add(
                    OrderItem(
                        external_id=f"ITEM-{index:05d}-{position + 1:02d}-{item_index:02d}",
                        order_id=order.id,
                        sku=product.sku,
                        product_name=product.name,
                        category=product.category,
                        brand=product.brand,
                        quantity=quantity,
                        unit_price=product.price,
                        line_total=line_total,
                    )
                )
                items_created += 1

            db.add(
                CustomerEvent(
                    customer_id=customer.id,
                    event_type=(
                        EventType.ORDER_COMPLETED.value
                        if status == OrderStatus.COMPLETED.value
                        else EventType.ORDER_CANCELLED.value
                    ),
                    occurred_at=ordered_at,
                    source="seed",
                    payload={"order_external_id": order.external_id, "total_amount": total},
                    idempotency_key=f"seed-order-{order.id}",
                )
            )

        if customers_created % 100 == 0:
            db.commit()

    db.commit()
    return {
        "customers": customers_created,
        "orders": orders_created,
        "order_items": items_created,
        "consent_events": consent_events,
    }


def summary(db: Session) -> dict:
    """Row counts for the seeded dataset."""
    def count(model) -> int:
        return db.execute(select(func.count()).select_from(model)).scalar_one()

    # Campaigns backing an automation are plumbing rather than campaigns
    # anybody made, so they are counted separately — a bare total would say
    # nine when six were created.
    backing = db.execute(
        select(func.count(func.distinct(Campaign.id))).where(
            Campaign.id.in_(
                select(Automation.campaign_id).where(Automation.campaign_id.is_not(None))
            )
        )
    ).scalar_one()

    return {
        "customers": count(Customer),
        "orders": count(Order),
        "order_items": count(OrderItem),
        "customer_events": count(CustomerEvent),
        "consent_events": count(ConsentEvent),
        "campaigns": count(Campaign) - backing,
        "automations": count(Automation),
        "messages": count(Message),
        "communication_events": count(CommunicationEvent),
        "attribution_records": count(AttributionRecord),
        "segments": count(Segment),
    }
