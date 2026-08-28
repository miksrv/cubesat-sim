import math

import pytest

from cubesat.hal import i2c
from cubesat.hal.i2c import I2CError
from cubesat.hal.interfaces import Imu
from cubesat.hal.rpi import bno055
from cubesat.hal.rpi.bno055 import ADDRESS, BNO055


def _euler(degrees: float) -> int:
    """Registers hold 16 LSB per degree — the scale from the hardware document."""
    return round(degrees * bno055.EULER_LSB_PER_DEGREE)


def _quaternion(value: float) -> int:
    return round(value * bno055.QUATERNION_LSB)


def _accel(m_s2: float) -> int:
    return round(m_s2 * bno055.ACCEL_LSB_PER_M_S2)


def _gyro(dps: float) -> int:
    return round(dps * bno055.GYRO_LSB_PER_DPS)


def _words(register: int, values: list[int]) -> dict[int, int]:
    """Signed 16-bit values as the chip lays them out: LSB first, then MSB."""
    out: dict[int, int] = {}
    for index, value in enumerate(values):
        raw = value & 0xFFFF
        out[register + index * 2] = raw & 0xFF
        out[register + index * 2 + 1] = raw >> 8
    return out


#: The bench reading from docs/hardware-bno055-bmp280-imu.md, 2026-08-23, board
#: resting: heading 0.00, roll 27.19, pitch 7.06; quaternion 0.970 / -0.057 /
#: -0.236 / -0.000; accel 4.39 / -1.00 / 8.27 m/s²; gyro -0.12 / -0.31 / -0.19
#: deg/s; chip temperature 31 °C.
BENCH_REGISTERS: dict[int, int] = {
    bno055.REG_CHIP_ID: bno055.CHIP_ID,
    bno055.REG_SYS_STATUS: bno055.SYS_STATUS_FUSION_RUNNING,
    bno055.REG_SYS_ERR: 0,
    bno055.REG_ST_RESULT: 0x0F,
    bno055.REG_CALIB_STAT: 0xFF,
    bno055.REG_TEMP: 31,
    **_words(bno055.REG_EUL_DATA, [_euler(0.00), _euler(27.19), _euler(7.06)]),
    **_words(
        bno055.REG_QUA_DATA,
        [
            _quaternion(0.970),
            _quaternion(-0.057),
            _quaternion(-0.236),
            _quaternion(-0.000),
        ],
    ),
    **_words(bno055.REG_ACC_DATA, [_accel(4.39), _accel(-1.00), _accel(8.27)]),
    **_words(bno055.REG_GYR_DATA, [_gyro(-0.12), _gyro(-0.31), _gyro(-0.19)]),
}

#: Half an LSB, the most a value can be out by after a round trip through the
#: registers. The bench output was printed to two decimals, so anything tighter
#: would be asserting the printf format rather than the scaling.
HALF_LSB_DEGREE = 0.5 / bno055.EULER_LSB_PER_DEGREE
HALF_LSB_DPS = 0.5 / bno055.GYRO_LSB_PER_DPS


class FakeBno055Bus:
    """A BNO055 as seen from the bus.

    Serves single bytes and register blocks, records every write, and models the
    one behaviour that matters for bring-up: a reset takes the chip off the bus
    for a while and then brings it back healthy.
    """

    def __init__(self, registers: dict[int, int] | None = None) -> None:
        self.registers = dict(registers or BENCH_REGISTERS)
        self.writes: list[tuple[int, int]] = []
        self.sleeps: list[float] = []
        self.depth_at_sleep: list[int] = []
        self.fail_registers: set[int] = set()
        self.depth = 0
        self.max_depth = 0
        self.resets = 0
        #: Reads that answer 0x00 before CHIP_ID comes back, and whether the
        #: chip ever comes back at all.
        self.chip_id_delay = 0
        self.chip_id_returns = True

    class _Txn:
        def __init__(self, bus: "FakeBno055Bus") -> None:
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
        if register in self.fail_registers:
            raise I2CError(f"read {address:#04x}[{register:#04x}] failed")
        if register == bno055.REG_CHIP_ID:
            if not self.chip_id_returns:
                return 0x00
            if self.chip_id_delay > 0:
                self.chip_id_delay -= 1
                return 0x00
        return self.registers.get(register, 0)

    def read_block(self, address: int, register: int, length: int) -> list[int]:
        return [self.read_byte(address, register + offset) for offset in range(length)]

    def write_byte(self, address: int, register: int, value: int) -> None:
        assert address == ADDRESS
        if register in self.fail_registers:
            raise I2CError(f"write {address:#04x}[{register:#04x}] failed")
        self.writes.append((register, value))
        if register == bno055.REG_SYS_TRIGGER and value == bno055.SYS_TRIGGER_RESET:
            self.resets += 1
            # A reset clears the configuration and, with it, the stale state
            # that made the half-configured device look calibrated.
            self.registers[bno055.REG_SYS_STATUS] = bno055.SYS_STATUS_FUSION_RUNNING
            self.registers[bno055.REG_SYS_ERR] = 0
            self.registers[bno055.REG_CALIB_STAT] = 0x00

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.depth_at_sleep.append(self.depth)


