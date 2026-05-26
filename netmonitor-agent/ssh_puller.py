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


def _pull_config_ssh(device: dict[str, Any]) -> str | None:
    """
    Open an SSH session to a device and return its running configuration.

    Args:
        device: Device dict from config.yaml.

    Returns:
        Configuration text on success, None on any failure.
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
        return output
    except NetmikoAuthenticationException:
        logger.error("[SSH] Auth failed for %s — check credentials", hostname)
    except NetmikoTimeoutException:
        logger.error("[SSH] Timeout connecting to %s (%s)", hostname, device["ip"])
    except Exception as exc:  # noqa: BLE001
        logger.error("[SSH] Unexpected error pulling config from %s: %s", hostname, exc)
    return None


class SSHPuller:
    """
    Periodic SSH config-pull loop for all configured devices.

    Config hashes are kept in memory. On each pull cycle the new hash is compared
    to the previous; a change triggers a config-change alert. Every pull also ships
    a backup to the API regardless of whether the config changed.
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

    def _process_device(self, device: dict[str, Any]) -> None:
        """
        Pull config for one device, compare hash, and call the API as needed.

        Args:
            device: Device dict from config.yaml.
        """
        hostname = device["hostname"]
        config_text = _pull_config_ssh(device)

        if config_text is None:
            logger.warning("[SSH] Skipping %s — config pull failed", hostname)
            return

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

        # Always ship weekly backup regardless of change
        self.api.config_backup(hostname, config_text, new_hash)
        logger.info("[SSH] Config backup sent for %s", hostname)

    async def _pull_all(self) -> None:
        """
        Pull configs from all devices sequentially, staggered by STAGGER_SECONDS
        to avoid saturating the network with simultaneous SSH sessions.
        """
        for index, device in enumerate(self.devices):
            if index > 0:
                await asyncio.sleep(STAGGER_SECONDS)

            hostname = device["hostname"]
            logger.info(
                "[SSH] Pulling config from %s (%d/%d)",
                hostname,
                index + 1,
                len(self.devices),
            )

            # Run blocking Netmiko call in thread pool
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._process_device, device)

    async def run(self) -> None:
        """
        Main async SSH pull loop. Pulls configs on every cycle.
        Runs indefinitely until cancelled.
        """
        logger.info(
            "[SSH] Pull loop started — %d device(s), interval %ds (%.1f hours)",
            len(self.devices),
            self.interval,
            self.interval / 3600,
        )
        while True:
            logger.info("[SSH] Starting config pull cycle")
            await self._pull_all()
            logger.info("[SSH] Pull cycle complete — sleeping %ds", self.interval)
            await asyncio.sleep(self.interval)
