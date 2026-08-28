import pytest

from cubesat.common import config
from cubesat.hal import i2c
from cubesat.hal.i2c import I2CError
from cubesat.hal.interfaces import EnvironmentSensor
from cubesat.hal.rpi import sen0501
from cubesat.hal.rpi.sen0501 import ADDRESS, SEN0501

#: The verified reading from docs/hardware-sen0501-environmental-sensor.md,
#: 2026-08-23 indoors: 27.96 degC (raw 27324), 41.21 % (raw 27008), 1000 hPa,
#: 388.96 lux (raw 377), and a UV register of 14 that reads as 0.00 on a V1.0
#: board and 84.35 on a V3.0 one.
BENCH_REGISTERS = {
    sen0501.REG_DEVICE_ID: sen0501.DEVICE_ID,
    sen0501.REG_UV: 14,
    sen0501.REG_LIGHT: 377,
    sen0501.REG_TEMPERATURE: 27324,
    sen0501.REG_HUMIDITY: 27008,
    sen0501.REG_PRESSURE: 1000,
}

#: Both readings of the bench's raw 14, to two decimals. They are not close, and
#: that is the entire reason uv_index is withheld.
BENCH_UV_V1 = 0.00
BENCH_UV_V3 = 84.35


class FakeSen0501Bus:
    """An SEN0501 as seen from the bus: 16-bit big-endian registers.

    Every quantity on this device is a word, so the fixture stores words and
    splits them on the way out — building it byte by byte would let a driver
    that read the halves in the wrong order still pass.
    """

    def __init__(self, registers: dict[int, int] | None = None) -> None:
        self.registers = dict(BENCH_REGISTERS if registers is None else registers)
        self.fail = False
        self.depth = 0
        self.max_depth = 0
        self.blocks: list[tuple[int, int]] = []

    class _Txn:
        def __init__(self, bus: "FakeSen0501Bus") -> None:
            self.bus = bus

        def __enter__(self):
            self.bus.depth += 1
            self.bus.max_depth = max(self.bus.max_depth, self.bus.depth)

        def __exit__(self, *_):
            self.bus.depth -= 1
            return False

    def transaction(self):
        return self._Txn(self)

    def read_block(self, address: int, register: int, length: int) -> list[int]:
        assert address == ADDRESS
        assert length == 2, "every register on this device is a 16-bit word"
        self.blocks.append((register, length))
        if self.fail:
            raise I2CError(f"read {address:#04x}[{register:#04x}:{length}] failed")
        word = self.registers.get(register, 0)
        return [(word >> 8) & 0xFF, word & 0xFF]


@pytest.fixture
def sensor():
    bus = FakeSen0501Bus()
    return SEN0501(bus=bus), bus


@pytest.fixture
def revision(monkeypatch):
    """Set the board revision where a deployment sets it: the config, not the
    driver. The bench settles this with one line of YAML or one env var, which
    is the whole reason it does not live in the module under test."""

    def set_to(value):
        monkeypatch.setattr(config, "SEN0501_REVISION", value)

    return set_to


def test_it_satisfies_the_environment_sensor_protocol(sensor):
    device, _ = sensor
    assert isinstance(device, EnvironmentSensor)


def test_the_bench_registers_decode_to_the_bench_reading(sensor):
    # The exact indoor reading in the hardware document. If a conversion drifts
    # — and the /1024/64 scaling is easy to mis-transcribe — this is what
    # notices.
    device, _ = sensor
    reading = device.read()
    assert reading.temperature == 27.96
    assert reading.humidity == 41.21
    assert reading.pressure == 1000.0
    assert reading.light == 388.96


def test_the_light_conversion_is_the_quartic_and_not_a_scale_factor(sensor):
    # At raw 377 the polynomial correction is worth about 3 % of the reading, so
    # a linear driver would produce 377.87 lux and look entirely plausible.
    device, _ = sensor
    assert device.read().light != pytest.approx(377.0, abs=5.0)


# ── the UV ambiguity ────────────────────────────────────────────────────────


def test_the_uv_index_is_withheld_while_the_board_revision_is_unknown(sensor, revision):
    # Two elements behind one register, and at raw 14 the two formulas give 0.00
    # and 84.35. Publishing either would be inventing a measurement.
    revision(None)
    device, _ = sensor
    assert device.read().uv_index is None


def test_the_raw_uv_count_is_published_even_when_the_index_is_not(sensor, revision):
    # An unresolved index still has to be a recorded observation: the raw count
    # is what lets the question be settled later from data already collected.
    revision(None)
    device, _ = sensor
    assert device.read().uv_raw == 14


def test_the_configured_revision_is_read_and_normalised(revision):
    # A person typing this into config.yaml or an env var will not match the
    # driver's casing, and " V3 " with a stray space is a settings file, not a
    # bug report.
    revision("  V3 ")
    assert sen0501.revision() == sen0501.REVISION_V3
    revision(None)
    assert sen0501.revision() is None


def test_the_unresolved_warning_names_both_candidates(sensor, revision, caplog):
    # This message is what settles the question on the bench: it prints both
    # readings of a real register, so one look in direct sunlight is enough.
    revision(None)
    device, _ = sensor
    with caplog.at_level("WARNING"):
        device.read()
    assert "0.00" in caplog.text
    assert "84.35" in caplog.text
    assert "sen0501_revision" in caplog.text


