"""
routers/agents.py — SpanGate Network Monitor Backend
Agent heartbeat endpoint.

Routes
------
POST /api/v1/agent/heartbeat
    Record agent liveness + device counts.
    Upserts a row into the agent_heartbeat table.
    Returns 200 OK.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from database import get_db
from schemas import Heartbeat
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
