"""CSV and API data ingestion.

Every ingest path funnels through the same row-level validators so a CSV
upload and an API POST accept exactly the same data and produce exactly the
same errors. Rows are validated independently: one bad row never rejects the
file, and every rejection is reported with its row number and reason.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.phone import normalize_nz_phone
from app.core.timezones import to_utc_naive
from app.core.enums import Channel, ConsentType, EventType, IngestionStatus, OrderStatus
from app.models.base import utcnow
from app.models.entities import (
    ConsentEvent,
    Customer,
    IngestionJob,
    Order,
    OrderItem,
)
from app.services.events import make_idempotency_key, record_customer_event

logger = logging.getLogger(__name__)

MAX_ERRORS_STORED = 500

# Structural check only. Deliberately permissive about the TLD so reserved
# domains (.test) and unusual corporate domains are both accepted.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%m/%d/%Y",
)

TRUE_VALUES = {"true", "t", "yes", "y", "1", "granted", "opted_in", "subscribed"}
FALSE_VALUES = {"false", "f", "no", "n", "0", "denied", "opted_out", "unsubscribed", ""}

#: The consent flags. Absent from a customer file, every row imports as
#: contactable on nothing — which is a silent way to load a customer list that
#: no campaign can ever send to, so their absence is reported rather than
#: defaulted past.
CONSENT_COLUMNS = ("marketing_consent", "email_consent", "sms_consent", "whatsapp_consent")

#: Customer columns that are true/false rather than text, and so cannot use the
#: empty string to mean "not supplied".
FLAG_COLUMNS = ("age_verified", *CONSENT_COLUMNS)

#: The consent event each flag stands for, for writing down where an assumed
#: consent came from.
CONSENT_TYPE_BY_COLUMN = {
    "marketing_consent": ConsentType.MARKETING.value,
    "email_consent": ConsentType.EMAIL.value,
    "sms_consent": ConsentType.SMS.value,
    "whatsapp_consent": ConsentType.WHATSAPP.value,
}

#: external_id of the worked example shipped in the downloadable template.
#: Skipped on import so a template filled in beneath the example does not turn
#: the instructions into a customer.
TEMPLATE_EXAMPLE_ID = "EXAMPLE-ROW-DELETE-ME"


class RowError(ValueError):
    """A per-row validation failure."""


# --------------------------------------------------------------------------
# Coercion helpers
# --------------------------------------------------------------------------
def parse_datetime(value: Any, field: str, *, required: bool = False) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RowError(f"'{field}' is required.")
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip().replace("Z", "")
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # A bare "2026-09-16 19:40:00" says nothing about which clock wrote
        # it. The column it lands in holds naive UTC, so reading a local
        # timestamp as UTC puts every order half a day out — and everything
        # downstream is built on the hour: the learned routine, Smart
        # Reorder's send time, #preferred_order_time#. Which it is depends on
        # the export, so it is configuration rather than a guess.
        if settings.IMPORT_TIMESTAMPS_ARE_LOCAL:
            return to_utc_naive(parsed)
        return parsed
    raise RowError(
        f"'{field}' value '{value}' is not a recognised date. "
        "Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."
    )


def parse_date(value: Any, field: str, *, required: bool = False) -> date | None:
    dt = parse_datetime(value, field, required=required)
    return dt.date() if dt else None


def parse_float(value: Any, field: str, *, default: float = 0.0, required: bool = False) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RowError(f"'{field}' is required.")
        return default
    try:
        parsed = float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        raise RowError(f"'{field}' value '{value}' is not a number.") from None
    if parsed < 0:
        raise RowError(f"'{field}' cannot be negative (got {parsed}).")
    return parsed


def parse_int(value: Any, field: str, *, default: int = 0, required: bool = False) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RowError(f"'{field}' is required.")
        return default
    try:
        return int(float(str(value).strip()))
    except ValueError:
        raise RowError(f"'{field}' value '{value}' is not a whole number.") from None


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return default


def parse_optional_bool(value: Any) -> bool | None:
    """A flag, or None when the file did not say.

    The distinction matters on update. ``parse_bool`` cannot express "no
    answer" — a missing column and an explicit "false" both come back False —
    and an update that writes that False over a customer's stored consent
    revokes it without anybody asking for it. Absent and blank mean "leave
    whatever is on record"; only a value actually present in the file changes
    one.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def require(row: dict, field: str) -> str:
    value = row.get(field)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise RowError(f"'{field}' is required.")
    return text


def clean(row: dict, field: str, default: str = "") -> str:
    value = row.get(field)
    return str(value).strip() if value is not None and str(value).strip() else default


def optional(row: dict, field: str) -> str | None:
    value = clean(row, field)
    return value or None


_SLUG = re.compile(r"[^a-z0-9]+")


def product_slug(product_name: str) -> str:
    """A stable id for a product, for files that carry names and no SKU.

    Coarse on purpose: two spellings of one drink stay apart, and that is a
    smaller problem than the alternative. An id that varies per line makes the
    same product look like a new one on every order, so nothing is ever bought
    twice and nothing can be recommended.
    """
    return _SLUG.sub("-", product_name.strip().lower()).strip("-")[:64] or "unknown"


