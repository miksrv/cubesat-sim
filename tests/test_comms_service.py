import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.common import TOPICS
import src.comms.service as service_mod
from src.comms.service import CommsService


def make_service(monkeypatch, tmp_path):
    monkeypatch.setattr(service_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(service_mod, "LoRaModule", MagicMock())
    service = CommsService()
    service.mqtt_client = MagicMock()
    return service


def command_msg(payload_dict, topic=None):
    msg = MagicMock()
    msg.topic = topic or TOPICS["command"]
    msg.payload = json.dumps(payload_dict).encode("utf-8")
    return msg


class TestCreateTable:
    def test_comms_log_table_has_expected_columns(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        cursor = service.conn.cursor()
        cursor.execute("PRAGMA table_info(comms_log)")
        columns = {row[1] for row in cursor.fetchall()}
        assert {
            "id", "timestamp", "battery", "voltage", "external_power",
            "roll", "pitch", "yaw", "imu_temp",
            "accel_x", "accel_y", "accel_z",
            "gyro_x", "gyro_y", "gyro_z",
            "temperature", "humidity", "pressure",
            "cpu_percent", "ram_percent", "swap_percent", "disk_percent",
            "uptime_seconds", "cpu_temperature", "obc_state", "raw_json",
        } == columns


class TestOnMqttConnect:
    def test_successful_connect_subscribes_to_all_topics(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.on_mqtt_connect(service.mqtt_client, None, {}, 0)
        for key in ("obc_status", "eps_status", "adcs_status", "payload_data", "command"):
            service.mqtt_client.subscribe.assert_any_call(TOPICS[key], qos=1)

    def test_failed_connect_does_not_subscribe(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.on_mqtt_connect(service.mqtt_client, None, {}, 1)
        service.mqtt_client.subscribe.assert_not_called()


class TestOnMqttMessage:
    @pytest.mark.parametrize(
        "topic_key, latest_key",
        [
            ("obc_status", "obc"),
            ("eps_status", "eps"),
            ("adcs_status", "adcs"),
            ("payload_data", "payload"),
        ],
    )
    def test_caches_latest_subsystem_data(self, monkeypatch, tmp_path, topic_key, latest_key):
        service = make_service(monkeypatch, tmp_path)
        msg = command_msg({"value": 42}, topic=TOPICS[topic_key])
        service.on_mqtt_message(service.mqtt_client, None, msg)
        assert service.latest[latest_key] == {"value": 42}

    def test_get_telemetry_publishes_comms_data_with_request_id(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.system_collector.collect = MagicMock(return_value={"cpu_percent": 1.0})
        msg = command_msg({"command": "get_telemetry", "request_id": "abc"})
        service.on_mqtt_message(service.mqtt_client, None, msg)

        args, kwargs = service.mqtt_client.publish.call_args
        assert args[0] == TOPICS["comms_data"]
        assert kwargs["retain"] is True
        packet = json.loads(args[1])
        assert packet["request_id"] == "abc"

    def test_set_comms_config_updates_flags(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        msg = command_msg({"command": "set_comms_config", "params": {"lora_enabled": True}})
        service.on_mqtt_message(service.mqtt_client, None, msg)
        assert service.lora_enabled is True

    def test_invalid_json_is_swallowed(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        msg = MagicMock()
        msg.topic = TOPICS["command"]
        msg.payload = b"not json"
        service.on_mqtt_message(service.mqtt_client, None, msg)  # must not raise

    def test_unmatched_topic_does_not_raise(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        msg = command_msg({"anything": True}, topic="cubesat/unrelated")
        service.on_mqtt_message(service.mqtt_client, None, msg)  # must not raise


class TestApplyConfig:
    def test_updates_only_provided_keys(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        original_api = service.api_enabled
        service._apply_config({"aggregation_enabled": False})
        assert service.aggregation_enabled is False
        assert service.api_enabled == original_api

    def test_updates_multiple_keys(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service._apply_config({"api_enabled": False, "lora_enabled": True, "aggregation_enabled": False})
        assert service.api_enabled is False
        assert service.lora_enabled is True
        assert service.aggregation_enabled is False

    def test_empty_params_changes_nothing(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        before = (service.api_enabled, service.lora_enabled, service.aggregation_enabled)
        service._apply_config({})
        after = (service.api_enabled, service.lora_enabled, service.aggregation_enabled)
        assert before == after


class TestRepublishCommand:
    def test_publishes_unchanged_to_command_topic(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        cmd = {"command": "safe_mode"}
        service._republish_command(cmd)
        service.mqtt_client.publish.assert_called_once_with(
            TOPICS["command"], json.dumps(cmd), qos=1
        )


class TestBuildCommsPacket:
    def test_defaults_to_unknown_obc_state(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.system_collector.collect = MagicMock(return_value={})
        packet = service.build_comms_packet()
        assert packet["obc_state"] == "UNKNOWN"
        assert packet["eps"] == {}
        assert packet["adcs"] == {}
        assert packet["payload"] == {}

    def test_includes_cached_subsystem_data_and_system_metrics(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.latest["obc"] = {"status": "SCIENCE"}
        service.latest["eps"] = {"battery": 80}
        service.system_collector.collect = MagicMock(return_value={"cpu_percent": 12.0})

        packet = service.build_comms_packet()

        assert packet["obc_state"] == "SCIENCE"
        assert packet["eps"] == {"battery": 80}
        assert packet["system"] == {"cpu_percent": 12.0}
        service.system_collector.collect.assert_called_once_with(with_interval=0.8)
        assert packet["timestamp"].endswith("Z")


class TestLogToDbAndAggregate:
    def _row(self, service):
        cursor = service.conn.cursor()
        cursor.execute("SELECT * FROM comms_log ORDER BY id DESC LIMIT 1")
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, cursor.fetchone()))

    def test_full_packet_persists_all_fields(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.latest["eps"] = {"battery": 77.0, "voltage": 4.0, "external_power": True}
        service.latest["adcs"] = {
            "roll": 1.0, "pitch": 2.0, "yaw": 3.0, "imu_temp": 25.0,
            "accel_g": {"x": 0.1, "y": 0.2, "z": 0.3},
            "gyro_dps": {"x": 0.4, "y": 0.5, "z": 0.6},
        }
        service.latest["payload"] = {"temperature": 21.0, "humidity": 40.0, "pressure": 1000.0}
        service.latest["obc"] = {"status": "SCIENCE"}
        service.system_collector.collect = MagicMock(return_value={
            "cpu_percent": 10.0, "ram_percent": 20.0, "swap_percent": 1.0,
            "disk_percent": 30.0, "uptime_seconds": 500, "cpu_temperature": 45.0,
        })

        service.aggregate()
        row = self._row(service)

        assert row["battery"] == 77.0
        assert row["external_power"] == 1
        assert row["roll"] == 1.0
        assert row["accel_x"] == 0.1
        assert row["gyro_z"] == 0.6
        assert row["temperature"] == 21.0
        assert row["cpu_percent"] == 10.0
        assert row["obc_state"] == "SCIENCE"
        assert json.loads(row["raw_json"])["obc_state"] == "SCIENCE"

    def test_missing_nested_fields_default_to_null(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.system_collector.collect = MagicMock(return_value={})
        service.aggregate()
        row = self._row(service)
        assert row["battery"] is None
        assert row["accel_x"] is None
        assert row["external_power"] == 0


class TestCleanupOldRecords:
    def test_purges_rows_older_than_retention_and_keeps_recent(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_DB_RETENTION_DAYS", 30)

        old_ts = (datetime.utcnow() - timedelta(days=365)).isoformat() + "Z"
        recent_ts = datetime.utcnow().isoformat() + "Z"

        cursor = service.conn.cursor()
        cursor.execute("INSERT INTO comms_log (timestamp) VALUES (?)", (old_ts,))
        cursor.execute("INSERT INTO comms_log (timestamp) VALUES (?)", (recent_ts,))
        service.conn.commit()

        service._cleanup_old_records()

        remaining = [r[0] for r in service.conn.execute("SELECT timestamp FROM comms_log")]
        assert old_ts not in remaining
        assert recent_ts in remaining


class TestSendToRemoteApi:
    def test_skips_send_when_no_api_key(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_KEY", None)
        service.send_to_remote_api({"timestamp": "x"})
        assert requests_mock.call_count == 0

    def test_posts_packet_with_api_key_header(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_KEY", "secret-key")
        monkeypatch.setattr(service_mod, "COMMS_API_URL", "http://ground.test")
        requests_mock.post("http://ground.test/api/cubesat/comms", status_code=201)

        service.send_to_remote_api({"timestamp": "2026-01-01T00:00:00Z"})

        assert requests_mock.call_count == 1
        sent = requests_mock.last_request
        assert sent.headers["X-API-Key"] == "secret-key"
        assert sent.json() == {"timestamp": "2026-01-01T00:00:00Z"}

    def test_non_201_response_does_not_raise(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_KEY", "secret-key")
        monkeypatch.setattr(service_mod, "COMMS_API_URL", "http://ground.test")
        requests_mock.post("http://ground.test/api/cubesat/comms", status_code=500)

        service.send_to_remote_api({"timestamp": "x"})  # must not raise

    def test_connection_error_does_not_raise(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_KEY", "secret-key")
        monkeypatch.setattr(service_mod, "COMMS_API_URL", "http://ground.test")
        requests_mock.post("http://ground.test/api/cubesat/comms", exc=ConnectionError)

        service.send_to_remote_api({"timestamp": "x"})  # must not raise


class TestPollRemoteCommands:
    def test_republishes_each_pending_command(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_URL", "http://ground.test")
        pending = [{"command": "safe_mode"}, {"command": "recover"}]
        requests_mock.get("http://ground.test/api/cubesat/commands/pending", json=pending)

        service.poll_remote_commands()

        assert service.mqtt_client.publish.call_count == 2
        published = [json.loads(c.args[1]) for c in service.mqtt_client.publish.call_args_list]
        assert published == pending

    def test_non_200_response_republishes_nothing(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_URL", "http://ground.test")
        requests_mock.get("http://ground.test/api/cubesat/commands/pending", status_code=404)

        service.poll_remote_commands()
        service.mqtt_client.publish.assert_not_called()

    def test_request_exception_does_not_raise(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_URL", "http://ground.test")
        requests_mock.get(
            "http://ground.test/api/cubesat/commands/pending", exc=ConnectionError
        )
        service.poll_remote_commands()  # must not raise


class TestCheckInternet:
    def test_true_when_reachable(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_URL", "http://ground.test")
        requests_mock.get("http://ground.test/api/cubesat/comms/latest", status_code=200)
        assert service._check_internet() is True

    def test_false_when_unreachable(self, monkeypatch, tmp_path, requests_mock):
        service = make_service(monkeypatch, tmp_path)
        monkeypatch.setattr(service_mod, "COMMS_API_URL", "http://ground.test")
        requests_mock.get(
            "http://ground.test/api/cubesat/comms/latest", exc=ConnectionError
        )
        assert service._check_internet() is False


class TestPollLora:
    def test_no_packet_is_a_noop(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.lora.receive.return_value = None
        service._poll_lora()
        service.mqtt_client.publish.assert_not_called()

    def test_valid_packet_is_republished(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        cmd = {"command": "recover"}
        service.lora.receive.return_value = json.dumps(cmd).encode("utf-8")
        service._poll_lora()
        service.mqtt_client.publish.assert_called_once_with(
            TOPICS["command"], json.dumps(cmd), qos=1
        )

    def test_malformed_packet_is_discarded(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        service.lora.receive.return_value = b"\xff\xfe not json"
        service._poll_lora()  # must not raise
        service.mqtt_client.publish.assert_not_called()


class TestRun:
    def _stub_loop_dependencies(self, service, monkeypatch):
        monkeypatch.setattr(service, "aggregate", MagicMock())
        monkeypatch.setattr(service, "_cleanup_old_records", MagicMock())
        # build_comms_packet's system_collector.collect() calls
        # psutil.cpu_percent(interval=...), which sleeps internally via the
        # same global time.sleep our run() tests monkeypatch to break the
        # loop — stub it out so that doesn't fire prematurely.
        monkeypatch.setattr(service, "build_comms_packet", MagicMock(return_value={"timestamp": "x"}))
        monkeypatch.setattr(service, "send_to_remote_api", MagicMock())
        monkeypatch.setattr(service, "poll_remote_commands", MagicMock())
        monkeypatch.setattr(service, "_check_internet", MagicMock(return_value=False))
        monkeypatch.setattr(service, "_poll_lora", MagicMock())

    def test_science_state_triggers_aggregation(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        self._stub_loop_dependencies(service, monkeypatch)
        service.latest["obc"] = {"status": "SCIENCE"}
        service.api_enabled = False
        service.lora_enabled = False

        monkeypatch.setattr(
            "src.comms.service.time.sleep",
            MagicMock(side_effect=KeyboardInterrupt),
        )
        service.run()

        service.aggregate.assert_called_once()
        service.mqtt_client.connect.assert_called_once()
        service.mqtt_client.loop_stop.assert_called_once()
        service.mqtt_client.disconnect.assert_called_once()

    def test_cleanup_runs_every_n_loops(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        self._stub_loop_dependencies(service, monkeypatch)
        service.latest["obc"] = {"status": "SCIENCE"}
        service.api_enabled = False
        service.lora_enabled = False
        service._loop_count = service_mod.CLEANUP_EVERY_N_LOOPS - 1

        monkeypatch.setattr(
            "src.comms.service.time.sleep",
            MagicMock(side_effect=KeyboardInterrupt),
        )
        service.run()

        service._cleanup_old_records.assert_called_once()

    def test_non_science_state_skips_aggregation(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        self._stub_loop_dependencies(service, monkeypatch)
        service.latest["obc"] = {"status": "NOMINAL"}
        service.api_enabled = False
        service.lora_enabled = False

        monkeypatch.setattr(
            "src.comms.service.time.sleep",
            MagicMock(side_effect=KeyboardInterrupt),
        )
        service.run()

        service.aggregate.assert_not_called()

    def test_api_enabled_and_reachable_sends_and_polls(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        self._stub_loop_dependencies(service, monkeypatch)
        service._check_internet.return_value = True
        service.api_enabled = True
        service.lora_enabled = False

        monkeypatch.setattr(
            "src.comms.service.time.sleep",
            MagicMock(side_effect=KeyboardInterrupt),
        )
        service.run()

        service.send_to_remote_api.assert_called_once()
        service.poll_remote_commands.assert_called_once()

    def test_lora_enabled_sends_and_polls(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        self._stub_loop_dependencies(service, monkeypatch)
        service.api_enabled = False
        service.lora_enabled = True

        monkeypatch.setattr(
            "src.comms.service.time.sleep",
            MagicMock(side_effect=KeyboardInterrupt),
        )
        service.run()

        service.lora.send.assert_called_once()
        service._poll_lora.assert_called_once()

    def test_unexpected_exception_still_shuts_down_cleanly(self, monkeypatch, tmp_path):
        service = make_service(monkeypatch, tmp_path)
        self._stub_loop_dependencies(service, monkeypatch)
        service.aggregate.side_effect = RuntimeError("boom")
        service.latest["obc"] = {"status": "SCIENCE"}

        service.run()  # must not raise

        service.mqtt_client.loop_stop.assert_called_once()
        service.mqtt_client.disconnect.assert_called_once()
