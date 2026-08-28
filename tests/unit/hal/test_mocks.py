import time

import pytest

from cubesat.hal.interfaces import Attitude, Environment, Position, Power
from cubesat.hal.mock.camera import MockCamera
from cubesat.hal.mock.environment import MockEnvironment
from cubesat.hal.mock.gnss import MockGnss
from cubesat.hal.mock.imu import MockImu
from cubesat.hal.mock.power import MockPowerMonitor
from cubesat.hal.mock.radio import MAX_MESSAGE_BYTES, MockRadio


def test_imu_reports_a_complete_plausible_attitude():
    device = MockImu()
    device._started -= 100  # past the calibration climb
    reading = device.read()
    assert isinstance(reading, Attitude)
    assert -180 <= reading.roll <= 180
    assert 0 <= reading.yaw <= 360
    assert reading.calibration.heading_usable


def test_the_mock_imu_starts_uncalibrated_and_withholds_the_heading():
    # A real BNO055 boots this way and reports a constant instead of a heading.
    # A mock that is calibrated from the first read would let a consumer be
    # written against a state the hardware never presents at startup.
    reading = MockImu().read()
    assert reading.calibration.mag == 0
    assert reading.yaw is None
    assert reading.roll is not None and reading.pitch is not None


def test_the_mock_imu_calibrates_over_time():
    device = MockImu()
    assert device.read().yaw is None
    device._started -= 100
    assert device.read().yaw is not None


def test_the_calibration_climb_can_be_switched_off(monkeypatch):
    import importlib

    from cubesat.hal.mock import imu as imu_module

    monkeypatch.setenv("CUBESAT_MOCK_CALIBRATION_SEC", "0")
    importlib.reload(imu_module)
    try:
        assert imu_module.MockImu().read().yaw is not None
    finally:
        monkeypatch.delenv("CUBESAT_MOCK_CALIBRATION_SEC")
        importlib.reload(imu_module)


def test_imu_values_move_over_time(monkeypatch):
    # Flat mock data hides bugs in anything that charts or diffs a series.
    first = MockImu().read()
    monkeypatch.setattr(time, "time", lambda: time.monotonic() + 10_000)
    assert MockImu().read().roll != first.roll


def test_gnss_reports_no_fix_while_acquiring():
    # Code that has never seen fix=False handles it badly the first time it
    # happens for real, which is outdoors, on a walk, with no SSH.
    reading = MockGnss().read()
    assert isinstance(reading, Position)
    assert reading.fix is False
    assert reading.lat is None and reading.satellites == 0


def test_gnss_produces_a_moving_track_once_fixed(monkeypatch):
    monkeypatch.setenv("CUBESAT_MOCK_FIX_DELAY_SEC", "0")
    import importlib

    from cubesat.hal.mock import gnss as gnss_module

    importlib.reload(gnss_module)
    device = gnss_module.MockGnss()
    first = device.read()
    device._started -= 100
    second = device.read()
    assert first.fix and second.fix
    assert second.lat != first.lat
    assert second.satellites >= 0


def test_environment_reports_all_five_measurements():
    reading = MockEnvironment().read()
    assert isinstance(reading, Environment)
    assert 15 <= reading.temperature <= 30
    assert 0 <= reading.humidity <= 100
    assert 900 <= reading.pressure <= 1100
    assert reading.light >= 0 and reading.uv_index >= 0


def test_power_starts_full_and_discharges():
    monitor = MockPowerMonitor()
    monitor._discharge_sec = 100
    full = monitor.read()
    assert isinstance(full, Power)
    assert full.battery_percent == 100.0
    monitor._started -= 50
    assert monitor.read().battery_percent == pytest.approx(50.0, abs=1)


def test_battery_can_be_pinned_to_reach_a_state_directly(monkeypatch):
    # LOW_POWER, SAFE and CRITICAL are unreachable in a test without this.
    monkeypatch.setenv("CUBESAT_MOCK_BATTERY", "15")
    assert MockPowerMonitor().read().battery_percent == 15.0


def test_pinned_battery_is_clamped(monkeypatch):
    monkeypatch.setenv("CUBESAT_MOCK_BATTERY", "500")
    assert MockPowerMonitor().read().battery_percent == 100.0


def test_unparseable_pin_is_ignored(monkeypatch):
    monkeypatch.setenv("CUBESAT_MOCK_BATTERY", "half")
    assert MockPowerMonitor().read().battery_percent == 100.0


def test_charge_rate_matches_what_the_level_is_doing(monkeypatch):
    monitor = MockPowerMonitor()
    monitor._discharge_sec = 3600
    assert monitor.read().charge_rate == -100.0
    monkeypatch.setenv("CUBESAT_MOCK_BATTERY", "40")
    assert MockPowerMonitor().read().charge_rate == 0.0


def test_mains_stops_the_discharge(monkeypatch):
    monkeypatch.setenv("CUBESAT_MOCK_EXTERNAL_POWER", "1")
    monitor = MockPowerMonitor()
    monitor._started -= 10_000
    reading = monitor.read()
    assert reading.external_power is True
    assert reading.battery_percent == 100.0
    assert reading.charge_rate == 0.0


def test_discharge_never_goes_below_empty():
    monitor = MockPowerMonitor()
    monitor._started -= 10_000
    reading = monitor.read()
    assert reading.battery_percent == 0.0
    assert reading.voltage == pytest.approx(3.2)


def test_camera_writes_a_real_decodable_file(tmp_path):
    path = tmp_path / "m_1" / "shot.jpg"
    photo = MockCamera().capture(path, overlay="test")
    assert photo.path.exists()
    raw = photo.path.read_bytes()
    assert raw[:2] == b"\xff\xd8" and raw[-2:] == b"\xff\xd9"
    assert photo.taken_at > 0


def test_camera_creates_the_mission_directory(tmp_path):
    MockCamera().capture(tmp_path / "deep" / "nested" / "m_7" / "a.jpg")
    assert (tmp_path / "deep" / "nested" / "m_7").is_dir()


def test_camera_close_is_harmless():
    assert MockCamera().close() is None


def test_radio_records_what_it_sent():
    radio = MockRadio()
    radio.send('{"b":1}')
    assert radio.sent == ['{"b":1}']


def test_radio_refuses_an_oversized_message():
    # Meshtastic's ceiling. Failing here on a laptop beats silently truncating
    # telemetry on a walk, which is what the pre-rewrite driver did.
    with pytest.raises(ValueError, match="at most 240"):
        MockRadio().send("x" * (MAX_MESSAGE_BYTES + 1))


def test_radio_delivers_injected_messages_once():
    radio = MockRadio()
    radio.inject('{"command":"set_profile"}')
    received = radio.poll()
    assert received[0].text == '{"command":"set_profile"}'
    assert received[0].sender and received[0].snr
    assert radio.poll() == []


def test_radio_close_is_harmless():
    assert MockRadio().close() is None


def test_readings_serialise_to_flat_dicts():
    # DHS writes these straight into database columns; a nested surprise there
    # becomes a schema mismatch.
    assert set(MockEnvironment().read().as_dict()) == {
        "temperature", "humidity", "pressure", "light", "uv_index", "uv_raw"
    }
    assert set(MockPowerMonitor().read().as_dict()) == {
        "battery_percent", "voltage", "external_power", "charge_rate"
    }
    imu = MockImu()
    imu._started -= 100
    attitude = imu.read().as_dict()
    assert attitude["quaternion"]["w"] and attitude["calibration"]["sys"] == 3
    assert set(MockGnss().read().as_dict()) == {
        "lat", "lon", "alt", "speed", "fix", "satellites"
    }
