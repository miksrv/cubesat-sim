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

The following are **taken from the Bosch datasheet that document links to, and
are not in our bench notes.** They are called out because a reader cannot
otherwise tell them apart from the verified constants, and each would fail
quietly rather than loudly:

- the data register addresses ``REG_ACC_DATA``, ``REG_GYR_DATA``,
  ``REG_EUL_DATA``, ``REG_QUA_DATA`` and ``REG_TEMP``;
- that each of those blocks is two bytes per axis, LSB first, signed;
- **the order of the Euler block — heading, roll, pitch.** This is the one to
  distrust most: a swapped roll and pitch would look entirely plausible on a
  dashboard, and nothing in the notes would catch it;
- the conversion of acceleration to g. The document's scale is 100 LSB per m/s²;
  ``Attitude.accel_g`` is in g, so the driver divides by standard gravity.

One bench print would settle all four: run ``~/test/bno055_bmp280_read.py``
beside this driver, tilt the board nose-up and confirm that ``pitch`` moves while
``roll`` does not, then check ``|a|`` against 9.8 m/s² at rest.
"""

from __future__ import annotations

import logging
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
REG_EUL_DATA = 0x1A  # heading, roll, pitch — the order to distrust
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
        """One coherent attitude sample.

        Every block is read under a single transaction: an attitude assembled
        from halves taken tens of milliseconds apart is not an attitude, and at
        10 kHz that is exactly how far apart they would be. The bus lock is
        reentrant, so the inner reads nest for free.
        """
        self._ensure_configured()
        with self._bus.transaction():
            euler = self._bus.read_block(ADDRESS, REG_EUL_DATA, 6)
            quaternion = self._bus.read_block(ADDRESS, REG_QUA_DATA, 8)
            accel = self._bus.read_block(ADDRESS, REG_ACC_DATA, 6)
            gyro = self._bus.read_block(ADDRESS, REG_GYR_DATA, 6)
            temperature = self._bus.read_byte(ADDRESS, REG_TEMP)
            calib_stat = self._bus.read_byte(ADDRESS, REG_CALIB_STAT)

        heading, roll, pitch = _axes(euler, EULER_LSB_PER_DEGREE)
        accel_x, accel_y, accel_z = _axes(accel, ACCEL_LSB_PER_M_S2 * STANDARD_GRAVITY)
        gyro_x, gyro_y, gyro_z = _axes(gyro, GYRO_LSB_PER_DPS)
        calibration = _calibration(calib_stat)
        return Attitude(
            roll=round(roll, 4),
            pitch=round(pitch, 4),
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


def _calibration(raw: int) -> Calibration:
    """Unpack ``CALIB_STAT``: two bits each, system, gyro, accel, magnetometer."""
    return Calibration(
        sys=(raw >> 6) & 0x03,
        gyro=(raw >> 4) & 0x03,
        accel=(raw >> 2) & 0x03,
        mag=raw & 0x03,
    )