# --------------------------------------------------------------------------
# Result container
# --------------------------------------------------------------------------
class IngestResult:
    def __init__(self, entity_type: str) -> None:
        self.entity_type = entity_type
        self.total_rows = 0
        self.accepted = 0
        self.updated = 0
        self.rejected = 0
        self.duplicates = 0
        self.errors: list[dict] = []
        #: Rows that were accepted but not stored exactly as supplied. Silently
        #: changing somebody's data is worse than rejecting it, so anything the
        #: import alters or drops is reported rather than absorbed.
        self.warnings: list[dict] = []
        #: Warnings about the file as a whole rather than any one row. A
        #: missing consent column is true of all 993 rows at once; reporting it
        #: per row would bury it in 993 copies of itself.
        self.file_warnings: list[str] = []
        #: Per-entity totals when one file loads several of them. A combined
        #: file has one row count and three sets of results, and collapsing
        #: them into a single "accepted" hides which part of the row landed.
        self.sections: list[dict] = []
        #: How many values were reshaped into a canonical form (phone numbers).
        self.normalized = 0
        self.affected_customer_ids: set[int] = set()
        self.created_order_ids: list[int] = []

    def reject(self, row_number: int, message: str, row: dict | None = None) -> None:
        self.rejected += 1
        if len(self.errors) < MAX_ERRORS_STORED:
            self.errors.append(
                {
                    "row": row_number,
                    "error": message,
                    "data": _safe_row_preview(row or {}),
                }
            )

    def warn(self, row_number: int, message: str, row: dict | None = None) -> None:
        if len(self.warnings) < MAX_ERRORS_STORED:
            self.warnings.append(
                {
                    "row": row_number,
                    "warning": message,
                    "data": _safe_row_preview(row or {}),
                }
            )

    def as_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "total_rows": self.total_rows,
            "accepted_rows": self.accepted,
            "updated_rows": self.updated,
            "rejected_rows": self.rejected,
            "duplicate_rows": self.duplicates,
            "errors": self.errors,
            "warnings": self.warnings,
            "file_warnings": self.file_warnings,
            "sections": self.sections,
            "normalized_values": self.normalized,
            "affected_customers": len(self.affected_customer_ids),
        }


def _safe_row_preview(row: dict) -> dict:
    """Trim a rejected row for the error report without dumping full PII."""
    preview = {}
    for key in ("external_id", "customer_external_id", "order_external_id", "sku"):
        if row.get(key):
            preview[key] = str(row[key])[:60]
    return preview


# --------------------------------------------------------------------------
# Entity ingestion
# --------------------------------------------------------------------------
def _record_assumed_consent(db: Session, customer: Customer, values: dict) -> None:
    """Write down that a consent came from the import, not from the customer.

    The flag on its own cannot tell an opt-in apart from an assumption, and an
    assumption that leaves no trace is indistinguishable from a claim the
    customer made. If a consent is ever questioned, this row is the answer —
    including when the answer is "nobody asked them".
    """
    for column, consent_type in CONSENT_TYPE_BY_COLUMN.items():
        if values.get(column) is not None:
            continue  # the file stated it; that is its own record
        db.add(
            ConsentEvent(
                customer_id=customer.id,
                consent_type=consent_type,
                granted=True,
                source="import_assumed",
                occurred_at=utcnow(),
            )
        )


def ingest_customers(db: Session, rows: list[dict], *, update_existing: bool = True) -> IngestResult:
    result = IngestResult("customers")
    result.total_rows = len(rows)
    seen_in_batch: set[str] = set()

    # Every row from a CSV carries the same keys, so the first one is the file.
    if rows and not any(column in rows[0] for column in CONSENT_COLUMNS):
        result.file_warnings.append(
            "This file has no consent columns, so every customer it creates was "
            "taken as consented on every channel, against a consent event "
            "recording that the import assumed it rather than the customer "
            "giving it. Add marketing_consent, email_consent, sms_consent and "
            "whatsapp_consent (true/false) to load what each one actually agreed to."
            if settings.IMPORT_ASSUME_CONSENT
            else "This file has no consent columns, so every customer in it will be "
            "stored as having consented to nothing and will be skipped by every "
            "campaign. Add marketing_consent, email_consent, sms_consent and "
            "whatsapp_consent (true/false) to reach these customers."
        )

    if rows and "age_verified" not in rows[0] and settings.IMPORT_ASSUME_AGE_VERIFIED:
        result.file_warnings.append(
            "This file has no age_verified column, so every customer it creates "
            "was recorded as age-verified. Nothing in the file establishes that. "
            "Alcohol marketing to an unverified customer is a licensing problem, "
            "so this is only safe if age was genuinely checked elsewhere for "
            "everyone in the file. Add an age_verified column (true/false) to "
            "load what was actually checked, or set "
            "IMPORT_ASSUME_AGE_VERIFIED=false to stop assuming it."
        )

    for index, row in enumerate(rows, start=1):
        try:
            external_id = require(row, "external_id")
            if external_id == TEMPLATE_EXAMPLE_ID:
                result.total_rows -= 1
                result.file_warnings.append(
                    "The template's example row was ignored. Delete it from the "
                    "file to stop this notice."
                )
                continue
            if external_id in seen_in_batch:
                result.duplicates += 1
                result.reject(index, f"Duplicate external_id '{external_id}' within this file.", row)
                continue
            seen_in_batch.add(external_id)

            email = optional(row, "email")
            phone = optional(row, "phone")
            # Held until the row is known to be keepable: a row rejected later
            # for an unrelated field must not report a change that never lands.
            phone_note: str | None = None
            phone_rewritten = False
            if not email and not phone:
                raise RowError("A customer needs at least an email address or a phone number.")
            if email and not EMAIL_PATTERN.match(email):
                raise RowError(f"'email' value '{email}' is not a valid email address.")
            if phone:
                # Store one canonical shape. A spreadsheet's "021 123 4567" and
                # an API's "+64211234567" are the same person, and the number
                # handed to TNZ has to be E.164 either way.
                normalized = normalize_nz_phone(phone)
                if normalized is None and not email:
                    # No usable phone and no email is nobody we can contact.
                    raise RowError(
                        f"'phone' value '{phone}' is not a mobile number an SMS "
                        "can reach, and there is no email address either."
                    )
                if normalized is None:
                    # A landline is not a reason to throw away a good email
                    # customer. Drop the number they cannot be texted on, and
                    # say so rather than losing it quietly.
                    phone_note = (
                        f"Kept this customer on email only: '{phone}' is not a mobile "
                        "number an SMS can reach, so it was not stored."
                    )
                elif normalized != phone:
                    phone_rewritten = True
                phone = normalized

            existing = db.execute(
                select(Customer).where(Customer.external_id == external_id)
            ).scalar_one_or_none()

            if existing is not None and not update_existing:
                result.duplicates += 1
                continue

            values = {
                "email": email,
                "phone": phone,
                "first_name": clean(row, "first_name"),
                "last_name": clean(row, "last_name"),
                "date_of_birth": parse_date(row.get("date_of_birth"), "date_of_birth"),
                "age_verified": parse_optional_bool(row.get("age_verified")),
                "city": optional(row, "city"),
                "region": optional(row, "region"),
                "postcode": optional(row, "postcode"),
                "country": clean(row, "country", "New Zealand"),
                "signup_date": parse_datetime(row.get("signup_date"), "signup_date"),
                "acquisition_source": optional(row, "acquisition_source"),
                "preferred_channel": _parse_channel(row.get("preferred_channel")),
                "marketing_consent": parse_optional_bool(row.get("marketing_consent")),
                "email_consent": parse_optional_bool(row.get("email_consent")),
                "sms_consent": parse_optional_bool(row.get("sms_consent")),
                "whatsapp_consent": parse_optional_bool(row.get("whatsapp_consent")),
            }

            if phone_note:
                result.warn(index, phone_note, row)
            if phone_rewritten:
                result.normalized += 1

            if existing is None:
                # Unstated only means "leave it alone" when there is something
                # already there to leave, so a new customer needs a real value.
                # Consent and age verification each take their own setting:
                # they are different promises, and a deployment willing to
                # assume one is not necessarily willing to assume the other.
                new_values = {}
                for key, value in values.items():
                    if value is None and key in CONSENT_COLUMNS:
                        value = settings.IMPORT_ASSUME_CONSENT
                    elif value is None and key == "age_verified":
                        value = settings.IMPORT_ASSUME_AGE_VERIFIED
                    elif value is None and key in FLAG_COLUMNS:
                        value = False
                    new_values[key] = value
                customer = Customer(external_id=external_id, **new_values)
                db.add(customer)
                db.flush()
                if settings.IMPORT_ASSUME_CONSENT:
                    _record_assumed_consent(db, customer, values)
                result.accepted += 1
                record_customer_event(
                    db,
                    customer_id=customer.id,
                    event_type=EventType.CUSTOMER_CREATED,
                    occurred_at=customer.signup_date or utcnow(),
                    source="ingestion",
                    payload={"external_id": external_id},
                    idempotency_key=make_idempotency_key("customer_created", customer.id),
                )
            else:
                for key, value in values.items():
                    # A blank column in an update file must not wipe existing
                    # data. For the flags that means None, since False is a
                    # real answer: a file that says "false" revokes consent,
                    # and one that never mentions it leaves consent alone.
                    if value not in (None, ""):
                        setattr(existing, key, value)
                customer = existing
                result.updated += 1
            result.affected_customer_ids.add(customer.id)
        except RowError as exc:
            result.reject(index, str(exc), row)
        except Exception as exc:  # noqa: BLE001 - row must not kill the batch
            logger.exception("Unexpected error ingesting customer row %s", index)
            result.reject(index, f"Unexpected error: {exc}", row)

    db.commit()
    return result


