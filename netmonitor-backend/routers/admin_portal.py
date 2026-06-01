"""
routers/admin_portal.py — SpanGate Network Monitor Backend
Internal admin operations portal.

Auth
----
Protected by ADMIN_SECRET environment variable — completely separate from
the site API key and Supabase JWT used by regular users.

POST /admin/auth returns a daily-rotating HMAC session token.
All other /admin/* routes require  Authorization: Bearer <token>.

Routes
------
POST   /admin/auth                      Verify admin secret → session token
GET    /admin/stats                     Aggregate stats for the ops dashboard
GET    /admin/users                     List all users with usage data
PATCH  /admin/users/{user_id}/status    suspend / ban / reinstate
PATCH  /admin/users/{user_id}/plan      Change subscription plan
PATCH  /admin/users/{user_id}/notes     Update internal admin notes
DELETE /admin/users/{user_id}           Hard delete (auth + all data)
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import Client, create_client

from database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin"])

# ── Plan pricing (for MRR estimate) ───────────────────────────────────────────

PLAN_MRR: dict[str, int] = {
    "starter":    199,
    "pro":        399,
    "enterprise": 999,
}

VALID_PLANS = ("free", "starter", "pro", "enterprise", "msp")


# ── Supabase client ───────────────────────────────────────────────────────────

_supabase: Client | None = None


def _get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        _supabase = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_KEY"],
        )
    return _supabase


# ── HMAC session token ────────────────────────────────────────────────────────

def _make_token(secret: str) -> str:
    """Generate a daily-rotating admin session token."""
    day = int(time.time() // 86400)
    msg = f"sg-admin-session:{day}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return f"sgadmin.{day}.{sig}"


def _verify_token(token: str, secret: str) -> bool:
    """Verify token — valid for today and yesterday (48-hour window)."""
    try:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "sgadmin":
            return False
        day = int(parts[1])
        sig = parts[2]
        today = int(time.time() // 86400)
        if day not in (today, today - 1):
            return False
        msg      = f"sg-admin-session:{day}".encode()
        expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


# ── Admin auth dependency ─────────────────────────────────────────────────────

async def _require_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency — validates the admin session token."""
    secret = os.environ.get("ADMIN_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin panel not configured (ADMIN_SECRET not set).",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authorization required.",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not _verify_token(token, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired admin token.",
        )


AdminAuth = Annotated[None, Depends(_require_admin)]


# ── Request / response models ─────────────────────────────────────────────────

class AuthBody(BaseModel):
    secret: str


class StatusBody(BaseModel):
    status: Literal["active", "suspended", "banned"]
    reason: str = ""


class PlanBody(BaseModel):
    plan: str


class NotesBody(BaseModel):
    notes: str


# ── POST /admin/auth ──────────────────────────────────────────────────────────