def calibrate(bus: FakeBno055Bus, mag: int = 3) -> None:
    """Set CALIB_STAT after the reset has cleared it, as waving the board does."""
    bus.registers[bno055.REG_CALIB_STAT] = (3 << 6) | (3 << 4) | (3 << 2) | mag


@pytest.fixture
def imu():
    bus = FakeBno055Bus()
    return BNO055(bus=bus, sleep=bus.sleep), bus


def test_it_satisfies_the_imu_protocol(imu):
    device, _ = imu
    assert isinstance(device, Imu)


def test_the_bench_reading_decodes_to_the_bench_numbers(imu):
    # The exact output recorded in docs/hardware-bno055-bmp280-imu.md. If a
    # refactor changes a scale factor, this is the test that notices. The
    # calibration is restored after the mandatory reset, which is the state the
    # bench print was taken in: magnetometer at 3, heading therefore reported.
    device, bus = imu
    device.read()
    calibrate(bus)
    reading = device.read()
    assert reading.roll == pytest.approx(27.19, abs=HALF_LSB_DEGREE)
    assert reading.pitch == pytest.approx(7.06, abs=HALF_LSB_DEGREE)
    assert reading.yaw == pytest.approx(0.00, abs=HALF_LSB_DEGREE)
    assert reading.quaternion.w == pytest.approx(0.970, abs=0.001)
    assert reading.quaternion.x == pytest.approx(-0.057, abs=0.001)
    assert reading.quaternion.y == pytest.approx(-0.236, abs=0.001)
    assert reading.quaternion.z == pytest.approx(0.0, abs=0.001)
    assert reading.gyro_dps.x == pytest.approx(-0.12, abs=HALF_LSB_DPS)
    assert reading.gyro_dps.y == pytest.approx(-0.31, abs=HALF_LSB_DPS)
    assert reading.gyro_dps.z == pytest.approx(-0.19, abs=HALF_LSB_DPS)
    assert reading.temperature == 31.0


def test_acceleration_is_reported_in_g_as_the_field_name_says(imu):
    # The registers are 100 LSB per m/s²; Attitude.accel_g is in g, so the
    # driver divides by standard gravity. Multiplying back reproduces the bench
    # vector and its 9.42 m/s² magnitude — one g, which is the whole check.
    device, _ = imu
    accel = device.read().accel_g
    x, y, z = (axis * bno055.STANDARD_GRAVITY for axis in (accel.x, accel.y, accel.z))
    assert (x, y, z) == pytest.approx((4.39, -1.00, 8.27), abs=0.01)
    assert math.sqrt(x * x + y * y + z * z) == pytest.approx(9.42, abs=0.01)


def test_the_documented_reset_sequence_is_followed_exactly(imu):
    # A device left half-configured reports SYS_STATUS 1 / SYS_ERR 9 and returns
    # all-zero magnetometer data while claiming its magnetometer is calibrated.
    # Nothing about that looks like a failure, so the state found on the bus is
    # never trusted: the documented sequence runs on every start.
    device, bus = imu
    device.read()
    assert bus.writes == [
        (bno055.REG_OPR_MODE, bno055.OPR_MODE_CONFIG),
        (bno055.REG_SYS_TRIGGER, bno055.SYS_TRIGGER_RESET),
        (bno055.REG_PAGE_ID, 0x00),
        (bno055.REG_PWR_MODE, bno055.PWR_MODE_NORMAL),
        (bno055.REG_SYS_TRIGGER, bno055.SYS_TRIGGER_INTERNAL_OSC),
        (bno055.REG_OPR_MODE, bno055.OPR_MODE_NDOF),
    ]