def _parse_channel(value: Any) -> str:
    try:
        return Channel(str(value).strip().upper()).value
    except (ValueError, AttributeError):
        return Channel.EMAIL.value


def ingest_orders(db: Session, rows: list[dict], *, update_existing: bool = True) -> IngestResult:
    result = IngestResult("orders")
    result.total_rows = len(rows)
    seen_in_batch: set[str] = set()

    for index, row in enumerate(rows, start=1):
        try:
            external_id = require(row, "external_id")
            if external_id in seen_in_batch:
                result.duplicates += 1
                result.reject(index, f"Duplicate external_id '{external_id}' within this file.", row)
                continue
            seen_in_batch.add(external_id)

            customer_external_id = require(row, "customer_external_id")
            customer = db.execute(
                select(Customer).where(Customer.external_id == customer_external_id)
            ).scalar_one_or_none()
            if customer is None:
                raise RowError(
                    f"No customer found with external_id '{customer_external_id}'. "
                    "Import customers before orders."
                )

            status = clean(row, "status", OrderStatus.COMPLETED.value).upper()
            if status not in {s.value for s in OrderStatus}:
                raise RowError(
                    f"'status' value '{status}' is not one of "
                    f"{', '.join(s.value for s in OrderStatus)}."
                )

            values = {
                "customer_id": customer.id,
                "ordered_at": parse_datetime(row.get("ordered_at"), "ordered_at", required=True),
                "status": status,
                "total_amount": parse_float(row.get("total_amount"), "total_amount", required=True),
                "discount_amount": parse_float(row.get("discount_amount"), "discount_amount"),
                "delivery_fee": parse_float(row.get("delivery_fee"), "delivery_fee"),
                "currency": clean(row, "currency", "NZD"),
                "channel": optional(row, "channel"),
                "coupon_code": optional(row, "coupon_code"),
                "delivery_city": optional(row, "delivery_city"),
            }

            existing = db.execute(
                select(Order).where(Order.external_id == external_id)
            ).scalar_one_or_none()

            if existing is not None:
                if not update_existing:
                    result.duplicates += 1
                    continue
                for key, value in values.items():
                    setattr(existing, key, value)
                order = existing
                result.updated += 1
            else:
                order = Order(external_id=external_id, **values)
                db.add(order)
                db.flush()
                result.accepted += 1
                result.created_order_ids.append(order.id)

            result.affected_customer_ids.add(customer.id)
        except RowError as exc:
            result.reject(index, str(exc), row)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error ingesting order row %s", index)
            result.reject(index, f"Unexpected error: {exc}", row)

    db.commit()
    return result


