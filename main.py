"""
AP Policy Engine — FastAPI Application Entry Point
"""
from __future__ import annotations
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.utils.config import settings

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ap_engine")


# ---------------------------------------------------------------------------
# Scheduler (APScheduler)
# ---------------------------------------------------------------------------
_scheduler = None

def get_scheduler():
    return _scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    global _scheduler

    # Ensure data directory exists
    os.makedirs("./data", exist_ok=True)

    # Start APScheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 3},
            timezone="UTC",
        )
        _scheduler.start()
        logger.info("APScheduler started")
    except ImportError:
        logger.warning("APScheduler not installed — scheduled notifications disabled")

    logger.info(f"🚀 {settings.APP_NAME} v{settings.APP_VERSION} started")
    yield

    # Shutdown
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
    logger.info("AP Policy Engine shut down")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="""
## AP Policy Engine

Converts Accounts Payable policy documents into **deterministic, machine-executable rules**.

### Pipeline
1. **Extract** — Upload a policy doc (`.md`, `.txt`, `.pdf`) → LLM extracts structured rules
2. **Validate** — Submit an invoice JSON → Engine returns verdict + full audit trail
3. **Conflicts** — Automatically detected overlapping/contradictory rules
4. **Diagrams** — Mermaid flowcharts generated for every rule category

### Key Features
- Clause-level traceability (every rule linked to its source section)
- Confidence scoring with human-review queue for ambiguous extractions
- NetworkX-based conflict detection (threshold overlaps, contradictory actors, circular references)
- Deterministic execution engine (no LLM at runtime — pure rule evaluation)
- APScheduler-based 48-hour escalation ladder for unresolved deviations
- OpenAPI docs at `/docs`
        """,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "type": type(exc).__name__},
        )

    # Include routes
    app.include_router(router)

    # Root redirect
    @app.get("/", include_in_schema=False)
    async def root():
        return {"message": "AP Policy Engine", "docs": "/docs", "health": "/api/v1/health"}

    return app


app = create_app()