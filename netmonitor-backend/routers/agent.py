"""
routers/agent.py — SpanGate Network Monitor Backend
Agent heartbeat endpoint.

Routes
------
POST /api/v1/agent/heartbeat — record agent liveness + device counts
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from supabase import Client

from auth import AuthContext
from database import get_supabase
from models import HeartbeatRequest

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Agent"])

_MAX_VERSION_LEN = 32


@router.post("/agent/heartbeat", status_code=status.HTTP_200_OK)
async def heartbeat(
    body: HeartbeatRequest,
    ctx: AuthContext,
    x_agent_version: Annotated[str | None, Header()] = None,
    db: Client = Depends(get_supabase),
) -> dict:
    """
    Record a heartbeat from the monitoring agent.

    Called every 5 minutes by the agent's heartbeat loop.  The dashboard
    uses the latest heartbeat timestamp to show whether an agent is alive,
    and the device counts to render the summary header row.

    The X-Agent-Version header is stored so the dashboard can surface
    version mismatches across multi-site deployments.

    Returns 200 on success.
    """
    agent_version = (x_agent_version or "unknown")[:_MAX_VERSION_LEN]

    row = {
        "user_id":       ctx["user_id"],
        "api_key_id":    ctx["api_key_id"],
        # Use site_name from the payload (the agent knows its own site name).
        # This may differ from ctx["site_name"] if the operator renamed the
        # site in config.yaml; we store both implicitly via the api_key_id FK.
        "site_name":     body.site_name,
        "device_count":  body.device_count,
        "devices_up":    body.devices_up,
        "devices_down":  body.devices_down,
        "agent_version": agent_version,
        "agent_ts":      body.timestamp.isoformat(),
    }

    try:
        db.table("nm_heartbeats").insert(row).execute()
    except Exception as exc:
        logger.error(
            "[HB] DB insert failed for %s: %s",
            body.site_name, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store heartbeat",
        )

    logger.debug(
        "[HB] %s — %d/%d up  agent_v=%s",
        body.site_name, body.devices_up, body.device_count, agent_version,
    )
    return {"ok": True}