def ingest_order_items(db: Session, rows: list[dict], *, update_existing: bool = True) -> IngestResult:
    result = IngestResult("order_items")
    result.total_rows = len(rows)
    seen_in_batch: set[str] = set()

    for index, row in enumerate(rows, start=1):
        try:
            external_id = require(row, "external_id")
            if external_id in seen_in_batch:
                result.duplicates += 1
                result.reject(index, f"Duplicate external_id '{external_id}' within this file.", row)
                continue
            seen_in_batch.add(external_id)

            order_external_id = require(row, "order_external_id")
            order = db.execute(
                select(Order).where(Order.external_id == order_external_id)
            ).scalar_one_or_none()
            if order is None:
                raise RowError(
                    f"No order found with external_id '{order_external_id}'. "
                    "Import orders before order items."
                )

            quantity = parse_int(row.get("quantity"), "quantity", default=1)
            if quantity <= 0:
                raise RowError(f"'quantity' must be at least 1 (got {quantity}).")
            unit_price = parse_float(row.get("unit_price"), "unit_price")
            line_total = parse_float(
                row.get("line_total"), "line_total", default=round(unit_price * quantity, 2)
            )

            values = {
                "order_id": order.id,
                "sku": require(row, "sku"),
                "product_name": require(row, "product_name"),
                "category": clean(row, "category"),
                "brand": clean(row, "brand"),
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": line_total,
            }

            existing = db.execute(
                select(OrderItem).where(OrderItem.external_id == external_id)
            ).scalar_one_or_none()
            if existing is not None:
                if not update_existing:
                    result.duplicates += 1
                    continue
                for key, value in values.items():
                    setattr(existing, key, value)
                result.updated += 1
            else:
                db.add(OrderItem(external_id=external_id, **values))
                result.accepted += 1

            result.affected_customer_ids.add(order.customer_id)
        except RowError as exc:
            result.reject(index, str(exc), row)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error ingesting order item row %s", index)
            result.reject(index, f"Unexpected error: {exc}", row)

    db.commit()
    return result


def ingest_events(db: Session, rows: list[dict]) -> IngestResult:
    result = IngestResult("events")
    result.total_rows = len(rows)
    valid_types = {e.value for e in EventType}

    for index, row in enumerate(rows, start=1):
        try:
            customer_external_id = require(row, "customer_external_id")
            customer = db.execute(
                select(Customer).where(Customer.external_id == customer_external_id)
            ).scalar_one_or_none()
            if customer is None:
                raise RowError(f"No customer found with external_id '{customer_external_id}'.")

            event_type = require(row, "event_type").upper()
            if event_type not in valid_types:
                raise RowError(f"'event_type' value '{event_type}' is not a supported event type.")

            occurred_at = parse_datetime(row.get("occurred_at"), "occurred_at") or utcnow()
            payload = row.get("payload")
            if isinstance(payload, str) and payload.strip():
                import json

                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {"raw": payload}
            elif not isinstance(payload, dict):
                payload = {}

            created = record_customer_event(
                db,
                customer_id=customer.id,
                event_type=event_type,
                occurred_at=occurred_at,
                source=clean(row, "source", "import"),
                payload=payload,
            )
            if created is None:
                result.duplicates += 1
            else:
                result.accepted += 1
                result.affected_customer_ids.add(customer.id)
        except RowError as exc:
            result.reject(index, str(exc), row)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error ingesting event row %s", index)
            result.reject(index, f"Unexpected error: {exc}", row)

    db.commit()
    return result


def ingest_consent_events(db: Session, rows: list[dict]) -> IngestResult:
    """Record consent changes and apply them to the customer's current state."""
    result = IngestResult("consent_events")
    result.total_rows = len(rows)
    valid_types = {c.value for c in ConsentType}
    field_by_type = {
        ConsentType.MARKETING.value: "marketing_consent",
        ConsentType.EMAIL.value: "email_consent",
        ConsentType.SMS.value: "sms_consent",
        ConsentType.WHATSAPP.value: "whatsapp_consent",
    }

    for index, row in enumerate(rows, start=1):
        try:
            customer_external_id = require(row, "customer_external_id")
            customer = db.execute(
                select(Customer).where(Customer.external_id == customer_external_id)
            ).scalar_one_or_none()
            if customer is None:
                raise RowError(f"No customer found with external_id '{customer_external_id}'.")

            consent_type = require(row, "consent_type").upper()
            if consent_type not in valid_types:
                raise RowError(
                    f"'consent_type' value '{consent_type}' is not one of "
                    f"{', '.join(sorted(valid_types))}."
                )

            raw_granted = row.get("granted")
            if raw_granted is None or str(raw_granted).strip() == "":
                raise RowError("'granted' is required.")
            granted = parse_bool(raw_granted)
            occurred_at = parse_datetime(row.get("occurred_at"), "occurred_at") or utcnow()

            db.add(
                ConsentEvent(
                    customer_id=customer.id,
                    consent_type=consent_type,
                    granted=granted,
                    source=clean(row, "source", "import"),
                    occurred_at=occurred_at,
                )
            )
            setattr(customer, field_by_type[consent_type], granted)
            # Revoking blanket marketing consent revokes every channel with it.
            if consent_type == ConsentType.MARKETING.value and not granted:
                customer.email_consent = False
                customer.sms_consent = False
                customer.whatsapp_consent = False

            result.accepted += 1
            result.affected_customer_ids.add(customer.id)
        except RowError as exc:
            result.reject(index, str(exc), row)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error ingesting consent row %s", index)
            result.reject(index, f"Unexpected error: {exc}", row)

    db.commit()
    return result


