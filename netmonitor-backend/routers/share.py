"""
routers/share.py — SpanGate Network Monitor Backend
Read-only share-link endpoints.

Routes
------
POST   /api/v1/site/share-token
    Generate (or regenerate) a cryptographically random view token for the
    authenticated site.  Requires owner API-key auth.  Stores the token in
    nm_profiles.view_token and returns it.

DELETE /api/v1/site/share-token
    Revoke the view token (sets nm_profiles.view_token to NULL).

GET    /api/v1/share/{token}
    No authentication required — the token itself IS the credential.
    Validates the token against nm_profiles.view_token, then returns a
    read-only snapshot of the site:
      - site name, plan
      - agent heartbeat / status
      - full device list (status only — no SSH credentials)
      - last 50 alerts
    Returns 404 if the token is not found or has been revoked.
"""

import logging
import os
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import Client, create_client

from auth import AuthContext
from database import get_db
from models import AgentHeartbeat, Alert, Device, DeviceConfig

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Share"])

# Lazy Supabase client (shared module-level singleton)
_supabase: Client | None = None


def _get_supabase() -> Client:
    """Return a cached Supabase service-role client."""
    global _supabase
    if _supabase is None:
        _supabase = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
    return _supabase


# ── Owner endpoints (require API-key auth) ────────────────────────────────────

@router.post(
    "/site/share-token",
    status_code=status.HTTP_200_OK,
    summary="Generate or regenerate a read-only view token",
)
async def generate_share_token(ctx: AuthContext) -> dict:
    """
    Create a new cryptographically random view token for the authenticated site.

    If a token already exists it is replaced (previous share links are
    immediately invalidated).  Store the returned token in the dashboard so the
    owner can copy the full shareable URL.

    Returns:
        {"view_token": "<token>"}
    """
    token  = secrets.token_urlsafe(32)
    client = _get_supabase()

    client.table("nm_profiles") \
          .update({"view_token": token}) \
          .eq("id", ctx["user_id"]) \
          .execute()

    logger.info("[SHARE] Token generated for site %s", ctx["site_id"])
    return {"view_token": token}


@router.delete(
    "/site/share-token",
    status_code=status.HTTP_200_OK,
    summary="Revoke the read-only view token",
)
async def revoke_share_token(ctx: AuthContext) -> dict:
    """
    Set nm_profiles.view_token to NULL, immediately invalidating any existing
    share links for this site.

    Returns:
        {"ok": True}
    """
    client = _get_supabase()

    client.table("nm_profiles") \
          .update({"view_token": None}) \
          .eq("id", ctx["user_id"]) \
          .execute()

    logger.info("[SHARE] Token revoked for site %s", ctx["site_id"])
    return {"ok": True}


@router.get(
    "/site/share-token",
    status_code=status.HTTP_200_OK,
    summary="Check whether a view token exists for this site",
)
async def get_share_token_status(ctx: AuthContext) -> dict:
    """
    Return whether a view token exists without exposing the token itself.
    Used by the Settings page to show/hide the share link UI.

    Returns:
        {"has_token": bool, "view_token": str | None}
    """
    client = _get_supabase()
    try:
        result = (
            client.table("nm_profiles")
            .select("view_token")
            .eq("id", ctx["user_id"])
            .single()
            .execute()
        )
        token = result.data.get("view_token") if result.data else None
    except Exception as exc:
        logger.warning("[SHARE] Failed to fetch token status: %s", exc)
        token = None

    return {"has_token": token is not None, "view_token": token}


# ── Public read-only endpoint (no auth — token IS the credential) ─────────────

@router.get(
    "/share/{token}",
    status_code=status.HTTP_200_OK,
    summary="Read-only dashboard snapshot for share-link visitors",
)
async def get_share_view(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return a read-only snapshot of a site identified by its view token.

    Never returns SSH usernames, passwords, or the site's API key.

    Args:
        token: The view token from the shareable URL.
        db:    Async DB session.

    Returns:
        Dict with site_name, plan, heartbeat, devices, alerts.

    Raises:
        404 if token is unknown or has been revoked.
    """
    # ── Validate token ────────────────────────────────────────────────────────
    client = _get_supabase()
    try:
        result = (
            client.table("nm_profiles")
            .select("site_id, site_name, plan")
            .eq("view_token", token)
            .single()
            .execute()
        )
        profile = result.data
    except Exception as exc:
        logger.debug("[SHARE] Token lookup failed: %s", exc)
        profile = None

    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid or expired share link.",
        )

    site_id   = profile["site_id"]
    site_name = profile.get("site_name") or "Network Monitor"

    # ── Device configs (dashboard list — for group_name) ──────────────────────
    dc_result = await db.execute(
        select(DeviceConfig)
        .where(DeviceConfig.site_id == site_id)
        .order_by(DeviceConfig.group_name.nullslast(), DeviceConfig.hostname)
    )
    device_configs = {dc.hostname: dc for dc in dc_result.scalars().all()}

    # ── Live device status ────────────────────────────────────────────────────
    dev_result = await db.execute(
        select(Device)
        .where(Device.site_id == site_id)
        .order_by(Device.hostname)
    )
    live_devices = {d.hostname: d for d in dev_result.scalars().all()}

    # Merge: every configured device gets a row; status filled from live table
    devices_out = []
    for hostname, dc in device_configs.items():
        live = live_devices.get(hostname)
        devices_out.append({
            "hostname":    dc.hostname,
            "ip":          dc.ip,
            "vendor":      dc.vendor,
            "group_name":  dc.group_name,
            "ping_enabled": dc.ping_enabled,
            "status":      live.status      if live else "unknown",
            "last_seen":   live.last_seen.isoformat() if (live and live.last_seen) else None,
            "maintenance": live.maintenance if live else False,
        })

    # ── Recent alerts (last 50) ───────────────────────────────────────────────
    alert_result = await db.execute(
        select(Alert, Device.hostname)
        .join(Device, Alert.device_id == Device.id)
        .where(Device.site_id == site_id)
        .order_by(Alert.created_at.desc())
        .limit(50)
    )
    alerts_out = [
        {
            "id":         a.id,
            "alert_type": a.alert_type,
            "message":    a.message,
            "created_at": a.created_at.isoformat(),
            "hostname":   hostname,
        }
        for a, hostname in alert_result.all()
    ]

    # ── Agent heartbeat ───────────────────────────────────────────────────────
    hb_result = await db.execute(
        select(AgentHeartbeat).where(AgentHeartbeat.site_id == site_id)
    )
    hb = hb_result.scalar_one_or_none()

    heartbeat_out: Optional[dict] = None
    if hb:
        heartbeat_out = {
            "last_seen":     hb.last_seen.isoformat(),
            "agent_version": hb.agent_version,
            "device_count":  hb.device_count,
            "devices_up":    hb.devices_up,
            "devices_down":  hb.devices_down,
            "cpu_percent":   hb.cpu_percent,
            "mem_percent":   hb.mem_percent,
            "temp_celsius":  hb.temp_celsius,
            "agent_model":   hb.agent_model,
        }

    return {
        "site_name": site_name,
        "plan":      profile.get("plan") or "free",
        "heartbeat": heartbeat_out,
        "devices":   devices_out,
        "alerts":    alerts_out,
    }
