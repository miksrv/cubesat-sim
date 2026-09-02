"""Hardware contracts — the seam that lets the whole stack run on a laptop.

These are ``typing.Protocol`` definitions, not base classes: a driver inherits
nothing and imports nothing from here, and a test fake is any object with the
right methods. mypy catches a mismatch; nothing has to be registered anywhere.

Every device also answers ``probe()``. That is what the ``DEPLOY`` self-test
uses to sweep the bus before declaring the satellite ready, instead of finding
out mid-mission that a sensor never answered.

The reading types are frozen dataclasses rather than dicts so that a renamed
field is a type error at the point of use, not a silent ``None`` in a database
column three services away.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Vector3:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Quaternion:
    w: float
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Calibration:
    """BNO055 calibration status, 0–3 per subsystem.

    Worth carrying all the way into telemetry: an uncalibrated magnetometer
    reports a confident heading that is simply wrong, and without this field
    there is no way to tell that from a good one.
    """

    sys: int
    gyro: int
    accel: int
    mag: int

    #: The value a field reaches when that subsystem is fully calibrated.
    FULL = 3

    @property
    def heading_usable(self) -> bool:
        """Whether the fused heading means anything yet.

        Nothing softer than full magnetometer calibration will do. Below it the
        BNO055 does not report a *poor* heading, it reports a constant —
        typically 0.00 — so a threshold of "2 is probably close enough" would
        pass a value that is not an estimate of anything.
        """
        return self.mag >= self.FULL


@dataclass(frozen=True)
class Attitude:
    roll: float
    pitch: float
    #: None until the magnetometer is fully calibrated. Roll and pitch do not
    #: depend on it and stay valid throughout; the heading does not exist yet,
    #: and publishing the sensor's placeholder constant instead would be
    #: indistinguishable downstream from a real measurement.
    yaw: float | None
    quaternion: Quaternion
    accel_g: Vector3
    gyro_dps: Vector3
    temperature: float | None
    calibration: Calibration

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Position:
    """A GNSS reading.

    ``fix`` false means the other fields are the last known values, or None if
    there has never been a fix. Position is never allowed to block a poll loop:
    a stale answer now beats a fresh answer after the cadence has slipped.
    """

    lat: float | None
    lon: float | None
    alt: float | None
    speed: float | None
    fix: bool
    satellites: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Environment:
    temperature: float
    humidity: float
    pressure: float
    light: float
    #: None while the board revision is unknown.
    #:
    #: The SEN0501 exposes one raw UV register that two board revisions read
    #: with different formulas, and they do not disagree slightly: at raw 14 the
    #: V1.0 formula gives 0.00 and the V3.0 one 84.35. Publishing either without
    #: knowing which board this is would be inventing a measurement — so the raw
    #: count is published instead and the index is withheld, the same way a
    #: heading is withheld before the magnetometer is calibrated.
    uv_index: float | None
    #: Always present: the register as read, so an unresolved index is still a
    #: recorded observation rather than a gap in the data.
    uv_raw: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Power:
    battery_percent: float
    voltage: float
    external_power: bool
    #: Signed charge rate in percent per hour: positive charging, negative
    #: draining. A driver fills this only if its gauge measures it; the X728's
    #: MAX17040/41 does not, so the driver leaves it None and EPS derives it from
    #: the state-of-charge history (``eps/charge_rate.py``). None on the wire
    #: means "not known yet", which the power policy reads as "trust the pin".
    charge_rate: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Photo:
    path: Path
    width: int
    height: int
    taken_at: float


#: Meshtastic's per-message ceiling. Lives beside the protocol rather than in
#: each implementation: a limit duplicated across a driver, a mock and a payload
#: builder is a limit that will eventually disagree with itself.
MAX_RADIO_MESSAGE_BYTES = 240


@dataclass(frozen=True)
class RadioMessage:
    """One inbound text message, with what the radio observed about the link.

    Every link field is optional and None when the node did not report it —
    withheld rather than substituted, like every other reading in this project.
    ``snr`` is the field the bench showed to be always present; ``rssi`` is
    sometimes absent, and ``hops`` is derived from packet fields the bench has
    not exercised yet (see the driver), so both lean on None freely.
    """

    text: str
    sender: str | None = None
    snr: float | None = None
    rssi: float | None = None
    #: Mesh hops this packet took to arrive: 0 means heard directly.
    hops: int | None = None


@runtime_checkable
class Device(Protocol):
    def probe(self) -> bool:
        """True if the device answers. Used by the DEPLOY self-test."""
        ...


class Imu(Device, Protocol):
    def read(self) -> Attitude: ...


class Gnss(Device, Protocol):
    def read(self) -> Position: ...


class EnvironmentSensor(Device, Protocol):
    def read(self) -> Environment: ...


class PowerMonitor(Device, Protocol):
    def read(self) -> Power: ...


class Camera(Device, Protocol):
    def capture(self, path: Path, *, overlay: str | None = None) -> Photo: ...
    def close(self) -> None: ...


class Radio(Device, Protocol):
    def send(self, payload: str) -> None: ...
    def poll(self) -> list[RadioMessage]:
        """Return messages received since the last call. Never blocks."""
        ...

    def close(self) -> None: ...
