"""
routers/devices.py — SpanGate Network Monitor Backend
Device listing and detail endpoints.

Routes
------
GET /api/v1/devices
    Return all devices for the authenticated site with live up/down status.

GET /api/v1/devices/{hostname}
    Return single device detail including last 5 alerts and latest config hash.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from database import get_db
from models import Alert, Config, Device
from schemas import AlertResponse, DeviceStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Devices"])


# ── GET /devices ──────────────────────────────────────────────────────────────

@router.get("/devices", response_model=list[DeviceStatus])
async def list_devices(
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> list[DeviceStatus]:
    """
    Return all monitored devices for the authenticated site.

    Each device entry includes:
    - Persistent fields from the database (hostname, ip, vendor, device_type)
    - Live ping status from in-memory state (status, last_seen)
    - Latest config hash from the most recent backup

    Args:
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        List of DeviceStatus objects.
    """
    site_id = ctx["site_id"]

    result = await db.execute(
        select(Device).where(Device.site_id == site_id).order_by(Device.hostname)
    )
    devices = result.scalars().all()

    response: list[DeviceStatus] = []
    for device in devices:
        # Latest config hash from DB
        hash_result = await db.execute(
            select(Config.config_hash)
            .where(Config.device_id == device.id)
            .order_by(Config.pulled_at.desc())
            .limit(1)
        )
        last_config_hash = hash_result.scalar_one_or_none()

        response.append(
            DeviceStatus(
                hostname=device.hostname,
                ip=device.ip,
                vendor=device.vendor,
                device_type=device.device_type,
                status=device.status,        # persisted in DB
                last_seen=device.last_seen,  # persisted in DB
                last_config_hash=last_config_hash,
            )
        )

    return response


# ── GET /devices/{hostname} ───────────────────────────────────────────────────

class DeviceDetail(DeviceStatus):
    """Extended device view including recent alerts and config metadata."""
    recent_alerts:    list[AlertResponse]
    last_config_hash: Optional[str]
    last_config_at:   Optional[str]


@router.get("/devices/{hostname}", response_model=DeviceDetail)
async def get_device(
    hostname: str,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> DeviceDetail:
    """
    Return detailed information for a single device.

    Includes live ping status, last 5 alerts, and the most recent
    config hash and pull timestamp.

    Args:
        hostname: Device hostname.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        DeviceDetail object.

    Raises:
        HTTPException 404: If the device is not found for this site.
    """
    site_id = ctx["site_id"]

    result = await db.execute(
        select(Device).where(Device.site_id == site_id, Device.hostname == hostname)
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{hostname}' not found")

    # Live status from DB columns (persisted by ping alert endpoint)
    status_str = device.status
    last_seen  = device.last_seen

    # Last 5 alerts
    alert_result = await db.execute(
        select(Alert)
        .where(Alert.device_id == device.id)
        .order_by(Alert.created_at.desc())
        .limit(5)
    )
    alerts = alert_result.scalars().all()

    recent_alerts = [
        AlertResponse(
            id=a.id,
            alert_type=a.alert_type,
            message=a.message,
            created_at=a.created_at,
            hostname=hostname,
        )
        for a in alerts
    ]

    # Latest config backup
    config_result = await db.execute(
        select(Config.config_hash, Config.pulled_at)
        .where(Config.device_id == device.id)
        .order_by(Config.pulled_at.desc())
        .limit(1)
    )
    config_row = config_result.first()

    return DeviceDetail(
        hostname=device.hostname,
        ip=device.ip,
        vendor=device.vendor,
        device_type=device.device_type,
        status=status_str,
        last_seen=last_seen,
        last_config_hash=config_row.config_hash if config_row else None,
        last_config_at=config_row.pulled_at.isoformat() if config_row else None,
        recent_alerts=recent_alerts,
    )
