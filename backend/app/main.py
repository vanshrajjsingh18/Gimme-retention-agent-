"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
    # A deployment must not inherit the development defaults. Refusing to boot
    # is deliberately louder than a warning nobody reads: a live host running
    # on a published SECRET_KEY can have its session tokens forged, and one on
    # the published admin password is simply open.
    if settings.is_production:
        problems = settings.unsafe_production_settings()
        if problems:
            raise RuntimeError(
                "Refusing to start in production with development settings: "
                + "; ".join(problems)
                + ". Set these as environment variables on the host."
            )
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


# --------------------------------------------------------------------------
# Serving the dashboard
# --------------------------------------------------------------------------
# In development the UI runs on its own Vite server and talks to this one
# across origins. A deployment has no reason to keep them apart: serving the
# built bundle from here means one service, one domain, one certificate, and
# no CORS configuration that can be got wrong. Mounted last so it can claim
# "/" without shadowing any API route registered above.
_dist = Path(settings.FRONTEND_DIST)
if (_dist / "index.html").is_file():

    #: Prefixes the dashboard must never answer for. Without this the catch-all
    #: swallows them: a GET on a POST-only endpoint, or a mistyped path, would
    #: return index.html with a 200, and the caller would be left parsing HTML
    #: as JSON with no clue why.
    API_PATHS = ("api/", "health", "docs", "redoc", "openapi.json")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_dashboard(full_path: str) -> FileResponse:
        """Serve the built dashboard, falling back to index.html.

        The dashboard routes on the client, so a deep link like /customers/42
        is not a file on disk — it has to return index.html and let the app
        resolve the path. Only genuine asset requests map to real files, and
        the path is resolved before use so it cannot escape the bundle.
        """
        if full_path.startswith(API_PATHS):
            # The path may well exist for another method; what is certain is
            # that it is not a page, and must not come back as one.
            raise HTTPException(
                status_code=404,
                detail=f"No GET endpoint at /{full_path}. See /docs for the API.",
            )
        candidate = (_dist / full_path).resolve()
        if full_path and candidate.is_file() and _dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")

    logger.info("Serving the dashboard from %s", _dist)
else:
    logger.info(
        "No built dashboard at %s — API only. Run 'npm run build' in frontend/ to bundle it.",
        _dist,
    )
