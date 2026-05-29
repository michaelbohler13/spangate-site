"""
ssh_puller.py — SpanGate Network Monitor Agent
SSH config-pull loop. Connects to each device via Netmiko, pulls the running
configuration, SHA256-hashes it, detects changes, and ships backups to the API.

Config hashes are stored in memory only — nothing is written to disk.
"""

import asyncio
import hashlib
import logging
from typing import Any

from netmiko import ConnectHandler, NetmikoAuthenticationException, NetmikoTimeoutException

from api_client import APIClient

logger = logging.getLogger(__name__)

# Seconds to stagger SSH pulls between devices to avoid hammering the network
STAGGER_SECONDS = 30

# Map config.yaml vendor names → Netmiko device_type strings
VENDOR_DEVICE_TYPE: dict[str, str] = {
    "cisco":    "cisco_ios",
    "aruba_cx": "aruba_osswitch",
    "juniper":  "juniper_junos",
}

# CLI command to retrieve the running configuration per vendor
VENDOR_COMMAND: dict[str, str] = {
    "cisco":    "show running-config",
    "aruba_cx": "show running-config",
    "juniper":  "show configuration",
}


def _sha256(text: str) -> str:
    """
    Return the SHA256 hex digest of a string.

    Args:
        text: Input string (config text).

    Returns:
        Lowercase hex digest string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pull_config_ssh(device: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Open an SSH session to a device and return its running configuration.

    Args:
        device: Device dict from config.yaml / agent device-list.

    Returns:
        ``(config_text, None)`` on success.
        ``(None, error_type)`` on failure, where error_type is one of:
        "auth_failed", "timeout", or "other".
    """
    hostname = device["hostname"]
    vendor = device.get("vendor", "cisco")
    device_type = VENDOR_DEVICE_TYPE.get(vendor, "cisco_ios")
    command = VENDOR_COMMAND.get(vendor, "show running-config")

    connection_params = {
        "device_type":   device_type,
        "host":          device["ip"],
        "username":      device["ssh_username"],
        "password":      device["ssh_password"],
        "port":          device.get("ssh_port", 22),
        "timeout":       30,
        "conn_timeout":  15,
        "banner_timeout": 20,
    }

    try:
        with ConnectHandler(**connection_params) as conn:
            output = conn.send_command(command, read_timeout=60)
        return output, None
    except NetmikoAuthenticationException:
        logger.error("[SSH] Auth failed for %s — check SSH username/password", hostname)
        return None, "auth_failed"
    except NetmikoTimeoutException:
        logger.error("[SSH] Timeout connecting to %s (%s)", hostname, device["ip"])
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001
        logger.error("[SSH] Unexpected error pulling config from %s: %s", hostname, exc)
        return None, "other"


_SSH_ERROR_MSGS: dict[str, str] = {
    "auth_failed": "Authentication failed — check SSH username and password",
    "timeout":     "Connection timed out — verify IP address and SSH port",
    "other":       "SSH connection failed",
}


