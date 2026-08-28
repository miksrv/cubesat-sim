import pytest

from cubesat.obc import commands
from cubesat.obc.commands import Command, parse, profile_request


@pytest.mark.parametrize(
    "name", ["set_profile", "science_start", "science_stop", "safe_mode", "recover"]
)
def test_the_mission_decisions_are_ours(name):
    assert parse({"command": name}).name == name


@pytest.mark.parametrize(
    "name", ["take_photo", "start_timelapse", "stop_timelapse", "get_telemetry",
             "set_comms_config"]
)
def test_another_service_s_command_is_ignored_without_complaint(name):
    # All commands share one topic, so OBC sees PAYLOAD's and COMMS' too. They
    # are not errors; warning about each would make every photo look like a fault.
    assert parse({"command": name}) is None


def test_the_request_id_is_carried_through_for_the_reply():
    command = parse({"command": "set_profile", "request_id": "req_010"})
    assert command.request_id == "req_010"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"command": None},
        {"command": 42},
        {"command": ["safe_mode"]},
        {"command": "SAFE_MODE"},
        {"command": ""},
    ],
)
def test_a_malformed_command_field_is_dropped_not_raised(payload):
    # The same command arrives over MQTT and over LoRa. Anything that can be
    # garbled by a radio ends up here.
    assert parse(payload) is None


@pytest.mark.parametrize("params", [None, "EXPO", ["EXPO"], 7])
def test_params_that_are_not_an_object_become_no_params(params):
    assert parse({"command": "safe_mode", "params": params}).params == {}


def test_a_non_string_request_id_is_treated_as_absent():
    assert parse({"command": "recover", "request_id": 12}).request_id is None


# ── set_profile params ───────────────────────────────────────────────────────


def test_a_profile_request_carries_its_ttl_and_label():
    command = Command(
        name=commands.SET_PROFILE,
        params={"profile": "FLIGHT", "ttl_minutes": 600, "mission_label": "walk to work"},
    )
    request = profile_request(command)
    assert (request.profile, request.ttl_minutes, request.mission_label) == (
        "FLIGHT", 600, "walk to work"
    )


def test_the_profile_name_is_not_validated_here():
    # Validation belongs to the profile machine, against profiles.yaml — profiles
    # are data, and this module must not grow a second copy of the list.
    assert profile_request(Command(commands.SET_PROFILE, params={"profile": "MARS"})).profile == (
        "MARS"
    )


@pytest.mark.parametrize("profile", [None, "", 5, {"name": "EXPO"}])
def test_a_request_without_a_profile_name_is_unusable(profile):
    assert profile_request(Command(commands.SET_PROFILE, params={"profile": profile})) is None


@pytest.mark.parametrize("ttl", [None, 0, -30, "600", True, [600]])
def test_a_ttl_that_is_not_a_positive_number_is_dropped_not_refused(ttl):
    # Losing an expiry is a smaller problem than refusing the profile change,
    # which is probably somebody's way back out of FLIGHT.
    request = profile_request(Command(commands.SET_PROFILE, params={"profile": "DIAG",
                                                                   "ttl_minutes": ttl}))
    assert request is not None
    assert request.ttl_minutes is None


def test_a_float_ttl_is_accepted_as_minutes():
    request = profile_request(
        Command(commands.SET_PROFILE, params={"profile": "DIAG", "ttl_minutes": 90.0})
    )
    assert request.ttl_minutes == 90


@pytest.mark.parametrize("label", [None, "", 3])
def test_an_unusable_label_becomes_no_label(label):
    request = profile_request(
        Command(commands.SET_PROFILE, params={"profile": "DEMO", "mission_label": label})
    )
    assert request.mission_label is None