def ingest_combined(db: Session, rows: list[dict], *, update_existing: bool = True) -> IngestResult:
    """Load customers, orders and order lines from one denormalised file.

    One row is one order line, with the customer repeated on every row and the
    order repeated on every line of it — which is the shape order data actually
    arrives in, and the reason three separate uploads in a fixed order was the
    wrong thing to ask for. Getting them out of order fails confusingly: orders
    before customers rejects every row for a customer that exists three files
    away.

    Nothing here validates a field. The rows are split into the three shapes
    and handed to the existing ingestors, so a combined upload and three
    separate ones accept exactly the same data and refuse it for exactly the
    same reasons. A second copy of those rules would drift within a month.

    The one thing this does own is row numbers. The split lists are shorter
    than the file — 8,578 lines hold 993 customers — so an error reported
    against the customer list would name a row the operator cannot find. Each
    split row remembers the line it came from, and the numbers are put back
    before anybody reads them.
    """
    result = IngestResult("combined")
    result.total_rows = len(rows)

    if rows and not any(column in rows[0] for column in CONSENT_COLUMNS):
        result.file_warnings.append(
            "This file has no consent columns, so every customer in it will be "
            "stored as having consented to nothing and will be skipped by every "
            "campaign. Add marketing_consent, email_consent, sms_consent and "
            "whatsapp_consent (true/false) to reach these customers."
        )

    customers: list[dict] = []
    orders: list[dict] = []
    items: list[dict] = []
    origins: dict[str, list[int]] = {"customers": [], "orders": [], "order_items": []}
    seen_customers: set[str] = set()
    seen_orders: set[str] = set()
    skipped_example = False

    for index, row in enumerate(rows, start=1):
        customer_id = clean(row, "customer_external_id")
        if customer_id == TEMPLATE_EXAMPLE_ID:
            skipped_example = True
            result.total_rows -= 1
            continue
        order_id = clean(row, "order_external_id")

        # The customer and the order repeat down the file; only their first
        # appearance is an instruction to write anything.
        if customer_id and customer_id not in seen_customers:
            seen_customers.add(customer_id)
            customers.append({**row, "external_id": customer_id})
            origins["customers"].append(index)

        if order_id and order_id not in seen_orders:
            seen_orders.add(order_id)
            orders.append({**row, "external_id": order_id})
            origins["orders"].append(index)

        # A row may carry no line detail — an order total with nothing itemised
        # is still an order, and it should not be rejected for the gap.
        item_id = clean(row, "item_external_id")
        product = clean(row, "product_name")
        if item_id or product:
            items.append({
                **row,
                "external_id": item_id or f"{order_id}-{index}",
                # Order exports carry a product name and no SKU. Requiring one
                # here would mean inventing an id per line, and an id invented
                # per line is a different product every time somebody buys the
                # same thing.
                "sku": clean(row, "sku") or product_slug(product),
            })
            origins["order_items"].append(index)

    if skipped_example:
        result.file_warnings.append(
            "The template's example row was ignored. Delete it from the file to "
            "stop this notice."
        )

    failed_lines: set[int] = set()

    def absorb(name: str, part: IngestResult) -> set[str]:
        """Merge a sub-result, and report which external_ids did not land."""
        batch = {"customers": customers, "orders": orders, "order_items": items}[name]
        origin = origins[name]
        rejected_ids = set()
        for entry in part.errors:
            position = entry["row"] - 1
            if 0 <= position < len(batch):
                rejected_ids.add(batch[position]["external_id"])
            entry["row"] = _origin_line(entry["row"], origin)
            failed_lines.add(entry["row"])
        for entry in part.warnings:
            entry["row"] = _origin_line(entry["row"], origin)
        result.errors.extend(part.errors)
        result.warnings.extend(part.warnings)
        result.file_warnings.extend(part.file_warnings)
        result.normalized += part.normalized
        result.affected_customer_ids |= part.affected_customer_ids
        result.created_order_ids.extend(part.created_order_ids)
        result.sections.append({
            "entity_type": name,
            "new": part.accepted,
            "updated": part.updated,
            "rejected": part.rejected,
        })
        return rejected_ids

    # Run in dependency order, and drop what has been orphaned on the way. A
    # customer rejected for a bad phone number would otherwise take its orders
    # down with it under "No customer found with external_id C2" — which reads
    # as though C2 were missing from the file, when it is three columns to the
    # left of the real error. One root cause should produce one error.
    dropped_customers = absorb("customers", ingest_customers(
        db, customers, update_existing=update_existing
    ))
    orphaned_orders = {
        order["external_id"] for order in orders
        if clean(order, "customer_external_id") in dropped_customers
    }
    kept_orders = [o for o in orders if o["external_id"] not in orphaned_orders]
    origins["orders"] = [
        line for o, line in zip(orders, origins["orders"])
        if o["external_id"] not in orphaned_orders
    ]
    orders = kept_orders

    dropped_orders = absorb("orders", ingest_orders(
        db, orders, update_existing=update_existing
    )) | orphaned_orders

    kept_items = [i for i in items if clean(i, "order_external_id") not in dropped_orders]
    origins["order_items"] = [
        line for i, line in zip(items, origins["order_items"])
        if clean(i, "order_external_id") not in dropped_orders
    ]
    items = kept_items

    absorb("order_items", ingest_order_items(db, items, update_existing=update_existing))

    result.errors.sort(key=lambda e: e["row"])
    result.warnings.sort(key=lambda w: w["row"])
    # Counted in lines of the file, which is the only unit the operator has.
    # One bad line can fail in more than one place; it is still one line.
    result.rejected = len(failed_lines)
    result.accepted = max(result.total_rows - result.rejected, 0)
    return result


def _origin_line(row_number: int, origin: list[int]) -> int:
    """Translate a position in a split batch back to its line in the file."""
    index = row_number - 1
    return origin[index] if 0 <= index < len(origin) else row_number


INGESTORS: dict[str, Callable[..., IngestResult]] = {
    "combined": ingest_combined,
    "customers": ingest_customers,
    "orders": ingest_orders,
    "order_items": ingest_order_items,
    "events": ingest_events,
    "consent_events": ingest_consent_events,
}

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "combined": ["customer_external_id", "order_external_id", "ordered_at", "total_amount"],
    "customers": ["external_id"],
    "orders": ["external_id", "customer_external_id", "ordered_at", "total_amount"],
    "order_items": ["external_id", "order_external_id", "sku", "product_name"],
    "events": ["customer_external_id", "event_type"],
    "consent_events": ["customer_external_id", "consent_type", "granted"],
}

