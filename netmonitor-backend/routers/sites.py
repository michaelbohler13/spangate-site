"""
routers/sites.py — SpanGate Network Monitor Backend
Multi-site support — site management and global summary.

Uses Supabase JWT auth (not API-key auth) so the logged-in user can
manage all their sites from a single authenticated session.

Routes
------
GET  /api/v1/sites          — list all sites owned by the JWT user
POST /api/v1/sites          — create a new site (plan limit enforced)
PATCH /api/v1/sites/{id}    — rename a site
DELETE /api/v1/sites/{id}   — delete a site (cannot delete the last one)
GET  /api/v1/sites/summary  — heartbeat + device-count card data for the
                              "All Sites" global view (JWT auth)
"""

import asyncio
import logging
import os
import secrets
import uuid as _uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import Client, create_client

from database import get_db
from models import AgentHeartbeat

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Sites"])

_supabase: Client | None = None

# ── Plan limits ───────────────────────────────────────────────────────────────

PLAN_SITE_LIMITS: dict[str, int] = {
    "free":       1,
    "pro":        5,
    "enterprise": 25,
    "msp":        999,
}


def _get_supabase() -> Client:
    """Return a cached Supabase service-role client."""
    global _supabase
    if _supabase is None:
        _supabase = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
    return _supabase


# ── JWT auth dependency ───────────────────────────────────────────────────────

