"""Assembling a telemetry row, and writing it without ever falling over.

DHS holds no hardware and reads no bus. Every value in a row arrived on MQTT
from the subsystem that owns the device, and DHS caches the latest of each; on
its own tick it flattens those cached payloads into the column set, adds the
host's own health from ``common/metrics.py``, and writes one row.

**A write that fails must not take the recorder down.** A full card, a database
locked for longer than the busy timeout, a corrupt page, a filesystem that has
gone read-only — every one of them is logged and survived. The mission stays
open, the cadence is kept, and the next row is attempted as though nothing
happened, because the alternative is a service that dies on one bad write and
takes the rest of the trip's track with it. A satellite whose recorder went
silent at the first hiccup is exactly the failure this subsystem exists to
prevent, and it is not made better by the process exiting loudly.

**The columns are a view, ``raw_json`` is the record.** A column exists because
something charts it; the raw copy exists because a decision made today about
what is worth charting should not be able to destroy a field measured on a walk
last spring. So the charge rate, the raw UV count, the per-axis calibration and
each source payload's own timestamp all survive in ``raw_json`` even though no
column holds them.

**Nothing here judges a cached value stale.** The cadence table gives every
subsystem a different interval — in ``LOW_POWER`` PAYLOAD reads every 300 s
while ADCS still reads every 5 s — so "older than DHS's own tick" is a rule that
would silently blank the science columns in exactly the state where they are
hardest to come by. Each source payload keeps its own ``timestamp`` inside
``raw_json``, which is what makes the age of any field recoverable afterwards.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from typing import Any

from cubesat.common.metrics import SystemMetrics
from cubesat.dhs.schema import (
    ATTITUDE_COLUMNS,
    RADIO_COLUMNS,
    TELEMETRY_COLUMNS,
    transaction,
    utc_iso,
)

#: Built from the schema's own column tuple rather than written out again. Both
#: halves come from a frozen module-level constant and never from a payload.
_INSERT = "INSERT INTO telemetry ({columns}) VALUES ({placeholders})".format(
    columns=", ".join(TELEMETRY_COLUMNS),
    placeholders=", ".join(f":{column}" for column in TELEMETRY_COLUMNS),
)

_INSERT_ATTITUDE = "INSERT INTO attitude ({columns}) VALUES ({placeholders})".format(
    columns=", ".join(ATTITUDE_COLUMNS),
    placeholders=", ".join(f":{column}" for column in ATTITUDE_COLUMNS),
)

_INSERT_RADIO = "INSERT INTO radio_log ({columns}) VALUES ({placeholders})".format(
    columns=", ".join(RADIO_COLUMNS),
    placeholders=", ".join(f":{column}" for column in RADIO_COLUMNS),
)


class AttitudeBuffer:
    """Attitude samples between two flushes, decimated and bounded.

    Two properties, and each is there for a specific failure:

    **Decimated on arrival, not on write.** ADCS publishes at 2 Hz, and in
    ``DIAG`` — where ``cadence_scale`` is 0.2 against a 0.5 s interval — at 10.
    ``min_interval`` is a single ceiling across every profile and state, so a
    bench session cannot quietly turn into ten rows a second on the card. A
    sample arriving too soon after the last one accepted is dropped here, before
    it costs any memory. Smoothness between the samples that are kept is the
    viewer's job: quaternions interpolate, which is most of why the table stores
    them rather than Euler angles.

    **Bounded.** If writes are failing — a full card, a read-only filesystem —
    the flush does not drain the buffer, and an unbounded one would then grow
    for as long as the service stays up. It is a recorder: staying up through a
    failing card is the whole point, so the memory has to be finite. Past the
    cap the *oldest* samples go, because on a timeline the recent ones are the
    ones a viewer is about to ask for.
    """

    def __init__(self, min_interval: float, capacity: int, log: logging.Logger) -> None:
        self._min_interval = min_interval
        self._log = log
        self._samples: deque[dict[str, Any]] = deque(maxlen=capacity)
        #: The ``t`` of the last sample accepted, so the spacing is measured
        #: against what was kept rather than against what arrived.
        self._last_t: float | None = None
        #: Dropped for arriving too soon, and dropped for overflowing the cap.
        #: Counted separately: the first is the design working, the second is
        #: the card not keeping up, and a status message must not confuse them.
        self.decimated = 0
        self.overflowed = 0

    def offer(self, sample: dict[str, Any] | None) -> bool:
        """Take one sample if it is far enough from the last. Returns whether it was kept."""
        if sample is None:
            return False
        t = sample["t"]
        if self._last_t is not None:
            if t < self._last_t:
                # A clock that went backwards — an NTP step landing on a
                # satellite that has just found a network. Tested before the
                # spacing, because a backwards jump also reads as "too soon" and
                # would otherwise wedge the buffer shut until wall time caught
                # up, which for a large step is the rest of the trip.
                self._log.info(
                    "attitude timestamp went backwards; taking the new one as the reference"
                )
            elif t - self._last_t < self._min_interval:
                self.decimated += 1
                return False
        if len(self._samples) == self._samples.maxlen:
            self.overflowed += 1
        self._samples.append(sample)
        self._last_t = t
        return True

    def drain(self) -> list[dict[str, Any]]:
        """Everything buffered, in arrival order, leaving the buffer empty."""
        drained = list(self._samples)
        self._samples.clear()
        return drained

    def restore(self, samples: list[dict[str, Any]]) -> None:
        """Put a failed batch back, oldest first, respecting the cap."""
        self._samples.extendleft(reversed(samples))

    def __len__(self) -> int:
        return len(self._samples)


class RadioBuffer:
    """Radio events between two flushes. Bounded, never decimated.

    No ``min_interval``, unlike :class:`AttitudeBuffer`, and that is the point
    of keeping them separate: attitude is a continuous signal where any sample
    stands for its neighbours, while a radio session is discrete events — the
    one packet dropped by decimation could be the uplink somebody is trying to
    find in the log. The bound alone protects memory; radio traffic is a beacon
    a minute in ``NOMINAL``, so the cap is hours of a failing card away. Past
    it the *oldest* events go, as in the attitude buffer and for the same
    reason.
    """

    def __init__(self, capacity: int) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        #: Dropped for overflowing the cap — the card not keeping up.
        self.overflowed = 0

    def offer(self, event: dict[str, Any] | None) -> bool:
        """Take one event. Returns whether it was kept."""
        if event is None:
            return False
        if len(self._events) == self._events.maxlen:
            self.overflowed += 1
        self._events.append(event)
        return True

    def drain(self) -> list[dict[str, Any]]:
        """Everything buffered, in arrival order, leaving the buffer empty."""
        drained = list(self._events)
        self._events.clear()
        return drained

    def restore(self, events: list[dict[str, Any]]) -> None:
        """Put a failed batch back, oldest first, respecting the cap."""
        self._events.extendleft(reversed(events))

    def __len__(self) -> int:
        return len(self._events)


class Recorder:
    """Writes rows into one open database, and counts what it has written."""

    def __init__(self, conn: sqlite3.Connection, log: logging.Logger) -> None:
        self._conn = conn
        self._log = log
        #: Rows this recorder has written since it was constructed, and the
        #: number of writes that failed. Both are reported, because a recorder
        #: that is failing every write looks identical from the outside to one
        #: with nothing to record.
        self.written = 0
        self.failed = 0
        #: Counted apart from ``written``: one telemetry row and sixty attitude
        #: samples are not sixty-one of the same thing, and a status message
        #: that added them up would say the recorder is doing sixty times more
        #: than it is.
        self.attitude_written = 0
        #: Radio events, apart from both for the same reason.
        self.radio_written = 0

    def write(self, row: dict[str, Any]) -> bool:
        """Write one assembled row. Returns whether it landed.

        Deliberately broad about ``OSError``: a full or read-only card reaches
        Python as either a ``sqlite3.OperationalError`` or an ``OSError``
        depending on where it was noticed, and telling them apart would only
        change which of two equally survivable messages goes in the log.
        """
        try:
            with transaction(self._conn) as conn:
                conn.execute(_INSERT, row)
        except (sqlite3.Error, OSError):
            self.failed += 1
            self._log.exception(
                "telemetry write failed (%d in this session); the mission stays open",
                self.failed,
            )
            return False
        self.written += 1
        return True

    def write_attitude(self, samples: list[dict[str, Any]]) -> bool:
        """Write a batch of attitude samples. Returns whether the batch landed.

        One transaction for the whole batch, on the tick that was going to open
        one anyway: the point of buffering is that recording at 1 Hz costs the
        same number of card writes as recording at 1/30 Hz did, not sixty times
        as many.

        All or nothing. A partial batch would leave the caller with no way to
        say which samples to put back, and a gap in the middle of a replay is
        worse than a gap at the end of one.
        """
        if not samples:
            return True
        try:
            with transaction(self._conn) as conn:
                conn.executemany(_INSERT_ATTITUDE, samples)
        except (sqlite3.Error, OSError):
            self.failed += 1
            self._log.exception(
                "attitude write failed (%d failures in this session); %d sample(s) held",
                self.failed,
                len(samples),
            )
            return False
        self.attitude_written += len(samples)
        return True

    def write_radio(self, events: list[dict[str, Any]]) -> bool:
        """Write a batch of radio events. Returns whether the batch landed.

        One transaction, all or nothing — the same contract as
        :meth:`write_attitude`, and for the same reason: a partial batch leaves
        the caller with nothing sayable about which events to put back.
        """
        if not events:
            return True
        try:
            with transaction(self._conn) as conn:
                conn.executemany(_INSERT_RADIO, events)
        except (sqlite3.Error, OSError):
            self.failed += 1
            self._log.exception(
                "radio-log write failed (%d failures in this session); %d event(s) held",
                self.failed,
                len(events),
            )
            return False
        self.radio_written += len(events)
        return True

    def count(self) -> int:
        """Rows in the whole table, for the status message."""
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()["n"])


def build_row(
    *,
    mission_id: int | None,
    profile: str | None,
    obc_state: str | None,
    eps: dict[str, Any] | None,
    adcs: dict[str, Any] | None,
    science: dict[str, Any] | None,
    metrics: SystemMetrics,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Flatten the cached payloads into one row of the telemetry table.

    Every column is present on every row, null where the subsystem that owns it
    has said nothing yet. A missing key and a null would otherwise mean the same
    thing to a consumer while being different things to a writer.

    ``mission_id`` is nullable here while the column is ``NOT NULL`` in the
    schema, and that is not a contradiction: since 2026-09-01 this row is also
    published on ``cubesat/dhs/telemetry`` for the dashboard's charts, and in
    ``DEMO``/``EXPO`` there is no mission to belong to. A row with a null
    ``mission_id`` is publishable and deliberately not writable — the database
    itself refuses it, which is the check being relied on rather than a comment
    asking the caller to be careful.
    """
    quaternion = _sub(adcs, "quaternion")
    accel = _sub(adcs, "accel_g")
    gyro = _sub(adcs, "gyro_dps")
    gnss = _sub(adcs, "gnss")
    eps = eps or {}
    science = science or {}
    system = metrics.as_dict()

    row: dict[str, Any] = {
        "timestamp": utc_iso(timestamp),
        "mission_id": mission_id,
        "profile": profile,
        "obc_state": obc_state,
        "battery": _number(eps.get("battery_percent")),
        "voltage": _number(eps.get("voltage")),
        # SQLite has no boolean type, and "true"/1/'t' in one column is how a
        # chart ends up filtering on a string. Stored as 0/1, null if unknown.
        "external_power": _flag(eps.get("external_power")),
        "roll": _number((adcs or {}).get("roll")),
        "pitch": _number((adcs or {}).get("pitch")),
        # Null until the magnetometer is calibrated — ADCS withholds it rather
        # than publishing the constant the BNO055 reports meanwhile.
        "yaw": _number((adcs or {}).get("yaw")),
        "quat_w": _number(quaternion.get("w")),
        "quat_x": _number(quaternion.get("x")),
        "quat_y": _number(quaternion.get("y")),
        "quat_z": _number(quaternion.get("z")),
        "imu_temp": _number((adcs or {}).get("imu_temp")),
        "accel_x": _number(accel.get("x")),
        "accel_y": _number(accel.get("y")),
        "accel_z": _number(accel.get("z")),
        "gyro_x": _number(gyro.get("x")),
        "gyro_y": _number(gyro.get("y")),
        "gyro_z": _number(gyro.get("z")),
        # A four-field object with no column of its own, kept as JSON because it
        # is what explains a null yaw and is worthless split up.
        "calib_status": _json_or_none((adcs or {}).get("calib_status")),
        "lat": _number(gnss.get("lat")),
        "lon": _number(gnss.get("lon")),
        "alt": _number(gnss.get("alt")),
        "speed": _number(gnss.get("speed")),
        # The column the track query filters on: with no fix the coordinates are
        # the last known ones, and drawing them as if they were current would
        # put the satellite somewhere it was not.
        "fix": _flag(gnss.get("fix")),
        "satellites": _integer(gnss.get("satellites")),
        "temperature": _number(science.get("temperature")),
        "humidity": _number(science.get("humidity")),
        "pressure": _number(science.get("pressure")),
        "light": _number(science.get("light")),
        "uv_index": _number(science.get("uv_index")),
        **{key: _number(value) for key, value in system.items()},
        "raw_json": json.dumps(
            {
                "context": {
                    "mission_id": mission_id,
                    "profile": profile,
                    "obc_state": obc_state,
                },
                "eps": eps or None,
                "adcs": adcs,
                "payload": science or None,
                "system": system,
            }
        ),
    }
    return row