#: Every column each entity accepts, in the order a downloaded template lists
#: them. Required columns come first so the ones that cannot be left out are
#: the ones on screen before anybody scrolls.
TEMPLATE_HEADERS: dict[str, list[str]] = {
    # One row per order line. The customer columns repeat on every row of
    # theirs and the order columns on every line of the order; the importer
    # takes the first appearance of each and ignores the repeats, so there is
    # nothing to keep in sync by hand.
    "combined": [
        # who
        "customer_external_id", "email", "phone", "first_name", "last_name",
        "date_of_birth", "age_verified", "city", "region", "postcode", "country",
        "signup_date", "acquisition_source", "preferred_channel",
        "marketing_consent", "email_consent", "sms_consent", "whatsapp_consent",
        # the order
        "order_external_id", "ordered_at", "status", "total_amount",
        "discount_amount", "delivery_fee", "currency", "channel", "coupon_code",
        "delivery_city",
        # the line
        "item_external_id", "sku", "product_name", "category", "brand",
        "quantity", "unit_price", "line_total",
    ],
    "customers": [
        "external_id", "email", "phone", "first_name", "last_name", "date_of_birth",
        "age_verified", "city", "region", "postcode", "country", "signup_date",
        "acquisition_source", "preferred_channel", "marketing_consent", "email_consent",
        "sms_consent", "whatsapp_consent",
    ],
    "orders": [
        "external_id", "customer_external_id", "ordered_at", "status", "total_amount",
        "discount_amount", "delivery_fee", "currency", "channel", "coupon_code",
        "delivery_city",
    ],
    "order_items": [
        "external_id", "order_external_id", "sku", "product_name", "category", "brand",
        "quantity", "unit_price", "line_total",
    ],
    "events": ["customer_external_id", "event_type", "occurred_at", "source", "payload"],
    "consent_events": [
        "customer_external_id", "consent_type", "granted", "source", "occurred_at"
    ],
}

#: A filled-in customer, so the template shows the shape of every column rather
#: than only its name. The consent flags are the reason this exists: a column
#: header alone does not say that leaving it blank means "no consent", and a
#: list loaded that way is one no campaign can send to.
TEMPLATE_EXAMPLE: dict[str, list[dict[str, str]]] = {
    # Two lines of one order, on purpose: it is the only way to show that the
    # customer and order columns repeat rather than being left blank on the
    # second line, which is the question anybody filling this in asks first.
    "combined": [
        {
            "customer_external_id": TEMPLATE_EXAMPLE_ID,
            "email": "jane@example.co.nz",
            "phone": "+64211234567",
            "first_name": "Jane",
            "last_name": "Example",
            "date_of_birth": "1990-04-23",
            "age_verified": "true",
            "city": "Auckland",
            "region": "Auckland",
            "postcode": "1010",
            "country": "New Zealand",
            "signup_date": "2026-01-15",
            "acquisition_source": "website",
            "preferred_channel": "SMS",
            "marketing_consent": "true",
            "email_consent": "true",
            "sms_consent": "true",
            "whatsapp_consent": "false",
            "order_external_id": "EXAMPLE-ORDER-1",
            "ordered_at": "2026-08-12 19:40:00",
            "status": "COMPLETED",
            "total_amount": "94.98",
            "discount_amount": "5.00",
            "delivery_fee": "4.99",
            "currency": "NZD",
            "channel": "web",
            "coupon_code": "GIMME5",
            "delivery_city": "Auckland",
            "item_external_id": "EXAMPLE-LINE-1",
            "sku": "steinlager-classic-12pk",
            "product_name": "Steinlager Classic 12pk",
            "category": "Beer",
            "brand": "Steinlager",
            "quantity": "1",
            "unit_price": "28.99",
            "line_total": "28.99",
        },
        {
            "customer_external_id": TEMPLATE_EXAMPLE_ID,
            "email": "jane@example.co.nz",
            "phone": "+64211234567",
            "first_name": "Jane",
            "last_name": "Example",
            "date_of_birth": "1990-04-23",
            "age_verified": "true",
            "city": "Auckland",
            "region": "Auckland",
            "postcode": "1010",
            "country": "New Zealand",
            "signup_date": "2026-01-15",
            "acquisition_source": "website",
            "preferred_channel": "SMS",
            "marketing_consent": "true",
            "email_consent": "true",
            "sms_consent": "true",
            "whatsapp_consent": "false",
            "order_external_id": "EXAMPLE-ORDER-1",
            "ordered_at": "2026-08-12 19:40:00",
            "status": "COMPLETED",
            "total_amount": "94.98",
            "discount_amount": "5.00",
            "delivery_fee": "4.99",
            "currency": "NZD",
            "channel": "web",
            "coupon_code": "GIMME5",
            "delivery_city": "Auckland",
            "item_external_id": "EXAMPLE-LINE-2",
            "sku": "fat-bird-sauv-blanc-750ml",
            "product_name": "Fat Bird Sauv Blanc 750ml",
            "category": "Wine",
            "brand": "Fat Bird",
            "quantity": "2",
            "unit_price": "19.99",
            "line_total": "39.98",
        },
    ],
    "customers": [{
        "external_id": TEMPLATE_EXAMPLE_ID,
        "email": "jane@example.co.nz",
        "phone": "+64211234567",
        "first_name": "Jane",
        "last_name": "Example",
        "date_of_birth": "1990-04-23",
        "age_verified": "true",
        "city": "Auckland",
        "region": "Auckland",
        "postcode": "1010",
        "country": "New Zealand",
        "signup_date": "2026-01-15",
        "acquisition_source": "website",
        "preferred_channel": "SMS",
        "marketing_consent": "true",
        "email_consent": "true",
        "sms_consent": "true",
        "whatsapp_consent": "false",
    }],
}


def template_csv(entity_type: str) -> str:
    """The header row for an entity, plus a worked example where there is one."""
    headers = TEMPLATE_HEADERS[entity_type]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for example in TEMPLATE_EXAMPLE.get(entity_type, ()):
        writer.writerow([example.get(column, "") for column in headers])
    return buffer.getvalue()


