"""
pinger.py — SpanGate Network Monitor Agent
Async ICMP ping loop. Pings every monitored device on a fixed interval
and fires an alert to the API on every up↔down status change.

Grace-period logic (fail-slow / recover-fast):
    A device must fail DOWN_THRESHOLD consecutive pings before a ping_down
    alert is sent and the device turns red in the dashboard.  A single
    successful ping immediately clears the state and sends ping_up.
    Transient blips that self-resolve within the grace window generate
    zero alerts and zero red flashes.

Ping results are held in memory only — nothing is written to disk.
"""

import asyncio
import logging
import platform
import subprocess
from datetime import datetime, timezone
from typing import Any

from api_client import APIClient

logger = logging.getLogger(__name__)

# Number of consecutive failed pings before a device is declared DOWN.
# At the default 30-second interval: 4 × 30 s = 2-minute grace period.
DOWN_THRESHOLD = 4

# Build the ping command once at import time based on the current OS.
# Linux/macOS: -c (count) and -W (timeout in seconds)
# Windows:     -n (count) and -w (timeout in milliseconds)
_IS_WINDOWS = platform.system() == "Windows"


def _ping_once(ip: str) -> bool:
    """
    Send a single ICMP echo request using the system ping binary.

    Uses platform-appropriate flags: Linux/macOS use ``-c``/``-W``,
    Windows uses ``-n``/``-w``.

    Args:
        ip: Target IP address.

    Returns:
        True if the host replied within the timeout, False otherwise.
    """
    if _IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", "1000", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
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

        ping_hosts = [d["hostname"] for d in devices if d.get("ping_enabled", True)]

        # Raw ping result per host: True = up, False = down, None = not yet pinged.
        self._status: dict[str, bool | None] = {h: None for h in ping_hosts}

        # What we have actually reported to the backend.
        # Separate from _status so grace-period failures don't trigger alerts.
        # None = never reported (first-ping or still in grace period).
        self._reported_status: dict[str, bool | None] = {h: None for h in ping_hosts}

        # Consecutive failure counter per host.  Resets to 0 on any success.
        self._fail_counts: dict[str, int] = {h: 0 for h in ping_hosts}

    def get_status_counts(self) -> tuple[int, int]:
        """
        Return (devices_up, devices_down) based on what has been reported to the backend.

        Devices still in the grace period (fail count < DOWN_THRESHOLD) are not
        counted as down yet — they match what the dashboard shows.

        Returns:
            Tuple of (up_count, down_count).
        """
        up   = sum(1 for v in self._reported_status.values() if v is True)
        down = sum(1 for v in self._reported_status.values() if v is False)
        return up, down

    async def _ping_device(self, device: dict[str, Any]) -> None:
        """
        Ping a single device and fire an alert when the confirmed status changes.

        Grace-period rules (fail-slow / recover-fast):
          - UP:   reset failure counter; if backend was told DOWN → send ping_up.
          - DOWN: increment failure counter; only send ping_down after
                  DOWN_THRESHOLD consecutive failures (default 4 × 30 s = 2 min).

        Devices with ping_enabled=False are silently skipped so SSH-only
        entries don't generate spurious down/up ping alerts.

        Args:
            device: Device dict with at minimum 'hostname' and 'ip' keys.
        """
        if not device.get("ping_enabled", True):
            return

        hostname = device["hostname"]
        ip       = device["ip"]

        # Ensure dicts have an entry for devices added after agent start
        if hostname not in self._status:
            self._status[hostname]          = None
            self._reported_status[hostname] = None
            self._fail_counts[hostname]     = 0

        # Run the blocking ping in a thread pool
        loop  = asyncio.get_event_loop()
        is_up = await loop.run_in_executor(None, _ping_once, ip)
        now   = datetime.now(timezone.utc)

        self._status[hostname] = is_up

        if is_up:
            # ── Device is UP ──────────────────────────────────────────────────
            prev_fails = self._fail_counts[hostname]
            self._fail_counts[hostname] = 0   # reset grace counter

            if self._reported_status[hostname] is False:
                # We had declared this device DOWN — report the recovery
                self._reported_status[hostname] = True
                logger.info("[PING] %s (%s) RECOVERED (up)", hostname, ip)
                self.api.ping_alert(hostname, ip, "up", now)

            elif self._reported_status[hostname] is None:
                # First successful ping — register device as UP in dashboard
                self._reported_status[hostname] = True
                logger.info("[PING] %s (%s) initial state: UP", hostname, ip)
                self.api.ping_alert(hostname, ip, "up", now)

            elif prev_fails > 0:
                # Was in grace period and recovered — silent, no alert
                logger.info(
                    "[PING] %s (%s) recovered during grace period (%d/%d fails) — no alert",
                    hostname, ip, prev_fails, DOWN_THRESHOLD,
                )
            # else: stable UP, nothing to do

        else:
            # ── Device is DOWN ────────────────────────────────────────────────
            self._fail_counts[hostname] += 1
            count = self._fail_counts[hostname]

            if self._reported_status[hostname] is None and count < DOWN_THRESHOLD:
                # First-ping failure — start the grace clock, don't report yet
                # (avoids false red on agent startup / device first seen)
                logger.debug(
                    "[PING] %s (%s) first-ping fail %d/%d — waiting for grace period",
                    hostname, ip, count, DOWN_THRESHOLD,
                )

            elif self._reported_status[hostname] is not False and count >= DOWN_THRESHOLD:
                # Grace period expired — device is genuinely down
                self._reported_status[hostname] = False
                logger.warning(
                    "[PING] %s (%s) DOWN confirmed (%d consecutive failures)",
                    hostname, ip, count,
                )
                self.api.ping_alert(hostname, ip, "down", now)

            else:
                # Either still inside the grace window, or already reported down
                logger.debug(
                    "[PING] %s (%s) fail %d/%d%s",
                    hostname, ip, count, DOWN_THRESHOLD,
                    " (already reported down)" if self._reported_status[hostname] is False else " (grace period)",
                )

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
