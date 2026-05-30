"""
routers/alerts.py — SpanGate Network Monitor Backend
Ping and config-change alert endpoints, plus alert feed query.

Routes
------
POST /api/v1/alerts/ping
    Insert a ping_down or ping_up alert. Updates in-memory ping status.

POST /api/v1/alerts/config-change
    Compute unified diff, insert config_diffs + config_change alert.

GET  /api/v1/alerts
    Return last 50 alerts for the authenticated site.
    Optional query params: hostname, alert_type
"""

import asyncio
import difflib
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import AuthContext, _get_supabase
from database import get_db
from email_service import send_ping_down_alert
from models import AgentHeartbeat, Alert, Config, ConfigDiff, Device
from schemas import AlertResponse, ConfigChange, PingAlert
from state import update_device_status

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Alerts"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_user_email(user_id: str) -> str | None:
    """
    Retrieve the registered email address for a user from Supabase auth.

    Used to send alert emails.  Returns None on any error so the alert
    endpoint never fails because of an email lookup problem.
    """
    try:
        client = _get_supabase()
        response = await asyncio.to_thread(client.auth.admin.get_user_by_id, user_id)
        return response.user.email if response and response.user else None
    except Exception as exc:
        logger.warning("[EMAIL] Could not fetch user email for %s: %s", user_id, exc)
        return None


async def _get_site_name(db: AsyncSession, site_id: str) -> str:
    """Return site_name from the latest heartbeat row, falling back to site_id."""
    try:
        result = await db.execute(
            select(AgentHeartbeat.site_name).where(AgentHeartbeat.site_id == site_id)
        )
        name = result.scalar_one_or_none()
        return name or site_id
    except Exception:
        return site_id


async def _get_or_create_device(
    db: AsyncSession,
    site_id: str,
    hostname: str,
    ip: str,
) -> Device:
    """
    Return the Device row for (site_id, hostname), creating it if necessary.

    Args:
        db: Async database session.
        site_id: Site identifier from auth context.
        hostname: Device hostname.
        ip: Device IP address (used when creating a new row).

    Returns:
        The existing or newly created Device ORM object.
    """
    result = await db.execute(
        select(Device).where(Device.site_id == site_id, Device.hostname == hostname)
    )
    device = result.scalar_one_or_none()

    if device is None:
        device = Device(site_id=site_id, hostname=hostname, ip=ip)
        db.add(device)
        await db.flush()   # populate device.id before referencing it in children
        logger.info("[DEVICE] Auto-created device %s/%s (%s)", site_id, hostname, ip)

    return device


def _compute_diff(old_text: str, new_text: str, hostname: str) -> str:
    """
    Generate a unified diff between two config texts.

    Args:
        old_text: Previous running-config.
        new_text: Current running-config.
        hostname: Used as filename label in the diff header.

    Returns:
        Unified diff string.  Empty string if the texts are identical.
    """
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{hostname}/config.old",
            tofile=f"{hostname}/config.new",
        )
    )
    return "".join(diff_lines)


# ── POST /alerts/ping ─────────────────────────────────────────────────────────

