"""
routers/configs.py — SpanGate Network Monitor Backend
Config backup storage and retrieval.

Routes
------
POST /api/v1/configs/backup
    Store a config snapshot. Idempotent — skips if same hash exists for device.

GET /api/v1/configs/{hostname}
    Return last 10 config backup metadata records (no config_text in list).

GET /api/v1/configs/{hostname}/{config_id}
    Return full config_text for a specific backup.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from database import get_db
from models import Config, Device
from schemas import ConfigBackup, ConfigDetail, ConfigMeta

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Configs"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_create_device(
    db: AsyncSession,
    site_id: str,
    hostname: str,
) -> Device:
    """
    Return the Device for (site_id, hostname), creating it if it doesn't exist.

    Args:
        db: Async database session.
        site_id: Site identifier from auth context.
        hostname: Device hostname.

    Returns:
        The existing or newly created Device ORM object.
    """
    result = await db.execute(
        select(Device).where(Device.site_id == site_id, Device.hostname == hostname)
    )
    device = result.scalar_one_or_none()

    if device is None:
        device = Device(site_id=site_id, hostname=hostname, ip="")
        db.add(device)
        await db.flush()
        logger.info("[DEVICE] Auto-created device %s/%s", site_id, hostname)

    return device


async def _get_device_or_404(
    db: AsyncSession,
    site_id: str,
    hostname: str,
) -> Device:
    """
    Return the Device for (site_id, hostname) or raise HTTP 404.

    Args:
        db: Async database session.
        site_id: Site identifier.
        hostname: Device hostname.

    Raises:
        HTTPException 404: If the device is not found.
    """
    result = await db.execute(
        select(Device).where(Device.site_id == site_id, Device.hostname == hostname)
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{hostname}' not found")
    return device


# ── POST /configs/backup ──────────────────────────────────────────────────────

@router.post("/configs/backup", status_code=status.HTTP_201_CREATED)
async def config_backup(
    body: ConfigBackup,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Store a periodic config backup for a device.

    This is idempotent: if a backup with the same config_hash already exists
    for this device, we skip the insert and return HTTP 200 with
    ``{"ok": True, "skipped": True}``.

    The config_text field is stored in the database but never logged.

    Args:
        body: Config backup payload from the agent.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"ok": True, "config_id": <id>}`` on insert, or
        ``{"ok": True, "skipped": True}`` if already stored.
    """
    site_id = ctx["site_id"]

    try:
        device = await _get_or_create_device(db, site_id, body.hostname)

        # Idempotency check — same hash already stored for this device?
        existing = await db.execute(
            select(Config.id)
            .where(Config.device_id == device.id, Config.config_hash == body.config_hash)
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            logger.debug(
                "[CONFIG-BACKUP] Skipped duplicate hash %s for %s/%s",
                body.config_hash[:12], site_id, body.hostname,
            )
            return {"ok": True, "skipped": True}

        config = Config(
            device_id=device.id,
            config_text=body.config_text,
            config_hash=body.config_hash,
        )
        db.add(config)
        await db.commit()
        await db.refresh(config)

    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.error("[CONFIG-BACKUP] DB error for %s/%s: %s", site_id, body.hostname, exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "[CONFIG-BACKUP] %s/%s — hash %s",
        site_id, body.hostname, body.config_hash[:12],
    )
    return {"ok": True, "config_id": config.id}


# ── GET /configs/{hostname} ───────────────────────────────────────────────────

@router.get("/configs/{hostname}", response_model=list[ConfigMeta])
async def list_configs(
    hostname: str,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> list[ConfigMeta]:
    """
    Return the last 10 config backup metadata records for a device.

    Config text is NOT included in the list response to keep payloads small.
    Use GET /configs/{hostname}/{config_id} to fetch the full text.

    Args:
        hostname: Device hostname.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        List of up to 10 ConfigMeta objects, newest first.

    Raises:
        HTTPException 404: If the device is not found.
    """
    site_id = ctx["site_id"]
    device  = await _get_device_or_404(db, site_id, hostname)

    result = await db.execute(
        select(Config.id, Config.config_hash, Config.pulled_at)
        .where(Config.device_id == device.id)
        .order_by(Config.pulled_at.desc())
        .limit(10)
    )
    rows = result.all()

    return [
        ConfigMeta(id=r.id, config_hash=r.config_hash, pulled_at=r.pulled_at)
        for r in rows
    ]


# ── GET /configs/{hostname}/{config_id} ───────────────────────────────────────

@router.get("/configs/{hostname}/{config_id}", response_model=ConfigDetail)
async def get_config(
    hostname:  str,
    config_id: int,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> ConfigDetail:
    """
    Return the full config_text for a specific backup.

    Args:
        hostname: Device hostname (used to verify site ownership).
        config_id: Primary key of the Config row.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ConfigDetail with id, config_hash, config_text, and pulled_at.

    Raises:
        HTTPException 404: If the config record is not found or belongs
            to a different site.
    """
    site_id = ctx["site_id"]
    device  = await _get_device_or_404(db, site_id, hostname)

    result = await db.execute(
        select(Config)
        .where(Config.id == config_id, Config.device_id == device.id)
    )
    config = result.scalar_one_or_none()

    if config is None:
        raise HTTPException(status_code=404, detail="Config backup not found")

    return ConfigDetail(
        id=config.id,
        config_hash=config.config_hash,
        config_text=config.config_text,
        pulled_at=config.pulled_at,
    )