def build_attitude(
    adcs: dict[str, Any] | None, *, mission_id: int, now: float
) -> dict[str, Any] | None:
    """One attitude sample out of an ``adcs_status`` payload, or ``None``.

    ``None`` when there is no orientation to record. A row of nine nulls would
    be indistinguishable on a chart from a satellite that was not moving, and
    the IMU being silent is exactly the case ADCS publishes nulls for.

    The sample's ``t`` is the payload's own timestamp — when the IMU was read —
    falling back to now only if the message carried none. At 2 Hz the difference
    between the two is most of the interval, which is the whole reason this
    table exists.
    """
    if adcs is None:
        return None
    quaternion = _sub(adcs, "quaternion")
    gyro = _sub(adcs, "gyro_dps")
    values = {
        "quat_w": _number(quaternion.get("w")),
        "quat_x": _number(quaternion.get("x")),
        "quat_y": _number(quaternion.get("y")),
        "quat_z": _number(quaternion.get("z")),
        "gyro_x": _number(gyro.get("x")),
        "gyro_y": _number(gyro.get("y")),
        "gyro_z": _number(gyro.get("z")),
    }
    if all(value is None for value in values.values()):
        return None
    return {"mission_id": mission_id, "t": _number(adcs.get("timestamp")) or now, **values}