@router.post("/alerts/ping", status_code=status.HTTP_201_CREATED)
async def ping_alert(
    body: PingAlert,
    ctx: AuthContext,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Record a device up/down status change.

    Updates the in-memory ping status table (read by GET /devices) and
    inserts an alert row (ping_down or ping_up).

    Args:
        body: Ping alert payload from the agent.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"ok": True, "alert_id": <id>}`` on success.
    """
    site_id = ctx["site_id"]

    try:
        device = await _get_or_create_device(db, site_id, body.hostname, body.ip)

        # Suppress status updates and alerts while device is in maintenance mode
        if device.maintenance:
            logger.debug(
                "[PING] %s/%s in maintenance — alert suppressed", site_id, body.hostname
            )
            return {"ok": True, "alert_id": None, "suppressed": True}

        # Persist live status to DB so it survives serverless restarts
        await update_device_status(
            db=db,
            site_id=site_id,
            hostname=body.hostname,
            ip=body.ip,
            status=body.status,
            last_seen=body.timestamp,
        )

        alert_type = Alert.PING_DOWN if body.status == "down" else Alert.PING_UP
        message    = (
            f"{body.hostname} ({body.ip}) went {body.status.upper()} "
            f"at {body.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

        alert = Alert(device_id=device.id, alert_type=alert_type, message=message)
        db.add(alert)
        await db.commit()
        await db.refresh(alert)

    except Exception as exc:
        await db.rollback()
        logger.error("[PING] DB error for %s/%s: %s", site_id, body.hostname, exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "[PING] %s/%s (%s) → %s",
        site_id, body.hostname, body.ip, body.status.upper(),
    )

    # ── Fire alert email in background (ping_down only) ───────────────────────
    if body.status == "down":
        user_id = ctx["user_id"]

        async def _email_task() -> None:
            to_email  = await _get_user_email(user_id)
            site_name = await _get_site_name(db, site_id)
            await send_ping_down_alert(
                to_email=to_email or "",
                site_name=site_name,
                site_id=site_id,
                hostname=body.hostname,
                ip=body.ip,
                timestamp=body.timestamp,
            )

        background_tasks.add_task(_email_task)

    return {"ok": True, "alert_id": alert.id}


# ── POST /alerts/config-change ────────────────────────────────────────────────

@router.post("/alerts/config-change", status_code=status.HTTP_201_CREATED)
async def config_change_alert(
    body: ConfigChange,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Record a configuration change detected by the agent.

    Looks up the previous config by old_hash to compute a unified diff,
    then inserts a ConfigDiff row and a config_change Alert row.

    Args:
        body: Config change payload from the agent.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"ok": True, "diff_id": <id>, "alert_id": <id>}`` on success.
    """
    site_id = ctx["site_id"]

    try:
        device = await _get_or_create_device(db, site_id, body.hostname, ip="")

        # Fetch old config text by hash so we can compute a diff
        old_config_result = await db.execute(
            select(Config.config_text)
            .where(Config.device_id == device.id, Config.config_hash == body.old_hash)
            .order_by(Config.pulled_at.desc())
            .limit(1)
        )
        old_text = old_config_result.scalar_one_or_none() or ""

        diff_text = _compute_diff(old_text, body.new_config, body.hostname) if old_text else (
            f"(Previous config with hash {body.old_hash[:12]}… not found in history. "
            f"New hash: {body.new_hash[:12]}…)"
        )

        diff_row = ConfigDiff(
            device_id=device.id,
            old_hash=body.old_hash,
            new_hash=body.new_hash,
            diff_text=diff_text,
        )
        db.add(diff_row)
        await db.flush()

        alert = Alert(
            device_id=device.id,
            alert_type=Alert.CONFIG_CHANGE,
            message=(
                f"Config changed on {body.hostname}: "
                f"{body.old_hash[:12]}… → {body.new_hash[:12]}…"
            ),
        )
        db.add(alert)
        await db.commit()
        await db.refresh(diff_row)
        await db.refresh(alert)

    except Exception as exc:
        await db.rollback()
        logger.error("[CONFIG-CHANGE] DB error for %s/%s: %s", site_id, body.hostname, exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "[CONFIG-CHANGE] %s/%s  %s → %s",
        site_id, body.hostname, body.old_hash[:12], body.new_hash[:12],
    )
    return {"ok": True, "diff_id": diff_row.id, "alert_id": alert.id}


# ── GET /alerts ───────────────────────────────────────────────────────────────

@router.get("/alerts", response_model=list[AlertResponse])
async def list_alerts(
    ctx: AuthContext,
    hostname:   Optional[str] = Query(None, description="Filter by device hostname"),
    alert_type: Optional[str] = Query(None, description="Filter by alert type: ping_down, ping_up, config_change"),
    db: AsyncSession = Depends(get_db),
) -> list[AlertResponse]:
    """
    Return the last 50 alerts for the authenticated site.

    Optionally filter by hostname and/or alert_type.

    Args:
        ctx: Auth context with site_id.
        hostname: Optional hostname filter.
        alert_type: Optional alert_type filter.
        db: Async database session.

    Returns:
        List of up to 50 AlertResponse objects, newest first.
    """
    site_id = ctx["site_id"]

    query = (
        select(Alert, Device.hostname)
        .join(Device, Alert.device_id == Device.id)
        .where(Device.site_id == site_id)
        .order_by(Alert.created_at.desc())
        .limit(50)
    )

    if hostname:
        query = query.where(Device.hostname == hostname)
    if alert_type:
        query = query.where(Alert.alert_type == alert_type)

    result = await db.execute(query)
    rows   = result.all()

    return [
        AlertResponse(
            id=alert.id,
            alert_type=alert.alert_type,
            message=alert.message,
            created_at=alert.created_at,
            hostname=hn,
        )
        for alert, hn in rows
    ]
