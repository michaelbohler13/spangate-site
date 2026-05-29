"""
device_configs.py — Device Configuration CRUD + Agent Device-List Endpoint

Dashboard routes  (Bearer SpanGate API key):
  GET    /api/v1/device-configs           — list all configured devices for the site
  POST   /api/v1/device-configs           — add a device
  PATCH  /api/v1/device-configs/{id}      — update a device
  DELETE /api/v1/device-configs/{id}      — remove a device

Agent route (Bearer SpanGate API key):
  GET    /api/v1/agent/device-list        — device list in agent-compatible format

The agent polls /agent/device-list every few minutes so changes made in the
dashboard take effect without editing config.yaml or restarting the agent.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from database import get_db
from models import DeviceConfig

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Device Configs"])

VALID_VENDORS = {"cisco", "aruba_cx", "juniper", "other", "internet"}

# Default netmiko device_type per vendor
VENDOR_DEFAULT_TYPE: dict[str, str] = {
    "cisco":    "cisco_ios",
    "aruba_cx": "aruba_osswitch",
    "juniper":  "juniper_junos",
    "other":    "linux",
    "internet": "ping_only",
}


# ── Schemas ───────────────────────────────────────────────────────────────────

class DeviceConfigIn(BaseModel):
    hostname:     str
    ip:           str
    vendor:       str        = "cisco"
    device_type:  Optional[str] = None   # auto-filled from vendor if omitted
    ssh_username: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_port:     int        = 22
    ping_enabled: bool       = True
    ssh_enabled:  bool       = False
    group_name:   Optional[str] = None   # display group / folder

    @field_validator("hostname")
    @classmethod
    def val_hostname(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 255:
            raise ValueError("Hostname must be 1–255 characters")
        return v

    @field_validator("ip")
    @classmethod
    def val_ip(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("IP address or domain name is required")
        if len(v) > 253:
            raise ValueError("IP / domain must be 253 characters or fewer")
        return v

    @field_validator("vendor")
    @classmethod
    def val_vendor(cls, v: str) -> str:
        v = v.strip().lower()
        return v if v in VALID_VENDORS else "other"

    @field_validator("ssh_port")
    @classmethod
    def val_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("Port must be 1–65535")
        return v


class DeviceConfigOut(BaseModel):
    """Returned to the dashboard — ssh_password intentionally excluded."""
    id:                  int
    hostname:            str
    ip:                  str
    vendor:              str
    device_type:         str
    ssh_username:        Optional[str]
    ssh_port:            int
    ping_enabled:        bool
    ssh_enabled:         bool
    group_name:          Optional[str]
    backup_requested_at: Optional[str]
    created_at:          str
    updated_at:          str


class DeviceConfigPatch(BaseModel):
    hostname:     Optional[str]  = None
    ip:           Optional[str]  = None
    vendor:       Optional[str]  = None
    device_type:  Optional[str]  = None
    ssh_username: Optional[str]  = None
    ssh_password: Optional[str]  = None
    ssh_port:     Optional[int]  = None
    ping_enabled: Optional[bool] = None
    ssh_enabled:  Optional[bool] = None
    group_name:   Optional[str]  = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_out(row: DeviceConfig) -> DeviceConfigOut:
    return DeviceConfigOut(
        id=row.id,
        hostname=row.hostname,
        ip=row.ip,
        vendor=row.vendor,
        device_type=row.device_type,
        ssh_username=row.ssh_username,
        ssh_port=row.ssh_port,
        ping_enabled=row.ping_enabled,
        ssh_enabled=row.ssh_enabled,
        group_name=row.group_name,
        backup_requested_at=row.backup_requested_at.isoformat() if row.backup_requested_at else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


# ── GET /device-configs ───────────────────────────────────────────────────────

@router.get("/device-configs", response_model=list[DeviceConfigOut])
async def list_device_configs(
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> list[DeviceConfigOut]:
    """Return all configured devices for the authenticated site."""
    result = await db.execute(
        select(DeviceConfig)
        .where(DeviceConfig.site_id == ctx["site_id"])
        .order_by(DeviceConfig.hostname)
    )
    return [_to_out(r) for r in result.scalars().all()]


# ── POST /device-configs ──────────────────────────────────────────────────────

@router.post("/device-configs", response_model=DeviceConfigOut, status_code=201)
async def add_device_config(
    payload: DeviceConfigIn,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> DeviceConfigOut:
    """Add a new device to the site's monitoring list."""
    device_type = payload.device_type or VENDOR_DEFAULT_TYPE.get(payload.vendor, "cisco_ios")

    row = DeviceConfig(
        site_id=ctx["site_id"],
        hostname=payload.hostname,
        ip=payload.ip,
        vendor=payload.vendor,
        device_type=device_type,
        ssh_username=payload.ssh_username or None,
        ssh_password=payload.ssh_password or None,
        ssh_port=payload.ssh_port,
        ping_enabled=payload.ping_enabled,
        ssh_enabled=payload.ssh_enabled,
        group_name=payload.group_name or None,
    )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A device named '{payload.hostname}' already exists for this site.",
        )

    logger.info("Device added: site=%s hostname=%s ip=%s", ctx["site_id"], payload.hostname, payload.ip)
    return _to_out(row)


# ── PATCH /device-configs/{id} ────────────────────────────────────────────────