class SSHPuller:
    """
    Periodic SSH config-pull loop for all configured devices.

    Config hashes are kept in memory. On each pull cycle the new hash is compared
    to the previous; a change triggers a config-change alert. Every pull also ships
    a backup to the API regardless of whether the config changed.

    Manual/forced backups are handled via queue_backup() + forced_backup_loop(),
    which runs as a separate asyncio task and wakes immediately on demand.
    """

    def __init__(
        self,
        devices: list[dict[str, Any]],
        api_client: APIClient,
        pull_interval: int,
    ) -> None:
        """
        Initialise the SSH puller.

        Args:
            devices: List of device dicts from config.yaml.
            api_client: Authenticated API client instance.
            pull_interval: Seconds between full pull cycles.
        """
        self.devices = devices
        self.api = api_client
        self.interval = pull_interval
        # In-memory hash store: hostname → last known SHA256 hash
        self._hashes: dict[str, str] = {}
        # Forced/manual backup queue
        self._pending_force: set[str] = set()
        self._wake: asyncio.Event = asyncio.Event()

    def _process_device(self, device: dict[str, Any]) -> None:
        """
        Pull config for one device, compare hash, and call the API as needed.

        Reports SSH success/failure to the backend so the dashboard can display
        per-device SSH health.

        Args:
            device: Device dict from config.yaml / agent device-list.
        """
        hostname = device["hostname"]
        config_text, error_type = _pull_config_ssh(device)

        if config_text is None:
            logger.warning("[SSH] Skipping %s — config pull failed (%s)", hostname, error_type)
            self.api.ssh_status(
                hostname,
                success=False,
                error_type=error_type,
                error_detail=_SSH_ERROR_MSGS.get(error_type or "", "SSH error"),
            )
            return

        # Successful pull — report to backend (clears any previous error)
        self.api.ssh_status(hostname, success=True)

        new_hash = _sha256(config_text)
        old_hash = self._hashes.get(hostname)

        if old_hash is None:
            # First successful pull — store hash, ship backup, no change alert
            logger.info("[SSH] First config pull for %s — hash %s", hostname, new_hash[:12])
            self._hashes[hostname] = new_hash
            self.api.config_backup(hostname, config_text, new_hash)
            return

        if new_hash != old_hash:
            logger.info(
                "[SSH] Config CHANGED on %s — old %s → new %s",
                hostname,
                old_hash[:12],
                new_hash[:12],
            )
            self._hashes[hostname] = new_hash
            self.api.config_changed(hostname, config_text, old_hash, new_hash)

        # Always ship backup regardless of change
        self.api.config_backup(hostname, config_text, new_hash)
        logger.info("[SSH] Config backup sent for %s", hostname)

    def pull_device_now(self, device: dict[str, Any]) -> None:
        """
        Trigger an immediate config pull for a device.

        Called by the backup_request_loop when the user clicks "Backup Now"
        in the dashboard.

        Args:
            device: Device dict (same format as agent device-list).
        """
        logger.info("[SSH] On-demand backup requested for %s", device["hostname"])
        self._process_device(device)

    async def _pull_all(self) -> None:
        """
        Pull configs from all SSH-enabled devices sequentially, staggered by
        STAGGER_SECONDS to avoid saturating the network with simultaneous SSH
        sessions.  Devices where ssh_enabled is False are silently skipped so
        ping-only and internet-monitoring entries never trigger SSH attempts.
        """
        ssh_devices = [d for d in self.devices if d.get("ssh_enabled")]
        if not ssh_devices:
            logger.info("[SSH] No SSH-enabled devices — skipping pull cycle")
            return

        for index, device in enumerate(ssh_devices):
            if index > 0:
                await asyncio.sleep(STAGGER_SECONDS)

            hostname = device["hostname"]
            logger.info(
                "[SSH] Pulling config from %s (%d/%d)",
                hostname,
                index + 1,
                len(ssh_devices),
            )

            # Run blocking Netmiko call in thread pool
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._process_device, device)

    def queue_backup(self, hostname: str) -> None:
        """
        Request an immediate manual config backup for a specific device.
        Wakes the forced_backup_loop so the pull happens without waiting
        for the next scheduled cycle.

        Args:
            hostname: Hostname of the device to back up now.
        """
        self._pending_force.add(hostname)
        self._wake.set()
        logger.info("[SSH] Manual backup queued for %s", hostname)

    async def forced_backup_loop(self) -> None:
        """
        Separate async loop that processes manual backup requests as they arrive.
        Runs concurrently with the scheduled run() loop so forced backups are
        handled immediately without interrupting or delaying the weekly cycle.
        """
        logger.info("[SSH] Forced-backup loop started")
        while True:
            await self._wake.wait()
            self._wake.clear()
            pending = list(self._pending_force)
            self._pending_force.clear()

            for hostname in pending:
                device = next((d for d in self.devices if d["hostname"] == hostname), None)
                if device is None:
                    logger.warning("[SSH] Manual backup requested for unknown device: %s", hostname)
                    continue
                if not device.get("ssh_enabled"):
                    logger.warning("[SSH] Manual backup requested for %s but SSH is not enabled", hostname)
                    continue
                logger.info("[SSH] Running manual backup for %s", hostname)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._process_device, device)

    async def run(self) -> None:
        """
        Main async SSH pull loop. Pulls configs on every cycle.
        Runs indefinitely until cancelled.
        """
        ssh_count = sum(1 for d in self.devices if d.get("ssh_enabled"))
        logger.info(
            "[SSH] Pull loop started — %d SSH-enabled device(s) of %d total, interval %ds (%.1f hours)",
            ssh_count,
            len(self.devices),
            self.interval,
            self.interval / 3600,
        )
        while True:
            logger.info("[SSH] Starting config pull cycle")
            await self._pull_all()
            logger.info("[SSH] Pull cycle complete — sleeping %ds", self.interval)
            await asyncio.sleep(self.interval)
