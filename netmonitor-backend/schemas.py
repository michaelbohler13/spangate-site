"""
schemas.py — SpanGate Network Monitor Backend
Pydantic v2 models for all request and response bodies.

Inbound (from agent):
    PingAlert, ConfigChange, ConfigBackup, Heartbeat

Outbound (to dashboard):
    DeviceStatus, AlertResponse, DashboardSummary, ConfigMeta, ConfigDetail
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Inbound request schemas (posted by the agent) ─────────────────────────────

class PingAlert(BaseModel):
    """
    Posted by the agent's PingLoop on every device up ↔ down transition.

    Fields mirror api_client.ping_alert() exactly.
    """

    hostname:  str                    = Field(..., max_length=255)
    ip:        str                    = Field(..., max_length=253)  # accepts IPv4, IPv6, or domain name
    status:    Literal["up", "down"]
    timestamp: datetime               # UTC ISO-8601 from the agent clock


class ConfigChange(BaseModel):
    """
    Posted by the agent's SSHPuller when the running-config hash changes.

    Fields mirror api_client.config_changed() exactly.
    """

    hostname:   str = Field(..., max_length=255)
    new_config: str                              # full running-config text after the change
    old_hash:   str = Field(..., min_length=64, max_length=64)   # SHA256 hex of previous config
    new_hash:   str = Field(..., min_length=64, max_length=64)   # SHA256 hex of new config


class ConfigBackup(BaseModel):
    """
    Posted by the agent on every weekly config pull, regardless of change.

    Fields mirror api_client.config_backup() exactly.
    Note: field is named 'config_text' here; the agent sends it as 'config'.
    """

    hostname:    str = Field(..., max_length=255)
    config_text: str = Field(..., alias="config")   # agent sends field as "config"
    config_hash: str = Field(..., alias="hash", min_length=64, max_length=64)

    model_config = ConfigDict(populate_by_name=True)


class Heartbeat(BaseModel):
    """
    Posted by the agent's heartbeat loop every 5 minutes.

    Fields mirror api_client.heartbeat() exactly.
    """

    site_name:    str = Field(..., max_length=255)
    device_count: int = Field(..., ge=0)
    devices_up:   int = Field(..., ge=0)
    devices_down: int = Field(..., ge=0)


# ── Outbound response schemas (returned to the dashboard) ─────────────────────

class DeviceStatus(BaseModel):
    """Current status and metadata for a single monitored device."""

    hostname:         str
    ip:               str
    vendor:           str
    device_type:      str
    status:           str           # "up" | "down" | "unknown"
    last_seen:        Optional[datetime]
    last_config_hash: Optional[str]
    last_ssh_error:   Optional[str]      = None
    last_ssh_at:      Optional[datetime] = None


class SshStatusReport(BaseModel):
    """
    Posted by the agent after each SSH config pull attempt.
    On success, clears any previous error.  On failure, stores the error.
    """

    hostname:     str           = Field(..., max_length=255)
    success:      bool
    error_type:   Optional[str] = None   # "auth_failed" | "timeout" | "other"
    error_detail: Optional[str] = None


class AlertResponse(BaseModel):
    """A single alert event for the alert feed."""

    id:         int
    alert_type: str
    message:    str
    created_at: datetime
    hostname:   str


class ConfigMeta(BaseModel):
    """Config backup metadata (no config_text — used in list views)."""

    id:          int
    config_hash: str
    pulled_at:   datetime


class ConfigDetail(BaseModel):
    """Full config backup including config_text (single-item view only)."""

    id:          int
    config_hash: str
    config_text: str
    pulled_at:   datetime


class DashboardSummary(BaseModel):
    """Aggregate summary returned by GET /api/v1/dashboard."""

    total_devices: int
    devices_up:    int
    devices_down:  int
    recent_alerts: list[AlertResponse]
    last_heartbeat: Optional[datetime]
    agent_version:  Optional[str]