@router.post("/admin/auth", status_code=status.HTTP_200_OK)
async def admin_auth(body: AuthBody) -> dict:
    """
    Verify the admin secret and return a daily-rotating session token.

    Returns:
        {"token": "sgadmin.<day>.<sig>"}
    """
    secret = os.environ.get("ADMIN_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Admin panel not configured.")
    if not hmac.compare_digest(body.secret, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin secret.")
    logger.info("[ADMIN] Successful admin login")
    return {"token": _make_token(secret)}


# ── GET /admin/stats ──────────────────────────────────────────────────────────

@router.get("/admin/stats", status_code=status.HTTP_200_OK)
async def admin_stats(
    _: AdminAuth,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return aggregate stats for the ops dashboard stats bar.

    Includes user counts by status and plan, total devices, and estimated MRR.
    """
    client = _get_supabase()

    # ── Auth users (total + last-seen data) ───────────────────────────────────
    try:
        auth_users = await asyncio.to_thread(client.auth.admin.list_users)
        total_users = len(auth_users) if auth_users else 0
    except Exception as exc:
        logger.warning("[ADMIN] stats: could not list auth users: %s", exc)
        total_users = 0

    # ── nm_profiles: plan + account_status breakdown ──────────────────────────
    try:
        profiles_res = (
            client.table("nm_profiles")
            .select("plan, account_status")
            .execute()
        )
        profiles = profiles_res.data or []
    except Exception as exc:
        logger.warning("[ADMIN] stats: nm_profiles query failed: %s", exc)
        profiles = []

    by_status: dict[str, int] = {"active": 0, "suspended": 0, "banned": 0}
    by_plan:   dict[str, int] = {}
    mrr = 0
    for p in profiles:
        st   = p.get("account_status") or "active"
        plan = p.get("plan") or "free"
        by_status[st] = by_status.get(st, 0) + 1
        by_plan[plan] = by_plan.get(plan, 0) + 1
        mrr += PLAN_MRR.get(plan, 0)

    # ── Total devices monitored ───────────────────────────────────────────────
    try:
        dev_result = await db.execute(text("SELECT COUNT(*) FROM devices"))
        total_devices = dev_result.scalar_one() or 0
    except Exception:
        total_devices = 0

    # ── Total sites ───────────────────────────────────────────────────────────
    try:
        site_result = client.table("sites").select("id", count="exact").execute()
        total_sites = site_result.count or 0
    except Exception:
        total_sites = 0

    return {
        "total_users":   total_users,
        "by_status":     by_status,
        "by_plan":       by_plan,
        "total_devices": total_devices,
        "total_sites":   total_sites,
        "est_mrr":       mrr,
    }


# ── GET /admin/users ──────────────────────────────────────────────────────────

@router.get("/admin/users", status_code=status.HTTP_200_OK)
async def admin_list_users(
    _: AdminAuth,
    db: AsyncSession = Depends(get_db),
) -> list:
    """
    Return all users with usage data, enriched from:
      - Supabase auth (email, created_at, last_sign_in_at)
      - nm_profiles (plan, account_status, suspension_reason, admin_notes)
      - sites table (site count per user)
      - devices + agent_heartbeat (device count, last seen)
    """
    client = _get_supabase()

    # ── 1. Auth users ─────────────────────────────────────────────────────────
    try:
        auth_users_raw = await asyncio.to_thread(client.auth.admin.list_users)
        auth_map = {
            u.id: {
                "email":         u.email or "",
                "created_at":    u.created_at,
                "last_sign_in":  u.last_sign_in_at,
                "banned_at":     getattr(u, "banned_at", None),
            }
            for u in (auth_users_raw or [])
        }
    except Exception as exc:
        logger.error("[ADMIN] list_users: auth.admin.list_users failed: %s", exc)
        auth_map = {}

    # ── 2. nm_profiles ────────────────────────────────────────────────────────
    try:
        profiles_res = (
            client.table("nm_profiles")
            .select("id, plan, account_status, suspension_reason, admin_notes")
            .execute()
        )
        profile_map = {p["id"]: p for p in (profiles_res.data or [])}
    except Exception as exc:
        logger.warning("[ADMIN] list_users: nm_profiles failed: %s", exc)
        profile_map = {}

    # ── 3. Site counts per owner ──────────────────────────────────────────────
    try:
        sites_res = client.table("sites").select("owner_user_id").execute()
        site_counts: dict[str, int] = {}
        for s in (sites_res.data or []):
            uid = s["owner_user_id"]
            site_counts[uid] = site_counts.get(uid, 0) + 1
    except Exception as exc:
        logger.warning("[ADMIN] list_users: sites count failed: %s", exc)
        site_counts = {}

    # ── 4. Device counts + last seen per owner (single SQL) ───────────────────
    device_counts: dict[str, int] = {}
    last_seen_map: dict[str, str] = {}
    try:
        row_result = await db.execute(text("""
            SELECT
                s.owner_user_id,
                COUNT(DISTINCT d.id)          AS device_count,
                MAX(ah.last_seen)             AS last_agent_seen
            FROM sites s
            LEFT JOIN devices d ON d.site_id = s.id
            LEFT JOIN agent_heartbeat ah ON ah.site_id = s.id
            GROUP BY s.owner_user_id
        """))
        for row in row_result:
            uid = row.owner_user_id
            device_counts[uid] = row.device_count or 0
            if row.last_agent_seen:
                last_seen_map[uid] = row.last_agent_seen.isoformat()
    except Exception as exc:
        logger.warning("[ADMIN] list_users: device/heartbeat query failed: %s", exc)

    # ── 5. Combine ─────────────────────────────────────────────────────────────
    # Include all auth users + any in nm_profiles not in auth (edge case)
    all_ids = set(auth_map) | set(profile_map)
    result = []
    for uid in all_ids:
        auth  = auth_map.get(uid, {})
        prof  = profile_map.get(uid, {})
        result.append({
            "id":               uid,
            "email":            auth.get("email") or prof.get("id", ""),
            "created_at":       auth.get("created_at"),
            "last_sign_in":     auth.get("last_sign_in"),
            "plan":             prof.get("plan") or "free",
            "account_status":   prof.get("account_status") or "active",
            "suspension_reason":prof.get("suspension_reason") or "",
            "admin_notes":      prof.get("admin_notes") or "",
            "sites_count":      site_counts.get(uid, 0),
            "device_count":     device_counts.get(uid, 0),
            "last_agent_seen":  last_seen_map.get(uid),
        })

    return sorted(result, key=lambda u: u.get("created_at") or "", reverse=True)


# ── PATCH /admin/users/{user_id}/status ───────────────────────────────────────

@router.patch("/admin/users/{user_id}/status", status_code=status.HTTP_200_OK)
async def admin_set_status(
    user_id: str,
    body: StatusBody,
    _: AdminAuth,
) -> dict:
    """
    Set account status:  active | suspended | banned.

    suspended — blocked at API level, dashboard shows a message, agent stops
    banned    — Supabase auth banned + API blocked (hard, no login possible)
    active    — fully reinstated
    """
    client = _get_supabase()
    new_status = body.status
    reason     = body.reason.strip()

    # ── Update Supabase auth banned flag ──────────────────────────────────────
    try:
        supabase_banned = (new_status == "banned")
        await asyncio.to_thread(
            client.auth.admin.update_user_by_id,
            user_id,
            {"banned": supabase_banned},
        )
    except Exception as exc:
        logger.warning("[ADMIN] set_status: Supabase auth update failed for %s: %s", user_id, exc)

    # ── Update nm_profiles ────────────────────────────────────────────────────
    try:
        client.table("nm_profiles").update({
            "account_status":    new_status,
            "suspension_reason": reason if new_status != "active" else None,
            "status_changed_at": "now()",
        }).eq("id", user_id).execute()
    except Exception as exc:
        logger.error("[ADMIN] set_status: nm_profiles update failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to update account status.")

    logger.info("[ADMIN] User %s status set to %r (reason: %r)", user_id, new_status, reason)
    return {"ok": True, "user_id": user_id, "status": new_status}


# ── PATCH /admin/users/{user_id}/plan ────────────────────────────────────────

@router.patch("/admin/users/{user_id}/plan", status_code=status.HTTP_200_OK)
async def admin_set_plan(
    user_id: str,
    body: PlanBody,
    _: AdminAuth,
) -> dict:
    """
    Change a user's subscription plan.

    Updates nm_profiles.plan AND all sites.plan for this owner so that
    device limits, site limits, and seat limits all reflect the new plan
    immediately.
    """
    plan = body.plan.lower().strip()
    if plan not in VALID_PLANS:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Must be one of: {', '.join(VALID_PLANS)}")

    client = _get_supabase()

    try:
        # Update nm_profiles (legacy / fallback)
        client.table("nm_profiles").update({"plan": plan}).eq("id", user_id).execute()
        # Update all sites owned by this user
        client.table("sites").update({"plan": plan}).eq("owner_user_id", user_id).execute()
    except Exception as exc:
        logger.error("[ADMIN] set_plan: failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to update plan.")

    logger.info("[ADMIN] User %s plan changed to %r", user_id, plan)
    return {"ok": True, "user_id": user_id, "plan": plan}


# ── PATCH /admin/users/{user_id}/notes ───────────────────────────────────────

@router.patch("/admin/users/{user_id}/notes", status_code=status.HTTP_200_OK)
async def admin_set_notes(
    user_id: str,
    body: NotesBody,
    _: AdminAuth,
) -> dict:
    """Update internal admin notes for a user (never visible to the user)."""
    client = _get_supabase()
    try:
        client.table("nm_profiles").update({"admin_notes": body.notes}).eq("id", user_id).execute()
    except Exception as exc:
        logger.error("[ADMIN] set_notes: failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to save notes.")

    logger.info("[ADMIN] Notes updated for user %s", user_id)
    return {"ok": True}


# ── DELETE /admin/users/{user_id} ────────────────────────────────────────────

@router.delete("/admin/users/{user_id}", status_code=status.HTTP_200_OK)
async def admin_delete_user(
    user_id: str,
    _: AdminAuth,
) -> dict:
    """
    Hard-delete a user.

    Removes from Supabase auth (cascades to nm_profiles via ON DELETE CASCADE)
    and also removes sites and all associated device/alert/config data.
    This is IRREVERSIBLE.
    """
    client = _get_supabase()

    try:
        # Delete sites first (cascades to devices, alerts, heartbeats, configs)
        client.table("sites").delete().eq("owner_user_id", user_id).execute()
        # Delete nm_profiles
        client.table("nm_profiles").delete().eq("id", user_id).execute()
        # Delete Supabase auth user (cascades remaining)
        await asyncio.to_thread(client.auth.admin.delete_user, user_id)
    except Exception as exc:
        logger.error("[ADMIN] delete_user: failed for %s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {exc}")

    logger.info("[ADMIN] User %s DELETED", user_id)
    return {"ok": True, "deleted": user_id}