#: The tx kinds COMMS publishes today. An unknown kind is stored as it arrived
#: rather than dropped — the log is a record of what was said on the air, and a
#: build that predates a new kind should still record the traffic.
RADIO_DIRECTIONS = frozenset({"rx", "tx"})


def build_radio_event(
    event: dict[str, Any] | None, *, mission_id: int, now: float
) -> dict[str, Any] | None:
    """One radio_log row out of a ``comms_radio`` payload, or ``None``.

    ``None`` when the payload does not say which direction it is — a session
    log entry that cannot say whether the satellite was talking or listening
    is not an entry, it is noise wearing a schema.

    ``t`` is the event's own timestamp — when the radio transacted — falling
    back to now only if the message carried none, exactly as attitude does.
    """
    if event is None:
        return None
    direction = event.get("direction")
    if direction not in RADIO_DIRECTIONS:
        return None
    text = event.get("text")
    kind = event.get("kind")
    sender = event.get("sender")
    return {
        "mission_id": mission_id,
        "t": _number(event.get("timestamp")) or now,
        "direction": direction,
        "kind": kind if isinstance(kind, str) else None,
        "text": text if isinstance(text, str) else None,
        "bytes": _integer(event.get("bytes")),
        "sender": sender if isinstance(sender, str) else None,
        "snr": _number(event.get("snr")),
        "rssi": _number(event.get("rssi")),
        "hops": _integer(event.get("hops")),
        "sent": _flag(event.get("sent")),
    }


def _sub(payload: dict[str, Any] | None, key: str) -> dict[str, Any]:
    """One nested object out of a payload, or an empty one.

    ADCS publishes a fixed key set with nulls where a device did not answer, so
    ``quaternion`` being explicitly null is the normal shape of a row taken
    while the IMU was silent — not a malformed message.
    """
    value = (payload or {}).get(key)
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    """A float, or None for anything that is not a number.

    ``bool`` is excluded on purpose: it passes ``isinstance(..., int)``, and a
    ``True`` that arrived where a reading was expected should be recorded as
    unknown rather than as 1.0.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _flag(value: Any) -> int | None:
    return None if not isinstance(value, bool) else int(value)


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value)
