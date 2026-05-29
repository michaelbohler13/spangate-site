"""
state.py — SpanGate Network Monitor Backend
Database-backed state functions.

Replaces the original in-memory dicts with persistent async DB operations
so device status and heartbeat data survive serverless function restarts.

All functions are async and require a db: AsyncSession argument.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import AgentHeartbeat, Device


# ── Device ping status ────────────────────────────────────────────────────────

async def update_device_status(
    db: AsyncSession,
    site_id: str,
    hostname: str,
    ip: str,
    status: str,
    last_seen: datetime,
) -> None:
    """
    Update the live status columns on an existing Device row.

    Called by the ping alert endpoint after the Device row is confirmed to exist.
    The caller is responsible for committing the session.

    Args:
        db: Async database session.
        site_id: Site identifier from auth context.
        hostname: Device hostname.
        ip: Device IP — used to keep the ip column current.
        status: "up" or "down".
        last_seen: UTC datetime of the ping event.
    """
    await db.execute(
        update(Device)
        .where(Device.site_id == site_id, Device.hostname == hostname)
        .values(
            status=status,
            ip=ip,
            last_seen=last_seen,
            last_status_change=last_seen,
        )
    )


async def get_site_status_counts(
    db: AsyncSession,
    site_id: str,
) -> tuple[int, int]:
    """
    Return (devices_up, devices_down) by querying Device.status in the DB.

    Args:
        db: Async database session.
        site_id: Site identifier from auth context.

    Returns:
        Tuple of (up_count, down_count).
    """
    result = await db.execute(
        select(Device.status).where(Device.site_id == site_id)
    )
    statuses = result.scalars().all()
    up   = sum(1 for s in statuses if s == "up")
    down = sum(1 for s in statuses if s == "down")
    return up, down


# ── Heartbeat state ───────────────────────────────────────────────────────────

async def upsert_heartbeat(
    db: AsyncSession,
    site_id: str,
    site_name: str,
    device_count: int,
    devices_up: int,
    devices_down: int,
    agent_version: str,
    cpu_percent:  float | None = None,
    mem_percent:  float | None = None,
    temp_celsius: float | None = None,
    agent_model:  str   | None = None,
) -> None:
    """
    Insert or update the heartbeat row for a site.

    Uses PostgreSQL INSERT … ON CONFLICT DO UPDATE so the first heartbeat
    creates the row and subsequent ones update it atomically.

    Args:
        db: Async database session.
        site_id: Site identifier from auth context.
        site_name: Human-readable site label from agent config.
        device_count: Total devices the agent is monitoring.
        devices_up: Devices currently reachable.
        devices_down: Devices currently unreachable.
        agent_version: Agent version string from X-Agent-Version header.
        cpu_percent: Host CPU utilisation % (None if not available).
        mem_percent: Host RAM utilisation % (None if not available).
        temp_celsius: Host CPU temperature °C (None if not available).
        agent_model: Platform/model string from the agent.
    """
    now = datetime.now(timezone.utc)
    values = dict(
        site_id=site_id,
        site_name=site_name,
        last_seen=now,
        agent_version=agent_version,
        device_count=device_count,
        devices_up=devices_up,
        devices_down=devices_down,
        cpu_percent=cpu_percent,
        mem_percent=mem_percent,
        temp_celsius=temp_celsius,
        agent_model=agent_model,
    )
    stmt = (
        pg_insert(AgentHeartbeat)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["site_id"],
            set_={k: v for k, v in values.items() if k != "site_id"},
        )
    )
    await db.execute(stmt)
    await db.commit()


async def get_heartbeat(
    db: AsyncSession,
    site_id: str,
) -> Optional[AgentHeartbeat]:
    """
    Return the most recent heartbeat row for a site, or None.

    Args:
        db: Async database session.
        site_id: Site identifier from auth context.

    Returns:
        AgentHeartbeat ORM object, or None if the agent has never connected.
    """
    result = await db.execute(
        select(AgentHeartbeat).where(AgentHeartbeat.site_id == site_id)
    )
    return result.scalar_one_or_none()
