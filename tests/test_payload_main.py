import base64
import json
from unittest.mock import MagicMock

from src.common import TOPICS
from src.payload.main import PayloadService


def make_service(monkeypatch):
    monkeypatch.setattr("src.payload.main.PayloadCamera", MagicMock())
    monkeypatch.setattr("src.payload.main.ScienceCollector", MagicMock())
    service = PayloadService()
    service.mqtt_client = MagicMock()
    service.camera = MagicMock()
    service.science = MagicMock()
    return service


def command_msg(payload_dict, topic=None):
    msg = MagicMock()
    msg.topic = topic or TOPICS["command"]
    msg.payload = json.dumps(payload_dict).encode("utf-8")
    return msg


class TestOnMqttConnect:
    def test_successful_connect_subscribes_and_publishes_idle(self, monkeypatch):
        service = make_service(monkeypatch)
        service.on_mqtt_connect(service.mqtt_client, None, {}, 0)

        service.mqtt_client.subscribe.assert_any_call(TOPICS["obc_status"], qos=1)
        service.mqtt_client.subscribe.assert_any_call(TOPICS["command"], qos=1)

        args, kwargs = service.mqtt_client.publish.call_args
        assert args[0] == TOPICS["payload_status"]
        payload = json.loads(args[1])
        assert payload["state"] == "IDLE"
        assert payload["alive"] is True

    def test_failed_connect_does_not_subscribe(self, monkeypatch):
        service = make_service(monkeypatch)
        service.on_mqtt_connect(service.mqtt_client, None, {}, 1)
        service.mqtt_client.subscribe.assert_not_called()


class TestObcStatusTracking:
    def test_updates_obc_state_from_status_topic(self, monkeypatch):
        service = make_service(monkeypatch)
        msg = command_msg({"status": "NOMINAL"}, topic=TOPICS["obc_status"])
        service.on_mqtt_message(service.mqtt_client, None, msg)
        assert service.obc_state == "NOMINAL"


