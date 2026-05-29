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
) -> None:
    """
    Poll the dashboard every DEVICE_POLL_INTERVAL seconds for the latest
    device list and update the shared *devices* list in-place.

    This means devices added, edited, or removed in the dashboard take effect
    within 5 minutes without touching config.yaml or restarting the agent.

    Args:
        api:     Authenticated API client.
        devices: The shared device list mutated in-place on each update.
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
    devices: list[dict[str, Any]],
    ssh_puller: SSHPuller,
) -> None:
    """
    Poll for on-demand backup requests every 60 seconds.

    When the user clicks "Backup Now" in the dashboard, the backend sets
    backup_requested_at on that device.  This loop detects the flag, runs an
    immediate SSH pull via SSHPuller.pull_device_now(), then clears the flag.

    Args:
        api:        Authenticated API client.
        devices:    Shared device list (kept current by device_config_loop).
        ssh_puller: SSHPuller instance used to execute the pull.
    """
    logger.info("[BK] Backup request loop started — checking every 60s")
    while True:
        await asyncio.sleep(60)
        try:
            pending = api.get_pending_backups()
        except Exception:
            continue

        if not pending:
            continue

        # Build a hostname → device-dict lookup from the live device list
        dev_map = {d["hostname"]: d for d in devices}
        loop = asyncio.get_event_loop()

        for req in pending:
            hostname = req.get("hostname", "")
            if hostname in dev_map:
                logger.info("[BK] On-demand backup for %s", hostname)
                await loop.run_in_executor(
                    None, ssh_puller.pull_device_now, dev_map[hostname]
                )
            else:
                # Device was removed from dashboard after the request was made
                logger.warning(
                    "[BK] Backup requested for unconfigured device %s — skipping", hostname
                )
            # Always clear the flag so it doesn't repeat
            api.clear_backup_request(hostname)


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
            heartbeat_loop(api, site_name, len(devices), ping_loop),
            device_config_loop(api, devices),
            backup_request_loop(api, devices, ssh_puller),
        )
    except asyncio.CancelledError:
        logger.info("Agent shut down cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
