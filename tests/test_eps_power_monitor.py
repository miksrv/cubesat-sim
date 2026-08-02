from unittest.mock import MagicMock

import pytest

import src.eps.power_monitor as pm
from tests.fakes import FakeI2CBus

ADDR = pm.BATTERY_I2C_ADDR


@pytest.fixture(autouse=True)
def fresh_gpio(monkeypatch):
    """RPi.GPIO is a single mock shared for the whole test session (see
    conftest.py) — give every test its own fresh method mocks so call
    assertions and configured return values don't leak between tests."""
    monkeypatch.setattr(pm.GPIO, "setwarnings", MagicMock())
    monkeypatch.setattr(pm.GPIO, "setmode", MagicMock())
    monkeypatch.setattr(pm.GPIO, "setup", MagicMock())
    monkeypatch.setattr(pm.GPIO, "input", MagicMock(return_value=0))
    monkeypatch.setattr(pm.GPIO, "cleanup", MagicMock())


def make_monitor(monkeypatch, byte_responses=None):
    bus = FakeI2CBus(byte_responses=byte_responses or {})
    monkeypatch.setattr(pm.smbus2, "SMBus", lambda _bus_num: bus)
    return pm.EPSMonitor(), bus


class TestInit:
    def test_configures_gpio(self, monkeypatch):
        make_monitor(monkeypatch)
        pm.GPIO.setwarnings.assert_called_once_with(False)
        pm.GPIO.setmode.assert_called_once_with(pm.GPIO.BCM)
        pm.GPIO.setup.assert_called_once_with(pm.PLD_PIN, pm.GPIO.IN)

    def test_propagates_gpio_setup_failure(self, monkeypatch):
        pm.GPIO.setup.side_effect = RuntimeError("no GPIO chip")
        with pytest.raises(RuntimeError, match="no GPIO chip"):
            make_monitor(monkeypatch)


class TestReadWord:
    def test_combines_msb_and_lsb(self, monkeypatch):
        monitor, _bus = make_monitor(
            monkeypatch, byte_responses={(ADDR, 0x10): 0x12, (ADDR, 0x11): 0x34}
        )
        assert monitor.read_word(0x10) == 0x1234

    def test_returns_zero_on_i2c_error(self, monkeypatch):
        monitor, bus = make_monitor(monkeypatch)

        def boom(_addr, _reg):
            raise OSError("I2C bus error")

        bus.read_byte_data = boom
        assert monitor.read_word(pm.REG_VCELL) == 0


class TestBatteryVoltage:
    def test_converts_raw_reading(self, monkeypatch):
        # raw = 0x1000 -> (raw >> 4) * 0.00125 = 256 * 0.00125 = 0.32
        monitor, _bus = make_monitor(
            monkeypatch,
            byte_responses={
                (ADDR, pm.REG_VCELL): 0x10,
                (ADDR, pm.REG_VCELL + 1): 0x00,
            },
        )
        assert monitor.get_battery_voltage() == 0.32

    def test_none_when_raw_is_zero(self, monkeypatch):
        monitor, _bus = make_monitor(monkeypatch)
        assert monitor.get_battery_voltage() is None


class TestBatteryPercent:
    def test_converts_raw_reading(self, monkeypatch):
        # raw = 0x8000 -> 32768 / 256.0 = 128.0, clamped to 100.0
        monitor, _bus = make_monitor(
            monkeypatch,
            byte_responses={
                (ADDR, pm.REG_SOC): 0x80,
                (ADDR, pm.REG_SOC + 1): 0x00,
            },
        )
        assert monitor.get_battery_percent() == 100.0

    def test_typical_value_within_range(self, monkeypatch):
        # raw = 0x3200 -> 12800 / 256.0 = 50.0
        monitor, _bus = make_monitor(
            monkeypatch,
            byte_responses={
                (ADDR, pm.REG_SOC): 0x32,
                (ADDR, pm.REG_SOC + 1): 0x00,
            },
        )
        assert monitor.get_battery_percent() == 50.0

    def test_none_when_raw_is_zero(self, monkeypatch):
        monitor, _bus = make_monitor(monkeypatch)
        assert monitor.get_battery_percent() is None


class TestExternalPower:
    def test_true_when_pin_low(self, monkeypatch):
        monitor, _bus = make_monitor(monkeypatch)
        pm.GPIO.input.return_value = 0
        assert monitor.get_external_power() is True

    def test_false_when_pin_high(self, monkeypatch):
        monitor, _bus = make_monitor(monkeypatch)
        pm.GPIO.input.return_value = 1
        assert monitor.get_external_power() is False

    def test_defaults_true_on_gpio_error(self, monkeypatch):
        monitor, _bus = make_monitor(monkeypatch)
        pm.GPIO.input.side_effect = RuntimeError("gpio gone")
        assert monitor.get_external_power() is True


class TestGetStatus:
    def test_returns_full_status_dict(self, monkeypatch):
        monitor, _bus = make_monitor(
            monkeypatch,
            byte_responses={
                (ADDR, pm.REG_SOC): 0x32,
                (ADDR, pm.REG_SOC + 1): 0x00,
                (ADDR, pm.REG_VCELL): 0x10,
                (ADDR, pm.REG_VCELL + 1): 0x00,
            },
        )
        pm.GPIO.input.return_value = 0
        status = monitor.get_status()
        assert set(status.keys()) == {"timestamp", "battery", "voltage", "external_power"}
        assert status["battery"] == 50.0
        assert status["voltage"] == 0.32
        assert status["external_power"] is True


class TestDel:
    def test_cleanup_called_and_errors_swallowed(self, monkeypatch):
        monitor, _bus = make_monitor(monkeypatch)
        pm.GPIO.cleanup.side_effect = RuntimeError("already cleaned")
        monitor.__del__()  # must not raise
        pm.GPIO.cleanup.assert_called_once()
