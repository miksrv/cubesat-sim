"""SEN0501 multifunctional environmental sensor at I2C ``0x22``.

Five measurements from one module — temperature, humidity, atmospheric
pressure, ambient light and UV — read as plain 16-bit big-endian registers.
Nothing here is clever; what the module needs is honesty about two values it
would rather report than it should.

**The UV index is withheld until the board revision is known.** The SEN0501
ships with two different UV elements behind one identical register: an LTR390 on
V1.0 and an S12DS on V3.0. The two conversions do not disagree slightly — at the
bench's ``raw = 14`` the V1.0 formula gives ``0.00`` and the V3.0 one ``84.35``.
Picking one without knowing which board is soldered on this satellite would be
inventing a measurement, so ``uv_index`` is None until
``CUBESAT_SEN0501_REVISION`` says which board this is, exactly the way the
BNO055 driver withholds a heading it cannot justify. ``uv_raw`` is published
regardless: an unresolved index is still a recorded observation, and a raw count
in the telemetry is what lets the question be settled from data already
collected rather than from a second bench session.

Both formulas are implemented, and the revision is a **setting**
(``science.sen0501_revision`` in ``config.yaml``, or ``CUBESAT_SEN0501_REVISION``
in the environment) rather than a constant here. Settling the question is then a
config change and not a code change — which matters, because the thing that
settles it is one reading in direct sunlight, taken by whoever is holding the
satellite, and asking that person to edit a driver is asking for a local edit
that never comes back.

**Altitude is not published, and that is deliberate.** DFRobot's
``get_elevation()`` reads this same pressure register and applies the barometric
formula against a hard-coded reference of 1015.0 hPa rather than the real local
sea-level pressure, so as an absolute height the number is meaningless. The
register also holds whole hectopascals — roughly 8 m of altitude per
least-significant bit. Pressure is published as pressure, a weather trend; the
altitude in this telemetry comes from the GNSS receiver, which measures it.

Verified versus inferred
------------------------

Everything below is transcribed from ``docs/hardware-sen0501-environmental-sensor.md``
and reproduces its verified bench reading of 2026-08-23 exactly: 27.96 °C from
raw 27324, 41.21 % from raw 27008, 1000 hPa, 388.96 lux from raw 377, and both
UV interpretations of raw 14. Those numbers are pinned by tests.

Two things are **not** in those notes:

- **The five registers are read inside one transaction, as five separate 2-byte
  block reads.** The document specifies the per-register read; that the five
  belong to one held transaction is this project's decision, so the five values
  describe one moment rather than five moments spread across a 10 kHz bus with
  three other processes on it. Reading ``0x10``–``0x19`` as a single ten-byte
  block would be faster and is probably fine, but nothing in the notes says the
  device supports it, so it is not done here.
- **``_UV_V3_DIVISOR``** is reproduced verbatim from the vendor formula. The
  document gives ``nanoamps = millivolts * 1e9 / 4303300`` without naming the
  units of the constant, and re-deriving it from a guess about transimpedance
  would change the number while looking like a tidy-up.
"""

from __future__ import annotations

import logging

from cubesat.common import config
from cubesat.hal.i2c import I2CBus, I2CError, shared_bus
from cubesat.hal.interfaces import Environment

logger = logging.getLogger(__name__)

ADDRESS = 0x22

#: Register map from the hardware document. Every quantity is 16-bit big-endian
#: and read as a 2-byte block.
REG_DEVICE_ID = 0x04  # reads 0x0022
REG_UV = 0x10  # raw count; the index depends on the board revision
REG_LIGHT = 0x12  # ambient light, via the quartic below
REG_TEMPERATURE = 0x14
REG_HUMIDITY = 0x16
REG_PRESSURE = 0x18  # whole hectopascals

DEVICE_ID = 0x0022

# ── the board revision ──────────────────────────────────────────────────────

REVISION_V1 = "v1"  # LTR390 UV element
REVISION_V3 = "v3"  # S12DS UV element



def revision() -> str | None:
    """The configured board revision, normalised. None means unknown.

    Read on every use rather than captured at import, so the answer a bench
    settles takes effect on the next start of the service and not on the next
    rebuild of anything. Unknown is the honest default: nobody has taken the
    sunlight reading that would settle it, and a default here would be a guess
    wearing a config key's clothes.
    """
    raw = config.SEN0501_REVISION
    return None if raw is None else str(raw).strip().lower()

#: The ADC behind every register on this module.
ADC_COUNTS = 1024.0

#: V1.0 (LTR390): the register is a voltage on a 3.0 V reference, and the index
#: is that voltage's position between a floor and a ceiling.
UV_V1_REFERENCE_VOLTS = 3.0
UV_V1_FLOOR_VOLTS = 0.99
UV_V1_CEILING_VOLTS = 2.9
UV_V1_FULL_SCALE_INDEX = 15.0

#: V3.0 (S12DS): the register is a voltage on a 3000 mV reference, converted to
#: a photocurrent and then to an index.
UV_V3_REFERENCE_MILLIVOLTS = 3000.0
_UV_V3_DIVISOR = 4303300.0  # vendor constant, units unstated — see the docstring
UV_V3_NANOAMPS_PER_INDEX = 113.0

# ── conversions ─────────────────────────────────────────────────────────────

#: Temperature and humidity share this fixed-point scaling. Kept as the vendor's
#: ``/1024/64`` rather than collapsed to ``/65536`` — the two are equal, and the
#: split form is what makes the formula comparable to DFRobot's library.
_TEMPERATURE_OFFSET_C = -45.0
_TEMPERATURE_SPAN_C = 175.0

