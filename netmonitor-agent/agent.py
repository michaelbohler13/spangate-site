"""
agent.py — SpanGate Network Monitor Agent v1.0.0
Main entry point. Loads config, starts the ping loop, SSH pull loop,
and heartbeat loop concurrently. Handles SIGINT/SIGTERM gracefully.
"""

import argparse
import asyncio
import logging
import platform
import signal
import sys
from pathlib import Path
from typing import Any

import yaml

from api_client import APIClient
from pinger import PingLoop
from ssh_puller import SSHPuller

AGENT_VERSION = "1.0.0"
HEARTBEAT_INTERVAL   = 300   # 5 minutes
DEVICE_POLL_INTERVAL = 300   # 5 minutes — how often to refresh devices from dashboard
DEFAULT_CONFIG_PATH  = Path(__file__).parent / "config.yaml"

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ── CLI arguments ─────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        Parsed namespace with a ``config`` attribute containing the config path.
    """
    parser = argparse.ArgumentParser(
        description="SpanGate Network Monitor Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=str(DEFAULT_CONFIG_PATH),
        help=(
            "Path to the agent config file "
            f"(default: {DEFAULT_CONFIG_PATH})"
        ),
    )
    return parser.parse_args()


# ── Config loader ─────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict[str, Any]:
    """
    Load and return the agent configuration from a YAML file.

    Args:
        path: Path to config.yaml.

    Returns:
        Parsed configuration dict.

    Raises:
        SystemExit: If the file is missing or cannot be parsed.
    """
    if not path.exists():
        logger.critical("Config file not found: %s", path)
        sys.exit(1)
    try:
        with path.open() as fh:
            cfg = yaml.safe_load(fh)
        return cfg
    except yaml.YAMLError as exc:
        logger.critical("Failed to parse config file: %s", exc)
        sys.exit(1)


def _apply_device_list(
    devices: list[dict[str, Any]],
    new_devices: list[dict[str, Any]],
) -> None:
    """
    Replace the contents of *devices* in-place with *new_devices*.

    Mutates the existing list so PingLoop and SSHPuller — which hold a
    reference to the same object — automatically see the updated list on
    their next iteration.
    """
    devices.clear()
    devices.extend(new_devices)


def validate_config(cfg: dict[str, Any]) -> None:
    """
    Validate required configuration fields and exit with a clear message if any
    are missing or still set to placeholder values.

    Args:
        cfg: Parsed configuration dict.

    Raises:
        SystemExit: If validation fails.
    """
    agent_cfg = cfg.get("agent", {})
    required = ["site_name", "api_url", "api_key"]
    for field in required:
        if not agent_cfg.get(field):
            logger.critical("Missing required config field: agent.%s", field)
            sys.exit(1)
    if agent_cfg["api_key"] == "YOUR_API_KEY_HERE":
        logger.critical(
            "agent.api_key is still the placeholder value. "
            "Set your real API key in config.yaml before starting the agent."
        )
        sys.exit(1)
    # devices section is now optional — agent will fetch from dashboard if absent


# ── Heartbeat loop ────────────────────────────────────────────────────────────

async def device_config_loop(
    api: APIClient,
    devices: list[dict[str, Any]],
    ssh_puller: SSHPuller,
) -> None:
    """
    Poll the dashboard every DEVICE_POLL_INTERVAL seconds for the latest
    device list and update the shared *devices* list in-place.

    On-demand backups ("Backup Now") are handled exclusively by
    backup_request_loop (60-second fast path), not here, to avoid the same
    request being picked up by both loops and triggering duplicate backups.

    Args:
        api:        Authenticated API client.
        devices:    The shared device list mutated in-place on each update.
        ssh_puller: SSHPuller instance (kept for signature compatibility).
    """
    logger.info("[DC] Device config loop started — polling every %ds", DEVICE_POLL_INTERVAL)
    while True:
        await asyncio.sleep(DEVICE_POLL_INTERVAL)
        new_devices = api.get_device_list()
        if new_devices is None:
            logger.warning("[DC] Could not reach backend — keeping current device list (%d devices)", len(devices))
        elif not new_devices:
            logger.info("[DC] Dashboard has no devices configured yet — keeping current list")
        else:
            if new_devices != devices:
                _apply_device_list(devices, new_devices)
                logger.info("[DC] Device list updated from dashboard — %d device(s)", len(devices))
            else:
                logger.debug("[DC] Device list unchanged")


async def backup_request_loop(
    api: APIClient,
    ssh_puller: SSHPuller,
) -> None:
    """
    Fast-path poller (60s) for on-demand backup requests.

    Supplements device_config_loop (5-min cycle): polls the backend every 60s
    for devices with backup_requested_at set and immediately wakes the
    forced_backup_loop via queue_backup(), so "Backup Now" responds within
    60 seconds instead of up to 5 minutes.

    The backend clears backup_requested_at when the device-list is next polled
    by device_config_loop; this loop simply queues the backup without clearing.

    Args:
        api:        Authenticated API client.
        ssh_puller: SSHPuller instance used to queue the backup.
    """
    logger.info("[BK] Backup request loop started — checking every 60s")
    while True:
        await asyncio.sleep(60)
        pending = api.get_pending_backups()
        if not pending:
            continue
        for req in pending:
            hostname = req.get("hostname", "")
            if hostname:
                logger.info("[BK] Fast-path backup queued for %s", hostname)
                ssh_puller.queue_backup(hostname)
                # Clear the flag so device_config_loop doesn't queue it again
                api.clear_backup_request(hostname)


async def ssh_test_loop(
    api: APIClient,
    ssh_puller: SSHPuller,
) -> None:
    """
    Fast-path poller (10 s) for on-demand SSH credential tests.

    When the user clicks "Test SSH Connection" in the dashboard, a test row
    is queued in the backend.  This loop picks it up within 10 seconds,
    runs _pull_config_ssh from the Pi (which has LAN access), and reports
    the result back so the dashboard can show ✓ or ✗ immediately.

    Args:
        api:        Authenticated API client.
        ssh_puller: SSHPuller instance that provides run_connection_test().
    """
    logger.info("[ST] SSH test loop started — polling every 10s")
    while True:
        try:
            await asyncio.sleep(10)
            tests = api.get_pending_ssh_tests()
            if not tests:
                continue
            loop = asyncio.get_event_loop()
            for test in tests:
                logger.info("[ST] Running credential test #%d for %s", test["id"], test["ip"])
                try:
                    success, msg = await loop.run_in_executor(
                        None, ssh_puller.run_connection_test, test
                    )
                    api.report_ssh_test_result(test["id"], success=success, result_msg=msg)
                except Exception as exc:  # noqa: BLE001
                    logger.error("[ST] Test #%d failed unexpectedly: %s", test["id"], exc)
                    api.report_ssh_test_result(test["id"], success=False, result_msg=f"Agent error: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[ST] SSH test loop error: %s", exc)


async def heartbeat_loop(
    api: APIClient,
    site_name: str,
    device_count: int,
    ping_loop: PingLoop,
) -> None:
    """
    Send a heartbeat to the SpanGate API every HEARTBEAT_INTERVAL seconds.

    Args:
        api: Authenticated API client.
        site_name: Human-readable site name from config.
        device_count: Total number of configured devices.
        ping_loop: PingLoop instance used to read current up/down counts.
    """
    logger.info("[HB] Heartbeat loop started — interval %ds", HEARTBEAT_INTERVAL)
    while True:
        devices_up, devices_down = ping_loop.get_status_counts()
        api.heartbeat(site_name, device_count, devices_up, devices_down)
        logger.debug(
            "[HB] Heartbeat sent — %d up / %d down", devices_up, devices_down
        )
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ── Graceful shutdown ─────────────────────────────────────────────────────────

def _handle_signal(signum: int, loop: asyncio.AbstractEventLoop) -> None:
    """
    Cancel all running tasks on SIGINT or SIGTERM.

    Args:
        signum: Signal number received.
        loop: The running event loop.
    """
    sig_name = signal.Signals(signum).name
    logger.info("Agent shutting down (received %s) …", sig_name)
    for task in asyncio.all_tasks(loop):
        task.cancel()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    """
    Load config and run the ping, SSH pull, and heartbeat loops concurrently.
    """
    args = parse_args()
    config_path = Path(args.config)

    cfg = load_config(config_path)
    validate_config(cfg)

    agent_cfg = cfg["agent"]
    devices: list[dict[str, Any]] = cfg["devices"]

    site_name: str = agent_cfg["site_name"]
    api_url: str = agent_cfg["api_url"]
    api_key: str = agent_cfg["api_key"]
    ping_interval: int = int(agent_cfg.get("ping_interval", 30))
    pull_interval: int = int(agent_cfg.get("config_pull_interval", 604800))

    logger.info(
        "SpanGate Network Monitor Agent v%s — %s — %d device(s)",
        AGENT_VERSION,
        site_name,
        len(devices),
    )

    api = APIClient(api_url, api_key)

    # ── Device list: try dashboard first, fall back to config.yaml ────────────
    remote_devices = api.get_device_list()
    if remote_devices:
        logger.info(
            "Loaded %d device(s) from dashboard (config.yaml devices section ignored)",
            len(remote_devices),
        )
        devices = remote_devices
    elif devices:
        logger.info(
            "Dashboard returned no devices — using %d device(s) from config.yaml",
            len(devices),
        )
    else:
        logger.warning(
            "No devices in dashboard or config.yaml — add devices at "
            "%s/netmonitor/dashboard and the agent will pick them up within 5 minutes.",
            api_url,
        )

    ping_loop = PingLoop(devices, api, ping_interval)
    ssh_puller = SSHPuller(devices, api, pull_interval)

    loop = asyncio.get_event_loop()

    # loop.add_signal_handler is not supported on Windows (raises NotImplementedError).
    # On Windows, asyncio handles KeyboardInterrupt (Ctrl+C) natively; NSSM sends a
    # termination event that Python catches as SystemExit, so graceful shutdown still works.
    if platform.system() != "Windows":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal, sig, loop)

    try:
        await asyncio.gather(
            ping_loop.run(),
            ssh_puller.run(),
            ssh_puller.forced_backup_loop(),
            heartbeat_loop(api, site_name, len(devices), ping_loop),
            device_config_loop(api, devices, ssh_puller),
            backup_request_loop(api, ssh_puller),
            ssh_test_loop(api, ssh_puller),
        )
    except asyncio.CancelledError:
        logger.info("Agent shut down cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
