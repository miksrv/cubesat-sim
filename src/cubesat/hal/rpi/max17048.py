"""X728 UPS HAT: MAX1704x fuel gauge over I2C, AC-loss detection over GPIO.

Two devices, one driver, because they answer one question together: how much
power is there and where is it coming from. Every register and pin here was
verified on the assembled satellite — see ``docs/hardware-x728-ups-hat.md``.

The gauge is a **MAX17040/41**, not the MAX17048 this file is named after. The
2026-08-23 bench note took ``CRATE`` "answering" as proof of a 17048; on
2026-09-01 the raw registers showed it answers ``0xFFFF`` — as do ``HIBRT`` and
``STATUS``, the other two 17048-only registers — which is what an unimplemented
address returns, while ``CONFIG`` reads the 17040/41 factory ``0x9700``. So
the ``charge_rate`` this driver used to publish was ``0xFFFF × 0.208 = −0.208``,
a constant, never a rate. It publishes ``None`` now, and EPS derives the rate
from the voltage slope instead (``eps/slopes.py``, ``common/battery.py``). The
registers read here (``VCELL``, ``SOC``, ``VERSION``) are the ones both
families share.

**What this driver is trusted for is one number: ``VCELL``.** Since 2026-09-04
the state of charge it also reads is reported as ``gauge_percent`` and nothing
acts on it — the percentage the rest of the system uses is derived from the
voltage through a curve, and the power thresholds are volts. That is the honest
end of the story that began with a misidentified part: this board is a voltmeter
with a fuel gauge attached to it, and the voltmeter is the half that works.
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
#: Not read. 0x16 is the MAX17048's CRATE; on this MAX17040/41 the address is
#: unimplemented and answers 0xFFFF (verified 2026-09-01). Kept as a name so
#: nobody re-adds the read believing the register was simply forgotten.
REG_CRATE_ABSENT = 0x16

#: 1.25 mV per LSB after the four unused low bits are shifted out.
VOLTS_PER_LSB = 0.00125
#: The state-of-charge register is in 1/256 of a percent.
SOC_LSB_PER_PERCENT = 256.0

#: AC power-loss detection, BCM numbering. Low means mains is present — the
#: signal is inverted, which is worth stating twice because getting it backwards
#: produces a satellite that shuts itself down while plugged in.
PLD_PIN = 6


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
            voltage=round(voltage, 3),
            external_power=self._external_power(),
            # Read and reported, decided on by nothing. `SOC` is still worth a
            # column because it is the raw output of the part's own model, and
            # comparing it against the curve-derived percentage over a few
            # missions is how that curve gets checked. Clamped because the
            # register can read a little over 100 % on a full charge, and a
            # chart with a 103 % point in it invites a bug hunt in the wrong
            # place.
            gauge_percent=round(min(100.0, max(0.0, percent)), 2),
            # This gauge has no rate register; EPS derives one from the voltage
            # slope. See the module docstring.
            charge_rate=None,
        )

    def close(self) -> None:
        if self._gpio is not None:
            self._gpio.cleanup(self._pld_pin)
            self._gpio = None
