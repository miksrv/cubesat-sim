import pytest

from cubesat.hal import i2c
from cubesat.hal.i2c import I2CError
from cubesat.hal.interfaces import Gnss
from cubesat.hal.rpi import tel0157
from cubesat.hal.rpi.tel0157 import ADDRESS, TEL0157

#: The verified fix from docs/hardware-tel0157-gnss.md, 2026-08-23, antenna on a
#: balcony: 37.676896, -121.876561 (N / W), 23 satellites, 116.59 m, 0.00 knots.
BENCH_LAT = (37, 40, 61376)  # degrees, minutes, minute-fraction ×10⁵
BENCH_LON = (121, 52, 59366)

#: A southern *and* western position — Santiago de Chile — because that is the
#: combination the vendor library gets wrong in both coordinates at once.
SANTIAGO_LAT = (33, 26, 93400)
SANTIAGO_LON = (70, 40, 15800)


def _degrees_block(value: tuple[int, int, int], hemisphere: str) -> list[int]:
    degrees, minutes, fraction = value
    return [
        degrees,
        minutes,
        (fraction >> 16) & 0xFF,
        (fraction >> 8) & 0xFF,
        fraction & 0xFF,
        ord(hemisphere) if hemisphere else 0x00,
    ]


def _triplet(value: float) -> list[int]:
    whole = int(value)
    return [whole >> 8, whole & 0xFF, round((value - whole) * 100)]


def gnss_registers(
    *,
    lat: tuple[int, int, int] = BENCH_LAT,
    lon: tuple[int, int, int] = BENCH_LON,
    lat_hemisphere: str = "N",
    lon_hemisphere: str = "W",
    satellites: int = 23,
    altitude: float = 116.59,
    speed: float = 0.0,
) -> dict[int, int]:
    """The registers as the firmware actually presents them.

    Note where the hemispheres go: the latitude block carries the *longitude*
    hemisphere and vice versa. That swap is the firmware's, and building the
    fixture any other way would test a device that does not exist.
    """
    registers = {tel0157.REG_DEVICE_ID: tel0157.DEVICE_ID}
    for register, block in (
        (tel0157.REG_LATITUDE, _degrees_block(lat, lon_hemisphere)),
        (tel0157.REG_LONGITUDE, _degrees_block(lon, lat_hemisphere)),
    ):
        registers.update({register + offset: byte for offset, byte in enumerate(block)})
    registers[tel0157.REG_SATELLITES] = satellites
    for register, triplet in (
        (tel0157.REG_ALTITUDE, _triplet(altitude)),
        (tel0157.REG_SPEED, _triplet(speed)),
    ):
        registers.update({register + offset: byte for offset, byte in enumerate(triplet)})
    return registers


#: What the module reports before it has a solution: tidy zeros everywhere,
#: which decode to a real place in the Gulf of Guinea.
NO_FIX_REGISTERS = {tel0157.REG_DEVICE_ID: tel0157.DEVICE_ID}


class FakeTel0157Bus:
    """A TEL0157 as seen from the bus: a register map, plus recorded writes."""

    def __init__(self, registers: dict[int, int] | None = None) -> None:
        self.registers = dict(NO_FIX_REGISTERS if registers is None else registers)
        self.writes: list[tuple[int, int]] = []
        self.fail = False
        self.depth = 0
        self.max_depth = 0
        self.blocks: list[tuple[int, int]] = []

    class _Txn:
        def __init__(self, bus: "FakeTel0157Bus") -> None:
            self.bus = bus

        def __enter__(self):
            self.bus.depth += 1
            self.bus.max_depth = max(self.bus.max_depth, self.bus.depth)

        def __exit__(self, *_):
            self.bus.depth -= 1
            return False

    def transaction(self):
        return self._Txn(self)

    def read_byte(self, address: int, register: int) -> int:
        assert address == ADDRESS
        if self.fail:
            raise I2CError(f"read {address:#04x}[{register:#04x}] failed")
        return self.registers.get(register, 0)

    def read_block(self, address: int, register: int, length: int) -> list[int]:
        self.blocks.append((register, length))
        assert self.depth > 0, "a multi-byte read must be inside a transaction"
        return [self.read_byte(address, register + offset) for offset in range(length)]

    def write_byte(self, address: int, register: int, value: int) -> None:
        assert address == ADDRESS
        if self.fail:
            raise I2CError(f"write {address:#04x}[{register:#04x}] failed")
        self.writes.append((register, value))


