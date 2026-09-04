"""Mock power monitor, with a discharge you can actually drive.

This is the mock that matters most: LOW_POWER, SAFE and CRITICAL are the whole
reason the mission state machine exists, and they are unreachable in testing
without a battery that falls. By default it discharges from 100% to empty over
an hour, so a service left running crosses every threshold on its own.

    CUBESAT_MOCK_BATTERY=15          pin the level (test SAFE directly)
    CUBESAT_MOCK_DISCHARGE_SEC=120   full to empty in two minutes
    CUBESAT_MOCK_EXTERNAL_POWER=1    mains present, so nothing discharges

The knobs stay in percent because that is how a test says what it wants — "sit
at 15 % and prove SAFE happens" — but the satellite decides on volts, so the
level is converted through the same pack curve the dashboard reads
(``common/battery.py``). A test that pins 15 % therefore gets the voltage a
15 % pack would show, and the policy meets it on its own terms. The curve is
not a straight line, so do not expect the voltage to be linear in the knob.
"""

from __future__ import annotations

import os
import time

from cubesat.common.battery import voltage_from_percent
from cubesat.hal.interfaces import Power


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
            voltage=voltage_from_percent(percent),
            external_power=self._external,
            # A mock gauge that agrees with the curve. The real one does not,
            # which is the entire reason the curve exists — but a mock that
            # disagreed would only be modelling one particular way of lying, and
            # nothing downstream decides on this field any more.
            gauge_percent=round(percent, 2),
            # None, like the real gauge: the X728's MAX17040/41 has no rate
            # register, and EPS derives the rate from the voltage slope. A mock
            # that reported one would exercise a path the hardware lacks.
            charge_rate=None,
        )

    def _level(self) -> float:
        if self._pinned is not None:
            return max(0.0, min(100.0, self._pinned))
        if self._external:
            return 100.0
        elapsed = time.monotonic() - self._started
        return max(0.0, 100.0 * (1.0 - elapsed / self._discharge_sec))
