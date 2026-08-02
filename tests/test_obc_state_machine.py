import json

import pytest
from transitions.core import MachineError

from src.common import TOPICS
from src.obc.state_machine import CubeSatStateMachine


class FakeOBC:
    """Bare-minimum stand-in for the OBC that CubeSatStateMachine talks to."""

    def __init__(self, mqtt_connected=False):
        self._mqtt_connected = mqtt_connected
        self.mqtt_client = _FakeMqtt()


class _FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False, qos=0):
        self.published.append({"topic": topic, "payload": payload, "retain": retain})


def make_sm(mqtt_connected=False):
    obc = FakeOBC(mqtt_connected=mqtt_connected)
    sm = CubeSatStateMachine(obc)
    return sm, obc


class TestBootSequence:
    def test_boots_straight_through_to_nominal(self):
        """__init__ chains auto_deploy() then deployment_complete()."""
        sm, _obc = make_sm()
        assert sm.state == "NOMINAL"


class TestPublishState:
    def test_skips_publish_when_mqtt_not_connected(self):
        sm, obc = make_sm(mqtt_connected=False)
        obc.mqtt_client.published.clear()
        sm.publish_state()
        assert obc.mqtt_client.published == []

    def test_publishes_when_mqtt_connected(self):
        sm, obc = make_sm(mqtt_connected=True)
        obc.mqtt_client.published.clear()
        sm.publish_state()
        assert len(obc.mqtt_client.published) == 1
        msg = obc.mqtt_client.published[0]
        assert msg["topic"] == TOPICS["obc_status"]
        assert msg["retain"] is True
        payload = json.loads(msg["payload"])
        assert payload["status"] == "NOMINAL"
        assert "timestamp" in payload

    def test_extra_fields_are_merged_into_payload(self):
        sm, obc = make_sm(mqtt_connected=True)
        obc.mqtt_client.published.clear()
        sm.publish_state({"step": "custom"})
        payload = json.loads(obc.mqtt_client.published[0]["payload"])
        assert payload["step"] == "custom"


class TestTransitions:
    def test_start_and_end_science(self):
        sm, _obc = make_sm()
        sm.start_science()
        assert sm.state == "SCIENCE"
        sm.end_science()
        assert sm.state == "NOMINAL"

    def test_enter_low_power_from_nominal(self):
        sm, _obc = make_sm()
        sm.enter_low_power()
        assert sm.state == "LOW_POWER"

    def test_enter_low_power_from_science(self):
        sm, _obc = make_sm()
        sm.start_science()
        sm.enter_low_power()
        assert sm.state == "LOW_POWER"

    def test_enter_safe_mode_from_any_state(self):
        sm, _obc = make_sm()
        sm.start_science()
        sm.enter_safe_mode()
        assert sm.state == "SAFE"

    def test_recover_from_low_power(self):
        sm, _obc = make_sm()
        sm.enter_low_power()
        sm.recover()
        assert sm.state == "NOMINAL"

    def test_recover_from_safe(self):
        sm, _obc = make_sm()
        sm.enter_safe_mode()
        sm.recover()
        assert sm.state == "NOMINAL"

    def test_start_science_rejected_from_safe_mode(self):
        sm, _obc = make_sm()
        sm.enter_safe_mode()
        with pytest.raises(MachineError):
            sm.start_science()
        assert sm.state == "SAFE"

    def test_end_science_rejected_when_not_in_science(self):
        sm, _obc = make_sm()
        with pytest.raises(MachineError):
            sm.end_science()
        assert sm.state == "NOMINAL"
