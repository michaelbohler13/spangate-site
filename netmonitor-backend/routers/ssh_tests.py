"""
routers/ssh_tests.py — SSH credential test endpoints

Dashboard routes (Bearer SpanGate API key):
  POST /api/v1/test-ssh
      Queue an SSH connection test. Returns test_id for polling.

  GET  /api/v1/test-ssh/{test_id}
      Poll for test result (pending / success / failed / expired).

Agent routes (Bearer SpanGate API key):
  GET  /api/v1/agent/pending-ssh-tests
      Return pending tests for the agent to run.

  POST /api/v1/agent/ssh-test-result
      Agent posts the result of a completed test.

The test is executed by the on-premises agent (not the backend), because
the agent has LAN access to the monitored devices.  Results typically
arrive within 15 seconds of queueing.  Tests expire after 5 minutes.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from database import get_db
from models import SshCredentialProfile, SshTest

logger = logging.getLogger(__name__)
router = APIRouter(tags=["SSH Tests"])

_TEST_TTL = timedelta(minutes=5)


# ── POST /test-ssh ─────────────────────────────────────────────────────────────

class SshTestRequest(BaseModel):
    """SSH test parameters posted by the dashboard."""
    ip:                    str           = Field(..., max_length=253)
    vendor:                str           = Field("cisco",     max_length=100)
    device_type:           str           = Field("cisco_ios", max_length=100)
    ssh_username:          str           = Field("",          max_length=100)
    ssh_password:          str           = Field("",          max_length=255)
    ssh_port:              int           = Field(22, ge=1, le=65535)
    # Credential resolution helpers — backend fills in username/password when set
    credential_profile_id: Optional[int] = None   # use named profile
    use_site_default:      bool          = False   # use site-wide default SSH creds


@router.post("/test-ssh", status_code=202)
async def create_ssh_test(
    body: SshTestRequest,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Queue an SSH credential test for the agent to execute.

    Args:
        body: SSH connection parameters.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"test_id": <id>}`` — use this to poll GET /test-ssh/{test_id}.
    """
    # ── Resolve credentials ────────────────────────────────────────────────────
    ssh_username = body.ssh_username or ""
    ssh_password = body.ssh_password or ""

    if body.credential_profile_id is not None:
        # Named credential profile — look it up in the DB
        prof_result = await db.execute(
            select(SshCredentialProfile).where(
                SshCredentialProfile.id      == body.credential_profile_id,
                SshCredentialProfile.site_id == ctx["site_id"],
            )
        )
        prof = prof_result.scalar_one_or_none()
        if prof:
            ssh_username = prof.ssh_user     or ""
            ssh_password = prof.ssh_password or ""
        else:
            raise HTTPException(status_code=404, detail="Credential profile not found.")

    elif body.use_site_default:
        # Site-wide fallback — fetch from nm_profiles via Supabase
        try:
            from supabase import create_client
            _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
            nm  = (
                _sb.table("nm_profiles")
                   .select("default_ssh_user,default_ssh_password")
                   .eq("id", ctx["user_id"])
                   .single()
                   .execute()
            )
            if nm.data:
                ssh_username = nm.data.get("default_ssh_user")     or ""
                ssh_password = nm.data.get("default_ssh_password") or ""
        except Exception as exc:
            logger.warning("[SSH-TEST] Could not fetch site default SSH creds: %s", exc)

    now = datetime.now(timezone.utc)
    row = SshTest(
        site_id=ctx["site_id"],
        ip=body.ip,
        vendor=body.vendor,
        device_type=body.device_type,
        ssh_username=ssh_username or None,
        ssh_password=ssh_password or None,
        ssh_port=body.ssh_port,
        status="pending",
        expires_at=now + _TEST_TTL,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "[SSH-TEST] Queued test #%d — site=%s ip=%s vendor=%s",
        row.id, ctx["site_id"], body.ip, body.vendor,
    )
    return {"test_id": row.id}


# ── GET /test-ssh/{test_id} ────────────────────────────────────────────────────

@router.get("/test-ssh/{test_id}")
async def poll_ssh_test(
    test_id: int,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Poll the status of a queued SSH test.

    A pending test that has passed its expiry time is automatically
    marked 'expired' (agent did not respond within the TTL window).

    Args:
        test_id: ID returned by POST /test-ssh.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"test_id": ..., "status": "pending|success|failed|expired",
           "result_msg": "..."}``

    Raises:
        HTTPException 404: If the test is not found.
    """
    result = await db.execute(
        select(SshTest).where(SshTest.id == test_id, SshTest.site_id == ctx["site_id"])
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Test not found")

    # Auto-expire stale pending tests
    if row.status == "pending" and datetime.now(timezone.utc) > row.expires_at:
        row.status     = "expired"
        row.result_msg = "Agent did not respond — check that the agent is running and connected."
        await db.commit()

    return {
        "test_id":    row.id,
        "status":     row.status,
        "result_msg": row.result_msg,
    }


# ── GET /agent/pending-ssh-tests ──────────────────────────────────────────────

@router.get("/agent/pending-ssh-tests")
async def agent_pending_ssh_tests(
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return pending SSH tests for the agent to run.

    The agent polls this every 10 seconds.  Only non-expired pending tests
    are returned.  ssh_password is intentionally included because the agent
    needs it to attempt the connection.

    Args:
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"count": N, "tests": [...]}``
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(SshTest).where(
            SshTest.site_id == ctx["site_id"],
            SshTest.status  == "pending",
            SshTest.expires_at > now,
        ).order_by(SshTest.created_at)
    )
    rows = result.scalars().all()
    return {
        "count": len(rows),
        "tests": [
            {
                "id":           r.id,
                "ip":           r.ip,
                "vendor":       r.vendor,
                "device_type":  r.device_type,
                "ssh_username": r.ssh_username or "",
                "ssh_password": r.ssh_password or "",
                "ssh_port":     r.ssh_port,
            }
            for r in rows
        ],
    }


# ── POST /agent/ssh-test-result ───────────────────────────────────────────────

class SshTestResultBody(BaseModel):
    test_id:    int
    success:    bool
    result_msg: str = Field("", max_length=500)


@router.post("/agent/ssh-test-result", status_code=200)
async def agent_ssh_test_result(
    body: SshTestResultBody,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Accept the result of an SSH test from the agent.

    Silently ignores unknown or already-expired test IDs so the agent
    doesn't need to handle 404 errors.

    Args:
        body: Test result including test_id, success flag, and human-readable message.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"ok": True}`` always.
    """
    result = await db.execute(
        select(SshTest).where(SshTest.id == body.test_id, SshTest.site_id == ctx["site_id"])
    )
    row = result.scalar_one_or_none()
    if row is None:
        return {"ok": True}  # already expired or from a different site

    row.status     = "success" if body.success else "failed"
    row.result_msg = body.result_msg
    await db.commit()
    logger.info(
        "[SSH-TEST] #%d result: %s — %s",
        body.test_id, row.status, body.result_msg,
    )
    return {"ok": True}
