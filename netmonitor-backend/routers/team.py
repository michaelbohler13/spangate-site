"""
routers/team.py — Team management endpoints

Owner-only routes (require role="owner"):
  GET    /team/members              — list all members + pending invites
  POST   /team/invite               — send an invite email / generate invite link
  PATCH  /team/members/{id}         — change role (admin ↔ viewer)
  DELETE /team/members/{id}         — revoke access

JWT-authenticated routes (Supabase access token, NOT the SpanGate API key):
  GET  /team/my-access              — return member_api_key(s) for the caller
  GET  /team/accept/{token}         — public — return invite info for display
  POST /team/accept/{token}         — accept an invite; returns member_api_key

Seat limits (total seats including owner):
  free:    1   (no team members)
  starter: 3
  pro:     10
"""

import asyncio
import logging
import os
import secrets
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext, _get_supabase, require_owner
from database import get_db
from models import SiteMember

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Team"])

# Total seats including the owner
# Free: 1 | Starter: 5 | Pro: 15 | Enterprise/MSP: unlimited
_SEAT_LIMITS: dict[str, int] = {
    "free":       1,
    "starter":    5,
    "pro":        15,
    "enterprise": 999999,
    "msp":        999999,
}

# Where the frontend is hosted (for invite links)
_FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://spangate.com")


# ── GET /team/members ─────────────────────────────────────────────────────────

