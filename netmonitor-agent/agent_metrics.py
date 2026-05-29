"""
agent_metrics.py — SpanGate Network Monitor Agent
Collects host system metrics: CPU %, RAM %, CPU temperature, and platform model.

All values degrade gracefully — any unavailable metric returns None so the
heartbeat can still be sent even when psutil is absent or a sensor doesn't
exist on the host platform.

Docker note (Raspberry Pi)
--------------------------
The agent runs inside a Docker container.  To expose host-level metrics:

  1. Add ``pid: "host"`` to the docker-compose service so psutil samples the
     host process table rather than the container's cgroup view.
  2. Add ``- /sys/class/thermal:/sys/class/thermal:ro`` to ``volumes`` so
     psutil.sensors_temperatures() can read the Pi's thermal zone.

     Alternatively (or in addition), mount just the single temp file:
       ``- /sys/class/thermal/thermal_zone0/temp:/host/thermal_temp:ro``

  On Windows and macOS the agent runs natively (no Docker layer), so psutil
  reads host metrics directly with no extra configuration.
"""

import logging
import os
import platform
from typing import Optional

logger = logging.getLogger(__name__)

# ── Optional psutil import ────────────────────────────────────────────────────

try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False
    logger.warning("[METRICS] psutil not installed — CPU/RAM metrics unavailable")


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _read_file(path: str) -> Optional[str]:
    """Return stripped file contents or None on any error."""
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


# ── Public metric functions ───────────────────────────────────────────────────

def get_cpu_percent() -> Optional[float]:
    """
    Return host CPU utilisation as a percentage (0–100), or None.

    Uses a 1-second blocking interval for an accurate point-in-time sample.
    On Docker with ``pid: "host"``, this reflects host CPU correctly.
    """
    if not _HAS_PSUTIL:
        return None
    try:
        return round(_psutil.cpu_percent(interval=1), 1)
    except Exception:
        return None


def get_mem_percent() -> Optional[float]:
    """
    Return host RAM utilisation as a percentage (0–100), or None.

    Docker containers inherit /proc/meminfo from the host, so this value
    is accurate even without ``pid: "host"``.
    """
    if not _HAS_PSUTIL:
        return None
    try:
        return round(_psutil.virtual_memory().percent, 1)
    except Exception:
        return None


def get_temp_celsius() -> Optional[float]:
    """
    Return CPU/SoC temperature in °C, or None if unavailable.

    Resolution order:
    1. ``psutil.sensors_temperatures()`` — works on native Linux / Pi, and
       on Docker when ``/sys/class/thermal`` is bind-mounted to the same path.
    2. ``/host/thermal_temp`` — bind-mount target for the single temp file:
       ``/sys/class/thermal/thermal_zone0/temp:/host/thermal_temp:ro``
    3. ``/sys/class/thermal/thermal_zone0/temp`` — native fallback.

    The raw value from the kernel file is in millidegrees Celsius.
    """
    # 1. psutil (requires /sys/class/thermal to be visible inside container)
    if _HAS_PSUTIL:
        try:
            temps = _psutil.sensors_temperatures()
            if temps:
                # Prefer known SoC / CPU sensor names
                for key in ("cpu_thermal", "coretemp", "k10temp", "acpitz", "cpu-thermal"):
                    if key in temps and temps[key]:
                        return round(temps[key][0].current, 1)
                # Fall through to first available sensor
                first = next(iter(temps.values()), None)
                if first:
                    return round(first[0].current, 1)
        except Exception:
            pass

    # 2 + 3. Read the raw millidegrees file from various possible paths
    for path in (
        "/host/thermal_temp",                       # single-file bind mount
        "/sys/class/thermal/thermal_zone0/temp",    # native or full /sys mount
    ):
        raw = _read_file(path)
        if raw:
            try:
                return round(int(raw) / 1000.0, 1)
            except ValueError:
                pass

    return None


def get_agent_model() -> str:
    """
    Return a human-readable agent platform/model string.

    Resolution order:
    1. ``SPNG_AGENT_MODEL`` environment variable — set by ``setup.sh`` on Pi
       using ``cat /proc/device-tree/model`` before the container starts.
    2. ``/proc/device-tree/model`` — readable inside Docker containers on Pi
       (device-tree is a kernel interface, not container-scoped).
    3. ``platform.system() / platform.machine()`` — generic fallback for
       Windows, macOS, and other Linux hosts.
    """
    # 1. Explicit env override (set by setup.sh from host)
    env_model = os.environ.get("SPNG_AGENT_MODEL", "").strip()
    if env_model:
        return env_model

    # 2. Pi device-tree model (often accessible from inside Docker on Pi)
    for path in ("/proc/device-tree/model", "/host/proc/device-tree/model"):
        raw = _read_file(path)
        if raw:
            # Strip trailing NUL bytes the kernel occasionally appends
            return raw.rstrip("\x00").strip()

    # 3. Generic platform string
    sys_name = platform.system()
    machine  = platform.machine()
    if sys_name == "Windows":
        return f"Windows ({machine})"
    if sys_name == "Darwin":
        return f"macOS ({machine})"
    return f"Linux ({machine})"


def collect_metrics() -> dict:
    """
    Collect all host metrics in a single call (blocking ~1 s for CPU sample).

    Returns:
        Dict with keys ``cpu_percent``, ``mem_percent``, ``temp_celsius``,
        ``agent_model``.  Numeric values are ``float | None``; ``agent_model``
        is always a non-empty string.
    """
    return {
        "cpu_percent":  get_cpu_percent(),
        "mem_percent":  get_mem_percent(),
        "temp_celsius": get_temp_celsius(),
        "agent_model":  get_agent_model(),
    }
