"""
auth.py — SpanGate Network Monitor Backend
API-key authentication dependency.

Two-tier key lookup
-------------------
1. nm_profiles (site owners)
   The owner's api_key → role="owner"

2. site_members (admin / viewer team members)
   A member's member_api_key → role="admin" or "viewer"
   The member's owner_user_id is also injected so Settings and other
   endpoints that write to nm_profiles operate on the correct owner row.

The agent sends:
    Authorization: Bearer <raw_api_key>

Required Supabase table (run in SQL editor if not present):
    create table if not exists nm_profiles (
      id        uuid    primary key references auth.users(id) on delete cascade,
      api_key   text    unique,
      site_id   text    not null default gen_random_uuid()::text,
      site_name text
    );
    alter table nm_profiles enable row level security;
    create policy "owner" on nm_profiles for all using (auth.uid() = id);
"""

import logging
import os
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException, Request, status
from supabase import Client, create_client

load_dotenv()

logger = logging.getLogger(__name__)

# Supabase client is built lazily on first auth call so the server can
# start even when env vars are temporarily missing (e.g., during container init).
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


async def verify_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """
    FastAPI dependency that validates a Bearer API key against Supabase.

    Lookup order:
      1. nm_profiles.api_key         → role="owner"
      2. site_members.member_api_key → role="admin" or "viewer"

    Args:
        request: FastAPI request (used to write state).
        authorization: Raw Authorization header value.

    Returns:
        dict with keys: user_id, site_id, plan, role, owner_user_id.

    Raises:
        HTTPException 401: If the header is missing, malformed, or the key
            is not found in either table.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Bearer <api_key>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_key = authorization.removeprefix("Bearer ").strip()

    client = _get_supabase()

    # ── 1. Try sites.api_key (multi-site — primary lookup post-Phase 3) ───────
    try:
        site_res = (
            client.table("sites")
            .select("id, owner_user_id, plan")
            .eq("api_key", raw_key)
            .single()
            .execute()
        )
        if site_res.data:
            s       = site_res.data
            site_id = s["id"]
            user_id = s["owner_user_id"]
            plan    = s.get("plan") or "free"

            request.state.user_id       = user_id
            request.state.site_id       = site_id
            request.state.plan          = plan
            request.state.role          = "owner"
            request.state.owner_user_id = user_id

            return {
                "user_id":       user_id,
                "site_id":       site_id,
                "plan":          plan,
                "role":          "owner",
                "owner_user_id": user_id,
            }
    except Exception as exc:
        logger.debug("sites api_key lookup failed (will try nm_profiles): %s", exc)

    # ── 2. Try nm_profiles (legacy / backward compat) ─────────────────────────
    profile = None
    try:
        result = (
            client.table("nm_profiles")
            .select("id, site_id, plan")
            .eq("api_key", raw_key)
            .single()
            .execute()
        )
        profile = result.data
    except Exception as exc:
        # Fallback: plan column may not exist yet (migration pending).
        logger.warning("Supabase nm_profiles lookup failed (%s) — retrying without plan", exc)
        try:
            result = (
                client.table("nm_profiles")
                .select("id, site_id")
                .eq("api_key", raw_key)
                .single()
                .execute()
            )
            profile = result.data
            if profile:
                profile["plan"] = "free"
        except Exception as exc2:
            logger.debug("nm_profiles retry also failed: %s", exc2)
            profile = None

    if profile:
        site_id      = profile.get("site_id") or profile["id"]
        user_id      = profile["id"]
        plan         = profile.get("plan") or "free"

        request.state.user_id       = user_id
        request.state.site_id       = site_id
        request.state.plan          = plan
        request.state.role          = "owner"
        request.state.owner_user_id = user_id

        return {
            "user_id":       user_id,
            "site_id":       site_id,
            "plan":          plan,
            "role":          "owner",
            "owner_user_id": user_id,
        }

    # ── 2. Try site_members (admin / viewer) ─────────────────────────────────
    member = None
    try:
        mresult = (
            client.table("site_members")
            .select("id, site_id, role, status, owner_user_id")
            .eq("member_api_key", raw_key)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        member = mresult.data[0] if mresult.data else None
    except Exception as exc:
        logger.debug("site_members lookup failed: %s", exc)
        member = None

    if member:
        site_id        = member["site_id"]
        role           = member.get("role") or "viewer"
        owner_user_id  = member.get("owner_user_id") or ""

        request.state.user_id       = str(member["id"])
        request.state.site_id       = site_id
        request.state.plan          = "free"   # member uses owner's plan; look up if needed
        request.state.role          = role
        request.state.owner_user_id = owner_user_id

        return {
            "user_id":       str(member["id"]),
            "site_id":       site_id,
            "plan":          "free",
            "role":          role,
            "owner_user_id": owner_user_id,
        }

    # ── Neither matched ───────────────────────────────────────────────────────
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── Role helpers ──────────────────────────────────────────────────────────────

def require_owner(ctx: dict) -> None:
    """Raise HTTP 403 unless the caller is the site owner."""
    if ctx.get("role") != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the site owner can perform this action.",
        )


def require_admin_or_owner(ctx: dict) -> None:
    """Raise HTTP 403 unless the caller is an owner or admin."""
    if ctx.get("role") not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions. Admin or owner role required.",
        )


# Convenience type alias — use in route signatures as:  ctx: AuthContext
AuthContext = Annotated[dict, Depends(verify_api_key)]
