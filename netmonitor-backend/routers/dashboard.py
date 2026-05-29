"""
routers/dashboard.py — SpanGate Network Monitor Backend
Dashboard summary endpoint.

Routes
------
GET /api/v1/dashboard
    Return aggregate summary: device counts, recent alerts, agent liveness.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from database import get_db
from models import Alert, Device
from schemas import AlertResponse, DashboardSummary
from state import get_heartbeat, get_site_status_counts

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Dashboard"])


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard(
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> DashboardSummary:
    """
    Return the dashboard summary for the authenticated site.

    Aggregates:
    - Total device count from the database
    - Live up/down counts from persisted Device.status column
    - Last 10 alerts from the database
    - Most recent heartbeat timestamp and agent version from the DB

    Args:
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        DashboardSummary with all aggregate fields populated.
    """
    site_id = ctx["site_id"]

    # ── Device counts ─────────────────────────────────────────────────────────
    count_result = await db.execute(
        select(func.count(Device.id)).where(Device.site_id == site_id)
    )
    total_devices = count_result.scalar_one() or 0

    devices_up, devices_down = await get_site_status_counts(db, site_id)

    # ── Recent alerts (last 10) ───────────────────────────────────────────────
    alert_result = await db.execute(
        select(Alert, Device.hostname)
        .join(Device, Alert.device_id == Device.id)
        .where(Device.site_id == site_id)
        .order_by(Alert.created_at.desc())
        .limit(10)
    )
    alert_rows = alert_result.all()

    recent_alerts = [
        AlertResponse(
            id=alert.id,
            alert_type=alert.alert_type,
            message=alert.message,
            created_at=alert.created_at,
            hostname=hostname,
        )
        for alert, hostname in alert_rows
    ]

    # ── Heartbeat state (from DB) ─────────────────────────────────────────────
    hb = await get_heartbeat(db, site_id)

    return DashboardSummary(
        total_devices=total_devices,
        devices_up=devices_up,
        devices_down=devices_down,
        recent_alerts=recent_alerts,
        last_heartbeat=hb.last_seen if hb else None,
        agent_version=hb.agent_version if hb else None,
        cpu_percent=hb.cpu_percent   if hb else None,
        mem_percent=hb.mem_percent   if hb else None,
        temp_celsius=hb.temp_celsius if hb else None,
        agent_model=hb.agent_model   if hb else None,
    )
