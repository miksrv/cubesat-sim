"""TEL0157 GPS/BeiDou/GLONASS receiver at I2C ``0x20``.

The module does the GNSS work itself — acquisition, tracking, NMEA parsing — and
exposes the answer as plain registers, so nothing here touches NMEA and
``pynmea2`` has no consumer left.

Everything awkward about this device produces *plausible wrong data* rather than
an error, which is why each of the following is pinned by a test. All four are
recorded in ``docs/hardware-tel0157-gnss.md``.

**The hemisphere bytes are swapped by the firmware.** The latitude block's last
byte carries the longitude hemisphere and vice versa: at a location in
California the latitude register held ``'W'`` and the longitude register held
``'N'``. So the hemisphere is decided by the byte's *content* — whichever of the
two is ``N``/``S`` belongs to the latitude — never by its position. Code that
trusts the position throws away a perfectly valid fix.

**The sign has to be applied by us.** The registers carry an unsigned magnitude;
the vendor library returns exactly that and reports the hemisphere separately,
so a southern latitude comes out looking northern. Combined with the swap, that
is why the bug survived upstream.

**No fix reads as tidy zeros, not as an error.** ``0.000000, 0.000000`` is a real
place in the Gulf of Guinea, so a fix is claimed only when satellites used is
above zero *and* both hemisphere characters are present.

**A read never blocks the poll loop.** With no signal the last known fix is
returned with ``fix=False``: a stale answer now beats a fresh answer after the
cadence has slipped. That contract is inherited from the A9G driver this
replaces, and the tidy zeros make it easy to get wrong.

Verified versus inferred
------------------------

The register map, the degrees/minutes/fraction arithmetic, the hemisphere swap,
the missing sign and the tidy zeros are all bench-verified — see the document.

Two things here are **not** in those notes:

- **The layout of the altitude and speed triplets.** The document says the third
  byte is hundredths; that the first two are a big-endian integer part is
  inferred. It reproduces the bench altitude of 116.59 m exactly, which is
  evidence but not proof — a value above 255 m would tell the difference, and the
  bench reading was taken below that.
- **Speed is converted to m/s.** The register is knots and nothing in the notes
  says what the telemetry should carry, so this is a project decision, not a
  measurement: the rest of the telemetry is SI, DHS is about to store this in a
  column a chart will label, and the mock has always implied m/s. The bench
  recorded 0.00 knots at rest, so the conversion factor itself is pinned by a
  constructed value in the tests rather than by a measured one — a moving fix
  would confirm it for real.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from cubesat.hal.i2c import I2CBus, I2CError, shared_bus
from cubesat.hal.interfaces import Position

logger = logging.getLogger(__name__)

ADDRESS = 0x20

#: Register map from the hardware document. Multi-byte quantities are
#: big-endian and the third byte of a triplet is hundredths.
REG_LATITUDE = 7  # degrees, minutes, minute-fraction (24-bit), hemisphere
REG_LONGITUDE = 13  # same layout
REG_SATELLITES = 19  # satellites used in the solution
#: Three bytes each, and the split is inferred: the document gives only "the
#: third byte is hundredths". See "Verified versus inferred" above.
REG_ALTITUDE = 20  # metres: big-endian integer part, then hundredths
REG_SPEED = 23  # knots, same layout — converted to m/s on the way out
REG_DEVICE_ID = 30  # reads 0x20
REG_CONSTELLATION = 34  # 1..7; 7 is all three
REG_RGB = 36  # onboard RGB LED

DEVICE_ID = 0x20
#: Turn the onboard RGB LED off at bring-up. The satellite is a closed box: the
#: LED lights the inside of its own shell, drawing current and adding heat to a
#: sealed volume for nobody to see. It is a bench affordance, and the bench has
#: its own way back — ``tel0157_gnss_read.py --rgb on``.
#:
#: **Verified visually on the assembled satellite, 2026-08-31:** the LED went
#: dark on the write and the module kept working — a fix with 15 satellites
#: immediately afterwards, so ``0x02`` darkens the LED rather than idling the
#: receiver. Note this had to be an eyeball check: register 36 is **write-only**,
#: reading back ``0x00`` whatever was written to it, so no amount of software
#: can confirm the setting. ``RGB_ON`` is the value the bench script writes and
#: is untested here — the check above only exercised the off path.
RGB_OFF = 0x02
RGB_ON = 0x05
#: GPS + BeiDou + GLONASS. The default is 3 (GPS + BeiDou); moving to 7 took
#: satellites in view from 6 to 9 immediately and raised the SNRs. The mode was
#: measured to survive a power cycle, but it is written on every start anyway:
#: one register write is cheaper than the assumption.
CONSTELLATION_ALL = 7

#: Registers 19..25 in one read: satellites, altitude, speed. Course (26..28) is
#: deliberately not read — ``Position`` has no field for it, and the module
#: retains its last value at rest (210.87° at 0.00 knots on the bench), so it
#: would be a stale number with nowhere useful to go.
_TAIL_LENGTH = REG_SPEED + 3 - REG_SATELLITES

_LAT_HEMISPHERES = ("N", "S")
_LON_HEMISPHERES = ("E", "W")
_NEGATIVE_HEMISPHERES = ("S", "W")

#: The fraction field is minutes × 10⁵.
_MINUTE_FRACTION_SCALE = 100_000.0

#: The register reports knots; ``Position.speed`` carries m/s, like every other
#: quantity in this telemetry. Publishing the raw register would be wrong by a
#: factor of two — the exact kind of plausible error this driver exists to avoid,
#: since a track in knots looks entirely reasonable until it is compared to a map.
M_S_PER_KNOT = 0.514444

_NO_POSITION = Position(lat=None, lon=None, alt=None, speed=None, fix=False, satellites=0)


def _hemisphere(raw: int) -> str:
    """The hemisphere byte as a character, or "" when it holds no character.

    A missing hemisphere is the tell for "no fix yet", so an unprintable byte
    has to come back as absent rather than as some arbitrary ``chr()``.
    """
    return chr(raw) if 0x20 <= raw <= 0x7E else ""


def _magnitude(block: list[int]) -> float:
    """Degrees, from ``dd``, ``mm`` and a 24-bit minute fraction. Unsigned."""
    degrees, minutes = block[0], block[1]
    fraction = (block[2] << 16) | (block[3] << 8) | block[4]
    return degrees + (minutes + fraction / _MINUTE_FRACTION_SCALE) / 60.0


def _hundredths(block: list[int]) -> float:
    """A three-byte triplet: big-endian integer part plus hundredths."""
    return ((block[0] << 8) | block[1]) + block[2] / 100.0


def _resolve_hemispheres(first: str, second: str) -> tuple[str, str]:
    """Sort the two hemisphere characters by content, because they arrive swapped.

    Returns ``(latitude, longitude)``; either is "" if no byte held the
    corresponding character, which is what marks the reading as fixless.
    """
    pair = (first, second)
    latitude = next((c for c in pair if c in _LAT_HEMISPHERES), "")
    longitude = next((c for c in pair if c in _LON_HEMISPHERES), "")
    return latitude, longitude


def _signed(magnitude: float, hemisphere: str) -> float:
    return -magnitude if hemisphere in _NEGATIVE_HEMISPHERES else magnitude


class TEL0157:
    """Reads position from the TEL0157, and never makes ADCS wait for a fix."""

    def __init__(self, bus: I2CBus | None = None) -> None:
        self._bus = bus if bus is not None else shared_bus()
        self._started = False
        self._last = _NO_POSITION

    # ── bring-up ────────────────────────────────────────────────────────────

    def _ensure_started(self) -> None:
        """Write the constellation mode and darken the LED, once per process.

        Both are retried on failure, because neither has happened until the
        write lands: ``_started`` is set only after both succeed.
        """
        if self._started:
            return
        self._bus.write_byte(ADDRESS, REG_CONSTELLATION, CONSTELLATION_ALL)
        self._bus.write_byte(ADDRESS, REG_RGB, RGB_OFF)
        self._started = True
        logger.info("TEL0157 constellation mode set to %d (GPS + BeiDou + GLONASS), LED off",
                    CONSTELLATION_ALL)

    # ── the Gnss protocol ───────────────────────────────────────────────────

    def probe(self) -> bool:
        try:
            device_id = self._bus.read_byte(ADDRESS, REG_DEVICE_ID)
            self._ensure_started()
        except I2CError as exc:
            logger.error("TEL0157 did not answer at %#04x: %s", ADDRESS, exc)
            return False
        if device_id != DEVICE_ID:
            logger.error(
                "TEL0157 reports device id %#04x, expected %#04x", device_id, DEVICE_ID
            )
            return False
        return True

    def read(self) -> Position:
        """The current fix, or the last known one with ``fix=False``.

        The three blocks are read under one transaction — this is the longest
        transaction in the project — so that the coordinates and the satellite
        count describe the same solution rather than two adjacent ones.
        """
        try:
            self._ensure_started()
            with self._bus.transaction():
                latitude = self._bus.read_block(ADDRESS, REG_LATITUDE, 6)
                longitude = self._bus.read_block(ADDRESS, REG_LONGITUDE, 6)
                tail = self._bus.read_block(ADDRESS, REG_SATELLITES, _TAIL_LENGTH)
        except I2CError as exc:
            # Not an error to propagate: ADCS publishes attitude on the same
            # tick, and a subsystem does not go quiet over one silent device.
            logger.warning("TEL0157 read failed (%s); reporting the last known fix", exc)
            return self._stale()

        lat_hemisphere, lon_hemisphere = _resolve_hemispheres(
            _hemisphere(latitude[5]), _hemisphere(longitude[5])
        )
        satellites = tail[0]
        if satellites == 0 or not lat_hemisphere or not lon_hemisphere:
            # The tidy-zeros case. Reporting the fresh satellite count is the
            # point: it is the only visible sign that acquisition is happening.
            logger.debug(
                "TEL0157 has no fix yet (%d satellites, hemispheres %r/%r)",
                satellites,
                lat_hemisphere,
                lon_hemisphere,
            )
            return self._stale(satellites)

        self._last = Position(
            lat=round(_signed(_magnitude(latitude), lat_hemisphere), 6),
            lon=round(_signed(_magnitude(longitude), lon_hemisphere), 6),
            alt=round(_hundredths(tail[1:4]), 2),
            speed=round(_hundredths(tail[4:7]) * M_S_PER_KNOT, 2),
            fix=True,
            satellites=satellites,
        )
        return self._last

    def _stale(self, satellites: int | None = None) -> Position:
        return replace(
            self._last,
            fix=False,
            satellites=self._last.satellites if satellites is None else satellites,
        )
