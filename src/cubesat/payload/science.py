"""Turning an SEN0501 reading into the ``cubesat/payload/data`` body.

Two rules live here rather than in the service, because both are about what the
measurement *means* and not about when it is taken.

**A failed read publishes nothing at all.** Unlike a GNSS fix there is no useful
last-known environment: a temperature from ten minutes ago is indistinguishable
in the payload from a current one, and DHS would happily write it into a chart
as if it were fresh. So the reading is either taken now or it is missing, and
its absence is what the retained ``payload_status`` reports.

**Altitude is not in this payload.** The SEN0501 can produce one from its
pressure register, and the vendor library does, but against a hard-coded
1015.0 hPa reference — which makes it a number that looks like a height and is
not one. The register also holds whole hectopascals, about 8 m per bit. Pressure
goes out as pressure; altitude comes from the GNSS receiver in ``adcs_status``,
which measures it. If a barometric altitude is ever wanted, it needs a real
local reference pressure fed in from somewhere, and it should be published under
a name that says so.
"""

from __future__ import annotations

import logging
from typing import Any

from cubesat.hal.interfaces import Environment, EnvironmentSensor


def read(sensor: EnvironmentSensor, log: logging.Logger) -> Environment | None:
    """One reading, or None if the sensor could not be read.

    Broad by intent, the same way ADCS reads its two devices: a driver may raise
    anything at all, and whatever it was, PAYLOAD still owns a camera and still
    has to keep its heartbeat going. One dead device degrades the payload; it
    does not take the subsystem off the bus.
    """
    try:
        return sensor.read()
    except Exception:
        log.exception("SEN0501 read failed")
        return None


def payload_from(reading: Environment) -> dict[str, Any]:
    """The documented body, plus the raw UV count.

    ``uv_raw`` travels beside ``uv_index`` for the same reason ``calib_status``
    travels beside ``yaw``: it is the only thing that explains the null. A
    consumer seeing ``uv_index: null`` cannot otherwise tell "the sensor was not
    read" from "the board revision is unknown", and the raw count also means the
    question can be settled later from data already recorded.
    """
    data = reading.as_dict()
    return {
        "temperature": data["temperature"],
        "humidity": data["humidity"],
        "pressure": data["pressure"],
        "light": data["light"],
        # Null until the board revision is known — the driver's judgement, not
        # this module's. See hal/rpi/sen0501.py.
        "uv_index": data["uv_index"],
        "uv_raw": data["uv_raw"],
    }
