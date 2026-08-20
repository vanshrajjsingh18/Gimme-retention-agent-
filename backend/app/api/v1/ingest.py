"""Data ingestion: authenticated APIs and CSV upload."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_api_key, get_current_user, require_write
from app.core.config import settings
from app.core.database import get_db
from app.models.entities import ApiKey, ApiRequestLog, IngestionJob, User
from app.schemas.models import (
    ConsentEventIn,
    CustomerIn,
    EventIn,
    IngestionJobOut,
    IngestResponse,
    OrderIn,
    OrderItemIn,
)
from app.services import ingestion

router = APIRouter()

ENTITY_TYPES = ["customers", "orders", "order_items", "events", "consent_events"]


def _log_request(
    db: Session,
    api_key: ApiKey,
    request: Request,
    *,
    status_code: int,
    duration_ms: float,
    record_count: int,
    error: str | None = None,
) -> None:
    """Record an API ingestion request. Never logs the request body."""
    db.add(
        ApiRequestLog(
            api_key_id=api_key.id,
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            duration_ms=round(duration_ms, 2),
            record_count=record_count,
            error_message=error,
        )
    )
    db.commit()


def _run_api_ingest(
    db: Session,
    api_key: ApiKey,
    request: Request,
    entity_type: str,
    rows: list[dict],
) -> IngestResponse:
    started = time.perf_counter()
    try:
        result = ingestion.INGESTORS[entity_type](db, rows)
    except Exception as exc:  # noqa: BLE001 - must still log the failure
        _log_request(
            db,
            api_key,
            request,
            status_code=500,
            duration_ms=(time.perf_counter() - started) * 1000,
            record_count=len(rows),
            error=str(exc)[:500],
        )
        raise

    ingestion._post_ingest(db, entity_type, result)
    _log_request(
        db,
        api_key,
        request,
        status_code=200,
        duration_ms=(time.perf_counter() - started) * 1000,
        record_count=len(rows),
    )
    return IngestResponse(**result.as_dict())


# --------------------------------------------------------------------------
# Machine-to-machine ingestion (API key)
# --------------------------------------------------------------------------
@router.post("/customers", response_model=IngestResponse, tags=["ingestion"])
def ingest_customers(
    payload: list[CustomerIn],
    request: Request,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(get_api_key),
) -> IngestResponse:
    rows = [p.model_dump(mode="json") for p in payload]
    return _run_api_ingest(db, api_key, request, "customers", rows)


@router.post("/orders", response_model=IngestResponse, tags=["ingestion"])
def ingest_orders(
    payload: list[OrderIn],
    request: Request,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(get_api_key),
) -> IngestResponse:
    rows = [p.model_dump(mode="json") for p in payload]
    return _run_api_ingest(db, api_key, request, "orders", rows)


@router.post("/order-items", response_model=IngestResponse, tags=["ingestion"])
def ingest_order_items(
    payload: list[OrderItemIn],
    request: Request,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(get_api_key),
) -> IngestResponse:
    rows = [p.model_dump(mode="json") for p in payload]
    return _run_api_ingest(db, api_key, request, "order_items", rows)


@router.post("/events", response_model=IngestResponse, tags=["ingestion"])
def ingest_events(
    payload: list[EventIn],
    request: Request,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(get_api_key),
) -> IngestResponse:
    rows = [p.model_dump(mode="json") for p in payload]
    return _run_api_ingest(db, api_key, request, "events", rows)


@router.post("/consent-events", response_model=IngestResponse, tags=["ingestion"])
def ingest_consent_events(
    payload: list[ConsentEventIn],
    request: Request,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(get_api_key),
) -> IngestResponse:
    rows = [p.model_dump(mode="json") for p in payload]
    return _run_api_ingest(db, api_key, request, "consent_events", rows)


# --------------------------------------------------------------------------
# CSV upload (dashboard session)
# --------------------------------------------------------------------------
def _read_upload(file: UploadFile) -> bytes:
    content = file.file.read()
    if len(content) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"File is larger than the {settings.MAX_UPLOAD_BYTES // (1024 * 1024)}MB "
                "upload limit."
            ),
        )
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return content


def _validate_entity_type(entity_type: str) -> str:
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity type '{entity_type}'. Expected one of: "
            f"{', '.join(ENTITY_TYPES)}.",
        )
    return entity_type


@router.post("/uploads/preview", tags=["ingestion"])
def preview_upload(
    entity_type: str = Form(...),
    file: UploadFile = File(...),
    _: User = Depends(require_write),
) -> dict:
    """Parse and validate a CSV without writing anything."""
    _validate_entity_type(entity_type)
    content = _read_upload(file)
    try:
        return ingestion.preview_csv(entity_type, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/uploads", response_model=IngestionJobOut, tags=["ingestion"])
def upload_csv(
    entity_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_write),
) -> IngestionJobOut:
    """Import a CSV file and return the resulting ingestion job."""
    _validate_entity_type(entity_type)
    content = _read_upload(file)
    job = ingestion.ingest_csv(
        db,
        entity_type,
        content,
        filename=file.filename or "upload.csv",
        user_id=user.id,
    )
    return IngestionJobOut.model_validate(job)


@router.get("/uploads", response_model=list[IngestionJobOut], tags=["ingestion"])
def list_jobs(
    limit: int = 25,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[IngestionJobOut]:
    jobs = (
        db.execute(
            select(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(min(limit, 200))
        )
        .scalars()
        .all()
    )
    return [IngestionJobOut.model_validate(j) for j in jobs]


@router.get("/uploads/{job_id}", response_model=IngestionJobOut, tags=["ingestion"])
def get_job(
    job_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> IngestionJobOut:
    job = db.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ingestion job not found.")
    return IngestionJobOut.model_validate(job)


@router.get("/uploads/{job_id}/errors.csv", tags=["ingestion"])
def download_error_report(
    job_id: int, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> PlainTextResponse:
    job = db.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ingestion job not found.")
    return PlainTextResponse(
        ingestion.error_report_csv(job),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="import-{job_id}-errors.csv"'
        },
    )


@router.get("/uploads/templates/{entity_type}.csv", tags=["ingestion"])
def download_template(entity_type: str, _: User = Depends(get_current_user)) -> PlainTextResponse:
    """Return an empty CSV with the correct headers for an entity type."""
    _validate_entity_type(entity_type)
    headers = {
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
    }[entity_type]
    return PlainTextResponse(
        ",".join(headers) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{entity_type}-template.csv"'},
    )