@router.get("/team/members")
async def list_members(
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return all non-removed members for the site (pending + active).
    Owner only.

    Args:
        ctx: Auth context (must be owner).
        db: Async database session.

    Returns:
        ``{"members": [...], "seat_limit": N, "seats_used": N}``
    """
    require_owner(ctx)

    result = await db.execute(
        select(SiteMember)
        .where(
            SiteMember.site_id == ctx["site_id"],
            SiteMember.status  != "removed",
        )
        .order_by(SiteMember.invited_at)
    )
    members = result.scalars().all()

    plan       = ctx.get("plan", "free")
    seat_limit = _SEAT_LIMITS.get(plan, 1)

    return {
        "members": [
            {
                "id":           m.id,
                "invited_email": m.invited_email,
                "role":         m.role,
                "status":       m.status,
                "invited_at":   m.invited_at.isoformat(),
                "accepted_at":  m.accepted_at.isoformat() if m.accepted_at else None,
            }
            for m in members
        ],
        "seat_limit":  seat_limit,
        "seats_used":  len(members) + 1,   # +1 for owner
    }


# ── POST /team/invite ─────────────────────────────────────────────────────────

class InviteBody(BaseModel):
    email: str = Field(..., max_length=255)
    role:  str = Field("viewer", pattern="^(admin|viewer)$")


@router.post("/team/invite", status_code=201)
async def invite_member(
    body: InviteBody,
    ctx:  AuthContext,
    db:   AsyncSession = Depends(get_db),
) -> dict:
    """
    Create a pending invite for an email address.

    Checks seat limits before creating.  Tries to send an invite email via the
    site owner's configured SMTP.  Always returns ``invite_url`` so the owner
    can share the link manually if email is not configured.

    Args:
        body: Email address and role.
        ctx: Auth context (must be owner).
        db: Async database session.

    Returns:
        ``{"member_id": N, "invite_url": "...", "email_sent": bool}``

    Raises:
        HTTPException 402: Seat limit reached for this plan.
        HTTPException 409: Email already has an active/pending invite.
    """
    require_owner(ctx)

    # ── Seat limit check ──────────────────────────────────────────────────────
    plan       = ctx.get("plan", "free")
    seat_limit = _SEAT_LIMITS.get(plan, 1)

    count_result = await db.execute(
        select(func.count(SiteMember.id)).where(
            SiteMember.site_id == ctx["site_id"],
            SiteMember.status  != "removed",
        )
    )
    member_count = count_result.scalar() or 0
    if member_count + 1 >= seat_limit:   # +1 for owner
        raise HTTPException(
            status_code=402,
            detail=(
                f"Your {plan.title()} plan allows {seat_limit} total seat(s) "
                f"(including you). Upgrade to invite more team members."
            ),
        )

    # ── Duplicate check ───────────────────────────────────────────────────────
    email = body.email.lower().strip()
    dup = await db.execute(
        select(SiteMember).where(
            SiteMember.site_id      == ctx["site_id"],
            SiteMember.invited_email == email,
            SiteMember.status       != "removed",
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="This email already has a pending or active invitation.",
        )

    # ── Create the invite row ─────────────────────────────────────────────────
    invite_token = secrets.token_urlsafe(32)
    row = SiteMember(
        site_id       = ctx["site_id"],
        owner_user_id = ctx["user_id"],
        invited_email = email,
        role          = body.role,
        invite_token  = invite_token,
        status        = "pending",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    invite_url = f"{_FRONTEND_URL}/netmonitor/accept-invite.html?token={invite_token}"

    # ── Try to send invite email ──────────────────────────────────────────────
    email_sent  = False
    email_error: Optional[str] = None
    try:
        email_sent = await _send_invite_email(ctx["site_id"], email, body.role, invite_url)
    except Exception as exc:
        logger.warning("[TEAM] Could not send invite email to %s: %s", email, exc)
        email_error = str(exc)

    logger.info(
        "[TEAM] Invited %s as %s — site=%s email_sent=%s",
        email, body.role, ctx["site_id"], email_sent,
    )
    return {
        "member_id":   row.id,
        "invite_url":  invite_url,
        "email_sent":  email_sent,
        "email_error": email_error,
    }


async def _send_invite_email(site_id: str, to_email: str, role: str, invite_url: str) -> bool:
    """
    Send an invite email using the site owner's configured SMTP.

    Returns True if the email was sent successfully, False if SMTP is not configured.
    Raises on SMTP delivery errors.
    """
    sb     = _get_supabase()
    result = sb.table("nm_profiles").select(
        "site_name, smtp_host, smtp_port, smtp_user, smtp_password, smtp_from"
    ).eq("site_id", site_id).single().execute()

    if not result.data:
        return False

    d = result.data
    if not d.get("smtp_host") or not d.get("smtp_user"):
        return False

    site_name = d.get("site_name") or "SpanGate Network Monitor"
    from_addr = d.get("smtp_from") or d["smtp_user"]
    role_label = role.title()

    subject   = f"You're invited to {site_name} on SpanGate"
    html_body = f"""
<html><body style="font-family:sans-serif;background:#111827;color:#e5e7eb;padding:32px;">
  <div style="max-width:520px;margin:0 auto;background:#1f2937;border-radius:12px;padding:28px 32px;">
    <h2 style="color:#5b8dee;margin:0 0 16px;">SpanGate Network Monitor</h2>
    <p style="margin:0 0 12px;">You've been invited to join <strong>{site_name}</strong>
       as a <strong>{role_label}</strong>.</p>
    <p style="margin:0 0 24px;color:#9ca3af;font-size:0.88rem;">
      As a {role_label} you can
      {'view and manage devices, request backups, and update settings.'
       if role == 'admin' else 'view device status and alerts in read-only mode.'}</p>
    <a href="{invite_url}"
       style="display:inline-block;background:#5b8dee;color:#fff;font-weight:600;
              padding:11px 28px;border-radius:8px;text-decoration:none;font-size:0.95rem;">
      Accept Invitation
    </a>
    <p style="margin:24px 0 0;font-size:0.75rem;color:#6b7280;">
      If you didn't expect this invitation, you can safely ignore this email.
    </p>
  </div>
</body></html>
"""
    msg              = MIMEMultipart("alternative")
    msg["Subject"]   = subject
    msg["From"]      = from_addr
    msg["To"]        = to_email
    msg.attach(MIMEText(html_body, "html"))

    def _smtp_send() -> None:
        ctx_ssl = ssl.create_default_context()
        port    = int(d.get("smtp_port") or 587)
        with smtplib.SMTP(d["smtp_host"], port, timeout=15) as srv:
            srv.ehlo()
            srv.starttls(context=ctx_ssl)
            srv.login(d["smtp_user"], d.get("smtp_password") or "")
            srv.sendmail(from_addr, [to_email], msg.as_string())

    await asyncio.to_thread(_smtp_send)
    return True


# ── PATCH /team/members/{member_id} ──────────────────────────────────────────

class MemberUpdateBody(BaseModel):
    role: str = Field(..., pattern="^(admin|viewer)$")


@router.patch("/team/members/{member_id}")
async def update_member_role(
    member_id: int,
    body:      MemberUpdateBody,
    ctx:       AuthContext,
    db:        AsyncSession = Depends(get_db),
) -> dict:
    """
    Change a member's role between admin and viewer.  Owner only.

    Args:
        member_id: ID of the site_members row.
        body: New role.
        ctx: Auth context (must be owner).
        db: Async database session.

    Returns:
        ``{"ok": True, "role": "<new role>"}``

    Raises:
        HTTPException 404: Member not found.
    """
    require_owner(ctx)

    result = await db.execute(
        select(SiteMember).where(
            SiteMember.id      == member_id,
            SiteMember.site_id == ctx["site_id"],
        )
    )
    member = result.scalar_one_or_none()
    if not member or member.status == "removed":
        raise HTTPException(status_code=404, detail="Member not found.")

    member.role = body.role
    await db.commit()
    logger.info("[TEAM] Member #%d role → %s — site=%s", member_id, body.role, ctx["site_id"])
    return {"ok": True, "role": body.role}


# ── DELETE /team/members/{member_id} ─────────────────────────────────────────

@router.delete("/team/members/{member_id}")
async def remove_member(
    member_id: int,
    ctx:       AuthContext,
    db:        AsyncSession = Depends(get_db),
) -> dict:
    """
    Remove a team member.  Their member_api_key is nulled immediately so
    any in-flight requests using that key will receive 401.  Owner only.

    Args:
        member_id: ID of the site_members row.
        ctx: Auth context (must be owner).
        db: Async database session.

    Returns:
        ``{"ok": True}``

    Raises:
        HTTPException 404: Member not found.
    """
    require_owner(ctx)

    result = await db.execute(
        select(SiteMember).where(
            SiteMember.id      == member_id,
            SiteMember.site_id == ctx["site_id"],
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found.")

    member.status         = "removed"
    member.member_api_key = None   # revoke immediately
    await db.commit()
    logger.info("[TEAM] Removed member #%d — site=%s", member_id, ctx["site_id"])
    return {"ok": True}


# ── GET /team/my-access ───────────────────────────────────────────────────────

@router.get("/team/my-access")
async def my_access(
    authorization: str | None = Header(default=None),
    db:            AsyncSession = Depends(get_db),
) -> dict:
    """
    Return all active site memberships for the Supabase-authenticated caller.

    The Authorization header must contain a **Supabase access token** (JWT),
    NOT a SpanGate API key.  The frontend calls this after a successful login
    when the user has no nm_profiles row.

    Args:
        authorization: Supabase JWT (``Bearer <access_token>``).
        db: Async database session.

    Returns:
        ``{"memberships": [{"site_id", "site_name", "role", "member_api_key"}, ...]}``

    Raises:
        HTTPException 401: Missing or invalid JWT.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token.")

    jwt_token = authorization.removeprefix("Bearer ").strip()

    try:
        sb        = _get_supabase()
        user_resp = await asyncio.to_thread(sb.auth.get_user, jwt_token)
        if not user_resp or not user_resp.user:
            raise HTTPException(status_code=401, detail="Invalid JWT token.")
        user_id = user_resp.user.id
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[TEAM] JWT verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid JWT token.")

    result = await db.execute(
        select(SiteMember).where(
            SiteMember.invited_user_id == user_id,
            SiteMember.status          == "active",
        )
    )
    members = result.scalars().all()

    if not members:
        return {"memberships": []}

    # Look up site names from nm_profiles (batch)
    site_ids      = list({m.site_id for m in members})
    profile_resp  = sb.table("nm_profiles").select("site_id, site_name").in_("site_id", site_ids).execute()
    site_names    = {
        p["site_id"]: (p.get("site_name") or "SpanGate Site")
        for p in (profile_resp.data or [])
    }

    return {
        "memberships": [
            {
                "site_id":        m.site_id,
                "site_name":      site_names.get(m.site_id, "SpanGate Site"),
                "role":           m.role,
                "member_api_key": m.member_api_key,
            }
            for m in members
        ]
    }


# ── GET /team/accept/{token} ──────────────────────────────────────────────────

@router.get("/team/accept/{token}")
async def get_invite_info(
    token: str,
    db:    AsyncSession = Depends(get_db),
) -> dict:
    """
    Public endpoint — return invite metadata so the accept page can display it.
    No authentication required.

    Args:
        token: The invite token from the email link.
        db: Async database session.

    Returns:
        ``{"invited_email": "...", "role": "...", "site_name": "..."}``

    Raises:
        HTTPException 404: Token not found or already accepted.
    """
    result = await db.execute(
        select(SiteMember).where(
            SiteMember.invite_token == token,
            SiteMember.status       == "pending",
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(
            status_code=404,
            detail="Invite not found. It may have already been accepted or has expired.",
        )

    sb          = _get_supabase()
    prof_result = sb.table("nm_profiles").select("site_name").eq("site_id", member.site_id).single().execute()
    site_name   = (prof_result.data or {}).get("site_name") or "SpanGate Site"

    return {
        "invited_email": member.invited_email,
        "role":          member.role,
        "site_name":     site_name,
    }


# ── POST /team/accept/{token} ─────────────────────────────────────────────────

@router.post("/team/accept/{token}", status_code=200)
async def accept_invite(
    token:         str,
    authorization: str | None = Header(default=None),
    db:            AsyncSession = Depends(get_db),
) -> dict:
    """
    Accept a pending invite.  The caller must provide a valid Supabase JWT.

    On success:
      - ``invited_user_id`` is set to the caller's Supabase user ID
      - A unique ``member_api_key`` is generated and stored
      - Status is set to "active"
      - The one-time ``invite_token`` is cleared

    Args:
        token: The invite token from the email link.
        authorization: Supabase JWT (``Bearer <access_token>``).
        db: Async database session.

    Returns:
        ``{"ok": True, "member_api_key": "mem_...", "role": "...", "site_id": "..."}``

    Raises:
        HTTPException 401: Missing or invalid JWT.
        HTTPException 404: Token not found or already accepted.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Must be logged in to accept an invite.")

    jwt_token = authorization.removeprefix("Bearer ").strip()

    try:
        sb        = _get_supabase()
        user_resp = await asyncio.to_thread(sb.auth.get_user, jwt_token)
        if not user_resp or not user_resp.user:
            raise HTTPException(status_code=401, detail="Invalid JWT token.")
        user_id = user_resp.user.id
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[TEAM] JWT verification failed on accept: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid JWT token.")

    result = await db.execute(
        select(SiteMember).where(
            SiteMember.invite_token == token,
            SiteMember.status       == "pending",
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(
            status_code=404,
            detail="Invite not found. It may have already been accepted or has expired.",
        )

    member_api_key          = "mem_" + secrets.token_urlsafe(32)
    member.invited_user_id  = user_id
    member.member_api_key   = member_api_key
    member.status           = "active"
    member.accepted_at      = datetime.now(timezone.utc)
    member.invite_token     = None    # one-time use — clear it
    await db.commit()

    logger.info(
        "[TEAM] Invite accepted: user=%s role=%s site=%s",
        user_id, member.role, member.site_id,
    )
    return {
        "ok":             True,
        "member_api_key": member_api_key,
        "role":           member.role,
        "site_id":        member.site_id,
    }
