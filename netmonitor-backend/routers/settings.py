"""
routers/settings.py — SpanGate Network Monitor Backend
User-configurable settings stored in nm_profiles.

Routes
------
GET  /api/v1/settings
    Return current settings for the authenticated user.
    SMTP password is never returned — smtp_password_set (bool) indicates
    whether one is stored.

PATCH /api/v1/settings
    Update settings fields.  Only fields present in the body are written.
    Sending an empty string clears a text field (stores NULL).
    Omitting smtp_password leaves the stored password unchanged.

POST /api/v1/settings/test-email
    Send a test email using the current delivery settings.
    Returns {"ok": True, "sent_to": "<address>"} on success.
    Returns 400 with a human-readable detail on delivery failure.
"""

import asyncio
import logging
import os
import smtplib
import socket
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from supabase import Client, create_client

from auth import AuthContext, require_admin_or_owner
from email_service import SmtpConfig, send_test_alert_email

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Settings"])

_supabase: Client | None = None

VALID_PROVIDERS = {"smtp"}


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
    PATCH body — only fields present in the request are written.
    Pass an empty string to clear a text field (stores NULL).
    Omit smtp_password to keep the current stored password.
    """

    alert_email:         Optional[str] = None
    email_provider:      Optional[str] = None
    smtp_host:           Optional[str] = None
    smtp_port:           Optional[int] = None
    smtp_user:           Optional[str] = None
    smtp_password:       Optional[str] = None   # omit = keep existing; "" = clear
    smtp_from:           Optional[str] = None
    default_ssh_user:    Optional[str] = None
    default_ssh_password: Optional[str] = None  # omit = keep existing; "" = clear

    @field_validator("alert_email")
    @classmethod
    def validate_alert_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v == "":
            return ""
        parts = v.split("@")
        if len(parts) != 2 or "." not in parts[1] or len(v) > 255:
            raise ValueError("Invalid email address")
        return v.lower()

    @field_validator("email_provider")
    @classmethod
    def validate_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if v not in VALID_PROVIDERS:
            raise ValueError(f"email_provider must be one of: {', '.join(VALID_PROVIDERS)}")
        return v

    @field_validator("smtp_port")
    @classmethod
    def validate_port(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        if not (1 <= v <= 65535):
            raise ValueError("smtp_port must be between 1 and 65535")
        return v

    @field_validator("smtp_host", "smtp_user", "smtp_from", "default_ssh_user")
    @classmethod
    def strip_text(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v is not None else None


# ── GET /settings ─────────────────────────────────────────────────────────────

@router.get("/settings", status_code=status.HTTP_200_OK)
async def get_settings(ctx: AuthContext) -> dict:
    """
    Return current settings for the authenticated user.

    SMTP password is not returned — smtp_password_set indicates
    whether a password is stored so the UI can show a placeholder.
    Accessible to admins and owners (viewers are blocked by role check).
    """
    require_admin_or_owner(ctx)
    # Use owner_user_id so admin members read the owner's settings profile
    owner_id = ctx.get("owner_user_id") or ctx["user_id"]
    client = _get_supabase()
    try:
        result = (
            client.table("nm_profiles")
            .select(
                "alert_email,email_provider,"
                "smtp_host,smtp_port,smtp_user,smtp_password,smtp_from,"
                "default_ssh_user,default_ssh_password"
            )
            .eq("id", owner_id)
            .single()
            .execute()
        )
        data = result.data or {}
    except Exception as exc:
        logger.warning("[SETTINGS] Failed to fetch settings for %s: %s", owner_id, exc)
        data = {}

    return {
        "alert_email":            data.get("alert_email") or "",
        "email_provider":         data.get("email_provider") or "smtp",
        "smtp_host":              data.get("smtp_host") or "",
        "smtp_port":              data.get("smtp_port") or 587,
        "smtp_user":              data.get("smtp_user") or "",
        "smtp_password_set":      bool(data.get("smtp_password")),
        "smtp_from":              data.get("smtp_from") or "",
        "default_ssh_user":       data.get("default_ssh_user") or "",
        "default_ssh_password_set": bool(data.get("default_ssh_password")),
    }


# ── PATCH /settings ───────────────────────────────────────────────────────────

@router.patch("/settings", status_code=status.HTTP_200_OK)
async def update_settings(body: SettingsUpdate, ctx: AuthContext) -> dict:
    """
    Update one or more settings fields for the authenticated user.

    Only fields explicitly included in the request body are written.
    Empty strings clear the field (stored as NULL).
    Omitting smtp_password leaves it unchanged.
    Accessible to admins and owners; admins write to the owner's profile.
    """
    require_admin_or_owner(ctx)
    owner_id = ctx.get("owner_user_id") or ctx["user_id"]
    client = _get_supabase()
    updates: dict = {}

    if body.alert_email is not None:
        updates["alert_email"] = body.alert_email or None

    if body.email_provider is not None:
        updates["email_provider"] = body.email_provider

    if body.smtp_host is not None:
        updates["smtp_host"] = body.smtp_host or None

    if body.smtp_port is not None:
        updates["smtp_port"] = body.smtp_port

    if body.smtp_user is not None:
        updates["smtp_user"] = body.smtp_user or None

    if body.smtp_password is not None:
        updates["smtp_password"] = body.smtp_password or None

    if body.smtp_from is not None:
        updates["smtp_from"] = body.smtp_from or None

    if body.default_ssh_user is not None:
        updates["default_ssh_user"] = body.default_ssh_user or None

    if body.default_ssh_password is not None:
        updates["default_ssh_password"] = body.default_ssh_password or None

    if not updates:
        return {"ok": True}

    try:
        client.table("nm_profiles") \
              .update(updates) \
              .eq("id", owner_id) \
              .execute()
        logger.info("[SETTINGS] Updated %s for user %s", list(updates.keys()), owner_id)
    except Exception as exc:
        logger.error("[SETTINGS] Failed to update settings for %s: %s", owner_id, exc)
        raise HTTPException(status_code=500, detail="Failed to save settings")

    return {"ok": True}


# ── POST /settings/test-email ─────────────────────────────────────────────────

@router.post("/settings/test-email", status_code=status.HTTP_200_OK)
async def test_email(ctx: AuthContext) -> dict:
    """
    Send a test alert email using the current delivery configuration.

    Fetches email_provider and SMTP settings from nm_profiles, then
    attempts delivery.  Returns a friendly error message on failure so
    the UI can display exactly what went wrong (auth failure, bad host, etc.).

    Returns:
        {"ok": True, "sent_to": "<address>"} on success.
    """
    require_admin_or_owner(ctx)
    owner_id = ctx.get("owner_user_id") or ctx["user_id"]
    client   = _get_supabase()

    # ── Fetch settings + alert email in one call ──────────────────────────────
    try:
        result = (
            client.table("nm_profiles")
            .select(
                "alert_email,email_provider,"
                "smtp_host,smtp_port,smtp_user,smtp_password,smtp_from"
            )
            .eq("id", owner_id)
            .single()
            .execute()
        )
        data = result.data or {}
    except Exception as exc:
        logger.warning("[SETTINGS] test-email: could not fetch settings for %s: %s", owner_id, exc)
        data = {}

    # ── Build SMTP config if applicable ──────────────────────────────────────
    smtp_config: SmtpConfig | None = None
    if data.get("email_provider") == "smtp":
        host = data.get("smtp_host", "").strip()
        if not host:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SMTP host is not configured. Save your SMTP settings first.",
            )
        smtp_config = SmtpConfig(
            host=host,
            port=int(data.get("smtp_port") or 587),
            user=data.get("smtp_user") or "",
            password=data.get("smtp_password") or "",
            from_addr=data.get("smtp_from") or data.get("smtp_user") or "",
        )

    # ── Resolve recipient address ─────────────────────────────────────────────
    to_email: str = data.get("alert_email") or ""
    if not to_email:
        try:
            from auth import _get_supabase as _auth_supabase
            auth_client = _auth_supabase()
            response = await asyncio.to_thread(
                auth_client.auth.admin.get_user_by_id, owner_id
            )
            to_email = (response.user.email or "") if (response and response.user) else ""
        except Exception as exc:
            logger.warning("[SETTINGS] test-email: could not fetch auth email: %s", exc)

    if not to_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No alert email address configured. Add one in Alert Notifications.",
        )

    # ── Attempt delivery ──────────────────────────────────────────────────────
    try:
        await send_test_alert_email(to_email=to_email, smtp_config=smtp_config)
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SMTP authentication failed. Check your username and password (use an App Password for Gmail).",
        )
    except smtplib.SMTPConnectError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not connect to SMTP server: {exc}",
        )
    except (socket.gaierror, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"SMTP host not reachable: {exc}",
        )
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connection timed out. Check the SMTP host and port.",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Email delivery failed: {exc}",
        )

    logger.info("[SETTINGS] Test email sent to %s for user %s", to_email, ctx["user_id"])
    return {"ok": True, "sent_to": to_email}
