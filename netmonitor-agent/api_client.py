"""
api_client.py — SpanGate Network Monitor Agent
HTTP client for posting data to the SpanGate backend API.
All requests are authenticated with the customer API key and retried on failure.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

AGENT_VERSION = "1.0.0"
MAX_RETRIES = 3
RETRY_BACKOFF = 5  # seconds


class APIClient:
    """Authenticated HTTP client for the SpanGate backend API."""

    def __init__(self, api_url: str, api_key: str) -> None:
        """
        Initialise the API client.

        Args:
            api_url: Base URL for the SpanGate backend (e.g. https://api.spangate.com).
            api_key: Customer API key from the SpanGate dashboard.
        """
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "X-Agent-Version": AGENT_VERSION,
                "Content-Type": "application/json",
            }
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _post(self, endpoint: str, payload: dict[str, Any]) -> bool:
        """
        POST JSON payload to an endpoint with retry logic.

        Args:
            endpoint: API path (e.g. /api/v1/alerts/ping).
            payload: Dictionary to serialise as JSON.

        Returns:
            True if the request succeeded, False after all retries exhausted.
        """
        url = f"{self.api_url}{endpoint}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.post(url, json=payload, timeout=10)
                response.raise_for_status()
                return True
            except requests.RequestException as exc:
                logger.error(
                    "API request failed (attempt %d/%d) POST %s — %s",
                    attempt,
                    MAX_RETRIES,
                    endpoint,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF)
        return False

    # ── Public API methods ────────────────────────────────────────────────────

    def ping_alert(
        self,
        hostname: str,
        ip: str,
        status: str,
        timestamp: datetime,
    ) -> bool:
        """
        Report a device up/down status change.

        Args:
            hostname: Device hostname.
            ip: Device IP address.
            status: "up" or "down".
            timestamp: UTC datetime of the status change.

        Returns:
            True if successfully delivered.
        """
        payload = {
            "hostname": hostname,
            "ip": ip,
            "status": status,
            "timestamp": timestamp.isoformat(),
        }
        logger.debug("Posting ping alert: %s is %s", hostname, status)
        return self._post("/api/v1/alerts/ping", payload)

    def config_changed(
        self,
        hostname: str,
        new_config: str,
        old_hash: str,
        new_hash: str,
    ) -> bool:
        """
        Report that a device's running configuration has changed.

        Args:
            hostname: Device hostname.
            new_config: Full running-config text after the change.
            old_hash: SHA256 hex digest of the previous config.
            new_hash: SHA256 hex digest of the new config.

        Returns:
            True if successfully delivered.
        """
        payload = {
            "hostname": hostname,
            "new_config": new_config,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.debug("Posting config change alert for %s", hostname)
        return self._post("/api/v1/alerts/config-change", payload)

    def config_backup(
        self,
        hostname: str,
        config_text: str,
        config_hash: str,
    ) -> bool:
        """
        Upload the weekly config backup for a device.

        Args:
            hostname: Device hostname.
            config_text: Full running-config text.
            config_hash: SHA256 hex digest of config_text.

        Returns:
            True if successfully delivered.
        """
        payload = {
            "hostname": hostname,
            "config": config_text,
            "hash": config_hash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.debug("Posting config backup for %s", hostname)
        return self._post("/api/v1/configs/backup", payload)

    def get_device_list(self) -> list[dict] | None:
        """
        Fetch the latest device list from the dashboard.

        Returns:
            List of device dicts on success, or None on failure.
            An empty list means the user has no devices configured yet.
        """
        url = f"{self.api_url}/api/v1/agent/device-list"
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.json().get("devices", [])
        except requests.RequestException as exc:
            logger.warning("Failed to fetch device list from backend: %s", exc)
            return None

    def get_pending_backups(self) -> list[dict] | None:
        """
        Fetch devices that have a pending on-demand backup request.

        Returns:
            List of device dicts on success (same format as get_device_list),
            or None on failure.  Empty list means no pending requests.
        """
        url = f"{self.api_url}/api/v1/agent/pending-backups"
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.json().get("devices", [])
        except requests.RequestException as exc:
            logger.warning("Failed to fetch pending backups: %s", exc)
            return None

    def clear_backup_request(self, hostname: str) -> bool:
        """
        Clear the backup_requested_at flag for a device after the on-demand
        backup has completed (or failed).

        Args:
            hostname: Device hostname to clear.

        Returns:
            True if successfully delivered.
        """
        return self._post(
            "/api/v1/agent/clear-backup-request",
            {"hostname": hostname},
        )

    def ssh_status(
        self,
        hostname: str,
        success: bool,
        error_type: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        """
        Report the result of an SSH config-pull attempt to the backend.

        Args:
            hostname: Device hostname.
            success: True if the pull succeeded, False otherwise.
            error_type: "auth_failed" | "timeout" | "other" (only on failure).
            error_detail: Human-readable error message (only on failure).

        Returns:
            True if successfully delivered.
        """
        payload: dict = {
            "hostname":     hostname,
            "success":      success,
            "error_type":   error_type,
            "error_detail": error_detail,
        }
        return self._post("/api/v1/agent/ssh-status", payload)

    def get_pending_ssh_tests(self) -> list[dict]:
        """
        Fetch pending SSH credential tests queued by the dashboard.

        The agent polls this every 10 seconds and runs each test immediately.

        Returns:
            List of test dicts (id, ip, vendor, device_type, ssh_username,
            ssh_password, ssh_port).  Returns an empty list on any error so
            the caller can safely iterate without extra error handling.
        """
        url = f"{self.api_url}/api/v1/agent/pending-ssh-tests"
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.json().get("tests", [])
        except requests.RequestException as exc:
            logger.warning("[ST] Failed to fetch pending SSH tests: %s", exc)
            return []

    def report_ssh_test_result(
        self,
        test_id: int,
        success: bool,
        result_msg: str,
    ) -> bool:
        """
        Report the outcome of an SSH credential test back to the backend.

        Args:
            test_id:    ID of the test row returned by POST /test-ssh.
            success:    True if SSH connected and pulled config successfully.
            result_msg: Human-readable result shown to the user.

        Returns:
            True if successfully delivered.
        """
        return self._post(
            "/api/v1/agent/ssh-test-result",
            {"test_id": test_id, "success": success, "result_msg": result_msg},
        )

    def heartbeat(
        self,
        site_name: str,
        device_count: int,
        devices_up: int,
        devices_down: int,
    ) -> bool:
        """
        Send a periodic heartbeat so the dashboard knows the agent is alive.

        Args:
            site_name: Human-readable site label from config.
            device_count: Total number of monitored devices.
            devices_up: Number of devices currently reachable.
            devices_down: Number of devices currently unreachable.

        Returns:
            True if successfully delivered.
        """
        payload = {
            "site_name": site_name,
            "device_count": device_count,
            "devices_up": devices_up,
            "devices_down": devices_down,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.debug("Sending heartbeat for site '%s'", site_name)
        return self._post("/api/v1/agent/heartbeat", payload)