#: The ambient-light quartic, coefficient by coefficient as documented. It is
#: not a linear scale factor: at raw 377 the correction is worth 3 % of the
#: reading, and it grows from there.
LUX_COEFFICIENTS = (1.0023, 8.1488e-5, -9.3924e-9, 6.0135e-13)


def _temperature(raw: int) -> float:
    return _TEMPERATURE_OFFSET_C + raw * _TEMPERATURE_SPAN_C / 1024.0 / 64.0


def _humidity(raw: int) -> float:
    return raw / 1024.0 * 100.0 / 64.0


def _light(raw: int) -> float:
    a, b, c, d = LUX_COEFFICIENTS
    return raw * (a + raw * (b + raw * (c + raw * d)))


def uv_index_v1(raw: int) -> float:
    """The V1.0 (LTR390) interpretation, clamped at zero.

    Indoors this formula goes negative — raw 14 works out to −7.45 — and a
    negative UV index is not a measurement of anything. DFRobot's library does
    not clamp it; this project's bench script does, and so does this.
    """
    volts = UV_V1_REFERENCE_VOLTS * raw / ADC_COUNTS
    index = (volts - UV_V1_FLOOR_VOLTS) * UV_V1_FULL_SCALE_INDEX / (
        UV_V1_CEILING_VOLTS - UV_V1_FLOOR_VOLTS
    )
    return max(0.0, index)


def uv_index_v3(raw: int) -> float:
    """The V3.0 (S12DS) interpretation."""
    millivolts = UV_V3_REFERENCE_MILLIVOLTS * raw / ADC_COUNTS
    nanoamps = millivolts * 1e9 / _UV_V3_DIVISOR
    return nanoamps / UV_V3_NANOAMPS_PER_INDEX


class SEN0501:
    """Reads the environmental package, and publishes no number it cannot justify."""

    def __init__(self, bus: I2CBus | None = None) -> None:
        self._bus = bus if bus is not None else shared_bus()
        #: The revision warning is worth saying once and then never again: it is
        #: aimed at a person reading a log on a bench, and PAYLOAD reads this
        #: sensor every 60 seconds for as long as the satellite is up.
        self._warned_revision = False

    # ── bus ─────────────────────────────────────────────────────────────────

    def _word(self, register: int) -> int:
        high, low = self._bus.read_block(ADDRESS, register, 2)
        return (high << 8) | low

    # ── the EnvironmentSensor protocol ──────────────────────────────────────

    def probe(self) -> bool:
        try:
            device_id = self._word(REG_DEVICE_ID)
        except I2CError as exc:
            logger.error("SEN0501 did not answer at %#04x: %s", ADDRESS, exc)
            return False
        if device_id != DEVICE_ID:
            logger.error(
                "SEN0501 reports device id %#06x, expected %#06x", device_id, DEVICE_ID
            )
            return False
        return True

    def read(self) -> Environment:
        """All five measurements from one held transaction.

        A failed read raises rather than returning a half-filled reading: unlike
        the GNSS receiver there is no useful "last known" environment — a stale
        temperature is indistinguishable from a current one — and PAYLOAD is
        written to publish nothing rather than something it did not measure.
        """
        with self._bus.transaction():
            raw_uv = self._word(REG_UV)
            raw_light = self._word(REG_LIGHT)
            raw_temperature = self._word(REG_TEMPERATURE)
            raw_humidity = self._word(REG_HUMIDITY)
            raw_pressure = self._word(REG_PRESSURE)

        return Environment(
            temperature=round(_temperature(raw_temperature), 2),
            humidity=round(_humidity(raw_humidity), 2),
            # Whole hectopascals out of the register, so this is already the
            # sensor's full resolution — there is nothing to round away.
            pressure=float(raw_pressure),
            light=round(_light(raw_light), 2),
            uv_index=self._uv_index(raw_uv),
            uv_raw=raw_uv,
        )

    # ── UV ──────────────────────────────────────────────────────────────────

    def _uv_index(self, raw: int) -> float | None:
        """The index for the configured revision, or None if there is not one.

        The unknown-revision path is not a failure: it is the accurate report of
        what this satellite currently knows about its own sensor.
        """
        configured = revision()
        if configured == REVISION_V1:
            return round(uv_index_v1(raw), 2)
        if configured == REVISION_V3:
            return round(uv_index_v3(raw), 2)
        self._warn_unresolved(raw, configured)
        return None

    def _warn_unresolved(self, raw: int, configured: str | None) -> None:
        """Say, once, which two values the index is being chosen between.

        This message is the one that settles the question: it prints both
        candidates for a real reading, so a bench in direct sunlight only has to
        look at the log to see which of the two is a UV index and which is
        nonsense — a real index tops out near 11, so a value in the eighties is
        not a bright day, it is the wrong formula.
        """
        if self._warned_revision:
            return
        self._warned_revision = True
        if configured is not None:
            logger.warning(
                "sen0501_revision=%r is not one of %r or %r; treating the board "
                "revision as unknown",
                configured,
                REVISION_V1,
                REVISION_V3,
            )
        logger.warning(
            "SEN0501 board revision is unknown, so uv_index is withheld and only uv_raw is "
            "published. Raw %d reads as %.2f on a V1.0 (LTR390) board and %.2f on a V3.0 "
            "(S12DS) board. Take a reading in direct sunlight and set science.sen0501_revision "
            "(or CUBESAT_SEN0501_REVISION) to %r or %r.",
            raw,
            uv_index_v1(raw),
            uv_index_v3(raw),
            REVISION_V1,
            REVISION_V3,
        )
