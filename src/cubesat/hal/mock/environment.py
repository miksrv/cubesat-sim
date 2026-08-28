"""Mock environmental sensor: a room that breathes."""

from __future__ import annotations

from cubesat.hal.interfaces import Environment
from cubesat.hal.mock._signal import drift


class MockEnvironment:
    def probe(self) -> bool:
        return True

    def read(self) -> Environment:
        # The mock reports a resolved index: it stands in for a board whose
        # revision is known. The withheld case is exercised by the driver's own
        # tests, where the ambiguity actually lives.
        raw = int(drift(1800, 0.0, 40.0))
        return Environment(
            temperature=round(drift(900, 21.0, 24.5), 2),
            humidity=round(drift(1200, 38.0, 52.0), 2),
            pressure=round(drift(3600, 1008.0, 1016.0), 2),
            light=round(drift(600, 120.0, 480.0), 1),
            uv_index=round(drift(1800, 0.0, 1.2), 2),
            uv_raw=raw,
        )