@router.patch("/device-configs/{device_id}", response_model=DeviceConfigOut)
async def update_device_config(
    device_id: int,
    payload: DeviceConfigPatch,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> DeviceConfigOut:
    """Update one or more fields on an existing device."""
    result = await db.execute(
        select(DeviceConfig).where(
            DeviceConfig.id == device_id,
            DeviceConfig.site_id == ctx["site_id"],
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(row, field, value)

    # Auto-refresh device_type when vendor changes (unless device_type was also supplied)
    if "vendor" in updates and "device_type" not in updates:
        row.device_type = VENDOR_DEFAULT_TYPE.get(row.vendor, "cisco_ios")

    row.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(row)
    logger.info("Device updated: site=%s id=%d hostname=%s", ctx["site_id"], device_id, row.hostname)
    return _to_out(row)


# ── DELETE /device-configs/{id} ───────────────────────────────────────────────

@router.delete("/device-configs/{device_id}", status_code=204)
async def delete_device_config(
    device_id: int,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a device from the site's monitoring list."""
    result = await db.execute(
        select(DeviceConfig).where(
            DeviceConfig.id == device_id,
            DeviceConfig.site_id == ctx["site_id"],
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")

    hostname = row.hostname
    await db.delete(row)
    await db.commit()
    logger.info("Device removed: site=%s id=%d hostname=%s", ctx["site_id"], device_id, hostname)


# ── POST /device-configs/{id}/request-backup ─────────────────────────────────

@router.post("/device-configs/{device_id}/request-backup", status_code=200)
async def request_backup(
    device_id: int,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Mark a device for an immediate on-demand config backup.

    Sets backup_requested_at = now().  The agent polls /agent/pending-backups
    every 60 seconds, picks up this flag, triggers an SSH pull, then calls
    /agent/clear-backup-request to clear the flag.

    Args:
        device_id: PK of the device_configs row.
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"ok": True, "hostname": ...}``

    Raises:
        HTTPException 404: Device not found.
        HTTPException 400: SSH is not enabled for this device.
    """
    result = await db.execute(
        select(DeviceConfig).where(
            DeviceConfig.id == device_id,
            DeviceConfig.site_id == ctx["site_id"],
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if not row.ssh_enabled:
        raise HTTPException(status_code=400, detail="SSH is not enabled for this device")

    row.backup_requested_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(
        "Backup queued: site=%s hostname=%s", ctx["site_id"], row.hostname
    )
    return {"ok": True, "hostname": row.hostname}


# ── GET /agent/pending-backups ────────────────────────────────────────────────

@router.get("/agent/pending-backups")
async def agent_pending_backups(
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return devices that have a pending on-demand backup request.

    The agent polls this every 60 seconds, executes an SSH pull for each
    returned device, then calls /agent/clear-backup-request to clear the flag.

    Returns the same format as /agent/device-list for compatibility.
    """
    result = await db.execute(
        select(DeviceConfig)
        .where(
            DeviceConfig.site_id == ctx["site_id"],
            DeviceConfig.ssh_enabled.is_(True),
            DeviceConfig.backup_requested_at.isnot(None),
        )
        .order_by(DeviceConfig.backup_requested_at)
    )
    rows = result.scalars().all()
    return {
        "count": len(rows),
        "devices": [
            {
                "hostname":     r.hostname,
                "ip":           r.ip,
                "vendor":       r.vendor,
                "device_type":  r.device_type,
                "ssh_username": r.ssh_username or "",
                "ssh_password": r.ssh_password or "",
                "ssh_port":     r.ssh_port,
                "ping_enabled": r.ping_enabled,
                "ssh_enabled":  r.ssh_enabled,
            }
            for r in rows
        ],
    }


# ── POST /agent/clear-backup-request ─────────────────────────────────────────

class ClearBackupRequest(BaseModel):
    hostname: str = Field(..., max_length=255)


@router.post("/agent/clear-backup-request", status_code=200)
async def agent_clear_backup_request(
    body: ClearBackupRequest,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Clear the backup_requested_at flag for a device after the agent has
    completed the on-demand backup.

    Args:
        body: ``{"hostname": "..."}``
        ctx: Auth context with site_id.
        db: Async database session.

    Returns:
        ``{"ok": True}`` always (silently ignores unknown hostnames).
    """
    result = await db.execute(
        select(DeviceConfig).where(
            DeviceConfig.site_id == ctx["site_id"],
            DeviceConfig.hostname == body.hostname,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None and row.backup_requested_at is not None:
        row.backup_requested_at = None
        await db.commit()
        logger.info(
            "Backup request cleared: site=%s hostname=%s", ctx["site_id"], body.hostname
        )
    return {"ok": True}


# ── GET /agent/device-list ────────────────────────────────────────────────────

@router.get("/agent/device-list")
async def agent_device_list(
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Return the configured device list in agent-compatible format.

    The agent polls this endpoint every few minutes and updates its in-memory
    device list, so dashboard changes take effect without a restart.

    Returns {"devices": [...], "count": N}
    """
    result = await db.execute(
        select(DeviceConfig)
        .where(DeviceConfig.site_id == ctx["site_id"])
        .order_by(DeviceConfig.hostname)
    )
    rows = result.scalars().all()

    return {
        "count": len(rows),
        "devices": [
            {
                "hostname":     r.hostname,
                "ip":           r.ip,
                "vendor":       r.vendor,
                "device_type":  r.device_type,
                "ssh_username": r.ssh_username or "",
                "ssh_password": r.ssh_password or "",
                "ssh_port":     r.ssh_port,
                "ping_enabled": r.ping_enabled,
                "ssh_enabled":  r.ssh_enabled,
            }
            for r in rows
        ],
    }
