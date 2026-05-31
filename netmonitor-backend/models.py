"""
models.py — SpanGate Network Monitor Backend
SQLAlchemy ORM models for all database tables.

Tables
------
devices                  — one row per monitored device per site
agent_heartbeat          — one row per site, upserted on every heartbeat
configs                  — running-config snapshots (30-day retention)
config_diffs             — detected config changes with unified diff text (30-day retention)
alerts                   — ping and config-change alert log (30-day retention)
ssh_credential_profiles  — named SSH credential sets assignable to devices
site_members             — team members invited to a site (admin / viewer roles)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
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
    ip:                 Mapped[str]            = mapped_column(String(253), nullable=False)  # IPv4, IPv6, or domain
    vendor:             Mapped[str]            = mapped_column(String(100), nullable=False, default="unknown")
    device_type:        Mapped[str]            = mapped_column(String(100), nullable=False, default="unknown")
    # Live status — written by the ping alert endpoint, persists across restarts
    status:             Mapped[str]            = mapped_column(String(20),  nullable=False, default="unknown")
    last_seen:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_change: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ssh_error:     Mapped[Optional[str]]      = mapped_column(Text, nullable=True)
    last_ssh_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Maintenance mode — suppresses ping alerts and status updates while True
    maintenance:        Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
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
    agent_version: Mapped[Optional[str]]  = mapped_column(String(50),  nullable=True)
    device_count:  Mapped[int]            = mapped_column(Integer,      nullable=False, default=0)
    devices_up:    Mapped[int]            = mapped_column(Integer,      nullable=False, default=0)
    devices_down:  Mapped[int]            = mapped_column(Integer,      nullable=False, default=0)
    # Host health metrics — sent by the agent on every heartbeat
    cpu_percent:   Mapped[Optional[float]] = mapped_column(Float,       nullable=True)
    mem_percent:   Mapped[Optional[float]] = mapped_column(Float,       nullable=True)
    temp_celsius:  Mapped[Optional[float]] = mapped_column(Float,       nullable=True)
    agent_model:   Mapped[Optional[str]]   = mapped_column(String(120), nullable=True)

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
        # Unique index enforces one row per (device, hash) — prevents duplicate backups
        Index("ix_configs_device_hash", "device_id", "config_hash", unique=True),
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


# ── Feedback ──────────────────────────────────────────────────────────────────

class Feedback(Base):
    """
    A feedback / help request submitted via the site's Feedback modal.

    Kept indefinitely (no automatic purge) so every message can be reviewed.
    ip_hash is a one-way SHA-256 digest of the submitter's IP — used for
    server-side rate limiting without storing PII.
    """

    __tablename__ = "feedback"

    id:         Mapped[int]           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name:       Mapped[str]           = mapped_column(String(200), nullable=False)
    email:      Mapped[str]           = mapped_column(String(255), nullable=False)
    subject:    Mapped[str]           = mapped_column(String(100), nullable=False, default="General Feedback")
    message:    Mapped[str]           = mapped_column(Text,        nullable=False)
    ip_hash:    Mapped[Optional[str]] = mapped_column(String(64),  nullable=True)
    created_at: Mapped[datetime]      = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_feedback_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Feedback id={self.id} subject={self.subject!r}>"


# ── SshCredentialProfile ──────────────────────────────────────────────────────

class SshCredentialProfile(Base):
    """
    A named SSH credential set that can be assigned to a group of devices.

    Priority chain (agent uses first that is set):
      per-device ssh_username/password  →  credential profile  →  site default
    """

    __tablename__ = "ssh_credential_profiles"

    id:           Mapped[int]           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id:      Mapped[str]           = mapped_column(String(255), nullable=False, index=True)
    name:         Mapped[str]           = mapped_column(String(100), nullable=False)
    ssh_user:     Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ssh_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    device_configs: Mapped[list["DeviceConfig"]] = relationship(back_populates="credential_profile")

    __table_args__ = (
        Index("ix_ssh_cred_profiles_site_name", "site_id", "name", unique=True),
    )

    def __repr__(self) -> str:
        return f"<SshCredentialProfile site={self.site_id!r} name={self.name!r}>"


# ── DeviceConfig ──────────────────────────────────────────────────────────────

class DeviceConfig(Base):
    """
    A user-configured device to be monitored.

    Created / edited / deleted via the dashboard UI.
    The agent polls GET /api/v1/agent/device-list every few minutes to pick up
    changes without needing a config.yaml edit or restart.

    Separate from the `devices` table, which stores live ping status written
    by the agent.  Both tables share the same site_id key.
    """

    __tablename__ = "device_configs"

    id:           Mapped[int]           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id:      Mapped[str]           = mapped_column(String(255), nullable=False, index=True)
    hostname:     Mapped[str]           = mapped_column(String(255), nullable=False)
    ip:           Mapped[str]           = mapped_column(String(253), nullable=False)  # IPv4, IPv6, or domain
    vendor:       Mapped[str]           = mapped_column(String(100), nullable=False, default="cisco")
    device_type:  Mapped[str]           = mapped_column(String(100), nullable=False, default="cisco_ios")
    ssh_username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ssh_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ssh_port:     Mapped[int]           = mapped_column(Integer,     nullable=False, default=22)
    ping_enabled: Mapped[bool]          = mapped_column(Boolean,     nullable=False, default=True)
    ssh_enabled:  Mapped[bool]          = mapped_column(Boolean,     nullable=False, default=False)
    group_name:           Mapped[Optional[str]]      = mapped_column(String(100), nullable=True)
    credential_profile_id: Mapped[Optional[int]]    = mapped_column(
        BigInteger,
        ForeignKey("ssh_credential_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    backup_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    credential_profile: Mapped[Optional["SshCredentialProfile"]] = relationship(
        back_populates="device_configs"
    )

    __table_args__ = (
        Index("ix_device_configs_site_hostname", "site_id", "hostname", unique=True),
    )

    def __repr__(self) -> str:
        return f"<DeviceConfig site={self.site_id!r} hostname={self.hostname!r}>"


# ── SshTest ───────────────────────────────────────────────────────────────────

class SshTest(Base):
    """
    A short-lived SSH credential test request queued by the dashboard.

    The monitoring agent polls GET /agent/pending-ssh-tests every 10 seconds,
    runs the SSH connection attempt from the LAN, and reports the result back
    via POST /agent/ssh-test-result.  Rows expire automatically after 5 minutes.

    status: "pending" | "success" | "failed" | "expired"
    """

    __tablename__ = "ssh_tests"

    id:           Mapped[int]           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id:      Mapped[str]           = mapped_column(String(255), nullable=False, index=True)
    ip:           Mapped[str]           = mapped_column(String(253), nullable=False)
    vendor:       Mapped[str]           = mapped_column(String(100), nullable=False, default="cisco")
    device_type:  Mapped[str]           = mapped_column(String(100), nullable=False, default="cisco_ios")
    ssh_username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ssh_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ssh_port:     Mapped[int]           = mapped_column(Integer,     nullable=False, default=22)
    status:       Mapped[str]           = mapped_column(String(20),  nullable=False, default="pending")
    result_msg:   Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_ssh_tests_site_status", "site_id", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<SshTest id={self.id} site={self.site_id!r} ip={self.ip!r} status={self.status!r}>"


# ── SiteMember ────────────────────────────────────────────────────────────────

class SiteMember(Base):
    """
    A team member invited to access a site.

    Roles: "admin" (read-write, no team management) | "viewer" (read-only).
    The original site owner is always stored in nm_profiles — not here.

    Lifecycle:
      status="pending"  — invite token generated, email sent / link shared
      status="active"   — invite accepted, member_api_key generated and stored
      status="removed"  — owner revoked access (member_api_key nulled out)
    """

    __tablename__ = "site_members"

    id:              Mapped[int]           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id:         Mapped[str]           = mapped_column(String(255), nullable=False, index=True)
    owner_user_id:   Mapped[str]           = mapped_column(String(255), nullable=False)  # UUID of the site owner
    invited_email:   Mapped[str]           = mapped_column(String(255), nullable=False)
    invited_user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)   # set on accept
    role:            Mapped[str]           = mapped_column(String(20),  nullable=False, default="viewer")
    member_api_key:  Mapped[Optional[str]] = mapped_column(String(255), nullable=True,  unique=True)
    invite_token:    Mapped[Optional[str]] = mapped_column(String(255), nullable=True,  unique=True)
    status:          Mapped[str]           = mapped_column(String(20),  nullable=False, default="pending")
    invited_at:      Mapped[datetime]      = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    accepted_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_site_members_site_id", "site_id"),
        Index("ix_site_members_member_api_key", "member_api_key", unique=True),
        Index("ix_site_members_invite_token",   "invite_token",   unique=True),
    )

    def __repr__(self) -> str:
        return f"<SiteMember site={self.site_id!r} email={self.invited_email!r} role={self.role!r}>"
