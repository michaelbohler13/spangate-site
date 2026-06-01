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
Free      : alerts older than 30 days deleted
Starter   : alerts older than 90 days deleted
Pro/Enterprise/MSP : alerts older than 365 days deleted
All plans : configs trimmed to MAX_CONFIGS_PER_DEVICE (free=1, paid=4)

Note: plan is read from the `sites` table (Phase 3+) with fallback to
`nm_profiles` for legacy single-site accounts.
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
_RETENTION_DAYS_FREE    = 30    # free plan alert history
_RETENTION_DAYS_STARTER = 90    # starter plan alert history
_RETENTION_DAYS_PAID    = 365   # pro / enterprise / msp alert history
_MAX_CONFIGS_FREE       = 1     # free plan: current config only, no diff
_MAX_CONFIGS_PAID       = 4     # paid plans: 4 weeks rolling history + download


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
    now             = datetime.now(timezone.utc)
    cutoff_paid     = now - timedelta(days=_RETENTION_DAYS_PAID)
    cutoff_starter  = now - timedelta(days=_RETENTION_DAYS_STARTER)
    cutoff_free     = now - timedelta(days=_RETENTION_DAYS_FREE)

    # Helper CTE: resolve each site's plan from the `sites` table (Phase 3+)
    # with fallback to `nm_profiles` for legacy single-site accounts.
    # Returns: site_id, plan
    _SITE_PLAN_CTE = """
        WITH site_plans AS (
            SELECT id AS site_id, COALESCE(plan, 'free') AS plan FROM sites
            UNION
            SELECT site_id, COALESCE(plan, 'free') AS plan FROM nm_profiles
            WHERE site_id NOT IN (SELECT id FROM sites)
        )
    """

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

    # ── Pass 2a: 30-day cap for free sites ───────────────────────────────────
    r_alerts_free = await db.execute(text(_SITE_PLAN_CTE + """
        DELETE FROM alerts
        WHERE created_at < :cutoff_free
          AND site_id IN (
              SELECT site_id FROM site_plans WHERE plan = 'free'
          )
    """), {"cutoff_free": cutoff_free})

    r_diffs_free = await db.execute(text(_SITE_PLAN_CTE + """
        DELETE FROM config_diffs
        WHERE detected_at < :cutoff_free
          AND device_id IN (
              SELECT d.id FROM devices d
              JOIN site_plans sp ON sp.site_id = d.site_id
              WHERE sp.plan = 'free'
          )
    """), {"cutoff_free": cutoff_free})

    # ── Pass 2b: 90-day cap for starter sites ────────────────────────────────
    r_alerts_starter = await db.execute(text(_SITE_PLAN_CTE + """
        DELETE FROM alerts
        WHERE created_at < :cutoff_starter
          AND site_id IN (
              SELECT site_id FROM site_plans WHERE plan = 'starter'
          )
    """), {"cutoff_starter": cutoff_starter})

    r_diffs_starter = await db.execute(text(_SITE_PLAN_CTE + """
        DELETE FROM config_diffs
        WHERE detected_at < :cutoff_starter
          AND device_id IN (
              SELECT d.id FROM devices d
              JOIN site_plans sp ON sp.site_id = d.site_id
              WHERE sp.plan = 'starter'
          )
    """), {"cutoff_starter": cutoff_starter})

    # ── Pass 3: per-device config count trim ─────────────────────────────────
    # Free plan: keep only 1 config (current snapshot, no diff capability).
    # Paid plans: keep 4 (~4 weeks rolling history).
    r_configs_trim_free = await db.execute(text(_SITE_PLAN_CTE + """
        DELETE FROM configs
        WHERE device_id IN (
            SELECT d.id FROM devices d
            JOIN site_plans sp ON sp.site_id = d.site_id
            WHERE sp.plan = 'free'
        )
        AND id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY device_id ORDER BY pulled_at DESC
                       ) AS rn
                FROM configs
                WHERE device_id IN (
                    SELECT d.id FROM devices d
                    JOIN site_plans sp ON sp.site_id = d.site_id
                    WHERE sp.plan = 'free'
                )
            ) ranked
            WHERE rn <= :max_free
        )
    """), {"max_free": _MAX_CONFIGS_FREE})

    r_configs_trim_paid = await db.execute(text("""
        DELETE FROM configs
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY device_id ORDER BY pulled_at DESC
                       ) AS rn
                FROM configs
            ) ranked
            WHERE rn <= :max_paid
        )
    """), {"max_paid": _MAX_CONFIGS_PAID})

    await db.commit()

    deleted = {
        "alerts_365d_ceiling":    r_alerts_paid.rowcount,
        "alerts_free_30d":        r_alerts_free.rowcount,
        "alerts_starter_90d":     r_alerts_starter.rowcount,
        "configs_365d_ceiling":   r_configs_paid.rowcount,
        "configs_trim_free_1":    r_configs_trim_free.rowcount,
        "configs_trim_paid_4":    r_configs_trim_paid.rowcount,
        "config_diffs_365d":      r_diffs_paid.rowcount,
        "config_diffs_free_30d":  r_diffs_free.rowcount,
        "config_diffs_starter_90d": r_diffs_starter.rowcount,
    }

    logger.info(
        "[CLEANUP] Plan-aware purge complete. "
        "Cutoffs — free: %s | starter: %s | paid: %s | Deleted: %s",
        cutoff_free.strftime("%Y-%m-%d"),
        cutoff_starter.strftime("%Y-%m-%d"),
        cutoff_paid.strftime("%Y-%m-%d"),
        deleted,
    )

    return {
        "ok":      True,
        "deleted": deleted,
        "cutoff":  {
            "free":    cutoff_free.isoformat(),
            "starter": cutoff_starter.isoformat(),
            "paid":    cutoff_paid.isoformat(),
        },
    }
