"""Mock power monitor, with a discharge you can actually drive.

This is the mock that matters most: LOW_POWER, SAFE and CRITICAL are the whole
reason the mission state machine exists, and they are unreachable in testing
without a battery that falls. By default it discharges from 100% to empty over
an hour, so a service left running crosses every threshold on its own.

    CUBESAT_MOCK_BATTERY=15          pin the level (test SAFE directly)
    CUBESAT_MOCK_DISCHARGE_SEC=120   full to empty in two minutes
    CUBESAT_MOCK_EXTERNAL_POWER=1    mains present, so nothing discharges
"""

from __future__ import annotations

import os
import time

from cubesat.hal.interfaces import Power

_FULL_VOLTS = 4.2
_EMPTY_VOLTS = 3.2


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class MockPowerMonitor:
    def __init__(self) -> None:
        self._started = time.monotonic()
        self._pinned = _env_float("CUBESAT_MOCK_BATTERY")
        self._discharge_sec = _env_float("CUBESAT_MOCK_DISCHARGE_SEC") or 3600.0
        self._external = os.getenv("CUBESAT_MOCK_EXTERNAL_POWER", "0") == "1"

    def probe(self) -> bool:
        return True

    def read(self) -> Power:
        percent = self._level()
        return Power(
            battery_percent=round(percent, 2),
            voltage=round(_EMPTY_VOLTS + (_FULL_VOLTS - _EMPTY_VOLTS) * percent / 100.0, 3),
            external_power=self._external,
            charge_rate=self._rate(),
        )

    def _rate(self) -> float:
        """Percent per hour, consistent with whatever the level is doing."""
        if self._external:
            return 0.0
        if self._pinned is not None:
            return 0.0
        return round(-100.0 * 3600.0 / self._discharge_sec, 2)

    def _level(self) -> float:
        if self._pinned is not None:
            return max(0.0, min(100.0, self._pinned))
        if self._external:
            return 100.0
        elapsed = time.monotonic() - self._started
        return max(0.0, 100.0 * (1.0 - elapsed / self._discharge_sec))