def test_the_unresolved_warning_is_said_once_and_not_every_poll(sensor, revision, caplog):
    # PAYLOAD reads this sensor every 60 s for as long as the satellite is up,
    # and the message is aimed at a person reading a log, not at a monitor.
    revision(None)
    device, _ = sensor
    with caplog.at_level("WARNING"):
        for _ in range(5):
            device.read()
    assert caplog.text.count("board revision is unknown") == 1


def test_a_v1_board_reads_the_bench_register_as_zero(sensor, revision):
    revision(sen0501.REVISION_V1)
    device, _ = sensor
    assert device.read().uv_index == BENCH_UV_V1


def test_a_v3_board_reads_the_same_register_as_eighty_four(sensor, revision):
    revision(sen0501.REVISION_V3)
    device, _ = sensor
    assert device.read().uv_index == BENCH_UV_V3


def test_the_v1_formula_is_clamped_because_a_negative_index_measures_nothing():
    # Unclamped, raw 14 works out to -7.45. DFRobot's library publishes that;
    # the bench script clamps, and so does this.
    assert sen0501.uv_index_v1(14) == 0.0
    assert sen0501.uv_index_v1(0) == 0.0


def test_a_sunlit_reading_is_a_plausible_index_on_one_board_and_absurd_on_the_other():
    # The hint, recorded rather than acted on: the real UV scale tops out near
    # 11, so at a raw count bright enough to matter only one of these two is a
    # UV index at all. It is not encoded as a default anywhere — a hint is not a
    # verification, and the definitive test is a reading in direct sunlight.
    assert sen0501.uv_index_v1(400) == pytest.approx(1.43, abs=0.01)
    assert sen0501.uv_index_v3(400) > 1000


def test_an_unrecognised_revision_is_treated_as_unknown_and_said_out_loud(
    sensor, revision, caplog
):
    # A typo in the env var must not silently become a published index, and the
    # log has to name the value so the typo is findable.
    revision("v2")
    device, _ = sensor
    with caplog.at_level("WARNING"):
        reading = device.read()
    assert reading.uv_index is None
    assert "'v2'" in caplog.text


def test_a_revision_that_is_not_even_a_string_is_treated_as_unknown(sensor, revision, caplog):
    # YAML turns an unquoted v1 into a string but an unquoted 1 into an int, and
    # a settings file is exactly where that happens.
    revision(1)
    device, _ = sensor
    with caplog.at_level("WARNING"):
        assert device.read().uv_index is None
    assert "'1'" in caplog.text


# ── what is deliberately absent ─────────────────────────────────────────────


def test_no_altitude_is_derived_from_the_pressure_register(sensor):
    # The vendor's elevation uses a hard-coded 1015.0 hPa reference, which makes
    # it meaningless as an absolute; the register is also whole hectopascals,
    # about 8 m per bit. Altitude comes from the GNSS receiver, which measures
    # it. This is the regression to catch if somebody ports get_elevation().
    device, _ = sensor
    fields = set(device.read().as_dict())
    assert "altitude" not in fields
    assert "elevation" not in fields


def test_pressure_keeps_the_registers_own_resolution(sensor):
    # Whole hectopascals out of the register, so there is nothing to round away
    # and nothing to interpolate. A driver returning 1000.37 would be inventing
    # precision the device does not have.
    device, bus = sensor
    bus.registers[sen0501.REG_PRESSURE] = 1013
    assert device.read().pressure == 1013.0


# ── the bus ─────────────────────────────────────────────────────────────────


def test_all_five_measurements_come_from_one_transaction(sensor):
    # Four processes share one 10 kHz bus. Splitting the five reads would let
    # another take the bus between them, and the payload would then describe
    # five moments rather than one.
    device, bus = sensor
    device.read()
    assert bus.max_depth == 1
    assert bus.blocks == [
        (sen0501.REG_UV, 2),
        (sen0501.REG_LIGHT, 2),
        (sen0501.REG_TEMPERATURE, 2),
        (sen0501.REG_HUMIDITY, 2),
        (sen0501.REG_PRESSURE, 2),
    ]


def test_a_failed_read_raises_rather_than_returning_a_stale_environment(sensor):
    # Unlike a GNSS fix there is no useful last known environment: a temperature
    # from ten minutes ago is indistinguishable in the payload from a current
    # one. PAYLOAD catches this and publishes nothing.
    device, bus = sensor
    bus.fail = True
    with pytest.raises(I2CError):
        device.read()


def test_probe_accepts_the_documented_device_id(sensor):
    device, _ = sensor
    assert device.probe() is True


def test_probe_rejects_a_foreign_device_id(sensor, caplog):
    device, bus = sensor
    bus.registers[sen0501.REG_DEVICE_ID] = 0x0000
    with caplog.at_level("ERROR"):
        assert device.probe() is False
    assert "expected 0x0022" in caplog.text


def test_probe_reports_a_silent_module(sensor, caplog):
    device, bus = sensor
    bus.fail = True
    with caplog.at_level("ERROR"):
        assert device.probe() is False
    assert "did not answer" in caplog.text


def test_it_defaults_to_the_shared_bus(monkeypatch):
    monkeypatch.setattr(i2c, "_shared", None)
    device = SEN0501()
    assert device._bus is i2c.shared_bus()
