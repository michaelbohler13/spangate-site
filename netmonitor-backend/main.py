"""
main.py — SpanGate Network Monitor Backend v1.0.0
FastAPI application entry point.

On startup:
  - Creates database tables if they don't exist (create_all)
  - Runs an immediate data-retention cleanup (deletes rows > 30 days)
  - Schedules cleanup to repeat every 24 hours via APScheduler

Routers mounted at /api/v1:
  POST /agent/heartbeat
  POST /alerts/ping
  POST /alerts/config-change
  GET  /alerts
  POST /configs/backup
  GET  /configs/{hostname}
  GET  /configs/{hostname}/{config_id}
  GET  /devices
  GET  /devices/{hostname}
  GET  /dashboard

GET /health  — no auth, for load-balancer probes
"""

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, text

from database import AsyncSessionLocal, engine
from models import Alert, Base, Config, ConfigDiff
from routers import agents, alerts, configs, dashboard, devices

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

RETENTION_DAYS = 30


# ── Data-retention cleanup ────────────────────────────────────────────────────

async def run_cleanup() -> None:
    """
    Delete configs, config_diffs, and alerts older than RETENTION_DAYS.

    Called on startup and every 24 hours by APScheduler.
    Never crashes the process — exceptions are logged and swallowed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    try:
        async with AsyncSessionLocal() as db:
            for model, col in (
                (Config,     Config.pulled_at),
                (ConfigDiff, ConfigDiff.detected_at),
                (Alert,      Alert.created_at),
            ):
                result = await db.execute(delete(model).where(col < cutoff))
                count  = result.rowcount
                if count:
                    logger.info("[CLEANUP] Deleted %d row(s) from %s", count, model.__tablename__)
            await db.commit()
    except Exception as exc:
        logger.error("[CLEANUP] Failed: %s", exc)


# ── Lifespan ──────────────────────────────────────────────────────────────────

_scheduler: AsyncIOScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Startup:
      1. Create all tables (idempotent — safe to run on every boot).
      2. Run data-retention cleanup immediately.
      3. Start APScheduler to repeat cleanup every 24 hours.

    Shutdown:
      4. Stop the scheduler cleanly.
    """
    global _scheduler

    # 1. Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables verified / created.")

    # 2. Immediate cleanup
    await run_cleanup()

    # 3. Schedule recurring cleanup
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(run_cleanup, "interval", hours=24, id="data-retention-cleanup")
    _scheduler.start()
    logger.info("Cleanup scheduler started — runs every 24 hours.")

    logger.info("SpanGate Network Monitor Backend v1.0.0 — ready.")
    yield

    # 4. Shutdown
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    logger.info("SpanGate Network Monitor Backend — shut down.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SpanGate Network Monitor API",
    version="1.0.0",
    description=(
        "Backend for the SpanGate Network Monitor. Receives telemetry from "
        "lightweight agents and serves data to the dashboard."
    ),
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)


# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://spangate.com",
        "https://www.spangate.com",
        "https://spangate-site.vercel.app",
        "http://localhost:3000",
        "http://localhost:8080",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Agent-Version"],
)


# ── Request logging middleware ────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    """
    Log every request with method, path, response status code, and duration.

    Skips /health to avoid noise in production logs.
    """
    if request.url.path == "/health":
        return await call_next(request)

    start   = time.perf_counter()
    response: Response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000

    logger.info(
        "%s %s → %d  (%.1fms)",
        request.method, request.url.path, response.status_code, elapsed,
    )
    return response


# ── Routers ───────────────────────────────────────────────────────────────────

for _router in (agents.router, alerts.router, configs.router, devices.router, dashboard.router):
    app.include_router(_router, prefix="/api/v1")


# ── Health probe ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"], include_in_schema=False)
async def health() -> dict:
    """No-auth health probe for load balancers and uptime monitors."""
    return {"status": "ok", "version": "1.0.0", "service": "netmonitor-backend"}
