import json

from src.obc.handlers import OBCMessageHandlers


class FakeStateMachine:
    def __init__(self, state="NOMINAL"):
        self.state = state
        self.calls = []

    def enter_safe_mode(self):
        self.calls.append("enter_safe_mode")

    def enter_low_power(self):
        self.calls.append("enter_low_power")

    def recover(self):
        self.calls.append("recover")

    def start_science(self):
        self.calls.append("start_science")

    def end_science(self):
        self.calls.append("end_science")


class FakeOBC:
    def __init__(self, state="NOMINAL"):
        self.state_machine = FakeStateMachine(state=state)


def make_handlers(state="NOMINAL"):
    obc = FakeOBC(state=state)
    return OBCMessageHandlers(obc), obc


class TestHandleEpsStatus:
    def test_low_battery_triggers_safe_mode(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_eps_status(json.dumps({"battery": 10, "external_power": False}))
        assert obc.state_machine.calls == ["enter_safe_mode"]

    def test_low_battery_triggers_safe_mode_even_with_external_power(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_eps_status(json.dumps({"battery": 5, "external_power": True}))
        assert obc.state_machine.calls == ["enter_safe_mode"]

    def test_medium_battery_triggers_low_power(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_eps_status(json.dumps({"battery": 35, "external_power": False}))
        assert obc.state_machine.calls == ["enter_low_power"]

    def test_medium_battery_does_not_repeat_low_power_transition(self):
        handlers, obc = make_handlers(state="LOW_POWER")
        handlers.handle_eps_status(json.dumps({"battery": 35, "external_power": False}))
        assert obc.state_machine.calls == []

    def test_medium_battery_does_not_trigger_when_already_safe(self):
        handlers, obc = make_handlers(state="SAFE")
        handlers.handle_eps_status(json.dumps({"battery": 35, "external_power": False}))
        assert obc.state_machine.calls == []

    def test_external_power_recovers_from_low_power(self):
        handlers, obc = make_handlers(state="LOW_POWER")
        handlers.handle_eps_status(json.dumps({"battery": 90, "external_power": True}))
        assert obc.state_machine.calls == ["recover"]

    def test_external_power_recovers_from_safe(self):
        handlers, obc = make_handlers(state="SAFE")
        handlers.handle_eps_status(json.dumps({"battery": 90, "external_power": True}))
        assert obc.state_machine.calls == ["recover"]

    def test_high_battery_nominal_state_no_transition(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_eps_status(json.dumps({"battery": 90, "external_power": True}))
        assert obc.state_machine.calls == []

    def test_low_power_state_without_external_power_stays_put(self):
        handlers, obc = make_handlers(state="LOW_POWER")
        handlers.handle_eps_status(json.dumps({"battery": 90, "external_power": False}))
        assert obc.state_machine.calls == []

    def test_missing_battery_field_defaults_to_full(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_eps_status(json.dumps({}))
        assert obc.state_machine.calls == []

    def test_invalid_json_is_swallowed(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_eps_status("not json")  # must not raise
        assert obc.state_machine.calls == []


class TestHandleCommand:
    def test_science_start_from_nominal(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_command(json.dumps({"command": "science_start"}))
        assert obc.state_machine.calls == ["start_science"]

    def test_science_start_ignored_outside_nominal(self):
        handlers, obc = make_handlers(state="SCIENCE")
        handlers.handle_command(json.dumps({"command": "science_start"}))
        assert obc.state_machine.calls == []

    def test_science_stop_from_science(self):
        handlers, obc = make_handlers(state="SCIENCE")
        handlers.handle_command(json.dumps({"command": "science_stop"}))
        assert obc.state_machine.calls == ["end_science"]

    def test_science_stop_ignored_outside_science(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_command(json.dumps({"command": "science_stop"}))
        assert obc.state_machine.calls == []

    def test_safe_mode_command_always_applies(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_command(json.dumps({"command": "safe_mode"}))
        assert obc.state_machine.calls == ["enter_safe_mode"]

    def test_recover_command_always_applies(self):
        handlers, obc = make_handlers(state="SAFE")
        handlers.handle_command(json.dumps({"command": "recover"}))
        assert obc.state_machine.calls == ["recover"]

    def test_unknown_command_is_noop(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_command(json.dumps({"command": "take_photo"}))
        assert obc.state_machine.calls == []

    def test_invalid_json_is_swallowed(self):
        handlers, obc = make_handlers(state="NOMINAL")
        handlers.handle_command("{not valid json")  # must not raise
        assert obc.state_machine.calls == []