# --------------------------------------------------------------------------
# CSV handling
# --------------------------------------------------------------------------
#: Header spellings that are a genuine rename rather than a difference in
#: punctuation, mapped to the field they mean. Punctuation and case are handled
#: by :func:`canonical_header` on their own — "First Name", "FirstName" and
#: "FIRST_NAME" all slug to ``first_name`` with nothing listed here. What needs
#: listing is a header that uses different *words*: an export calling the
#: product "Item Name" is not a formatting difference, and the importer used to
#: drop that column silently and load every row with a blank product.
#:
#: Deliberately not exhaustive. A spelling nobody has sent us is a guess, and a
#: wrong guess maps real data onto the wrong field.
HEADER_ALIASES: dict[str, str] = {
    # Who
    "customer_first_name": "first_name",
    "given_name": "first_name",
    "customer_last_name": "last_name",
    "surname": "last_name",
    "family_name": "last_name",
    "name": "full_name",
    "customer_name": "full_name",
    "email_address": "email",
    "customer_email": "email",
    "mobile": "phone",
    "mobile_number": "phone",
    "phone_number": "phone",
    "customer_phone": "phone",
    "dob": "date_of_birth",
    "birth_date": "date_of_birth",
    "suburb": "city",
    "town": "city",
    "post_code": "postcode",
    "zip": "postcode",
    "customer_id": "customer_external_id",
    "customer_reference": "customer_external_id",
    # The order
    "order_id": "order_external_id",
    "order_number": "order_external_id",
    "order_date": "ordered_at",
    "order_placed_at": "ordered_at",
    "date": "ordered_at",
    "order_status": "status",
    "order_total": "total_amount",
    "total": "total_amount",
    "amount": "total_amount",
    "grand_total": "total_amount",
    "discount": "discount_amount",
    "delivery_charge": "delivery_fee",
    "shipping_fee": "delivery_fee",
    # The line
    "item_id": "item_external_id",
    "line_id": "item_external_id",
    "product": "product_name",
    "item": "product_name",
    "item_name": "product_name",
    "description": "product_name",
    "product_code": "sku",
    "item_code": "sku",
    "product_category": "category",
    "product_type": "category",
    "product_brand": "brand",
    "supplier": "brand",
    "qty": "quantity",
    "units": "quantity",
    "price": "unit_price",
    "item_price": "unit_price",
    "line_amount": "line_total",
}

#: A full-name column is split into the two the model actually holds.
_FULL_NAME_COLUMN = "full_name"

_HEADER_SLUG = re.compile(r"[^a-z0-9]+")
#: The word break inside a camelCase header. "FirstName" carries the same break
#: as "First Name" and has to slug the same way, or an export written by a
#: developer imports differently from one written by hand.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _raw_headers(content: bytes) -> tuple[list[str], str]:
    """The header row exactly as the file spells it, for reporting back."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return [], ""
    reader = csv.DictReader(io.StringIO(text))
    return [h.strip() for h in (reader.fieldnames or []) if (h or "").strip()], text


def canonical_header(header: str) -> str:
    """The internal field name a spreadsheet column means.

    Two steps, in this order. Word breaks and case are normalised — "First
    Name", "FirstName" and " FIRST_NAME " are all ``first_name`` — and then a
    genuine rename is looked up in :data:`HEADER_ALIASES`. A header that
    matches neither is left as its slug, so an unrecognised column is ignored
    by the ingestors rather than mapped onto something it is not.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", (header or "").strip())
    slug = _HEADER_SLUG.sub("_", spaced.lower()).strip("_")
    return HEADER_ALIASES.get(slug, slug)


def header_mapping(headers: list[str]) -> list[dict[str, str]]:
    """How each column in the file was read, for the preview to show.

    Worth putting on screen: "we read your 'Item Name' column as product_name"
    is the difference between trusting an import and finding out a month later
    that every product was blank.
    """
    return [
        {"column": header, "field": canonical_header(header)}
        for header in headers
        if (header or "").strip()
    ]


def _split_full_name(row: dict) -> None:
    """Fill first and last name from a single name column, if that is all there is.

    Only when the file has no first/last of its own. An export with both a
    "Customer Name" and a "First Name" means the split columns; guessing over
    the top of them would overwrite better data with a worse split.
    """
    full = str(row.get(_FULL_NAME_COLUMN) or "").strip()
    if not full:
        return
    if not str(row.get("first_name") or "").strip():
        first, _, last = full.partition(" ")
        row["first_name"] = first
        if not str(row.get("last_name") or "").strip():
            row["last_name"] = last.strip()


