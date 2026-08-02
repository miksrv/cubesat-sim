import json
from unittest.mock import MagicMock

from src.common import TOPICS
from src.adcs.main import ADCS

DEFAULT_ORIENTATION = {
    "roll": 1.0,
    "pitch": 2.0,
    "yaw": 3.0,
    "accel_g": {"x": 0.1, "y": 0.2, "z": 0.3},
    "gyro_dps": {"x": 0.4, "y": 0.5, "z": 0.6},
}
DEFAULT_GPS_FIX = {"lat": None, "lon": None, "alt": None, "speed": None, "fix": False}


def make_adcs(monkeypatch):
    monkeypatch.setattr("src.adcs.main.IMU", MagicMock())
    monkeypatch.setattr("src.adcs.main.GPS", MagicMock())
    adcs = ADCS()
    adcs.mqtt_client = MagicMock()
    adcs.imu = MagicMock()
    adcs.imu.get_orientation_deg.return_value = dict(DEFAULT_ORIENTATION)
    adcs.imu.read_imu_temp.return_value = 25.456
    adcs.gps = MagicMock()
    adcs.gps.read_position.return_value = dict(DEFAULT_GPS_FIX)
    return adcs


class TestPublishStatus:
    def test_publishes_full_packet(self, monkeypatch):
        adcs = make_adcs(monkeypatch)
        adcs.publish_status()

        adcs.mqtt_client.publish.assert_called_once()
        args, kwargs = adcs.mqtt_client.publish.call_args
        assert args[0] == TOPICS["adcs_status"]
        assert kwargs["qos"] == 1

        packet = json.loads(args[1])
        assert packet["roll"] == 1.0
        assert packet["pitch"] == 2.0
        assert packet["yaw"] == 3.0
        assert packet["imu_temp"] == 25.46  # rounded to 2 decimals
        assert packet["accel_g"] == DEFAULT_ORIENTATION["accel_g"]
        assert packet["gyro_dps"] == DEFAULT_ORIENTATION["gyro_dps"]
        assert packet["gps"] == DEFAULT_GPS_FIX
        assert "timestamp" in packet

    def test_gps_fix_is_forwarded_when_available(self, monkeypatch):
        adcs = make_adcs(monkeypatch)
        adcs.gps.read_position.return_value = {
            "lat": 48.117,
            "lon": 11.516,
            "alt": 545.4,
            "speed": None,
            "fix": True,
        }
        adcs.publish_status()
        packet = json.loads(adcs.mqtt_client.publish.call_args.args[1])
        assert packet["gps"]["fix"] is True
        assert packet["gps"]["lat"] == 48.117

    def test_sensor_error_is_caught_and_publish_skipped(self, monkeypatch):
        adcs = make_adcs(monkeypatch)
        adcs.imu.get_orientation_deg.side_effect = RuntimeError("I2C error")
        adcs.publish_status()  # must not raise
        adcs.mqtt_client.publish.assert_not_called()


class TestRun:
    def test_connects_loops_and_shuts_down_cleanly(self, monkeypatch):
        adcs = make_adcs(monkeypatch)

        def raise_keyboard_interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr("src.adcs.main.time.sleep", raise_keyboard_interrupt)

        adcs.run()

        adcs.mqtt_client.connect.assert_called_once()
        adcs.mqtt_client.loop_start.assert_called_once()
        adcs.mqtt_client.publish.assert_called_once()
        adcs.mqtt_client.loop_stop.assert_called_once()
        adcs.mqtt_client.disconnect.assert_called_once()

    def test_unexpected_exception_in_loop_still_shuts_down_cleanly(self, monkeypatch):
        # connect()/loop_start() in ADCS.run() sit outside the try/except,
        # so only errors raised once the polling loop is running are caught.
        adcs = make_adcs(monkeypatch)

        def raise_runtime_error(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("src.adcs.main.time.sleep", raise_runtime_error)

        adcs.run()  # must not raise

        adcs.mqtt_client.loop_stop.assert_called_once()
        adcs.mqtt_client.disconnect.assert_called_once()

    def test_connect_failure_propagates(self, monkeypatch):
        adcs = make_adcs(monkeypatch)
        adcs.mqtt_client.connect.side_effect = RuntimeError("network down")

        try:
            adcs.run()
            assert False, "expected RuntimeError to propagate"
        except RuntimeError:
            pass

        adcs.mqtt_client.loop_stop.assert_not_called()
