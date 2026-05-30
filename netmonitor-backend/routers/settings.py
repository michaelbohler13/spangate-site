"""
routers/settings.py — SpanGate Network Monitor Backend
User-configurable settings stored in nm_profiles.

Routes
------
GET  /api/v1/settings
    Return current settings for the authenticated user.

PATCH /api/v1/settings
    Update one or more settings fields.

Supported settings
------------------
alert_email : Optional[str]
    Override email address for ping-down alert emails.
    When blank / NULL the backend falls back to the user's Supabase auth email.
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from supabase import Client, create_client

from auth import AuthContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Settings"])

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


# ── Schemas ───────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    """
    PATCH body — only fields that are present are updated.
    Pass an empty string to clear a field (stores NULL in DB).
    Pass None / omit the field to leave it unchanged.
    """

    alert_email: Optional[str] = None

    @field_validator("alert_email")
    @classmethod
    def validate_alert_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None          # not in body — skip
        v = v.strip()
        if v == "":
            return ""            # explicit clear — store NULL
        parts = v.split("@")
        if len(parts) != 2 or "." not in parts[1] or len(v) > 255:
            raise ValueError("Invalid email address")
        return v.lower()


# ── GET /settings ─────────────────────────────────────────────────────────────

@router.get("/settings", status_code=status.HTTP_200_OK)
async def get_settings(ctx: AuthContext) -> dict:
    """
    Return current user settings.

    Returns:
        dict with key ``alert_email`` (str, may be empty).
    """
    client = _get_supabase()
    try:
        result = (
            client.table("nm_profiles")
            .select("alert_email")
            .eq("id", ctx["user_id"])
            .single()
            .execute()
        )
        data = result.data or {}
    except Exception as exc:
        logger.warning("[SETTINGS] Failed to fetch settings for %s: %s", ctx["user_id"], exc)
        data = {}

    return {"alert_email": data.get("alert_email") or ""}


# ── PATCH /settings ───────────────────────────────────────────────────────────

@router.patch("/settings", status_code=status.HTTP_200_OK)
async def update_settings(body: SettingsUpdate, ctx: AuthContext) -> dict:
    """
    Update one or more settings fields for the authenticated user.

    Only fields explicitly included in the request body are written.
    Sending an empty string clears the field (stores NULL).

    Returns:
        ``{"ok": True}`` on success.
    """
    client = _get_supabase()

    updates: dict = {}

    if body.alert_email is not None:
        # Store None (NULL) for an empty string — "use account email" semantics
        updates["alert_email"] = body.alert_email or None

    if not updates:
        return {"ok": True}

    try:
        client.table("nm_profiles") \
              .update(updates) \
              .eq("id", ctx["user_id"]) \
              .execute()
        logger.info("[SETTINGS] Updated %s for user %s", list(updates.keys()), ctx["user_id"])
    except Exception as exc:
        logger.error("[SETTINGS] Failed to update settings for %s: %s", ctx["user_id"], exc)
        raise HTTPException(status_code=500, detail="Failed to save settings")

    return {"ok": True}
