"""
state.py — SpanGate Network Monitor Backend
In-memory state shared across routers.  No Redis, no persistence.
All state resets when the process restarts.

Ping status is written by routers/alerts.py and read by routers/devices.py.
Heartbeat state is written by routers/agents.py and read by routers/dashboard.py.
"""

from datetime import datetime
from typing import Optional

# ── Ping status ───────────────────────────────────────────────────────────────
# Structure: {site_id: {hostname: PingEntry}}

class PingEntry:
    """Current known status for one device."""

    __slots__ = ("status", "ip", "last_seen")

    def __init__(self, status: str, ip: str, last_seen: Optional[datetime]) -> None:
        self.status:    str                = status     # "up" | "down"
        self.ip:        str                = ip
        self.last_seen: Optional[datetime] = last_seen

    def to_dict(self) -> dict:
        return {
            "status":    self.status,
            "ip":        self.ip,
            "last_seen": self.last_seen,
        }


# {site_id: {hostname: PingEntry}}
ping_status: dict[str, dict[str, PingEntry]] = {}


def update_ping_status(
    site_id: str,
    hostname: str,
    ip: str,
    status: str,
    last_seen: datetime,
) -> None:
    """
    Update the in-memory ping status for a device.

    Args:
        site_id: Site identifier from auth context.
        hostname: Device hostname.
        ip: Device IP address.
        status: "up" or "down".
        last_seen: UTC datetime of the status event.
    """
    if site_id not in ping_status:
        ping_status[site_id] = {}
    ping_status[site_id][hostname] = PingEntry(status, ip, last_seen)


def get_device_status(site_id: str, hostname: str) -> Optional[PingEntry]:
    """Return the current ping entry for a device, or None if never seen."""
    return ping_status.get(site_id, {}).get(hostname)


def get_site_status_counts(site_id: str) -> tuple[int, int]:
    """
    Return (devices_up, devices_down) for all known devices in a site.

    Returns:
        Tuple of (up_count, down_count).
    """
    devices = ping_status.get(site_id, {})
    up   = sum(1 for e in devices.values() if e.status == "up")
    down = len(devices) - up
    return up, down


# ── Heartbeat state ───────────────────────────────────────────────────────────
# Structure: {site_id: HeartbeatEntry}

class HeartbeatEntry:
    """Most recent heartbeat from the agent for a site."""

    __slots__ = ("site_name", "device_count", "devices_up", "devices_down",
                 "agent_version", "received_at")

    def __init__(
        self,
        site_name: str,
        device_count: int,
        devices_up: int,
        devices_down: int,
        agent_version: str,
        received_at: datetime,
    ) -> None:
        self.site_name    = site_name
        self.device_count = device_count
        self.devices_up   = devices_up
        self.devices_down = devices_down
        self.agent_version = agent_version
        self.received_at  = received_at


# {site_id: HeartbeatEntry}
heartbeat_state: dict[str, HeartbeatEntry] = {}


def update_heartbeat(
    site_id: str,
    site_name: str,
    device_count: int,
    devices_up: int,
    devices_down: int,
    agent_version: str,
    received_at: datetime,
) -> None:
    """
    Update the in-memory heartbeat state for a site.

    Args:
        site_id: Site identifier from auth context.
        site_name: Human-readable site label from agent config.
        device_count: Total devices the agent is monitoring.
        devices_up: Devices currently reachable.
        devices_down: Devices currently unreachable.
        agent_version: X-Agent-Version header value.
        received_at: UTC datetime the heartbeat was received.
    """
    heartbeat_state[site_id] = HeartbeatEntry(
        site_name=site_name,
        device_count=device_count,
        devices_up=devices_up,
        devices_down=devices_down,
        agent_version=agent_version,
        received_at=received_at,
    )


def get_heartbeat(site_id: str) -> Optional[HeartbeatEntry]:
    """Return the most recent heartbeat entry for a site, or None."""
    return heartbeat_state.get(site_id)
