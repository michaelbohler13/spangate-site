"""
pinger.py — SpanGate Network Monitor Agent
Async ICMP ping loop. Pings every monitored device on a fixed interval
and fires an alert to the API on every up↔down status change.

Ping results are held in memory only — nothing is written to disk.
"""

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any

from api_client import APIClient

logger = logging.getLogger(__name__)


def _ping_once(ip: str) -> bool:
    """
    Send a single ICMP echo request using the system ping binary.

    Args:
        ip: Target IP address.

    Returns:
        True if the host replied within the timeout, False otherwise.
    """
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


class PingLoop:
    """
    Async loop that pings all configured devices and fires alerts on state changes.

    Status is tracked in memory only. No disk writes occur.
    """

    def __init__(
        self,
        devices: list[dict[str, Any]],
        api_client: APIClient,
        ping_interval: int,
    ) -> None:
        """
        Initialise the ping loop.

        Args:
            devices: List of device dicts from config.yaml.
            api_client: Authenticated API client instance.
            ping_interval: Seconds between full ping sweeps.
        """
        self.devices = devices
        self.api = api_client
        self.interval = ping_interval
        # In-memory status store: hostname → bool (True = up, None = unknown)
        self._status: dict[str, bool | None] = {
            d["hostname"]: None for d in devices
        }

    def get_status_counts(self) -> tuple[int, int]:
        """
        Return (devices_up, devices_down) based on current in-memory state.

        Returns:
            Tuple of (up_count, down_count). Devices with unknown state count as down.
        """
        up = sum(1 for v in self._status.values() if v is True)
        down = len(self._status) - up
        return up, down

    async def _ping_device(self, device: dict[str, Any]) -> None:
        """
        Ping a single device and fire an alert if its status changed.

        Args:
            device: Device dict with at minimum 'hostname' and 'ip' keys.
        """
        hostname = device["hostname"]
        ip = device["ip"]

        # Run blocking ping in a thread pool so it doesn't stall the event loop
        loop = asyncio.get_event_loop()
        is_up = await loop.run_in_executor(None, _ping_once, ip)

        previous = self._status.get(hostname)

        if previous is None:
            # First ping — record state without firing an alert
            self._status[hostname] = is_up
            status_str = "up" if is_up else "down"
            logger.info("[PING] %s (%s) initial state: %s", hostname, ip, status_str)
            return

        if is_up != previous:
            # Status changed — update memory and fire alert
            self._status[hostname] = is_up
            status_str = "up" if is_up else "down"
            now = datetime.now(timezone.utc)
            logger.info(
                "[PING] %s (%s) status changed → %s at %s",
                hostname,
                ip,
                status_str.upper(),
                now.isoformat(),
            )
            self.api.ping_alert(hostname, ip, status_str, now)

    async def run(self) -> None:
        """
        Main async ping loop. Pings all devices concurrently on each cycle.
        Runs indefinitely until cancelled.
        """
        logger.info(
            "[PING] Loop started — %d device(s), interval %ds",
            len(self.devices),
            self.interval,
        )
        while True:
            tasks = [self._ping_device(device) for device in self.devices]
            await asyncio.gather(*tasks)
            await asyncio.sleep(self.interval)
