"""
routers/cleanup.py — SpanGate Network Monitor Backend
Scheduled data-retention cleanup endpoint.

Routes
------
GET /api/v1/admin/cleanup
    Plan-aware purge of stale rows from alerts, configs, and config_diffs.
    Called by Vercel Cron (configured in vercel.json) — not authenticated
    by site API key, but protected by the CRON_SECRET header that Vercel
    injects automatically on cron invocations.

Retention policy
----------------
Free plan   : alerts and configs older than 30 days deleted
Paid plans  : alerts and configs older than 365 days deleted
All plans   : configs trimmed to at most MAX_CONFIGS_PER_DEVICE per device
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Alert, Config, ConfigDiff

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin"])

# ── Retention constants ───────────────────────────────────────────────────────
_RETENTION_DAYS_FREE    = 30    # alerts / configs kept for free-plan sites
_RETENTION_DAYS_PAID    = 365   # alerts / configs kept for paid-plan sites
_MAX_CONFIGS_PER_DEVICE = 4     # keep the 4 most recent backups per device (~1/week = ~4/month)


def _verify_cron_secret(authorization: str | None = Header(default=None)) -> None:
    """
    Verify the request comes from Vercel Cron by checking the Authorization
    header against the CRON_SECRET environment variable.

    Vercel sets  Authorization: Bearer <CRON_SECRET>  on every cron invocation.
    Reject any request that doesn't carry the correct secret.
    """
    expected = os.environ.get("CRON_SECRET", "")
    if not expected:
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
    Purge stale rows from time-series tables using plan-aware retention.

    Three passes:
    1. Global ceiling: delete rows older than 365 days for everyone.
    2. Free-plan cap: additionally delete rows older than 30 days for
       sites whose nm_profiles.plan is NULL or 'free'.
    3. Count-based trim: delete configs beyond MAX_CONFIGS_PER_DEVICE most
       recent per device, so daily manual backups don't accumulate unbounded.

    The devices and agent_heartbeat tables are NOT purged (reference/state data).

    Returns:
        Dict with counts of deleted rows per table and pass.
    """
    now           = datetime.now(timezone.utc)
    cutoff_paid   = now - timedelta(days=_RETENTION_DAYS_PAID)
    cutoff_free   = now - timedelta(days=_RETENTION_DAYS_FREE)

    # ── Pass 1: global 365-day ceiling (all plans) ───────────────────────────
    r_alerts_paid = await db.execute(
        delete(Alert).where(Alert.created_at < cutoff_paid)
    )
    r_configs_paid = await db.execute(
        delete(Config).where(Config.pulled_at < cutoff_paid)
    )
    r_diffs_paid = await db.execute(
        delete(ConfigDiff).where(ConfigDiff.detected_at < cutoff_paid)
    )

    # ── Pass 2: 30-day cap for free-plan sites ───────────────────────────────
    # nm_profiles lives in the same Supabase Postgres instance — query it
    # via raw SQL so we don't need a separate ORM model for it.
    r_alerts_free = await db.execute(text("""
        DELETE FROM alerts
        WHERE created_at < :cutoff_free
          AND site_id IN (
              SELECT site_id FROM nm_profiles
              WHERE plan IS NULL OR plan = 'free'
          )
    """), {"cutoff_free": cutoff_free})

    r_configs_free = await db.execute(text("""
        DELETE FROM configs
        WHERE pulled_at < :cutoff_free
          AND device_id IN (
              SELECT d.id FROM devices d
              JOIN nm_profiles p ON p.site_id = d.site_id
              WHERE p.plan IS NULL OR p.plan = 'free'
          )
    """), {"cutoff_free": cutoff_free})

    r_diffs_free = await db.execute(text("""
        DELETE FROM config_diffs
        WHERE detected_at < :cutoff_free
          AND device_id IN (
              SELECT d.id FROM devices d
              JOIN nm_profiles p ON p.site_id = d.site_id
              WHERE p.plan IS NULL OR p.plan = 'free'
          )
    """), {"cutoff_free": cutoff_free})

    # ── Pass 3: per-device count trim (paid only — free sites can't back up) ─
    r_configs_trim = await db.execute(text("""
        DELETE FROM configs
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY device_id ORDER BY pulled_at DESC
                       ) AS rn
                FROM configs
            ) ranked
            WHERE rn <= :max_per_device
        )
    """), {"max_per_device": _MAX_CONFIGS_PER_DEVICE})

    await db.commit()

    deleted = {
        "alerts_paid_ceiling":   r_alerts_paid.rowcount,
        "alerts_free_cap":       r_alerts_free.rowcount,
        "configs_paid_ceiling":  r_configs_paid.rowcount,
        "configs_free_cap":      r_configs_free.rowcount,
        "configs_trimmed":       r_configs_trim.rowcount,
        "config_diffs_paid":     r_diffs_paid.rowcount,
        "config_diffs_free":     r_diffs_free.rowcount,
    }

    logger.info(
        "[CLEANUP] Plan-aware purge complete. Free cutoff: %s | Paid cutoff: %s | "
        "Max configs/device: %d | Deleted: %s",
        cutoff_free.strftime("%Y-%m-%d"),
        cutoff_paid.strftime("%Y-%m-%d"),
        _MAX_CONFIGS_PER_DEVICE,
        deleted,
    )

    return {
        "ok":      True,
        "deleted": deleted,
        "cutoff":  {
            "free": cutoff_free.isoformat(),
            "paid": cutoff_paid.isoformat(),
        },
    }
