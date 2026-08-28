"""Host health metrics, collected with ``psutil``.

Recorded alongside every telemetry row: on a satellite the computer's own
temperature and free space are as much telemetry as the battery is.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class SystemMetrics:
    cpu_percent: float
    ram_percent: float
    swap_percent: float
    disk_percent: float
    uptime_seconds: float
    cpu_temperature: float | None

    def as_dict(self) -> dict[str, float | None]:
        return asdict(self)


def _cpu_temperature() -> float | None:
    """CPU temperature in degrees Celsius, or None where it is not exposed.

    ``psutil.sensors_temperatures`` does not exist on macOS at all, and returns
    an empty mapping on some Linux builds, so both are treated as "unknown"
    rather than as an error: a missing reading must not stop telemetry.
    """
    getter = getattr(psutil, "sensors_temperatures", None)
    if getter is not None:
        try:
            for entries in (getter() or {}).values():
                for entry in entries:
                    if entry.current:
                        return float(entry.current)
        except OSError:
            pass
    # Raspberry Pi always has this, even when psutil reports nothing.
    sysfs = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(sysfs.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def collect(disk_path: str = "/") -> SystemMetrics:
    return SystemMetrics(
        cpu_percent=psutil.cpu_percent(interval=None),
        ram_percent=psutil.virtual_memory().percent,
        swap_percent=psutil.swap_memory().percent,
        disk_percent=psutil.disk_usage(disk_path).percent,
        uptime_seconds=max(0.0, time.time() - psutil.boot_time()),
        cpu_temperature=_cpu_temperature(),
    )
