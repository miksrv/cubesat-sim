"""BNO055 9-axis absolute orientation sensor at I2C ``0x28``.

The chip runs Bosch's sensor fusion on its own Cortex-M0 and hands back a fused
quaternion and Euler angles, so there is no AHRS filter in this repo. What is
left for the driver is the awkward part: bringing the device up correctly, and
saying how much of its output can be believed.

Two things here are the outcome of a bench session and are recorded in
``docs/hardware-bno055-bmp280-imu.md``:

**The reset is mandatory, not an optimisation.** A device left half-configured —
which is what happens if it was configured while the bus was corrupting reads —
reports ``SYS_STATUS = 1`` with ``SYS_ERR = 9`` and returns all-zero
magnetometer data *while still claiming ``CALIB_STAT`` mag = 3*. Nothing in that
combination looks like a failure from the outside, so the driver never trusts
the state it finds: it resets on every start and waits for ``CHIP_ID`` to answer
again before configuring.

**Heading is a constant until the magnetometer is calibrated.** Below full
``CALIB_STAT`` mag the fused heading reads a fixed value, typically 0.00, so
``read()`` returns ``yaw=None`` and the calibration alongside it. That rule lives
here rather than in ADCS because it is a property of this sensor, not of the
subsystem that happens to read it: a DIAG tool talking straight to the ``Imu``
protocol gets the same protection. Roll and pitch do not depend on the
magnetometer and stay valid throughout.

Deliberately **not** implemented: calibration profile save/restore. The
calibration register block is not in our bench-verified documentation, and
writing unverified registers into the fusion engine on every boot is exactly
what produced the ``SYS_ERR = 9`` session above. It needs a bench pass, not a
guess.

Verified versus inferred
------------------------

Everything in ``docs/hardware-bno055-bmp280-imu.md`` is bench-verified on the
assembled satellite: the configuration and diagnostic registers below, the reset
sequence, the ``CALIB_STAT`` bit layout and every scale factor.

**Bench-verified 2026-08-28, first hardware run (closes ROADMAP V1 and V4):**

- The data register addresses and the two-bytes-per-axis LSB-first signed layout
  produce physically consistent readings across all five blocks.
- The Euler block order is heading, roll, pitch as the datasheet says — **but
  Bosch's roll and pitch name the mirror of the aerospace convention.** Two
  controlled tilts settled it: camera (nose) up 45° moved ``EUL_ROLL`` to −36
  while ``EUL_PITCH`` sat still; right side up 45° moved ``EUL_PITCH`` to −40
  while ``EUL_ROLL`` sat still. The datasheet agrees once you look at the
  ranges: Bosch roll is ±90° (the asin-shaped one — aerospace *pitch*) and
  Bosch pitch ±180° (aerospace *roll*). So this driver publishes
  ``pitch = −EUL_ROLL`` and ``roll = +EUL_PITCH``, giving nose-up-positive
  pitch and right-side-down-positive roll. The quaternion needs no remap: the
  standard ZYX extraction from it matched the tilts as-is.
- The acceleration scale: at rest ``|a|`` ≈ 0.97 g with the accelerometer still
  uncalibrated — a wrong factor would be off by 2× or 100×, not 3 %.
- Sensor axes on the frame: **+X points away from the camera, +Y to the right
  side (viewed from behind the camera), +Z up.**

**The 10 kHz bus still corrupts reads — rarely.** The bit-7 failure that forced
the bus down from 100 kHz (see the hardware doc) is not entirely gone at
10 kHz: during the bench session roughly one read in ten came back with bit 7
of one high byte flipped, observed in the Euler, acceleration *and* quaternion
blocks. A flipped high-byte bit 7 moves a value by half the 16-bit range —
+33 g on an accelerometer axis, −2000° on an angle, ±2 on a quaternion
component — which is why ``read()`` validates every block against physical
plausibility and re-reads on failure rather than publishing a confident
impossibility. A flipped *low* byte bit 7 (an error of 8°, 0.13 g, 0.008 in a
quaternion component) slips through undetected; it is bounded and rare, and no
range check can catch it.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable

from cubesat.hal.i2c import I2CBus, I2CError, shared_bus
from cubesat.hal.interfaces import Attitude, Calibration, Quaternion, Vector3

logger = logging.getLogger(__name__)

ADDRESS = 0x28

#: Configuration and diagnostic registers, all from the hardware document.
REG_CHIP_ID = 0x00
REG_PAGE_ID = 0x07
REG_ST_RESULT = 0x36
REG_CALIB_STAT = 0x35
REG_SYS_STATUS = 0x39
REG_SYS_ERR = 0x3A
REG_OPR_MODE = 0x3D
REG_PWR_MODE = 0x3E
REG_SYS_TRIGGER = 0x3F

#: Data registers. **Not bench-verified** — these come from the Bosch datasheet
#: the hardware document links to, since the document records the scaling but not
#: the addresses, the byte order or the field order. Each block is LSB-first,
#: signed 16-bit per axis. See "Verified versus inferred" above.
REG_ACC_DATA = 0x08  # x, y, z
REG_GYR_DATA = 0x14  # x, y, z
REG_EUL_DATA = 0x1A  # heading, then Bosch-roll, then Bosch-pitch — see the remap note
REG_QUA_DATA = 0x20  # w, x, y, z
REG_TEMP = 0x34  # single signed byte

CHIP_ID = 0xA0
OPR_MODE_CONFIG = 0x00
OPR_MODE_NDOF = 0x0C  # 9-axis fusion
PWR_MODE_NORMAL = 0x00
SYS_TRIGGER_RESET = 0x20
SYS_TRIGGER_INTERNAL_OSC = 0x00
SYS_STATUS_FUSION_RUNNING = 5

#: What ``SYS_STATUS`` means, so an unhealthy device is diagnosable from the log
#: instead of from a datasheet lookup at the wrong moment.
SYS_STATUS_MEANINGS = {
    0: "idle",
    1: "system error",
    2: "initializing peripherals",
    3: "system initialization",
    4: "executing selftest",
    5: "fusion running",
    6: "running without fusion",
}

#: Scaling, all from the hardware document.
EULER_LSB_PER_DEGREE = 16.0
QUATERNION_LSB = float(1 << 14)
ACCEL_LSB_PER_M_S2 = 100.0
GYRO_LSB_PER_DPS = 16.0

#: ``Attitude.accel_g`` is in g, while the chip reports m/s². Standard gravity is
#: a definition rather than a calibration constant, but the *decision* to publish
#: g is ours and not in the bench notes — see "Verified versus inferred" above.
STANDARD_GRAVITY = 9.80665

#: How many times ``read()`` tries before conceding the bus is not cooperating.
#: The detectable-corruption rate reached ~20 % of reads in one bench session —
#: reproduced with a bare ``i2cget`` (one word read in four came back with the
#: low byte's bit 7 flipped: 0x167F → 0x16FF), so this is the bus, not this
#: driver's transaction pattern.
READ_ATTEMPTS = 5

#: The pause between attempts, growing per attempt. Not a courtesy: the flips
#: are phase-correlated, not independent — the same byte came back corrupted on
#: five back-to-back reads (temperature 0x21 → 0xA1, bench 2026-08-28), because
#: an immediate retry lands on the same alignment against the chip's 100 Hz
#: fusion cycle, whose register updates are when it stretches the clock. A
#: growing, non-multiple-of-10-ms pause walks the retry across that phase.
RETRY_BACKOFF_SEC = 0.023

#: Plausibility bounds for the corruption check. A flipped bit 7 in a high byte
#: moves a value by half the 16-bit range, far outside anything a handheld
#: satellite can physically do — these are deliberately loose so no honest
#: reading is ever rejected.
MAX_ACCEL_G = 8.0
MAX_GYRO_DPS = 1000.0
QUATERNION_NORM_TOLERANCE = 0.15
TEMPERATURE_RANGE_C = (-40.0, 85.0)  # the chip's own operating range

#: The power-on reset takes about 650 ms; a full second is the value that was
#: used on the bench. The mode change needs a further moment to settle.
RESET_SETTLE_SEC = 1.0
MODE_SETTLE_SEC = 0.3
#: Polling for CHIP_ID after the reset. Counted in attempts rather than against
#: a clock so the wait is deterministic and testable.
CHIP_ID_ATTEMPTS = 20
CHIP_ID_POLL_SEC = 0.05


def _signed16(low: int, high: int) -> int:
    raw = (high << 8) | low
    return raw - 0x10000 if raw & 0x8000 else raw


def _signed8(raw: int) -> int:
    return raw - 0x100 if raw & 0x80 else raw


def _axes(block: list[int], scale: float) -> tuple[float, float, float]:
    """Three signed LSB-first words from one register block, scaled."""
    x, y, z = (_signed16(block[i], block[i + 1]) / scale for i in (0, 2, 4))
    return x, y, z


class BNO055:
    """Reads fused orientation from the BNO055, resetting it first every time."""

    def __init__(
        self,
        bus: I2CBus | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._bus = bus if bus is not None else shared_bus()
        self._sleep = sleep
        self._configured = False
        #: None until the first reading, so the first one always says out loud
        #: whether the heading can be trusted yet.
        self._heading_usable: bool | None = None

    # ── bring-up ────────────────────────────────────────────────────────────

    def _ensure_configured(self) -> None:
        """Configure once per process — and retry until it succeeds.

        ``_configured`` is set only after the whole sequence went through, so a
        device that was missing at start-up and appeared later still gets its
        reset. Being a cadence late is cheaper than reading a chip whose fusion
        engine was never configured.
        """
        if self._configured:
            return
        self._configure()
        self._configured = True

    def _configure(self) -> None:
        """The exact sequence from the hardware document.

        Note what is *not* here: a ``transaction()`` around the whole thing. The
        sleeps add up to more than a second, and holding a 10 kHz bus lock that
        long would stall EPS mid-poll. Each write is atomic on its own, and no
        other process configures this chip.
        """
        self._bus.write_byte(ADDRESS, REG_OPR_MODE, OPR_MODE_CONFIG)
        self._bus.write_byte(ADDRESS, REG_SYS_TRIGGER, SYS_TRIGGER_RESET)
        self._sleep(RESET_SETTLE_SEC)
        self._await_chip_id()
        self._bus.write_byte(ADDRESS, REG_PAGE_ID, 0x00)
        self._bus.write_byte(ADDRESS, REG_PWR_MODE, PWR_MODE_NORMAL)
        self._bus.write_byte(ADDRESS, REG_SYS_TRIGGER, SYS_TRIGGER_INTERNAL_OSC)
        self._bus.write_byte(ADDRESS, REG_OPR_MODE, OPR_MODE_NDOF)
        self._sleep(MODE_SETTLE_SEC)
        logger.info("BNO055 reset and configured for 9-axis fusion")

    def _await_chip_id(self) -> None:
        """Wait for the chip to come back with ``CHIP_ID = 0xA0``.

        A resetting BNO055 is simply not on the bus for a while, so a failed
        read here is expected rather than fatal. Continuing before it answers is
        how the half-configured state gets created in the first place.
        """
        for _ in range(CHIP_ID_ATTEMPTS):
            try:
                if self._bus.read_byte(ADDRESS, REG_CHIP_ID) == CHIP_ID:
                    return
            except I2CError as exc:
                logger.debug("BNO055 still resetting: %s", exc)
            self._sleep(CHIP_ID_POLL_SEC)
        raise I2CError(
            f"BNO055 did not report CHIP_ID {CHIP_ID:#04x} within "
            f"{CHIP_ID_ATTEMPTS} attempts after reset"
        )

    # ── the Imu protocol ────────────────────────────────────────────────────

    def probe(self) -> bool:
        """Whether the chip answers with the right identity.

        An unhealthy fusion state is logged with both ``SYS_STATUS`` and
        ``SYS_ERR`` — those two numbers together are what made the fusion
        configuration error diagnosable — but it does not fail the probe. The
        chip reports ``initializing`` for a moment after every reset, and
        refusing to work over a transient would trade a working satellite for a
        tidy assumption.
        """
        try:
            self._ensure_configured()
            chip_id = self._bus.read_byte(ADDRESS, REG_CHIP_ID)
        except I2CError as exc:
            logger.error("BNO055 did not answer at %#04x: %s", ADDRESS, exc)
            return False
        if chip_id != CHIP_ID:
            logger.error(
                "BNO055 reports chip id %#04x, expected %#04x", chip_id, CHIP_ID
            )
            return False
        self._log_health()
        return True

    def _log_health(self) -> None:
        try:
            with self._bus.transaction():
                status = self._bus.read_byte(ADDRESS, REG_SYS_STATUS)
                error = self._bus.read_byte(ADDRESS, REG_SYS_ERR)
                selftest = self._bus.read_byte(ADDRESS, REG_ST_RESULT)
        except I2CError as exc:
            logger.debug("BNO055 health registers unreadable: %s", exc)
            return
        if status == SYS_STATUS_FUSION_RUNNING and error == 0:
            logger.info("BNO055 fusion running, self-test %#04x", selftest)
            return
        logger.error(
            "BNO055 unhealthy: SYS_STATUS=%d (%s), SYS_ERR=%d, ST_RESULT=%#04x",
            status,
            SYS_STATUS_MEANINGS.get(status, "unknown"),
            error,
            selftest,
        )

    def read(self) -> Attitude:
        """One coherent attitude sample, validated before it is believed.

        The 10 kHz bus still flips bit 7 of a byte about once in ten reads (see
        the module docstring), and a flipped high byte is half the 16-bit range
        — a confident impossibility that no consumer could tell from a
        measurement. So every block is checked against physical plausibility
        and the whole sample is re-read on failure; only after ``READ_ATTEMPTS``
        consecutive corrupted reads does this raise, at which point the bus
        genuinely is not cooperating.
        """
        self._ensure_configured()
        problem: str | None = "unread"
        for attempt in range(READ_ATTEMPTS):
            if attempt:
                # Decorrelate from the chip's fusion cycle — see RETRY_BACKOFF_SEC.
                self._sleep(RETRY_BACKOFF_SEC * attempt)
            heading, attitude = self._read_once()
            problem = _corruption(heading, attitude)
            if problem is None:
                return attitude
            logger.warning("BNO055 corrupted read (%s); re-reading", problem)
        raise I2CError(
            f"BNO055 returned corrupted data on {READ_ATTEMPTS} consecutive reads ({problem})"
        )

    def _read_once(self) -> tuple[float, Attitude]:
        """One raw sample. The heading comes back separately because the
        ``Attitude`` may honestly withhold it as ``yaw`` while the validation
        still needs to see what the register said.

        Every block is read under a single transaction: an attitude assembled
        from halves taken tens of milliseconds apart is not an attitude, and at
        10 kHz that is exactly how far apart they would be. The bus lock is
        reentrant, so the inner reads nest for free.
        """
        with self._bus.transaction():
            euler = self._bus.read_block(ADDRESS, REG_EUL_DATA, 6)
            quaternion = self._bus.read_block(ADDRESS, REG_QUA_DATA, 8)
            accel = self._bus.read_block(ADDRESS, REG_ACC_DATA, 6)
            gyro = self._bus.read_block(ADDRESS, REG_GYR_DATA, 6)
            temperature = self._bus.read_byte(ADDRESS, REG_TEMP)
            calib_stat = self._bus.read_byte(ADDRESS, REG_CALIB_STAT)

        heading, bosch_roll, bosch_pitch = _axes(euler, EULER_LSB_PER_DEGREE)
        accel_x, accel_y, accel_z = _axes(accel, ACCEL_LSB_PER_M_S2 * STANDARD_GRAVITY)
        gyro_x, gyro_y, gyro_z = _axes(gyro, GYRO_LSB_PER_DPS)
        calibration = _calibration(calib_stat)
        return heading, Attitude(
            # Bosch's names mirror the aerospace convention — bench-verified
            # with two controlled tilts, 2026-08-28 (module docstring). This
            # remap gives nose-up-positive pitch and right-side-down-positive
            # roll, matching what the quaternion says under standard ZYX.
            roll=round(bosch_pitch, 4),
            pitch=round(-bosch_roll, 4),
            yaw=self._heading(heading, calibration),
            quaternion=Quaternion(
                w=round(_signed16(quaternion[0], quaternion[1]) / QUATERNION_LSB, 5),
                x=round(_signed16(quaternion[2], quaternion[3]) / QUATERNION_LSB, 5),
                y=round(_signed16(quaternion[4], quaternion[5]) / QUATERNION_LSB, 5),
                z=round(_signed16(quaternion[6], quaternion[7]) / QUATERNION_LSB, 5),
            ),
            accel_g=Vector3(round(accel_x, 4), round(accel_y, 4), round(accel_z, 4)),
            gyro_dps=Vector3(round(gyro_x, 4), round(gyro_y, 4), round(gyro_z, 4)),
            temperature=float(_signed8(temperature)),
            calibration=calibration,
        )

    def _heading(self, heading: float, calibration: Calibration) -> float | None:
        """The fused heading, or None while the magnetometer is uncalibrated.

        A constant masquerading as a heading is worse than an absent one:
        nothing downstream can tell the two apart, whereas a null is
        self-explanatory when read next to ``calib_status``.
        """
        usable = calibration.heading_usable
        if usable is not self._heading_usable:
            # Once per transition, not twice a second. "Why is yaw null?" is the
            # first question this telemetry provokes, and this is the answer.
            self._heading_usable = usable
            logger.info(
                "BNO055 magnetometer calibration %d/%d — heading %s",
                calibration.mag,
                Calibration.FULL,
                "reported" if usable else "withheld",
            )
        return round(heading, 4) if usable else None


def _corruption(heading: float, attitude: Attitude) -> str | None:
    """Name what is physically impossible about this sample, or None.

    Each bound is loose enough that no honest reading is ever rejected — see
    the constants — so a hit here is evidence of the bit-flip corruption, not a
    judgement about how the satellite is being handled.
    """
    if not 0.0 <= heading <= 360.0:
        return f"heading {heading:.2f} outside 0..360"
    # After the remap: our pitch spans Bosch-roll's ±90, our roll Bosch-pitch's ±180.
    if not -90.0 <= attitude.pitch <= 90.0:
        return f"pitch {attitude.pitch:.2f} outside ±90"
    if not -180.0 <= attitude.roll <= 180.0:
        return f"roll {attitude.roll:.2f} outside ±180"
    q = attitude.quaternion
    norm = math.sqrt(q.w**2 + q.x**2 + q.y**2 + q.z**2)
    if abs(norm - 1.0) > QUATERNION_NORM_TOLERANCE:
        return f"quaternion norm {norm:.3f}"
    a = attitude.accel_g
    magnitude = math.sqrt(a.x**2 + a.y**2 + a.z**2)
    if magnitude > MAX_ACCEL_G:
        return f"|accel| {magnitude:.1f} g"
    g = attitude.gyro_dps
    if max(abs(g.x), abs(g.y), abs(g.z)) > MAX_GYRO_DPS:
        return f"gyro ({g.x:.0f}, {g.y:.0f}, {g.z:.0f}) dps"
    low, high = TEMPERATURE_RANGE_C
    # This driver always sets a temperature; the None in the interface belongs
    # to sensors that may withhold one, and a withheld value is not corrupt.
    if attitude.temperature is not None and not low <= attitude.temperature <= high:
        return f"temperature {attitude.temperature:.0f} degC"
    return None


def _calibration(raw: int) -> Calibration:
    """Unpack ``CALIB_STAT``: two bits each, system, gyro, accel, magnetometer."""
    return Calibration(
        sys=(raw >> 6) & 0x03,
        gyro=(raw >> 4) & 0x03,
        accel=(raw >> 2) & 0x03,
        mag=raw & 0x03,
    )