def test_a_half_configured_device_is_reset_rather_than_read_as_it_stands(imu):
    # The exact stale state from the debugging session: a fusion configuration
    # error, and a CALIB_STAT that claims the magnetometer is fully calibrated
    # while the magnetometer returns zeros.
    bus = FakeBno055Bus(
        {**BENCH_REGISTERS, bno055.REG_SYS_STATUS: 1, bno055.REG_SYS_ERR: 9,
         bno055.REG_CALIB_STAT: 0xFF}
    )
    device = BNO055(bus=bus, sleep=bus.sleep)
    assert device.probe() is True
    assert bus.resets == 1
    assert bus.registers[bno055.REG_SYS_ERR] == 0
    # And the claim of a calibrated magnetometer went with it, which is what
    # lets the service suppress yaw instead of publishing a constant.
    assert device.read().calibration.mag == 0


def test_the_reset_is_configured_only_once_per_process(imu):
    device, bus = imu
    device.probe()
    device.read()
    device.read()
    assert bus.resets == 1


def test_the_driver_waits_for_the_chip_id_before_configuring(imu):
    # A resetting BNO055 is simply not on the bus. Carrying on before it answers
    # 0xA0 is how the half-configured state is created in the first place.
    device, bus = imu
    bus.chip_id_delay = 3
    device.read()
    assert bus.chip_id_delay == 0
    assert bus.writes[2] == (bno055.REG_PAGE_ID, 0x00)


def test_a_read_failure_during_the_reset_is_expected_not_fatal():
    # A resetting chip is off the bus entirely, so the read fails rather than
    # answering something wrong. Treating that as fatal would make every start
    # a coin toss.
    bus = FakeBno055Bus()
    bus.fail_registers.add(bno055.REG_CHIP_ID)

    def sleep(seconds):
        bus.sleep(seconds)
        if len(bus.sleeps) >= 2:  # by now the chip is back on the bus
            bus.fail_registers.discard(bno055.REG_CHIP_ID)

    device = BNO055(bus=bus, sleep=sleep)
    assert device.read().temperature == 31.0


def test_a_chip_that_never_comes_back_after_the_reset_is_an_error(imu):
    device, bus = imu
    bus.chip_id_returns = False
    with pytest.raises(I2CError, match="CHIP_ID"):
        device.read()
    assert len(bus.sleeps) == 1 + bno055.CHIP_ID_ATTEMPTS


def test_the_reset_delay_matches_the_documented_power_on_time(imu):
    # The power-on reset takes about 650 ms; anything shorter reads a chip that
    # is still coming up.
    device, bus = imu
    device.read()
    assert bus.sleeps[0] >= 0.65
    assert bno055.MODE_SETTLE_SEC in bus.sleeps


def test_the_bus_is_not_held_across_the_reset_delays(imu):
    # Over a second of sleeping with the 10 kHz bus lock held would stall EPS
    # mid-poll for no reason. Each write is atomic on its own.
    device, bus = imu
    device.read()
    assert bus.depth_at_sleep and all(depth == 0 for depth in bus.depth_at_sleep)


def test_every_multi_byte_read_is_inside_one_transaction(imu):
    # An attitude assembled from halves taken tens of milliseconds apart is not
    # an attitude, and at 10 kHz that is exactly how far apart they would be.
    device, bus = imu
    device.read()
    assert bus.max_depth >= 1


def test_calib_stat_unpacks_into_four_two_bit_fields(imu):
    device, bus = imu
    device.read()  # get the reset out of the way; it clears the calibration
    bus.registers[bno055.REG_CALIB_STAT] = 0b11_10_01_00
    calibration = device.read().calibration
    assert (calibration.sys, calibration.gyro, calibration.accel, calibration.mag) == (3, 2, 1, 0)


