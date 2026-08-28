"""Mock GNSS: a slow walk, so a recorded track is an actual line on a map.

The first minute reports no fix, which is the honest default: acquiring a fix
outdoors takes tens of seconds from cold, and code that has never seen
``fix=False`` handles it badly the first time it happens for real.
"""

from __future__ import annotations

import os
import time

from cubesat.hal.interfaces import Position
from cubesat.hal.mock._signal import wave

#: Somewhere to start walking from. Overridable so a demo can be local.
_ORIGIN_LAT = float(os.getenv("CUBESAT_MOCK_LAT", "56.8389"))
_ORIGIN_LON = float(os.getenv("CUBESAT_MOCK_LON", "60.6057"))
_ACQUIRE_SEC = float(os.getenv("CUBESAT_MOCK_FIX_DELAY_SEC", "60"))


class MockGnss:
    def __init__(self) -> None:
        self._started = time.monotonic()

    def probe(self) -> bool:
        return True

    def read(self) -> Position:
        elapsed = time.monotonic() - self._started
        if elapsed < _ACQUIRE_SEC:
            return Position(None, None, None, None, fix=False, satellites=0)
        # ~1.4 m/s, which is a walking pace, drifting north-east.
        metres = (elapsed - _ACQUIRE_SEC) * 1.4
        return Position(
            lat=round(_ORIGIN_LAT + metres / 111_320.0, 6),
            lon=round(_ORIGIN_LON + metres / 61_000.0, 6),
            alt=round(250.0 + wave(120, 3.0), 1),
            speed=round(1.4 + wave(45, 0.3), 2),
            fix=True,
            satellites=int(9 + wave(300, 4, 4)),
        )
