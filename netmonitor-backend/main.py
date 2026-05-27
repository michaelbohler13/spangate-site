"""
main.py — SpanGate Network Monitor Backend v1.0.0
FastAPI application entry point.

Designed for deployment on Vercel serverless functions + Supabase PostgreSQL.

On startup:
  - Creates database tables if they don't exist (create_all).
  - No long-running scheduler — data-retention cleanup is triggered by
    Vercel Cron calling GET /api/v1/admin/cleanup once per day.

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
  GET  /admin/cleanup   ← called by Vercel Cron

GET /health  — no auth, for uptime probes
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from database import engine
from models import Base
from routers import agents, alerts, cleanup, configs, dashboard, devices, feedback

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Startup:
      - Create all tables (idempotent — safe to run on every cold start).

    No scheduler is started here.  Data-retention cleanup runs via
    Vercel Cron → GET /api/v1/admin/cleanup once per day.
    """
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables verified / created.")
    except Exception as exc:
        # Log but don't crash — lets /health respond even if DB is unreachable
        # on cold start.  Individual endpoints will still fail if DB is down.
        logger.warning("DB create_all failed on startup (tables may not exist): %s", exc)
    logger.info("SpanGate Network Monitor Backend v1.0.0 — ready.")
    yield
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

    start    = time.perf_counter()
    response: Response = await call_next(request)
    elapsed  = (time.perf_counter() - start) * 1000

    logger.info(
        "%s %s → %d  (%.1fms)",
        request.method, request.url.path, response.status_code, elapsed,
    )
    return response


# ── Routers ───────────────────────────────────────────────────────────────────

for _router in (
    agents.router,
    alerts.router,
    cleanup.router,
    configs.router,
    dashboard.router,
    devices.router,
    feedback.router,
):
    app.include_router(_router, prefix="/api/v1")


# ── Health probe ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"], include_in_schema=False)
async def health() -> dict:
    """No-auth health probe for uptime monitors."""
    return {"status": "ok", "version": "1.0.0", "service": "netmonitor-backend"}
