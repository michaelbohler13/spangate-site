"""
routers/agents.py — SpanGate Network Monitor Backend
Agent heartbeat and status endpoints.

Routes
------
POST /api/v1/agent/heartbeat
    Record agent liveness + device counts.
    Upserts a row into the agent_heartbeat table.
    Returns 200 OK.

POST /api/v1/agent/ssh-status
    Record SSH pull success/failure for a device.
    Clears last_ssh_error on success; stores error message on failure.
    Returns 200 OK.
"""

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from database import get_db
from models import Device
from schemas import Heartbeat, SshStatusReport
from state import upsert_heartbeat

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Agent"])

_MAX_VERSION_LEN = 32


@router.post("/agent/heartbeat", status_code=status.HTTP_200_OK)
async def heartbeat(
    body: Heartbeat,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
    x_agent_version: Annotated[str | None, Header()] = None,
) -> dict:
    """
    Record a heartbeat from the monitoring agent.

    Called by the agent every 5 minutes. Upserts into the agent_heartbeat
    table so liveness data persists across serverless function restarts.

    Args:
        body: Heartbeat payload with site name and device counts.
        ctx: Auth context containing site_id.
        db: Async database session.
        x_agent_version: Agent version string from request header.

    Returns:
        ``{"ok": True}`` on success.
    """
    agent_version = (x_agent_version or "unknown")[:_MAX_VERSION_LEN]

    await upsert_heartbeat(
        db=db,
        site_id=ctx["site_id"],
        site_name=body.site_name,
        device_count=body.device_count,
        devices_up=body.devices_up,
        devices_down=body.devices_down,
        agent_version=agent_version,
    )

    logger.debug(
        "[HB] site=%s  %d/%d up  v=%s",
        body.site_name, body.devices_up, body.device_count, agent_version,
    )
    return {"ok": True}


@router.post("/agent/ssh-status", status_code=status.HTTP_200_OK)
async def ssh_status(
    body: SshStatusReport,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Record the result of an SSH config-pull attempt for a device.

    On success: clears last_ssh_error so the dashboard shows no warning.
    On failure: stores a human-readable error so the dashboard can display it.

    Auto-creates a Device row if one doesn't exist yet (same behaviour as
    the ping-alert endpoint).

    Args:
        body: SSH status payload from the agent.
        ctx: Auth context containing site_id.
        db: Async database session.

    Returns:
        ``{"ok": True}`` on success.
    """
    site_id = ctx["site_id"]

    result = await db.execute(
        select(Device).where(Device.site_id == site_id, Device.hostname == body.hostname)
    )
    device = result.scalar_one_or_none()

    if device is None:
        device = Device(site_id=site_id, hostname=body.hostname, ip="")
        db.add(device)
        await db.flush()
        logger.info("[SSH-STATUS] Auto-created device %s/%s", site_id, body.hostname)

    device.last_ssh_at = datetime.now(timezone.utc)
    if body.success:
        device.last_ssh_error = None
        logger.debug("[SSH-STATUS] %s/%s — success", site_id, body.hostname)
    else:
        device.last_ssh_error = body.error_detail or body.error_type or "SSH connection failed"
        logger.info(
            "[SSH-STATUS] %s/%s — %s: %s",
            site_id, body.hostname, body.error_type, body.error_detail,
        )

    await db.commit()
    return {"ok": True}
