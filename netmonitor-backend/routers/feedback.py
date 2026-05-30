"""
feedback.py — SpanGate Feedback Router
POST /api/v1/feedback — public endpoint, no API key required.

Spam protection layers
-----------------------
1. Honeypot field ("website"): bots auto-fill every input including hidden
   fields; humans never see or touch it.  A non-empty value triggers a
   fake-success response so bots don't retry.
2. Server-side IP rate limit: max 3 submissions per IP per hour.
   The raw IP is hashed (SHA-256) before storage — no PII is kept.
3. Client-side rate limit: the JS widget enforces a 10-minute cooldown via
   localStorage so casual mis-clicks don't flood the backend.
"""

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from email_service import send_feedback_notification
from models import Feedback

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Feedback"])

RATE_LIMIT_MAX   = 3    # submissions allowed per IP within the window
RATE_LIMIT_HOURS = 1    # rolling window length in hours
MAX_NAME_LEN     = 100
MAX_MESSAGE_LEN  = 2000

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

VALID_SUBJECTS = {
    "General Feedback",
    "Bug Report",
    "Feature Request",
    "Help / Support",
    "Billing",
    "Other",
}


# ── Request schema ────────────────────────────────────────────────────────────

class FeedbackIn(BaseModel):
    name:    str
    email:   str
    subject: str = "General Feedback"
    message: str
    website:  str = ""   # honeypot — must always be empty for real users
    cf_token: str = ""   # Cloudflare Turnstile challenge token

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > MAX_NAME_LEN:
            raise ValueError(f"Name must be 1–{MAX_NAME_LEN} characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        parts = v.split("@")
        if not v or len(v) > 255 or len(parts) != 2 or "." not in parts[1]:
            raise ValueError("Invalid email address")
        return v

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, v: str) -> str:
        return v if v in VALID_SUBJECTS else "General Feedback"

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > MAX_MESSAGE_LEN:
            raise ValueError(f"Message must be 1–{MAX_MESSAGE_LEN} characters")
        return v


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _verify_turnstile(token: str, ip: str) -> bool:
    """
    Verify a Cloudflare Turnstile challenge token.

    Returns True (allow) in two safe-fallback cases:
      - TURNSTILE_SECRET env var not configured (dev/test environment)
      - Cloudflare's API is unreachable (fail open to avoid blocking real users)

    Returns False when Cloudflare explicitly says the token is invalid.
    """
    secret = os.environ.get("TURNSTILE_SECRET", "")
    if not secret:
        logger.warning("TURNSTILE_SECRET not set — skipping Turnstile check")
        return True

    if not token:
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                TURNSTILE_VERIFY_URL,
                data={"secret": secret, "response": token, "remoteip": ip},
            )
            result = resp.json()
            return bool(result.get("success", False))
    except Exception as exc:
        # Fail open — Cloudflare outage shouldn't block legitimate feedback
        logger.warning("Turnstile verification error (failing open): %s", exc)
        return True


def _hash_ip(ip: str) -> str:
    """One-way SHA-256 digest of a client IP — used for rate limiting only."""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:32]


def _client_ip(request: Request) -> str:
    """
    Extract the real client IP.
    Vercel injects the originating IP in X-Forwarded-For; the first entry is
    the client (subsequent entries are proxies).
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host or ""


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/feedback", status_code=201)
async def submit_feedback(
    payload: FeedbackIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Accept a feedback submission from a site visitor.

    Returns {"ok": true} on success or fake-success (honeypot case).
    Returns 429 if the IP has hit the rate limit.
    """
    # ── 1. Honeypot ───────────────────────────────────────────────────────────
    if payload.website:
        # Return a fake 201 so automated scanners don't learn the field is checked.
        logger.info("Feedback honeypot triggered — silent 201")
        return {"ok": True}

    # ── 2. Turnstile CAPTCHA ──────────────────────────────────────────────────
    client_ip = _client_ip(request)
    if not await _verify_turnstile(payload.cf_token, client_ip):
        raise HTTPException(status_code=400, detail="CAPTCHA verification failed. Please try again.")

    # ── 3. IP rate limit ──────────────────────────────────────────────────────
    ip_hash      = _hash_ip(client_ip)
    window_start = datetime.now(timezone.utc) - timedelta(hours=RATE_LIMIT_HOURS)

    count_result = await db.execute(
        select(func.count()).where(
            Feedback.ip_hash == ip_hash,
            Feedback.created_at >= window_start,
        )
    )
    count = count_result.scalar_one()

    if count >= RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again in an hour.",
        )

    # ── 3. Persist ────────────────────────────────────────────────────────────
    row = Feedback(
        name=payload.name,
        email=payload.email,
        subject=payload.subject,
        message=payload.message,
        ip_hash=ip_hash,
    )
    db.add(row)
    await db.commit()

    logger.info(
        "Feedback saved — %r <%s> subject=%r",
        payload.name,
        payload.email,
        payload.subject,
    )

    # Notify admin via email (fires after response is sent)
    background_tasks.add_task(
        send_feedback_notification,
        name=payload.name,
        email=payload.email,
        subject=payload.subject,
        message=payload.message,
    )

    return {"ok": True}
