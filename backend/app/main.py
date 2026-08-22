"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import (
    analytics,
    auth,
    automations,
    brand,
    campaigns,
    customers,
    ingest,
    integrations,
    journeys,
    messages,
    segments,
    system,
)
from app.core.config import settings
from app.core.database import session_scope
from app.jobs.scheduler import shutdown_scheduler, start_scheduler
from app.services.bootstrap import bootstrap, create_tables

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepare the database and start background jobs."""
    create_tables()
    with session_scope() as db:
        result = bootstrap(db)
    logger.info("Bootstrap complete: %s", result)
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description=(
        "AI-assisted customer retention, churn intelligence and compliance-gated "
        "campaign management for GIMME Beverage Delivery."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    # Explicit origin list rather than "*": credentials are sent with requests.
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return validation errors in a shape the frontend can render directly."""
    errors = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error["loc"] if p not in ("body", "query"))
        errors.append({"field": location or "request", "message": error["msg"]})
    return JSONResponse(
        status_code=422,
        content={
            "detail": "; ".join(f"{e['field']}: {e['message']}" for e in errors),
            "errors": errors,
        },
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "app": settings.APP_NAME, "environment": settings.ENVIRONMENT}


for router in (
    auth.router,
    ingest.router,
    customers.router,
    segments.router,
    brand.router,
    messages.router,
    campaigns.router,
    automations.router,
    analytics.router,
    integrations.router,
    journeys.router,
    system.router,
):
    app.include_router(router, prefix=API_PREFIX)
