import pytest

from cubesat.hal import i2c
from cubesat.hal.i2c import I2CError
from cubesat.hal.interfaces import PowerMonitor
from cubesat.hal.rpi import max17048
from cubesat.hal.rpi.max17048 import ADDRESS, PowerMonitorX728


class FakeBus:
    """Records traffic and serves 16-bit MSB-first registers."""

    def __init__(self, words=None):
        self.words = dict(words or {})
        self.reads = []
        self.fail_on = set()
        self.depth = 0
        self.max_depth = 0

    class _Txn:
        def __init__(self, bus):
            self.bus = bus

        def __enter__(self):
            self.bus.depth += 1
            self.bus.max_depth = max(self.bus.max_depth, self.bus.depth)

        def __exit__(self, *_):
            self.bus.depth -= 1
            return False

    def transaction(self):
        return self._Txn(self)

    def read_byte(self, address, register):
        base = register if register % 2 == 0 else register - 1
        if base in self.fail_on:
            raise I2CError(f"read {address:#04x}[{register:#04x}] failed")
        self.reads.append((address, register))
        word = self.words.get(base, 0)
        return (word >> 8) & 0xFF if register == base else word & 0xFF


class FakeGpio:
    BCM = "BCM"
    IN = "in"

    def __init__(self, level=0):
        self.level = level
        self.mode = None
        self.configured = []
        self.cleaned = []

    def setmode(self, mode):
        self.mode = mode

    def setup(self, pin, direction):
        self.configured.append((pin, direction))

    def input(self, _pin):
        return self.level

    def cleanup(self, pin):
        self.cleaned.append(pin)


@pytest.fixture
def monitor(monkeypatch):
    # Values measured on the assembled satellite, from the hardware document:
    # 4.044 V at 79.61%. 0x16 answers 0xFFFF because the register is not there —
    # this is a MAX17040/41 — and the driver must not read it.
    bus = FakeBus({0x02: 51760, 0x04: 20379, 0x08: 0x0002, 0x16: 0xFFFF})
    gpio = FakeGpio(level=0)
    monkeypatch.setitem(
        __import__("sys").modules, "RPi", type("RPi", (), {"GPIO": gpio})
    )
    monkeypatch.setitem(__import__("sys").modules, "RPi.GPIO", gpio)
    device = PowerMonitorX728(bus=bus)
    return device, bus, gpio


def test_it_satisfies_the_power_monitor_protocol(monitor):
    device, _, _ = monitor
    assert isinstance(device, PowerMonitor)


def test_bench_measured_registers_decode_to_the_bench_values(monitor):
    # The exact numbers from docs/hardware-x728-ups-hat.md. If a refactor
    # changes the scaling, this is the test that notices.
    device, _, _ = monitor
    reading = device.read()
    assert reading.voltage == 4.044
    assert reading.battery_percent == 79.61


def test_a_word_read_is_one_indivisible_transaction(monitor):
    # The high and low halves must not be separable: at 10 kHz another process
    # could take the bus in between and the word would come back torn.
    device, bus, _ = monitor
    device.read()
    assert bus.max_depth >= 1
    assert (ADDRESS, 0x02) in bus.reads and (ADDRESS, 0x03) in bus.reads


def test_mains_present_is_a_low_pld_pin(monitor):
    # Inverted signal. Backwards here means a satellite that shuts down while
    # plugged in.
    device, _, gpio = monitor
    assert device.read().external_power is True
    gpio.level = 1
    assert device.read().external_power is False


def test_pld_pin_is_configured_as_an_input_once(monitor):
    device, _, gpio = monitor
    device.read()
    device.read()
    assert gpio.mode == "BCM"
    assert gpio.configured == [(max17048.PLD_PIN, "in")]


def test_the_gauge_has_no_rate_register_so_none_is_reported_and_never_read(monitor):
    # 0x16 is the MAX17048's CRATE. On this MAX17040/41 the address answers
    # 0xFFFF (verified 2026-09-01), which the old driver decoded into a
    # confident −0.208 %/h that never changed. The rate is EPS's to derive now;
    # the driver neither reads the address nor invents a number.
    device, bus, _ = monitor
    reading = device.read()
    assert reading.charge_rate is None
    assert (ADDRESS, max17048.REG_CRATE_ABSENT) not in bus.reads
    assert (ADDRESS, max17048.REG_CRATE_ABSENT + 1) not in bus.reads
    assert reading.battery_percent == 79.61


def test_state_of_charge_is_clamped(monitor):
    # A full pack reads slightly over 100%; letting that through would make
    # every threshold and chart downstream subtly wrong.
    device, bus, _ = monitor
    bus.words[0x04] = 0x6800  # 104%
    assert device.read().battery_percent == 100.0


def test_probe_accepts_a_gauge_that_answers(monitor):
    device, _, _ = monitor
    assert device.probe() is True


def test_probe_warns_about_an_unexpected_version_but_still_works(monitor, caplog):
    device, bus, _ = monitor
    bus.words[0x08] = 0x0011
    with caplog.at_level("WARNING"):
        assert device.probe() is True
    assert "expected 0x0002" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        device.probe()
    assert caplog.text == ""  # warned once, not every cycle


def test_probe_reports_a_silent_gauge(monitor, caplog):
    device, bus, _ = monitor
    bus.fail_on.add(0x08)
    with caplog.at_level("ERROR"):
        assert device.probe() is False
    assert "did not answer" in caplog.text


def test_close_releases_the_pin_and_is_idempotent(monitor):
    device, _, gpio = monitor
    device.read()
    device.close()
    device.close()
    assert gpio.cleaned == [max17048.PLD_PIN]


def test_missing_rpi_gpio_says_how_to_run_without_hardware(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_gpio(name, *args, **kwargs):
        if name.startswith("RPi"):
            raise ImportError("No module named 'RPi'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_gpio)
    device = PowerMonitorX728(bus=FakeBus())
    with pytest.raises(I2CError, match="CUBESAT_MOCK_HARDWARE"):
        device.read()


def test_it_defaults_to_the_shared_bus(monkeypatch):
    monkeypatch.setattr(i2c, "_shared", None)
    device = PowerMonitorX728()
    assert device._bus is i2c.shared_bus()