@pytest.fixture
def gnss():
    bus = FakeTel0157Bus(gnss_registers())
    return TEL0157(bus=bus), bus


def test_it_satisfies_the_gnss_protocol(gnss):
    device, _ = gnss
    assert isinstance(device, Gnss)


def test_the_bench_fix_decodes_to_the_bench_coordinates(gnss):
    # The exact fix recorded in docs/hardware-tel0157-gnss.md. If the
    # degrees/minutes/fraction arithmetic drifts, this is what notices.
    device, _ = gnss
    position = device.read()
    assert position.fix is True
    assert position.lat == 37.676896
    assert position.lon == -121.876561
    assert position.satellites == 23
    assert position.alt == 116.59
    assert position.speed == 0.0  # 0.00 knots at rest, and 0 m/s either way


def test_the_hemisphere_comes_from_the_bytes_content_not_its_position(gnss):
    # The firmware swaps them, so the fixture holds 'W' in the latitude block.
    # A driver that trusted the position would find no N/S there and reject a
    # perfectly valid fix.
    device, bus = gnss
    assert bus.registers[tel0157.REG_LATITUDE + 5] == ord("W")
    assert bus.registers[tel0157.REG_LONGITUDE + 5] == ord("N")
    assert device.read().fix is True


def test_a_firmware_that_stopped_swapping_them_would_still_decode(gnss):
    # Deciding by content means the driver survives the quirk being fixed, which
    # is the other half of not depending on the position.
    device, bus = gnss
    bus.registers[tel0157.REG_LATITUDE + 5] = ord("N")
    bus.registers[tel0157.REG_LONGITUDE + 5] = ord("W")
    position = device.read()
    assert (position.lat, position.lon) == (37.676896, -121.876561)


def test_a_southern_and_western_position_is_signed_negative():
    # The registers carry an unsigned magnitude. The vendor library returns it
    # as-is, which turns Santiago into a place off the coast of China.
    bus = FakeTel0157Bus(
        gnss_registers(
            lat=SANTIAGO_LAT, lon=SANTIAGO_LON, lat_hemisphere="S", lon_hemisphere="W"
        )
    )
    position = TEL0157(bus=bus).read()
    assert position.lat == -33.4489
    assert position.lon == -70.6693


def test_a_northern_and_eastern_position_keeps_both_signs_positive():
    bus = FakeTel0157Bus(
        gnss_registers(
            lat=(56, 50, 33400), lon=(60, 36, 34200), lat_hemisphere="N", lon_hemisphere="E"
        )
    )
    position = TEL0157(bus=bus).read()
    assert position.lat == 56.8389
    assert position.lon == 60.6057


def test_tidy_zeros_are_not_reported_as_a_position():
    # 0.000000, 0.000000 is the Gulf of Guinea, and it is what this module reads
    # before it has a solution. Publishing it would put the satellite at sea.
    device = TEL0157(bus=FakeTel0157Bus())
    position = device.read()
    assert position.fix is False
    assert position.lat is None
    assert position.lon is None


def test_satellites_alone_do_not_make_a_fix():
    # Time is decoded from one satellite long before a position exists, so a
    # non-zero count says nothing on its own — the hemisphere characters do.
    registers = gnss_registers(lat_hemisphere="", lon_hemisphere="", satellites=6)
    position = TEL0157(bus=FakeTel0157Bus(registers)).read()
    assert position.fix is False


def test_one_missing_hemisphere_is_enough_to_withhold_the_fix():
    registers = gnss_registers(lon_hemisphere="")
    position = TEL0157(bus=FakeTel0157Bus(registers)).read()
    assert position.fix is False


def test_a_lost_signal_returns_the_last_known_fix_rather_than_nothing(gnss):
    # The contract inherited from the A9G driver: never make the poll loop wait,
    # and never replace a known position with a zero.
    device, bus = gnss
    device.read()
    bus.registers = dict(NO_FIX_REGISTERS)
    position = device.read()
    assert position.fix is False
    assert position.lat == 37.676896


