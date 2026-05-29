"""
auth.py — SpanGate Network Monitor Backend
API-key authentication dependency.

The agent sends:
    Authorization: Bearer <raw_api_key>

We validate the key by querying the `nm_profiles` table in Supabase for a row
where `api_key` matches.  On success we attach site_id to request state and
return an auth context dict for use in route handlers.  On failure we raise
HTTP 401.

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

    Looks up the raw key in the ``nm_profiles`` table (``api_key`` column).
    On success, attaches ``site_id`` and ``user_id`` to ``request.state``
    and returns an auth context dict.

    Args:
        request: FastAPI request (used to write state).
        authorization: Raw Authorization header value.

    Returns:
        dict with keys: user_id (str), site_id (str).

    Raises:
        HTTPException 401: If the header is missing, malformed, or the key
            is not found in the nm_profiles table.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Bearer <api_key>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_key = authorization.removeprefix("Bearer ").strip()

    try:
        client = _get_supabase()
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
        # Retry without it so auth never breaks on a missing optional column.
        logger.warning("Supabase auth lookup failed (%s) — retrying without plan", exc)
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
                profile["plan"] = "free"   # safe default until migration runs
        except Exception as exc2:
            logger.warning("Supabase auth retry also failed: %s", exc2)
            profile = None

    if not profile:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # site_id falls back to the user's profile id if not explicitly set
    site_id = profile.get("site_id") or profile["id"]
    user_id = profile["id"]
    plan    = profile.get("plan") or "free"

    # Attach to request state so middleware and other deps can read it
    request.state.user_id = user_id
    request.state.site_id = site_id
    request.state.plan    = plan

    return {"user_id": user_id, "site_id": site_id, "plan": plan}


# Convenience type alias — use in route signatures as:  ctx: AuthContext
AuthContext = Annotated[dict, Depends(verify_api_key)]
