"""
models.py — SpanGate Network Monitor Backend
SQLAlchemy ORM models for all database tables.

Tables
------
devices         — one row per monitored device per site
agent_heartbeat — one row per site, upserted on every heartbeat
configs         — running-config snapshots (30-day retention)
config_diffs    — detected config changes with unified diff text (30-day retention)
alerts          — ping and config-change alert log (30-day retention)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


# ── Device ────────────────────────────────────────────────────────────────────

class Device(Base):
    """
    A monitored network device within a site.

    Devices are created automatically when the backend first receives a ping
    alert or config backup for an unknown hostname.
    """

    __tablename__ = "devices"

    id:                 Mapped[int]            = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id:            Mapped[str]            = mapped_column(String(255), nullable=False, index=True)
    hostname:           Mapped[str]            = mapped_column(String(255), nullable=False)
    ip:                 Mapped[str]            = mapped_column(String(45),  nullable=False)
    vendor:             Mapped[str]            = mapped_column(String(100), nullable=False, default="unknown")
    device_type:        Mapped[str]            = mapped_column(String(100), nullable=False, default="unknown")
    # Live status — written by the ping alert endpoint, persists across restarts
    status:             Mapped[str]            = mapped_column(String(20),  nullable=False, default="unknown")
    last_seen:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_change: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:         Mapped[datetime]       = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    configs:      Mapped[list["Config"]]     = relationship(back_populates="device", cascade="all, delete-orphan")
    config_diffs: Mapped[list["ConfigDiff"]] = relationship(back_populates="device", cascade="all, delete-orphan")
    alerts:       Mapped[list["Alert"]]      = relationship(back_populates="device", cascade="all, delete-orphan")

    __table_args__ = (
        # Unique device within a site
        Index("ix_devices_site_hostname", "site_id", "hostname", unique=True),
    )

    def __repr__(self) -> str:
        return f"<Device site={self.site_id!r} hostname={self.hostname!r}>"


# ── AgentHeartbeat ───────────────────────────────────────────────────────────

class AgentHeartbeat(Base):
    """
    Most-recent heartbeat from the monitoring agent for a site.

    One row per site_id — upserted on every heartbeat POST.
    Replaces the in-memory heartbeat_state dict so state survives
    serverless function restarts.
    """

    __tablename__ = "agent_heartbeat"

    id:            Mapped[int]            = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id:       Mapped[str]            = mapped_column(String(255), unique=True, nullable=False)
    site_name:     Mapped[str]            = mapped_column(String(255), nullable=False, default="")
    last_seen:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), nullable=False)
    agent_version: Mapped[Optional[str]]  = mapped_column(String(50), nullable=True)
    device_count:  Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    devices_up:    Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    devices_down:  Mapped[int]            = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<AgentHeartbeat site={self.site_id!r} last_seen={self.last_seen}>"


# ── Config ────────────────────────────────────────────────────────────────────

class Config(Base):
    """
    A running-config snapshot pulled from a device via SSH.

    One row per weekly backup per device.  Retained for 30 days.
    config_text is never logged — only config_hash is safe to expose.
    """

    __tablename__ = "configs"

    id:          Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id:   Mapped[int]      = mapped_column(
        BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    config_text: Mapped[str]      = mapped_column(Text,       nullable=False)
    config_hash: Mapped[str]      = mapped_column(String(64), nullable=False)
    pulled_at:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    device: Mapped["Device"] = relationship(back_populates="configs")

    __table_args__ = (
        Index("ix_configs_device_pulled", "device_id", "pulled_at"),
        # Index by hash for dedup lookups
        Index("ix_configs_device_hash", "device_id", "config_hash"),
    )

    def __repr__(self) -> str:
        return f"<Config device_id={self.device_id} hash={self.config_hash[:12]!r}>"


# ── ConfigDiff ────────────────────────────────────────────────────────────────

class ConfigDiff(Base):
    """
    A record of a detected configuration change on a device.

    Stores the unified diff so the dashboard can render the change without
    needing to fetch two full config snapshots.  Retained for 30 days.
    """

    __tablename__ = "config_diffs"

    id:          Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id:   Mapped[int]      = mapped_column(
        BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    old_hash:    Mapped[str]      = mapped_column(String(64), nullable=False)
    new_hash:    Mapped[str]      = mapped_column(String(64), nullable=False)
    diff_text:   Mapped[str]      = mapped_column(Text,       nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    device: Mapped["Device"] = relationship(back_populates="config_diffs")

    __table_args__ = (
        Index("ix_config_diffs_device_detected", "device_id", "detected_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ConfigDiff device_id={self.device_id} "
            f"{self.old_hash[:8]!r} → {self.new_hash[:8]!r}>"
        )


# ── Alert ─────────────────────────────────────────────────────────────────────

class Alert(Base):
    """
    An alert event — device went down, came back up, or config changed.

    alert_type is one of: ping_down | ping_up | config_change
    Retained for 30 days.
    """

    __tablename__ = "alerts"

    PING_DOWN     = "ping_down"
    PING_UP       = "ping_up"
    CONFIG_CHANGE = "config_change"

    id:         Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id:  Mapped[int]      = mapped_column(
        BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    alert_type: Mapped[str]      = mapped_column(String(50),  nullable=False)
    message:    Mapped[str]      = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    device: Mapped["Device"] = relationship(back_populates="alerts")

    __table_args__ = (
        Index("ix_alerts_device_created", "device_id", "created_at"),
        # Fast feed queries across all devices in a site
        Index("ix_alerts_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Alert type={self.alert_type!r} device_id={self.device_id}>"
