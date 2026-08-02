import paho.mqtt.client as mqtt

from src.common.mqtt_client import get_mqtt_client


class TestGetMqttClient:
    def test_returns_mqttv5_client(self):
        client = get_mqtt_client("cubesat-test")
        assert isinstance(client, mqtt.Client)
        assert client._protocol == mqtt.MQTTv5

    def test_client_id_has_random_suffix(self):
        client = get_mqtt_client("cubesat-test")
        client_id = client._client_id.decode()
        assert client_id.startswith("cubesat-test_")
        suffix = client_id.rsplit("_", 1)[1]
        assert suffix.isdigit()
        assert 1000 <= int(suffix) <= 9999

    def test_different_instances_get_different_ids(self):
        c1 = get_mqtt_client("cubesat-test")
        c2 = get_mqtt_client("cubesat-test")
        assert c1._client_id != c2._client_id

    def test_sets_username_password_when_both_given(self):
        client = get_mqtt_client("cubesat-test", username="alice", password="secret")
        assert client._username == b"alice"
        assert client._password == b"secret"

    def test_no_auth_when_credentials_omitted(self):
        client = get_mqtt_client("cubesat-test")
        assert client._username is None
        assert client._password is None

    def test_username_without_password_is_ignored(self):
        client = get_mqtt_client("cubesat-test", username="alice")
        assert client._username is None

    def test_reconnect_delay_defaults(self):
        client = get_mqtt_client("cubesat-test")
        assert client._reconnect_min_delay == 1
        assert client._reconnect_max_delay == 120

    def test_reconnect_delay_custom(self):
        client = get_mqtt_client(
            "cubesat-test", reconnect_delay_min=5, reconnect_delay_max=60
        )
        assert client._reconnect_min_delay == 5
        assert client._reconnect_max_delay == 60

    def test_userdata_carries_reconnect_settings(self):
        client = get_mqtt_client("cubesat-test", reconnect_delay_min=2, reconnect_delay_max=30)
        assert client._userdata == {
            "reconnect_delay_min": 2,
            "reconnect_delay_max": 30,
        }
