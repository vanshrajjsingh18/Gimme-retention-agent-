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
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
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

    def as_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "total_rows": self.total_rows,
            "accepted_rows": self.accepted,
            "updated_rows": self.updated,
            "rejected_rows": self.rejected,
            "duplicate_rows": self.duplicates,
            "errors": self.errors,
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
def ingest_customers(db: Session, rows: list[dict], *, update_existing: bool = True) -> IngestResult:
    result = IngestResult("customers")
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

            email = optional(row, "email")
            phone = optional(row, "phone")
            if not email and not phone:
                raise RowError("A customer needs at least an email address or a phone number.")
            if email and not EMAIL_PATTERN.match(email):
                raise RowError(f"'email' value '{email}' is not a valid email address.")

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
                "age_verified": parse_bool(row.get("age_verified")),
                "city": optional(row, "city"),
                "region": optional(row, "region"),
                "postcode": optional(row, "postcode"),
                "country": clean(row, "country", "New Zealand"),
                "signup_date": parse_datetime(row.get("signup_date"), "signup_date"),
                "acquisition_source": optional(row, "acquisition_source"),
                "preferred_channel": _parse_channel(row.get("preferred_channel")),
                "marketing_consent": parse_bool(row.get("marketing_consent")),
                "email_consent": parse_bool(row.get("email_consent")),
                "sms_consent": parse_bool(row.get("sms_consent")),
                "whatsapp_consent": parse_bool(row.get("whatsapp_consent")),
            }

            if existing is None:
                customer = Customer(external_id=external_id, **values)
                db.add(customer)
                db.flush()
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
                    # A blank column in an update file must not wipe existing data.
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


INGESTORS: dict[str, Callable[..., IngestResult]] = {
    "customers": ingest_customers,
    "orders": ingest_orders,
    "order_items": ingest_order_items,
    "events": ingest_events,
    "consent_events": ingest_consent_events,
}

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "customers": ["external_id"],
    "orders": ["external_id", "customer_external_id", "ordered_at", "total_amount"],
    "order_items": ["external_id", "order_external_id", "sku", "product_name"],
    "events": ["customer_external_id", "event_type"],
    "consent_events": ["customer_external_id", "consent_type", "granted"],
}


# --------------------------------------------------------------------------
# CSV handling
# --------------------------------------------------------------------------
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

    headers = [h.strip() for h in reader.fieldnames]
    rows = []
    for raw in reader:
        rows.append({(k.strip() if k else ""): v for k, v in raw.items() if k})
    return headers, rows


def validate_headers(entity_type: str, headers: list[str]) -> list[str]:
    """Return the required columns that are missing from the header row."""
    required = REQUIRED_COLUMNS.get(entity_type, [])
    present = {h.lower() for h in headers}
    return [c for c in required if c.lower() not in present]


def preview_csv(entity_type: str, content: bytes, *, rows: int = 5) -> dict:
    """Parse a file for preview without writing anything."""
    headers, parsed = parse_csv(content)
    missing = validate_headers(entity_type, headers)
    return {
        "entity_type": entity_type,
        "headers": headers,
        "total_rows": len(parsed),
        "missing_required_columns": missing,
        "valid": not missing,
        "sample_rows": parsed[:rows],
    }


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
    """Recompute intelligence for the customers a load touched."""
    from app.services.attribution import process_new_order  # local: avoids a cycle
    from app.services.intelligence import refresh_customer, refresh_rfm

    if entity_type == "orders" and result.created_order_ids:
        for order_id in result.created_order_ids:
            order = db.get(Order, order_id)
            if order is not None:
                process_new_order(db, order)
        refresh_rfm(db)
        return

    if not result.affected_customer_ids:
        return

    for customer_id in result.affected_customer_ids:
        customer = db.get(Customer, customer_id)
        if customer is not None:
            refresh_customer(db, customer, commit=False)
    db.commit()
    refresh_rfm(db)


def error_report_csv(job: IngestionJob) -> str:
    """Render an ingestion job's errors as a downloadable CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["row", "error", "identifiers"])
    for error in job.errors or []:
        identifiers = ", ".join(f"{k}={v}" for k, v in (error.get("data") or {}).items())
        writer.writerow([error.get("row", ""), error.get("error", ""), identifiers])
    return buffer.getvalue()
