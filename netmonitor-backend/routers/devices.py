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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from database import get_db
from models import Alert, Config, Device
from schemas import AlertResponse, DeviceStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Devices"])


# ── DELETE /devices/{hostname} ────────────────────────────────────────────────

@router.delete("/devices/{hostname}", status_code=204)
async def delete_device(
    hostname: str,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Remove a device and all its associated alerts and config snapshots.

    Used by the dashboard to delete a device that appeared via the agent's
    config.yaml but has no corresponding device_configs entry.

    Args:
        hostname: Device hostname to remove.
        ctx: Auth context with site_id.
        db: Async database session.

    Raises:
        HTTPException 404: If the device is not found for this site.
    """
    result = await db.execute(
        select(Device).where(Device.site_id == ctx["site_id"], Device.hostname == hostname)
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{hostname}' not found")

    await db.delete(device)
    await db.commit()
    logger.info("Device deleted: site=%s hostname=%s", ctx["site_id"], hostname)


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

    # Single query for latest config per device — replaces the old N+1 loop
    # that fired one SELECT per device (351 devices = 351 queries).
    config_map: dict[int, tuple] = {}
    if devices:
        ranked = (
            select(
                Config.device_id,
                Config.config_hash,
                Config.pulled_at,
                func.row_number().over(
                    partition_by=Config.device_id,
                    order_by=Config.pulled_at.desc(),
                ).label("rn"),
            )
            .where(Config.device_id.in_([d.id for d in devices]))
            .subquery()
        )
        cfg_result = await db.execute(
            select(ranked.c.device_id, ranked.c.config_hash, ranked.c.pulled_at)
            .where(ranked.c.rn == 1)
        )
        config_map = {
            row.device_id: (row.config_hash, row.pulled_at)
            for row in cfg_result.all()
        }

    # ── 24-hour uptime — calculated from ping state-change alerts ────────────────
    # One batch query; no per-device round-trips.
    # Algorithm: walk ping_down / ping_up events chronologically, summing downtime
    # windows.  The starting state at T-24h is inferred from the first event type:
    #   ping_down → device was UP just before it → was UP at window start
    #   ping_up   → device was DOWN just before it → was DOWN at window start
    # Devices with no events in the window keep their current status for all 24h.
    uptime_map: dict[int, Optional[float]] = {}
    if devices:
        now_utc      = datetime.now(timezone.utc)
        window_start = now_utc - timedelta(hours=24)

        up_rows = await db.execute(
            select(Alert.device_id, Alert.alert_type, Alert.created_at)
            .where(
                Alert.device_id.in_([d.id for d in devices]),
                Alert.alert_type.in_(["ping_down", "ping_up"]),
                Alert.created_at >= window_start,
            )
            .order_by(Alert.device_id, Alert.created_at)
        )

        # Group by device
        device_alerts: dict[int, list[tuple[str, datetime]]] = defaultdict(list)
        for row in up_rows.all():
            device_alerts[int(row.device_id)].append((row.alert_type, row.created_at))

        for device in devices:
            if device.status == "unknown":
                uptime_map[device.id] = None
                continue

            alerts = device_alerts.get(device.id, [])

            if not alerts:
                # No state changes in window — device held its current status all 24h
                uptime_map[device.id] = 100.0 if device.status == "up" else 0.0
                continue

            # Infer starting state from first event type
            currently_up = (alerts[0][0] == "ping_down")
            down_secs    = 0.0
            last_ts      = window_start

            for alert_type, ts in alerts:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if alert_type == "ping_down" and currently_up:
                    currently_up = False
                    last_ts      = ts
                elif alert_type == "ping_up" and not currently_up:
                    down_secs   += (ts - last_ts).total_seconds()
                    currently_up = True
                    last_ts      = ts

            if not currently_up:
                down_secs += (now_utc - last_ts).total_seconds()

            pct = max(0.0, min(100.0, (86400.0 - down_secs) / 86400.0 * 100.0))
            uptime_map[device.id] = round(pct, 1)

    response: list[DeviceStatus] = []
    for device in devices:
        cfg = config_map.get(device.id)
        response.append(
            DeviceStatus(
                hostname=device.hostname,
                ip=device.ip,
                vendor=device.vendor,
                device_type=device.device_type,
                status=device.status,
                last_seen=device.last_seen,
                last_config_hash=cfg[0] if cfg else None,
                last_backup_at=cfg[1]   if cfg else None,
                last_ssh_error=device.last_ssh_error,
                last_ssh_at=device.last_ssh_at,
                maintenance=device.maintenance,
                uptime_24h=uptime_map.get(device.id),
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
        maintenance=device.maintenance,
    )


# ── PUT /devices/{hostname}/maintenance ──────────────────────────────────────

class MaintenanceUpdate(BaseModel):
    maintenance: bool


@router.put("/devices/{hostname}/maintenance", status_code=200)
async def set_maintenance(
    hostname: str,
    body: MaintenanceUpdate,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Enable or disable maintenance mode for a device.

    While maintenance=True the ping alert endpoint silently suppresses status
    updates and alert creation for this device.  The dashboard shows a
    🔧 MAINTENANCE badge instead of up/down.

    Args:
        hostname: Device hostname.
        body: ``{"maintenance": true|false}``
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"ok": True, "maintenance": <bool>}``
    """
    result = await db.execute(
        select(Device).where(Device.site_id == ctx["site_id"], Device.hostname == hostname)
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail=f"Device '{hostname}' not found")

    device.maintenance = body.maintenance
    await db.commit()
    logger.info(
        "[MAINTENANCE] %s/%s → %s",
        ctx["site_id"], hostname, "ON" if body.maintenance else "OFF",
    )
    return {"ok": True, "maintenance": body.maintenance}
