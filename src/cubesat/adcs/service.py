"""ADCS — Attitude Determination and Control System, minus the control.

*Where and how the satellite is*, which is why orientation and position live in
one subsystem: the BNO055 at ``0x28`` and the TEL0157 at ``0x20``. Sensing only.
There are no reaction wheels and no magnetorquers on this satellite, and none is
planned — the "C" is in the name because that is what the subsystem is called.

Three properties of this service are load-bearing:

**One dead device degrades the payload, it does not silence the subsystem.** A
GNSS receiver that cannot be read must not cost us the attitude, and vice versa.
The two are read independently and the payload carries nulls where a device did
not answer — the same treatment ``max17048.py`` gives an unreadable ``CRATE``.
Only when *both* are silent is nothing published, because then there is nothing
to say and ADCS' silence is what OBC reacts to.

**The bus is never held across both devices.** Each driver takes the advisory
lock for its own multi-byte reads and releases it before the next device is
touched. A GNSS block is the longest transaction in the project, and holding the
lock across both would keep EPS off a 10 kHz bus for no reason.

**OBC's DEPLOY waits for the first ``adcs_status``.** That message is the
evidence that the IMU and the receiver answered to the process that owns them,
and the bring-up window is bounded, so the first tick must not be deferred.

A null ``yaw`` is not this service's doing: the driver withholds the heading
while the magnetometer is uncalibrated, because that is a property of the sensor
rather than of the subsystem reading it, and ADCS publishes what it is handed.
``calib_status`` travels beside it and is what explains the null.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from cubesat.common.service import Service
from cubesat.hal import registry
from cubesat.hal.interfaces import Attitude, Gnss, Imu, Position

_Reading = TypeVar("_Reading")


class AdcsService(Service):
    name = "adcs"
    cadence_key = "adcs"

    def __init__(self, imu: Imu | None = None, gnss: Gnss | None = None) -> None:
        super().__init__()
        self._imu = imu if imu is not None else registry.imu()
        self._gnss = gnss if gnss is not None else registry.gnss()

    def on_start(self) -> None:
        """Say which device answered, and stay up either way.

        A vanished process takes its heartbeat with it, and then OBC cannot tell
        a broken sensor from a broken service. Staying up means the missing
        field in ``adcs_status`` is what it has to reason about.
        """
        for label, device in (("BNO055 orientation", self._imu), ("TEL0157 GNSS", self._gnss)):
            if device.probe():
                self.log.info("%s answered", label)
            else:
                self.log.error("%s is not answering; its telemetry will be unavailable", label)

    def tick(self) -> None:
        attitude = self._read(self._imu.read, "IMU")
        position = self._read(self._gnss.read, "GNSS")
        if attitude is None and position is None:
            # Nothing to publish. The gap on adcs_status is the signal; an empty
            # payload would tell OBC's DEPLOY that hardware answered when none
            # did, which is the one lie this message must never carry.
            self.log.error("neither the IMU nor the GNSS receiver could be read")
            return
        self.publish("adcs_status", **_payload(attitude, position))

    def _read(self, read: Callable[[], _Reading], label: str) -> _Reading | None:
        """Read one device, or None if it failed.

        Broad by intent: a driver may raise anything, and whatever it was, the
        other device's reading still has to reach the ground.
        """
        try:
            return read()
        except Exception:
            self.log.exception("%s read failed", label)
            return None


def _payload(attitude: Attitude | None, position: Position | None) -> dict[str, Any]:
    """The ``cubesat/adcs/status`` body, with nulls for whatever went missing.

    The key set is fixed whether a device answered or not: a consumer reading a
    column out of this should not have to distinguish "absent" from "null", and
    DHS writes a row of the same shape every time.
    """
    fields: dict[str, Any] = {
        "roll": None,
        "pitch": None,
        "yaw": None,
        "quaternion": None,
        "calib_status": None,
        "imu_temp": None,
        "accel_g": None,
        "gyro_dps": None,
        "gnss": None,
    }
    if attitude is not None:
        data = attitude.as_dict()
        fields.update(
            roll=data["roll"],
            pitch=data["pitch"],
            # Already None if the driver judged the magnetometer uncalibrated —
            # whether a heading exists is the sensor's business, not ours.
            yaw=data["yaw"],
            quaternion=data["quaternion"],
            # Always published, even when yaw is not: it is the only thing that
            # explains the null, and it is what says how much to trust the rest.
            calib_status=data["calibration"],
            imu_temp=data["temperature"],
            accel_g=data["accel_g"],
            gyro_dps=data["gyro_dps"],
        )
    if position is not None:
        fields["gnss"] = position.as_dict()
    return fields
