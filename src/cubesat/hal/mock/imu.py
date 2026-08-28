"""Mock IMU: a satellite tumbling gently, and calibrating as it goes.

The calibration climbs from nothing to full over the first half minute, which is
not decoration. A real BNO055 reports no usable heading until its magnetometer
reaches full calibration, and returns a constant in the meantime — so anything
built against a mock that is calibrated from the first read has never been shown
the state it will actually boot into. This way a running stack exercises both:
``yaw`` is None at first and becomes a heading, exactly as it does on the bench.
"""

from __future__ import annotations

import os
import time

from cubesat.hal.interfaces import Attitude, Calibration, Quaternion, Vector3
from cubesat.hal.mock._signal import drift, wave

#: How long the mock takes to reach full calibration. Roughly what waving the
#: real thing through a figure of eight costs.
_CALIBRATION_SEC = float(os.getenv("CUBESAT_MOCK_CALIBRATION_SEC", "30"))


class MockImu:
    def __init__(self) -> None:
        self._started = time.monotonic()

    def probe(self) -> bool:
        return True

    def _calibration(self) -> Calibration:
        elapsed = time.monotonic() - self._started
        if _CALIBRATION_SEC <= 0:
            return Calibration(sys=3, gyro=3, accel=3, mag=3)
        # Gyroscope and accelerometer settle almost at once on real hardware;
        # the magnetometer is the one that takes the waving.
        progress = min(1.0, elapsed / _CALIBRATION_SEC)
        return Calibration(
            sys=min(3, int(progress * 4)),
            gyro=3,
            accel=3,
            mag=min(3, int(progress * 4)),
        )

    def read(self) -> Attitude:
        calibration = self._calibration()
        return Attitude(
            roll=round(wave(37, 12.0), 3),
            pitch=round(wave(53, 8.0, phase=0.25), 3),
            # None until the magnetometer is there — the same rule the real
            # driver applies, so a consumer cannot be written against a mock
            # that is more generous than the hardware.
            yaw=round(drift(180, 0.0, 360.0), 3) if calibration.heading_usable else None,
            quaternion=Quaternion(
                w=round(wave(37, 0.02, 0.999), 5),
                x=round(wave(37, 0.1, phase=0.1), 5),
                y=round(wave(53, 0.1, phase=0.3), 5),
                z=round(wave(180, 0.1, phase=0.6), 5),
            ),
            accel_g=Vector3(
                x=round(wave(37, 0.05), 4),
                y=round(wave(53, 0.05, phase=0.5), 4),
                z=round(wave(90, 0.02, 0.98), 4),
            ),
            gyro_dps=Vector3(
                x=round(wave(23, 1.5), 3),
                y=round(wave(29, 1.5, phase=0.4), 3),
                z=round(wave(31, 1.5, phase=0.8), 3),
            ),
            temperature=round(drift(600, 28.0, 36.0), 1),
            calibration=calibration,
        )
