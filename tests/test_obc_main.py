import json
from unittest.mock import MagicMock

from src.common import TOPICS
from src.obc.main import OBC


def make_obc():
    obc = OBC()
    obc.mqtt_client = MagicMock()
    return obc


class TestInit:
    def test_boots_to_nominal_and_not_connected(self):
        obc = OBC()
        assert obc.state_machine.state == "NOMINAL"
        assert obc._mqtt_connected is False


class TestOnMqttConnect:
    def test_successful_connect_subscribes_and_publishes(self):
        obc = make_obc()
        obc.on_mqtt_connect(obc.mqtt_client, None, {}, 0)

        assert obc._mqtt_connected is True
        obc.mqtt_client.subscribe.assert_any_call(TOPICS["eps_status"], qos=1)
        obc.mqtt_client.subscribe.assert_any_call(TOPICS["command"], qos=1)

        publish_call = obc.mqtt_client.publish.call_args
        assert publish_call.args[0] == TOPICS["obc_status"]
        payload = json.loads(publish_call.args[1])
        assert payload["status"] == "NOMINAL"
        assert publish_call.kwargs["retain"] is True

    def test_failed_connect_does_not_subscribe(self):
        obc = make_obc()
        obc.on_mqtt_connect(obc.mqtt_client, None, {}, 5)

        assert obc._mqtt_connected is False
        obc.mqtt_client.subscribe.assert_not_called()
        obc.mqtt_client.publish.assert_not_called()


class TestOnMqttMessage:
    def _msg(self, topic, payload_dict):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = json.dumps(payload_dict).encode("utf-8")
        return msg

    def test_routes_eps_status_to_handler(self):
        obc = make_obc()
        obc.handlers.handle_eps_status = MagicMock()
        msg = self._msg(TOPICS["eps_status"], {"battery": 50})
        obc.on_mqtt_message(obc.mqtt_client, None, msg)
        obc.handlers.handle_eps_status.assert_called_once_with(msg.payload.decode("utf-8"))

    def test_routes_command_to_handler(self):
        obc = make_obc()
        obc.handlers.handle_command = MagicMock()
        msg = self._msg(TOPICS["command"], {"command": "safe_mode"})
        obc.on_mqtt_message(obc.mqtt_client, None, msg)
        obc.handlers.handle_command.assert_called_once_with(msg.payload.decode("utf-8"))

    def test_unhandled_topic_does_not_raise(self):
        obc = make_obc()
        msg = self._msg("cubesat/unknown", {})
        obc.on_mqtt_message(obc.mqtt_client, None, msg)  # must not raise

    def test_malformed_payload_does_not_raise(self):
        obc = make_obc()
        msg = MagicMock()
        msg.topic = TOPICS["command"]
        msg.payload = b"\xff\xfe not utf-8"
        obc.on_mqtt_message(obc.mqtt_client, None, msg)  # must not raise


class TestRun:
    def test_connects_loops_and_shuts_down_cleanly(self, monkeypatch):
        obc = make_obc()

        def raise_keyboard_interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr("src.obc.main.time.sleep", raise_keyboard_interrupt)

        obc.run()

        obc.mqtt_client.connect.assert_called_once()
        obc.mqtt_client.loop_start.assert_called_once()
        obc.mqtt_client.publish.assert_called_once()
        obc.mqtt_client.loop_stop.assert_called_once()
        obc.mqtt_client.disconnect.assert_called_once()

    def test_unexpected_exception_still_shuts_down_cleanly(self, monkeypatch):
        obc = make_obc()
        obc.mqtt_client.publish.side_effect = RuntimeError("boom")

        obc.run()  # must not raise, exception is caught and logged

        obc.mqtt_client.loop_stop.assert_called_once()
        obc.mqtt_client.disconnect.assert_called_once()
