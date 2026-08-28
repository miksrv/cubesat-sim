"""X728 UPS HAT: MAX17048 fuel gauge over I2C, AC-loss detection over GPIO.

Two devices, one driver, because they answer one question together: how much
power is there and where is it coming from. Every register and pin here was
verified on the assembled satellite — see ``docs/hardware-x728-ups-hat.md``.

The gauge is a MAX17048 and not the MAX17040 it is often mistaken for: the
``CRATE`` register answers, and that register exists only on the 17048.
"""

from __future__ import annotations

import logging

from cubesat.hal.i2c import I2CBus, I2CError, shared_bus
from cubesat.hal.interfaces import Power

logger = logging.getLogger(__name__)

ADDRESS = 0x36

REG_VCELL = 0x02  # word, MSB first
REG_SOC = 0x04  # word, MSB first
REG_VERSION = 0x08  # reads 0x0002 on this unit
REG_CRATE = 0x16  # signed word, 0.208 %/hour per LSB

#: 1.25 mV per LSB after the four unused low bits are shifted out.
VOLTS_PER_LSB = 0.00125
#: The state-of-charge register is in 1/256 of a percent.
SOC_LSB_PER_PERCENT = 256.0
#: CRATE, per the MAX17048 datasheet.
CRATE_PERCENT_PER_HOUR_PER_LSB = 0.208

#: AC power-loss detection, BCM numbering. Low means mains is present — the
#: signal is inverted, which is worth stating twice because getting it backwards
#: produces a satellite that shuts itself down while plugged in.
PLD_PIN = 6


def _signed16(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


class PowerMonitorX728:
    """Reads battery state and mains presence from the X728 V2.5 UPS HAT."""

    def __init__(self, bus: I2CBus | None = None, pld_pin: int = PLD_PIN) -> None:
        self._bus = bus if bus is not None else shared_bus()
        self._pld_pin = pld_pin
        self._gpio = None
        self._warned_version = False

    # ── I2C ─────────────────────────────────────────────────────────────────

    def _word(self, register: int) -> int:
        """Read a 16-bit MSB-first register as one indivisible transaction.

        Two byte reads rather than a block read, matching what was verified on
        the bench — but wrapped in a transaction so nothing can take the bus
        between the high and low halves. At 10 kHz that gap is wide enough to
        matter, and a torn word here becomes a nonsense battery reading.
        """
        with self._bus.transaction():
            high = self._bus.read_byte(ADDRESS, register)
            low = self._bus.read_byte(ADDRESS, register + 1)
        return (high << 8) | low

    # ── GPIO ────────────────────────────────────────────────────────────────

    def _gpio_module(self):
        if self._gpio is None:
            try:
                import RPi.GPIO as GPIO
            except ImportError as exc:
                raise I2CError(
                    "RPi.GPIO is not installed, so AC-loss detection is unreadable. "
                    "Set CUBESAT_MOCK_HARDWARE=1 to run without hardware."
                ) from exc
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._pld_pin, GPIO.IN)
            self._gpio = GPIO
        return self._gpio

    def _external_power(self) -> bool:
        return self._gpio_module().input(self._pld_pin) == 0

    # ── the PowerMonitor protocol ───────────────────────────────────────────

    def probe(self) -> bool:
        """Whether the fuel gauge answers.

        Any successful read counts. The version is logged rather than asserted:
        refusing to work because a board revision reports something other than
        0x0002 would be trading a working satellite for a tidy assumption.
        """
        try:
            version = self._word(REG_VERSION)
        except I2CError as exc:
            logger.error("MAX17048 did not answer at %#04x: %s", ADDRESS, exc)
            return False
        if version != 0x0002 and not self._warned_version:
            logger.warning("MAX17048 reports version %#06x, expected 0x0002", version)
            self._warned_version = True
        return True

    def read(self) -> Power:
        voltage = (self._word(REG_VCELL) >> 4) * VOLTS_PER_LSB
        percent = self._word(REG_SOC) / SOC_LSB_PER_PERCENT
        return Power(
            # The gauge can read slightly above 100% on a full charge; clamping
            # keeps every downstream threshold and chart honest.
            battery_percent=round(min(100.0, max(0.0, percent)), 2),
            voltage=round(voltage, 3),
            external_power=self._external_power(),
            charge_rate=self._charge_rate(),
        )

    def _charge_rate(self) -> float | None:
        """Signed percent per hour, or None if the register cannot be read.

        A missing charge rate is a nice-to-have gone missing, not a reason to
        lose the battery telemetry that the whole power policy depends on.
        """
        try:
            raw = self._word(REG_CRATE)
        except I2CError as exc:
            logger.debug("CRATE unreadable: %s", exc)
            return None
        return round(_signed16(raw) * CRATE_PERCENT_PER_HOUR_PER_LSB, 3)

    def close(self) -> None:
        if self._gpio is not None:
            self._gpio.cleanup(self._pld_pin)
            self._gpio = None