def parse_csv(content: bytes) -> tuple[list[str], list[dict]]:
    """Decode and parse CSV bytes into headers and row dicts."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("The file could not be decoded as UTF-8 or Latin-1 text.")

    if not text.strip():
        raise ValueError("The file is empty.")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("The file has no header row.")

    # Canonicalised once, here, so every ingestor and the header check all see
    # the same field names. Doing it per-reader is how one of them ends up
    # understanding "Order Date" and the others quietly not.
    headers = [canonical_header(h) for h in reader.fieldnames if (h or "").strip()]
    rows = []
    for raw in reader:
        row = {canonical_header(k): v for k, v in raw.items() if (k or "").strip()}
        _split_full_name(row)
        rows.append(row)
    return headers, rows


def validate_headers(entity_type: str, headers: list[str]) -> list[str]:
    """Return the required columns that are missing from the header row."""
    required = REQUIRED_COLUMNS.get(entity_type, [])
    present = {h.lower() for h in headers}
    return [c for c in required if c.lower() not in present]


def preview_csv(entity_type: str, content: bytes, *, rows: int = 5) -> dict:
    """Parse a file for preview without writing anything.

    This is a genuine dry run: every row goes through the same ingestor the
    import uses, inside a transaction that is discarded. So the counts and the
    error list are not a second implementation of the rules that could drift
    from them — they are the rules, executed.

    That matters most for the things a header check cannot see: a duplicate
    external id, a date in an unexpected format, a landline where a mobile was
    expected. Finding those after committing a customer list means unpicking it.
    """
    raw_headers, _ = _raw_headers(content)
    headers, parsed = parse_csv(content)
    missing = validate_headers(entity_type, headers)
    preview = {
        "entity_type": entity_type,
        "headers": headers,
        # What each of their columns was read as. A header check that only
        # says "first_name is missing" is unhelpful to somebody looking at a
        # file whose first column is plainly called "First Name".
        "column_mapping": header_mapping(raw_headers),
        "total_rows": len(parsed),
        "missing_required_columns": missing,
        "valid": not missing,
        "sample_rows": parsed[:rows],
        "dry_run": None,
    }
    if missing or entity_type not in INGESTORS:
        return preview

    preview["dry_run"] = dry_run_rows(entity_type, parsed)
    return preview


class _DryRunSession(Session):
    """A session that cannot persist anything.

    Every ingestor commits when it finishes — they are written to be called for
    real — so a preview cannot just call one and roll back afterwards: the
    commit has already happened. Nesting the work in a SAVEPOINT does not help
    either, because pysqlite's transaction handling lets the inner commit
    through (measured, not assumed).

    So the commit is removed instead. `flush` gives the ingestor everything a
    commit would — its own writes are visible to its own subsequent queries, so
    duplicate detection still works — while leaving the transaction open for
    the caller to discard. An ingestor that grows a new commit tomorrow is
    still contained, because there is no code path here that can persist.
    """

    def commit(self) -> None:  # noqa: D102 - deliberately not a commit
        self.flush()


def dry_run_rows(entity_type: str, rows: list[dict]) -> dict:
    """Run an ingestor for its report, keeping none of its writes."""
    from app.core.database import engine

    session = _DryRunSession(bind=engine, autoflush=False, future=True)
    try:
        return INGESTORS[entity_type](session, rows).as_dict()
    finally:
        # Session.rollback, not the overridden commit — this really does undo.
        Session.rollback(session)
        session.close()


def ingest_csv(
    db: Session,
    entity_type: str,
    content: bytes,
    *,
    filename: str = "",
    user_id: int | None = None,
    source: str = "csv_upload",
) -> IngestionJob:
    """Parse, validate and ingest a CSV file, recording an ingestion job."""
    if entity_type not in INGESTORS:
        raise ValueError(
            f"Unknown entity type '{entity_type}'. Expected one of: "
            f"{', '.join(sorted(INGESTORS))}."
        )

    job = IngestionJob(
        source=source,
        entity_type=entity_type,
        filename=filename,
        status=IngestionStatus.RUNNING.value,
        started_at=utcnow(),
        created_by_id=user_id,
    )
    db.add(job)
    db.commit()

    try:
        headers, rows = parse_csv(content)
        missing = validate_headers(entity_type, headers)
        if missing:
            raise ValueError(
                f"The file is missing required columns: {', '.join(missing)}."
            )
        result = INGESTORS[entity_type](db, rows)
    except ValueError as exc:
        job.status = IngestionStatus.FAILED.value
        job.finished_at = utcnow()
        job.errors = [{"row": 0, "error": str(exc), "data": {}}]
        db.commit()
        return job

    job.status = IngestionStatus.COMPLETED.value
    job.total_rows = result.total_rows
    job.accepted_rows = result.accepted
    job.updated_rows = result.updated
    job.rejected_rows = result.rejected
    job.duplicate_rows = result.duplicates
    job.errors = result.errors
    job.finished_at = utcnow()
    db.commit()

    _post_ingest(db, entity_type, result)
    return job


def _post_ingest(db: Session, entity_type: str, result: IngestResult) -> None:
    """Recompute intelligence and segment membership for what a load touched."""
    from app.services.segments import refresh_all_segments  # local: avoids a cycle

    if not _recompute_intelligence(db, entity_type, result):
        return

    # Segment membership is what a campaign picks its audience from, and it is
    # stored rather than evaluated on read. Without this a load lands the
    # orders, moves every lifecycle stage, and leaves the counts frozen on the
    # previous run — the data is in, analytics shows it, and every cohort the
    # campaign builder offers still reads zero.
    refresh_all_segments(db)


def _recompute_intelligence(db: Session, entity_type: str, result: IngestResult) -> bool:
    """Recompute intelligence for the customers a load touched.

    Returns whether anything was recomputed.
    """
    from app.services.attribution import process_new_order  # local: avoids a cycle
    from app.services.intelligence import refresh_customer, refresh_rfm

    if entity_type == "combined":
        # A combined file lands customers and orders at once, so it needs both
        # halves: attribution for the new orders, and a recompute for everyone
        # the file touched. Either alone leaves the dashboard half-right.
        for order_id in result.created_order_ids:
            order = db.get(Order, order_id)
            if order is not None:
                process_new_order(db, order)
        for customer_id in result.affected_customer_ids:
            customer = db.get(Customer, customer_id)
            if customer is not None:
                refresh_customer(db, customer, commit=False)
        db.commit()
        refresh_rfm(db)
        return True

    if entity_type == "orders" and result.created_order_ids:
        for order_id in result.created_order_ids:
            order = db.get(Order, order_id)
            if order is not None:
                process_new_order(db, order)
        refresh_rfm(db)
        return True

    if not result.affected_customer_ids:
        return False

    for customer_id in result.affected_customer_ids:
        customer = db.get(Customer, customer_id)
        if customer is not None:
            refresh_customer(db, customer, commit=False)
    db.commit()
    refresh_rfm(db)
    return True


def error_report_csv(job: IngestionJob) -> str:
    """Render an ingestion job's errors as a downloadable CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["row", "error", "identifiers"])
    for error in job.errors or []:
        identifiers = ", ".join(f"{k}={v}" for k, v in (error.get("data") or {}).items())
        writer.writerow([error.get("row", ""), error.get("error", ""), identifiers])
    return buffer.getvalue()
