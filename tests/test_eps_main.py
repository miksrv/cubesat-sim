import json
from unittest.mock import MagicMock

from src.common import TOPICS
from src.eps.main import EPSService


def make_service(monkeypatch, status=None):
    monkeypatch.setattr("src.eps.main.EPSMonitor", MagicMock())
    service = EPSService()
    service.mqtt_client = MagicMock()
    service.monitor = MagicMock()
    service.monitor.get_status.return_value = status or {
        "timestamp": 123.0,
        "battery": 80.0,
        "voltage": 4.1,
        "external_power": True,
    }
    return service


class TestPublishStatus:
    def test_publishes_monitor_status_to_eps_topic(self, monkeypatch):
        service = make_service(monkeypatch)
        service.publish_status()

        service.mqtt_client.publish.assert_called_once()
        args, kwargs = service.mqtt_client.publish.call_args
        assert args[0] == TOPICS["eps_status"]
        assert json.loads(args[1]) == service.monitor.get_status.return_value
        assert kwargs["qos"] == 1
        assert kwargs["retain"] is True


class TestRun:
    def test_connects_loops_and_shuts_down_cleanly(self, monkeypatch):
        service = make_service(monkeypatch)

        def raise_keyboard_interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr("src.eps.main.time.sleep", raise_keyboard_interrupt)

        service.run()

        service.mqtt_client.connect.assert_called_once()
        service.mqtt_client.loop_start.assert_called_once()
        service.mqtt_client.publish.assert_called_once()
        service.mqtt_client.loop_stop.assert_called_once()
        service.mqtt_client.disconnect.assert_called_once()

    def test_unexpected_exception_still_shuts_down_cleanly(self, monkeypatch):
        service = make_service(monkeypatch)
        service.monitor.get_status.side_effect = RuntimeError("boom")

        service.run()  # must not raise

        service.mqtt_client.loop_stop.assert_called_once()
        service.mqtt_client.disconnect.assert_called_once()