def test_negative_values_are_decoded_as_signed(imu):
    device, bus = imu
    bus.registers.update(_words(bno055.REG_EUL_DATA, [0, _euler(-90.0), 0]))
    bus.registers[bno055.REG_TEMP] = 0xF6  # -10 °C
    reading = device.read()
    assert reading.roll == pytest.approx(-90.0, abs=HALF_LSB_DEGREE)
    assert reading.temperature == -10.0


def test_the_heading_is_withheld_until_the_magnetometer_is_fully_calibrated(imu):
    # Below full calibration the chip does not report a poor heading, it reports
    # a constant — typically 0.00 — and a constant masquerading as a heading is
    # indistinguishable from a real one for every consumer downstream.
    device, bus = imu
    device.read()
    for mag in (0, 1, 2):
        calibrate(bus, mag)
        assert device.read().yaw is None
    calibrate(bus, 3)
    assert device.read().yaw == pytest.approx(0.0, abs=HALF_LSB_DEGREE)


def test_the_rule_lives_here_so_every_consumer_of_the_protocol_is_covered(imu):
    # Not only ADCS reads this device: a DIAG tool talking straight to the Imu
    # protocol gets the same protection, because the quirk belongs to the sensor.
    device, bus = imu
    calibrate(bus, 0)
    reading = device.read()
    assert reading.yaw is None
    assert reading.calibration.heading_usable is False


def test_roll_and_pitch_survive_an_uncalibrated_magnetometer(imu):
    # They do not depend on the magnetometer, so withholding them too would
    # throw away good data along with the bad.
    device, bus = imu
    calibrate(bus, 0)
    reading = device.read()
    assert reading.roll == pytest.approx(27.19, abs=HALF_LSB_DEGREE)
    assert reading.pitch == pytest.approx(7.06, abs=HALF_LSB_DEGREE)


def test_a_withheld_heading_is_explained_once_not_twice_a_second(imu, caplog):
    device, bus = imu
    calibrate(bus, 0)
    with caplog.at_level("INFO"):
        device.read()
        device.read()
    assert caplog.text.count("heading withheld") == 1
    caplog.clear()
    calibrate(bus, 3)
    with caplog.at_level("INFO"):
        device.read()
        device.read()
    assert caplog.text.count("heading reported") == 1


def test_probe_accepts_a_chip_that_answers_with_the_right_identity(imu):
    device, _ = imu
    assert device.probe() is True


def test_probe_rejects_a_wrong_chip_id(imu, caplog):
    device, bus = imu
    device.probe()
    bus.registers[bno055.REG_CHIP_ID] = 0x32  # what the magnetometer id reads
    with caplog.at_level("ERROR"):
        assert device.probe() is False
    assert "expected 0xa0" in caplog.text.lower()


def test_probe_reports_sys_status_and_sys_err_when_the_chip_is_unhealthy(imu, caplog):
    # Those two numbers together are what made the fusion configuration error
    # diagnosable, so an unhealthy chip has to name both in the log.
    device, bus = imu
    device.probe()
    bus.registers[bno055.REG_SYS_STATUS] = 1
    bus.registers[bno055.REG_SYS_ERR] = 9
    with caplog.at_level("ERROR"):
        assert device.probe() is True  # it answers; it is just not healthy
    assert "SYS_STATUS=1" in caplog.text
    assert "system error" in caplog.text
    assert "SYS_ERR=9" in caplog.text


def test_probe_reports_a_silent_chip(imu, caplog):
    device, bus = imu
    bus.fail_registers.add(bno055.REG_OPR_MODE)
    with caplog.at_level("ERROR"):
        assert device.probe() is False
    assert "did not answer" in caplog.text


def test_unreadable_health_registers_do_not_fail_the_probe(imu):
    # The identity is the probe; the health registers are commentary on it.
    device, bus = imu
    device.probe()
    bus.fail_registers.add(bno055.REG_SYS_STATUS)
    assert device.probe() is True


def test_it_defaults_to_the_shared_bus(monkeypatch):
    monkeypatch.setattr(i2c, "_shared", None)
    device = BNO055()
    assert device._bus is i2c.shared_bus()