def test_the_satellite_count_stays_fresh_while_acquiring(gnss):
    # A stale coordinate is useful; a stale satellite count is not. The count is
    # the only visible sign that acquisition is making progress.
    device, bus = gnss
    device.read()
    bus.registers = gnss_registers(lat_hemisphere="", lon_hemisphere="", satellites=4)
    assert device.read().satellites == 4


def test_a_bus_failure_is_reported_as_a_stale_fix_not_raised(gnss, caplog):
    # ADCS publishes attitude on the same tick; a silent receiver must not cost
    # it that, and must not stall the cadence either.
    device, bus = gnss
    device.read()
    bus.fail = True
    with caplog.at_level("WARNING"):
        position = device.read()
    assert position.fix is False
    assert position.lat == 37.676896
    assert "last known fix" in caplog.text


def test_a_failure_before_any_fix_reports_nothing_known(gnss):
    device, bus = gnss
    bus.fail = True
    position = device.read()
    assert (position.fix, position.lat, position.satellites) == (False, None, 0)


def test_the_constellation_mode_is_written_on_start(gnss):
    # Writing it removes the assumption that it survived the last power cycle,
    # and it measurably improved acquisition: 6 satellites in view to 9.
    device, bus = gnss
    device.read()
    assert bus.writes == [(tel0157.REG_CONSTELLATION, tel0157.CONSTELLATION_ALL)]


def test_the_constellation_mode_is_not_rewritten_every_poll(gnss):
    device, bus = gnss
    device.probe()
    device.read()
    device.read()
    assert len(bus.writes) == 1


def test_a_failed_mode_write_is_retried_on_the_next_read(gnss):
    device, bus = gnss
    bus.fail = True
    device.read()
    bus.fail = False
    device.read()
    assert bus.writes == [(tel0157.REG_CONSTELLATION, tel0157.CONSTELLATION_ALL)]


def test_the_whole_reading_is_one_transaction(gnss):
    # The longest transaction in the project. Splitting it would let another
    # process read the bus between the coordinates and the satellite count, and
    # the two would then describe different solutions.
    device, bus = gnss
    device.read()
    assert bus.max_depth == 1
    assert bus.blocks == [
        (tel0157.REG_LATITUDE, 6),
        (tel0157.REG_LONGITUDE, 6),
        (tel0157.REG_SATELLITES, 7),
    ]


def test_altitude_carries_its_hundredths_byte_above_the_first_255_metres():
    # The bench fix was at 116.59 m, inside one byte. This is the value that
    # would tell a big-endian integer part from something else.
    bus = FakeTel0157Bus(gnss_registers(altitude=1043.75))
    assert TEL0157(bus=bus).read().alt == 1043.75


def test_speed_is_published_in_metres_per_second_not_knots():
    # The register is knots; the rest of this telemetry is SI, and DHS is about
    # to store this in a column a chart will label. The bench had 0.00 knots at
    # rest, so the factor is pinned here by a constructed value instead.
    bus = FakeTel0157Bus(gnss_registers(speed=12.5))
    assert TEL0157(bus=bus).read().speed == 6.43


def test_the_raw_knots_register_is_never_published_as_it_stands():
    # Wrong by a factor of two, and a track in knots looks entirely reasonable
    # until someone compares it to a map. This is the regression to catch.
    bus = FakeTel0157Bus(gnss_registers(speed=10.0))
    speed = TEL0157(bus=bus).read().speed
    assert speed != 10.0
    assert speed == pytest.approx(10.0 * tel0157.M_S_PER_KNOT, abs=0.01)


def test_probe_accepts_the_documented_device_id(gnss):
    device, _ = gnss
    assert device.probe() is True


def test_probe_rejects_a_foreign_device_id(gnss, caplog):
    device, bus = gnss
    bus.registers[tel0157.REG_DEVICE_ID] = 0x00
    with caplog.at_level("ERROR"):
        assert device.probe() is False
    assert "expected 0x20" in caplog.text


def test_probe_reports_a_silent_module(gnss, caplog):
    device, bus = gnss
    bus.fail = True
    with caplog.at_level("ERROR"):
        assert device.probe() is False
    assert "did not answer" in caplog.text


def test_it_defaults_to_the_shared_bus(monkeypatch):
    monkeypatch.setattr(i2c, "_shared", None)
    device = TEL0157()
    assert device._bus is i2c.shared_bus()
