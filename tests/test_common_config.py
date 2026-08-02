"""
config.py reads config/config.yaml and environment variables at *import
time*, so these tests exercise `_load_yaml_config` and `get_config`
directly rather than re-importing src.common.config with different env
vars (re-importing a cached module under a different name is brittle and
not worth it here — the interesting logic, "env overrides YAML", is
covered by reading the module as already imported plus `get_config`).
"""

import importlib

from src.common import config


class TestTopics:
    def test_all_documented_topics_present(self):
        expected_keys = {
            "command",
            "obc_status",
            "eps_status",
            "adcs_status",
            "payload_status",
            "payload_data",
            "payload_photo",
            "comms_data",
        }
        assert expected_keys == set(config.TOPICS.keys())

    def test_topic_values_are_namespaced(self):
        for topic in config.TOPICS.values():
            assert topic.startswith("cubesat/")


class TestDefaults:
    def test_mqtt_defaults(self):
        assert config.MQTT_BROKER == "localhost"
        assert config.MQTT_PORT == 1883
        assert config.MQTT_KEEPALIVE == 60

    def test_photo_resolution_is_tuple(self):
        assert isinstance(config.PHOTO_RESOLUTION, tuple)
        assert len(config.PHOTO_RESOLUTION) == 2

    def test_comms_defaults(self):
        assert config.COMMS_LOOP_INTERVAL_SEC == 30
        assert config.COMMS_DB_RETENTION_DAYS == 30

    def test_comms_channel_defaults(self):
        # LoRa defaults OFF (unverified against hardware); the other two default ON.
        assert config.COMMS_API_ENABLED == 1
        assert config.COMMS_LORA_ENABLED == 0
        assert config.COMMS_AGGREGATION_ENABLED == 1

    def test_data_paths_are_under_base_dir(self):
        assert config.PHOTOS_DIR == config.DATA_DIR / "photos"
        assert config.DB_PATH == config.DATA_DIR / "comms.db"
        assert config.DATA_DIR.parent == config.BASE_DIR


class TestLoadYamlConfig:
    def test_returns_empty_dict_when_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_CONFIG_FILE", tmp_path / "does-not-exist.yaml")
        assert config._load_yaml_config() == {}

    def test_loads_yaml_contents(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("mqtt:\n  broker: 10.0.0.5\n  port: 1884\n")
        monkeypatch.setattr(config, "_CONFIG_FILE", yaml_file)
        loaded = config._load_yaml_config()
        assert loaded == {"mqtt": {"broker": "10.0.0.5", "port": 1884}}

    def test_empty_yaml_file_returns_empty_dict(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("")
        monkeypatch.setattr(config, "_CONFIG_FILE", yaml_file)
        assert config._load_yaml_config() == {}


class TestGetConfig:
    def test_returns_env_var_uppercased(self, monkeypatch):
        monkeypatch.setenv("MY_CUSTOM_KEY", "value123")
        assert config.get_config("my_custom_key") == "value123"

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("TOTALLY_UNSET_KEY", raising=False)
        assert config.get_config("totally_unset_key", "fallback") == "fallback"

    def test_returns_none_when_unset_and_no_default(self, monkeypatch):
        monkeypatch.delenv("TOTALLY_UNSET_KEY", raising=False)
        assert config.get_config("totally_unset_key") is None


class TestEnvOverridesYaml:
    def test_mqtt_broker_env_override(self, monkeypatch):
        """Reimport config in an isolated module to verify env vars win over
        YAML defaults, without disturbing the shared cached config module
        used by the rest of the app/tests."""
        monkeypatch.setenv("MQTT_BROKER", "192.168.1.50")
        monkeypatch.setenv("MQTT_PORT", "9999")
        spec = importlib.util.spec_from_file_location(
            "cubesat_config_reload_test", config.__file__
        )
        reloaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reloaded)
        assert reloaded.MQTT_BROKER == "192.168.1.50"
        assert reloaded.MQTT_PORT == 9999
