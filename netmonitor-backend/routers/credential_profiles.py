"""
routers/credential_profiles.py — SpanGate Network Monitor Backend
Named SSH credential profiles for grouping devices by shared credentials.

Routes
------
GET    /api/v1/credential-profiles           List all profiles for the site
POST   /api/v1/credential-profiles           Create a new profile
PATCH  /api/v1/credential-profiles/{id}      Update name / credentials
DELETE /api/v1/credential-profiles/{id}      Delete (devices fall back to site default)

Password is stored as-is (same pattern as all other SSH passwords in this
codebase). GET returns ssh_password_set: bool — the actual password is never
returned in any response.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext, require_admin_or_owner
from database import get_db
from models import DeviceConfig, SshCredentialProfile

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Credential Profiles"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ProfileIn(BaseModel):
    name:         str
    ssh_user:     Optional[str] = None
    ssh_password: Optional[str] = None

    @field_validator("name")
    @classmethod
    def val_name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 100:
            raise ValueError("Name must be 1–100 characters")
        return v

    @field_validator("ssh_user")
    @classmethod
    def strip_user(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else None


class ProfilePatch(BaseModel):
    name:         Optional[str] = None
    ssh_user:     Optional[str] = None
    ssh_password: Optional[str] = None

    @field_validator("name")
    @classmethod
    def val_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v or len(v) > 100:
            raise ValueError("Name must be 1–100 characters")
        return v

    @field_validator("ssh_user")
    @classmethod
    def strip_user(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else None


class ProfileOut(BaseModel):
    id:               int
    name:             str
    ssh_user:         str
    ssh_password_set: bool
    device_count:     int
    created_at:       str


def _to_out(row: SshCredentialProfile, device_count: int = 0) -> ProfileOut:
    return ProfileOut(
        id=row.id,
        name=row.name,
        ssh_user=row.ssh_user or "",
        ssh_password_set=bool(row.ssh_password),
        device_count=device_count,
        created_at=row.created_at.isoformat(),
    )


# ── GET /credential-profiles ──────────────────────────────────────────────────

@router.get("/credential-profiles", status_code=status.HTTP_200_OK)
async def list_profiles(
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> list[ProfileOut]:
    """Return all credential profiles for the site, each with its device count."""
    result = await db.execute(
        select(SshCredentialProfile)
        .where(SshCredentialProfile.site_id == ctx["site_id"])
        .order_by(SshCredentialProfile.name)
    )
    profiles = result.scalars().all()

    # Device count per profile in one query
    counts_result = await db.execute(
        select(DeviceConfig.credential_profile_id, func.count(DeviceConfig.id))
        .where(
            DeviceConfig.site_id == ctx["site_id"],
            DeviceConfig.credential_profile_id.isnot(None),
        )
        .group_by(DeviceConfig.credential_profile_id)
    )
    counts = {row[0]: row[1] for row in counts_result}

    return [_to_out(p, counts.get(p.id, 0)) for p in profiles]


# ── POST /credential-profiles ─────────────────────────────────────────────────

@router.post("/credential-profiles", status_code=status.HTTP_201_CREATED)
async def create_profile(
    payload: ProfileIn,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> ProfileOut:
    """Create a new named SSH credential profile."""
    require_admin_or_owner(ctx)
    row = SshCredentialProfile(
        site_id=ctx["site_id"],
        name=payload.name,
        ssh_user=payload.ssh_user or None,
        ssh_password=payload.ssh_password or None,
    )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A profile named '{payload.name}' already exists.",
        )
    logger.info("[PROFILES] Created %r for site %s", payload.name, ctx["site_id"])
    return _to_out(row, 0)


# ── PATCH /credential-profiles/{id} ───────────────────────────────────────────

@router.patch("/credential-profiles/{profile_id}", status_code=status.HTTP_200_OK)
async def update_profile(
    profile_id: int,
    payload: ProfilePatch,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> ProfileOut:
    """Update name and/or credentials on an existing profile."""
    require_admin_or_owner(ctx)
    result = await db.execute(
        select(SshCredentialProfile).where(
            SshCredentialProfile.id == profile_id,
            SshCredentialProfile.site_id == ctx["site_id"],
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        # Empty string clears text fields; omitting preserves existing
        if field in ("ssh_user", "ssh_password"):
            setattr(row, field, value or None)
        else:
            setattr(row, field, value)

    try:
        await db.commit()
        await db.refresh(row)
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A profile named '{payload.name}' already exists.",
        )

    count_result = await db.execute(
        select(func.count(DeviceConfig.id)).where(
            DeviceConfig.credential_profile_id == profile_id,
        )
    )
    logger.info("[PROFILES] Updated profile %d for site %s", profile_id, ctx["site_id"])
    return _to_out(row, count_result.scalar_one())


# ── DELETE /credential-profiles/{id} ──────────────────────────────────────────

@router.delete("/credential-profiles/{profile_id}", status_code=status.HTTP_200_OK)
async def delete_profile(
    profile_id: int,
    ctx: AuthContext,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Delete a credential profile.  Devices using it automatically fall back to
    the site default (credential_profile_id → NULL via ON DELETE SET NULL).
    """
    require_admin_or_owner(ctx)
    result = await db.execute(
        select(SshCredentialProfile).where(
            SshCredentialProfile.id == profile_id,
            SshCredentialProfile.site_id == ctx["site_id"],
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Count affected devices before deletion
    count_result = await db.execute(
        select(func.count(DeviceConfig.id)).where(
            DeviceConfig.credential_profile_id == profile_id,
        )
    )
    affected = count_result.scalar_one()

    await db.delete(row)
    await db.commit()
    logger.info(
        "[PROFILES] Deleted profile %d for site %s — %d device(s) reverted to site default",
        profile_id, ctx["site_id"], affected,
    )
    return {"ok": True, "devices_reverted": affected}