class TestTakePhotoCommand:
    def test_denied_when_obc_not_nominal(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "SAFE"
        msg = command_msg({"command": "take_photo", "request_id": "r1"})
        service.on_mqtt_message(service.mqtt_client, None, msg)

        service.camera.take_photo.assert_not_called()
        args, kwargs = service.mqtt_client.publish.call_args
        assert args[0] == TOPICS["payload_photo"]
        response = json.loads(args[1])
        assert response["status"] == "ERROR"
        assert response["request_id"] == "r1"
        assert "SAFE" in response["reason"]

    def test_successful_capture_publishes_base64_photo_and_deletes_file(
        self, monkeypatch, tmp_path
    ):
        service = make_service(monkeypatch)
        service.obc_state = "NOMINAL"

        photo_path = tmp_path / "photo_test.jpg"
        photo_path.write_bytes(b"jpeg-bytes")
        service.camera.take_photo.return_value = str(photo_path)

        msg = command_msg(
            {"command": "take_photo", "request_id": "r2", "params": {"overlay": True}}
        )
        service.on_mqtt_message(service.mqtt_client, None, msg)

        service.camera.take_photo.assert_called_once_with(overlay=True)
        args, kwargs = service.mqtt_client.publish.call_args
        assert args[0] == TOPICS["payload_photo"]
        assert kwargs["retain"] is False
        response = json.loads(args[1])
        assert response["status"] == "SUCCESS"
        assert response["request_id"] == "r2"
        assert base64.b64decode(response["photo_base64"]) == b"jpeg-bytes"
        assert response["size_bytes"] == len(b"jpeg-bytes")
        assert not photo_path.exists()  # deleted after sending

    def test_capture_failure_sends_error_response(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "NOMINAL"
        service.camera.take_photo.return_value = None

        msg = command_msg({"command": "take_photo", "request_id": "r3"})
        service.on_mqtt_message(service.mqtt_client, None, msg)

        photo_call = service.mqtt_client.publish.call_args_list[0]
        assert photo_call.args[0] == TOPICS["payload_photo"]
        response = json.loads(photo_call.args[1])
        assert response["status"] == "ERROR"
        assert response["request_id"] == "r3"

        status_call = service.mqtt_client.publish.call_args_list[1]
        assert status_call.args[0] == TOPICS["payload_status"]

    def test_encoding_failure_sends_error_response(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch)
        service.obc_state = "NOMINAL"
        photo_path = tmp_path / "photo.jpg"
        photo_path.write_bytes(b"jpeg-bytes")
        service.camera.take_photo.return_value = str(photo_path)

        def boom(_data):
            raise ValueError("encoding blew up")

        monkeypatch.setattr("src.payload.main.base64.b64encode", boom)

        msg = command_msg({"command": "take_photo", "request_id": "r4"})
        service.on_mqtt_message(service.mqtt_client, None, msg)

        photo_call = service.mqtt_client.publish.call_args_list[0]
        response = json.loads(photo_call.args[1])
        assert response["status"] == "ERROR"
        assert response["reason"] == "Failed to encode photo"

    def test_file_removal_failure_after_success_is_logged_not_raised(
        self, monkeypatch, tmp_path
    ):
        service = make_service(monkeypatch)
        service.obc_state = "NOMINAL"
        photo_path = tmp_path / "photo.jpg"
        photo_path.write_bytes(b"jpeg-bytes")
        service.camera.take_photo.return_value = str(photo_path)

        def boom(_path):
            raise OSError("permission denied")

        monkeypatch.setattr("src.payload.main.os.remove", boom)

        msg = command_msg({"command": "take_photo", "request_id": "r5"})
        service.on_mqtt_message(service.mqtt_client, None, msg)  # must not raise

        response = json.loads(service.mqtt_client.publish.call_args.args[1])
        assert response["status"] == "SUCCESS"
        assert photo_path.exists()  # removal failed, file stays put

    def test_unexpected_error_during_handling_is_swallowed(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "NOMINAL"
        service.camera.take_photo.side_effect = RuntimeError("camera driver crashed")

        msg = command_msg({"command": "take_photo", "request_id": "r6"})
        service.on_mqtt_message(service.mqtt_client, None, msg)  # must not raise

    def test_request_id_defaults_when_missing(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "SAFE"
        msg = command_msg({"command": "take_photo"})
        service.on_mqtt_message(service.mqtt_client, None, msg)
        response = json.loads(service.mqtt_client.publish.call_args.args[1])
        assert response["request_id"].startswith("req_")


class TestTimelapseCommands:
    def test_start_timelapse_when_nominal(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "NOMINAL"
        msg = command_msg({"command": "start_timelapse", "params": {"interval_sec": 30}})
        service.on_mqtt_message(service.mqtt_client, None, msg)
        service.camera.start_timelapse.assert_called_once_with(interval_sec=30)

    def test_start_timelapse_denied_outside_nominal(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "SCIENCE"
        msg = command_msg({"command": "start_timelapse"})
        service.on_mqtt_message(service.mqtt_client, None, msg)
        service.camera.start_timelapse.assert_not_called()

    def test_start_timelapse_default_interval(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "NOMINAL"
        msg = command_msg({"command": "start_timelapse", "params": {}})
        service.on_mqtt_message(service.mqtt_client, None, msg)
        service.camera.start_timelapse.assert_called_once_with(interval_sec=60)

    def test_stop_timelapse_allowed_from_any_state(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "SAFE"
        msg = command_msg({"command": "stop_timelapse"})
        service.on_mqtt_message(service.mqtt_client, None, msg)
        service.camera.stop_timelapse.assert_called_once()


class TestMessageErrorHandling:
    def test_invalid_json_is_swallowed(self, monkeypatch):
        service = make_service(monkeypatch)
        msg = MagicMock()
        msg.topic = TOPICS["command"]
        msg.payload = b"not json"
        service.on_mqtt_message(service.mqtt_client, None, msg)  # must not raise

    def test_unknown_command_is_noop(self, monkeypatch):
        service = make_service(monkeypatch)
        service.obc_state = "NOMINAL"
        msg = command_msg({"command": "unknown_thing"})
        service.on_mqtt_message(service.mqtt_client, None, msg)  # must not raise
        service.camera.take_photo.assert_not_called()
        service.camera.start_timelapse.assert_not_called()
        service.camera.stop_timelapse.assert_not_called()


class TestRun:
    def test_connects_publishes_science_data_and_shuts_down_cleanly(self, monkeypatch):
        service = make_service(monkeypatch)
        service.science.collect.return_value = {"temperature": 21.0}

        def raise_keyboard_interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr("src.payload.main.time.sleep", raise_keyboard_interrupt)

        service.run()

        service.mqtt_client.connect.assert_called_once()
        service.mqtt_client.loop_start.assert_called_once()
        args, kwargs = service.mqtt_client.publish.call_args
        assert args[0] == TOPICS["payload_data"]
        assert json.loads(args[1]) == {"temperature": 21.0}
        service.mqtt_client.loop_stop.assert_called_once()
        service.mqtt_client.disconnect.assert_called_once()
        service.camera.cleanup.assert_called_once()

    def test_unexpected_exception_still_shuts_down_cleanly(self, monkeypatch):
        service = make_service(monkeypatch)
        service.science.collect.side_effect = RuntimeError("boom")

        service.run()  # must not raise

        service.mqtt_client.loop_stop.assert_called_once()
        service.mqtt_client.disconnect.assert_called_once()
        service.camera.cleanup.assert_called_once()
