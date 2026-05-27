"""
routers/cleanup.py — SpanGate Network Monitor Backend
Scheduled data-retention cleanup endpoint.

Routes
------
GET /api/v1/admin/cleanup
    Delete records older than 30 days from alerts, configs, and config_diffs.
    Called by Vercel Cron (configured in vercel.json) — not authenticated
    by site API key, but protected by the CRON_SECRET header that Vercel
    injects automatically on cron invocations.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Alert, Config, ConfigDiff

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin"])

_RETENTION_DAYS = 30


def _verify_cron_secret(authorization: str | None = Header(default=None)) -> None:
    """
    Verify the request comes from Vercel Cron by checking the Authorization
    header against the CRON_SECRET environment variable.

    Vercel sets  Authorization: Bearer <CRON_SECRET>  on every cron invocation.
    Reject any request that doesn't carry the correct secret.
    """
    expected = os.environ.get("CRON_SECRET", "")
    if not expected:
        # If no secret is configured, only allow in local dev (no-op guard)
        logger.warning("[CLEANUP] CRON_SECRET not set — skipping auth check")
        return

    if authorization != f"Bearer {expected}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing cron secret",
        )


@router.get(
    "/admin/cleanup",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_verify_cron_secret)],
)
async def run_cleanup(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Purge rows older than 30 days from time-series tables.

    Targeted tables:
    - alerts         — ping and config-change events
    - configs        — running-config snapshots
    - config_diffs   — unified diff records

    The devices and agent_heartbeat tables are NOT purged (they are reference /
    state data, not time-series logs).

    Returns:
        Dict with counts of deleted rows per table.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)

    result_alerts = await db.execute(
        delete(Alert).where(Alert.created_at < cutoff)
    )
    result_configs = await db.execute(
        delete(Config).where(Config.pulled_at < cutoff)
    )
    result_diffs = await db.execute(
        delete(ConfigDiff).where(ConfigDiff.detected_at < cutoff)
    )

    await db.commit()

    deleted = {
        "alerts":       result_alerts.rowcount,
        "configs":      result_configs.rowcount,
        "config_diffs": result_diffs.rowcount,
    }

    logger.info(
        "[CLEANUP] Purged rows older than %s UTC: %s",
        cutoff.strftime("%Y-%m-%d"),
        deleted,
    )

    return {"ok": True, "deleted": deleted, "cutoff": cutoff.isoformat()}