async def verify_supabase_jwt(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """
    Validate a Supabase session JWT sent by the dashboard frontend.

    The token comes from ``supabase.auth.getSession()`` and is passed
    as ``Authorization: Bearer <jwt>``.

    Returns:
        dict with ``user_id`` and ``email``.

    Raises:
        HTTP 401 on any validation failure.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    jwt = authorization.removeprefix("Bearer ").strip()
    client = _get_supabase()
    try:
        response = await asyncio.to_thread(client.auth.get_user, jwt)
        if not response or not response.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session token",
            )
        return {"user_id": response.user.id, "email": response.user.email or ""}
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("[SITES] JWT verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
        )


JwtUser = Annotated[dict, Depends(verify_supabase_jwt)]


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateSiteBody(BaseModel):
    site_name: str


class RenameSiteBody(BaseModel):
    site_name: str


# ── GET /sites ────────────────────────────────────────────────────────────────

@router.get("/sites", status_code=status.HTTP_200_OK)
async def list_sites(user: JwtUser) -> list:
    """
    Return all sites owned by the authenticated user.

    Includes the per-site api_key so the dashboard can switch sites
    without an additional round-trip.
    """
    client = _get_supabase()
    try:
        result = (
            client.table("sites")
            .select("id, site_name, api_key, plan, created_at")
            .eq("owner_user_id", user["user_id"])
            .order("created_at", desc=False)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.error("[SITES] list_sites failed for %s: %s", user["user_id"], exc)
        raise HTTPException(status_code=500, detail="Failed to fetch sites")


# ── GET /sites/summary ────────────────────────────────────────────────────────

@router.get("/sites/summary", status_code=status.HTTP_200_OK)
async def sites_summary(
    user: JwtUser,
    db: AsyncSession = Depends(get_db),
) -> list:
    """
    Return heartbeat + device-count summary for every site owned by the user.

    Used by the "All Sites" global card wall.  One row per site; enriched
    with live agent health data from the agent_heartbeat table.

    Returns:
        List of site summary dicts, one per owned site.
    """
    client = _get_supabase()

    # ── Fetch all sites owned by this user ────────────────────────────────────
    try:
        result = (
            client.table("sites")
            .select("id, site_name, api_key, plan")
            .eq("owner_user_id", user["user_id"])
            .order("created_at", desc=False)
            .execute()
        )
        sites = result.data or []
    except Exception as exc:
        logger.error("[SITES] summary sites lookup failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch sites")

    if not sites:
        return []

    site_ids = [s["id"] for s in sites]

    # ── Fetch heartbeats for all sites in one query ───────────────────────────
    hb_result = await db.execute(
        select(AgentHeartbeat).where(AgentHeartbeat.site_id.in_(site_ids))
    )
    heartbeats: dict[str, AgentHeartbeat] = {
        hb.site_id: hb for hb in hb_result.scalars().all()
    }

    # ── Build summary rows ────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    summary = []

    for site in sites:
        sid = site["id"]
        hb: Optional[AgentHeartbeat] = heartbeats.get(sid)

        agent_online    = False
        last_seen_iso   = None
        agent_version   = None
        cpu_percent     = None
        mem_percent     = None
        temp_celsius    = None
        agent_model     = None
        device_count    = 0
        devices_up      = 0
        devices_down    = 0

        if hb is not None:
            last_seen_iso = hb.last_seen.isoformat()
            age_min       = (now - hb.last_seen).total_seconds() / 60
            agent_online  = age_min < 10          # offline if > 10 min since last HB
            agent_version = hb.agent_version
            cpu_percent   = hb.cpu_percent
            mem_percent   = hb.mem_percent
            temp_celsius  = hb.temp_celsius
            agent_model   = hb.agent_model
            device_count  = hb.device_count
            devices_up    = hb.devices_up
            devices_down  = hb.devices_down

        summary.append({
            "id":           sid,
            "site_name":    site["site_name"],
            "api_key":      site["api_key"],
            "plan":         site["plan"],
            "agent_online": agent_online,
            "last_seen":    last_seen_iso,
            "agent_version": agent_version,
            "device_count": device_count,
            "devices_up":   devices_up,
            "devices_down": devices_down,
            "cpu_percent":  cpu_percent,
            "mem_percent":  mem_percent,
            "temp_celsius": temp_celsius,
            "agent_model":  agent_model,
        })

    return summary


# ── POST /sites ───────────────────────────────────────────────────────────────

@router.post("/sites", status_code=status.HTTP_201_CREATED)
async def create_site(body: CreateSiteBody, user: JwtUser) -> dict:
    """
    Create a new monitoring site for the authenticated user.

    Enforces per-plan site limits (free=1, pro=5, enterprise=25).
    Generates a new random API key for the site.

    Args:
        body: ``{"site_name": "Branch Office"}``

    Returns:
        The newly created site row.

    Raises:
        HTTP 403: If the plan site limit would be exceeded.
    """
    site_name = body.site_name.strip()[:100]
    if not site_name:
        raise HTTPException(status_code=400, detail="site_name is required")

    client = _get_supabase()

    # ── Check plan limit ──────────────────────────────────────────────────────
    try:
        existing = (
            client.table("sites")
            .select("id, plan")
            .eq("owner_user_id", user["user_id"])
            .execute()
        )
        current_sites = existing.data or []
    except Exception as exc:
        logger.error("[SITES] create_site count check failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to check site count")

    current_count = len(current_sites)
    current_plan  = current_sites[0]["plan"] if current_sites else "free"
    site_limit    = PLAN_SITE_LIMITS.get(current_plan, 1)

    if current_count >= site_limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Your {current_plan} plan allows up to {site_limit} site(s). "
                "Upgrade to add more sites."
            ),
        )

    # ── Insert new site ───────────────────────────────────────────────────────
    new_site_id = str(_uuid.uuid4())
    new_api_key = "sg_" + secrets.token_hex(24)     # 51 chars total

    try:
        result = (
            client.table("sites")
            .insert({
                "id":            new_site_id,
                "owner_user_id": user["user_id"],
                "site_name":     site_name,
                "api_key":       new_api_key,
                "plan":          current_plan,
            })
            .execute()
        )
        new_site = result.data[0] if result.data else {
            "id": new_site_id, "site_name": site_name,
            "api_key": new_api_key, "plan": current_plan,
        }
    except Exception as exc:
        logger.error("[SITES] create_site insert failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to create site")

    logger.info("[SITES] Created site %s (%r) for user %s", new_site_id, site_name, user["user_id"])
    return new_site


# ── PATCH /sites/{site_id} ────────────────────────────────────────────────────

@router.patch("/sites/{site_id}", status_code=status.HTTP_200_OK)
async def rename_site(site_id: str, body: RenameSiteBody, user: JwtUser) -> dict:
    """
    Rename a site.  Only the owner can rename their own site.

    Args:
        site_id: The site's UUID string.
        body: ``{"site_name": "New Name"}``

    Returns:
        The updated site row.
    """
    site_name = body.site_name.strip()[:100]
    if not site_name:
        raise HTTPException(status_code=400, detail="site_name is required")

    client = _get_supabase()
    try:
        result = (
            client.table("sites")
            .update({"site_name": site_name})
            .eq("id", site_id)
            .eq("owner_user_id", user["user_id"])   # owner-only guard
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Site not found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[SITES] rename_site failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to rename site")


# ── DELETE /sites/{site_id} ───────────────────────────────────────────────────

@router.delete("/sites/{site_id}", status_code=status.HTTP_200_OK)
async def delete_site(site_id: str, user: JwtUser) -> dict:
    """
    Delete a site.

    Prevented if it is the user's only site.  Device/alert/config data
    for the site is cleaned by the daily retention job.

    Args:
        site_id: The site's UUID string.

    Returns:
        ``{"ok": True}``

    Raises:
        HTTP 400: If this is the user's only site.
        HTTP 404: If the site doesn't exist or belongs to another user.
    """
    client = _get_supabase()

    # ── Guard: cannot delete the last site ───────────────────────────────────
    try:
        existing = (
            client.table("sites")
            .select("id")
            .eq("owner_user_id", user["user_id"])
            .execute()
        )
        if len(existing.data or []) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete your only site. Create another site first.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[SITES] delete_site count check failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to check sites")

    # ── Delete ────────────────────────────────────────────────────────────────
    try:
        result = (
            client.table("sites")
            .delete()
            .eq("id", site_id)
            .eq("owner_user_id", user["user_id"])   # owner-only guard
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Site not found")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[SITES] delete_site failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to delete site")

    logger.info("[SITES] Deleted site %s for user %s", site_id, user["user_id"])
    return {"ok": True}
