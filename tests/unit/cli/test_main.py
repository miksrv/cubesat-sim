"""The command line itself: parsing, dispatch and the exit codes a script sees.

Three codes, and the middle one is the interesting one: ``0`` the thing was done
or the question answered, ``1`` the satellite did not answer or answered badly,
``2`` the command line was wrong. A profile that applied only in part is ``1`` —
it says what happened, and it is not a success.
"""

from __future__ import annotations

import pytest

from cubesat.cli import main as cli
from cubesat.cli.session import BrokerUnavailable, Session
from cubesat.common.profiles import ProfileError
from cubesat.common.topics import TOPICS


@pytest.fixture
def broker(monkeypatch, fake_client):
    """Make `main` talk to the fake client, with waits short enough to test."""

    def build(**_kwargs):
        return Session(client=fake_client, collect_window=0.01, apply_timeout=0.05)

    monkeypatch.setattr(cli, "Session", build)
    return fake_client


# ── the TTL a person types ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("typed", "minutes"),
    [("8h", 480), ("45m", 45), ("90", 90), ("2H", 120)],
)
def test_a_ttl_is_read_in_the_units_a_person_thinks_in(typed, minutes):
    # "the walk takes an hour, give it two" — converted here because
    # `ttl_minutes` is what the wire carries.
    assert cli._ttl_minutes(typed) == minutes


@pytest.mark.parametrize("typed", ["8x", "later", "", "-5", "0", "0h"])
def test_a_ttl_that_is_not_a_duration_is_refused_before_anything_is_published(typed):
    with pytest.raises(Exception, match="expected 8h|zero"):
        cli._ttl_minutes(typed)


# ── dispatch ────────────────────────────────────────────────────────────────


def test_no_arguments_prints_the_help_and_is_not_a_success(capsys):
    assert cli.main([]) == 2
    assert "profile" in capsys.readouterr().out


def test_a_typo_in_a_profile_needs_no_broker_at_all(capsys):
    # Being told "no such profile" must not require a reachable satellite.
    assert cli.main(["profile", "MARS"]) == 2
    assert "Unknown profile" in capsys.readouterr().err


def test_mission_list_needs_no_broker_either(monkeypatch, capsys):
    # The archive is on this disk, and a broker that has fallen over is exactly
    # when the last trip is the thing being investigated.
    def explode(**_kwargs):
        raise AssertionError("mission list must not open a session")

    monkeypatch.setattr(cli, "Session", explode)
    assert cli.main(["mission", "list"]) == 0
    assert "mission" in capsys.readouterr().out.lower()


def test_mission_without_a_subcommand_is_a_usage_error(capsys):
    assert cli.main(["mission"]) == 2
    assert "usage: cubesat mission list" in capsys.readouterr().err


def test_a_profiles_file_that_will_not_load_is_reported_not_raised(monkeypatch, capsys):
    # A typo in profiles.yaml is an operator's problem, and a traceback is a
    # worse way to be told about it than a sentence.
    def explode():
        raise ProfileError("profiles.yaml: EXPO: unknown key 'downlnk'")

    monkeypatch.setattr(cli.profiles_module, "load", explode)
    assert cli.main(["profile", "demo"]) == 1
    assert "cannot read the profile definitions" in capsys.readouterr().err


def test_an_unreachable_broker_says_what_to_check(monkeypatch, capsys):
    def refuse(**_kwargs):
        raise BrokerUnavailable("cannot reach the broker at localhost:1883")

    monkeypatch.setattr(cli, "Session", refuse)
    assert cli.main(["status"]) == 1
    assert "systemctl status mosquitto" in capsys.readouterr().err


def test_profile_with_no_name_shows_the_platform(broker, capsys):
    broker.deliver(TOPICS["host_status"], {"profile": "DEMO", "profile_requested": "DEMO"})
    broker.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    assert cli.main(["profile"]) == 0
    assert "DEMO" in capsys.readouterr().out


def test_profile_with_a_name_publishes_the_switch(broker, capsys):
    broker.deliver(
        TOPICS["host_status"], {"profile": "FLIGHT", "profile_requested": "FLIGHT", "errors": []}
    )
    assert cli.main(["profile", "flight", "--ttl", "8h", "--mission", "walk to work"]) == 0
    published = broker.payloads(TOPICS["command"])[-1]
    assert published["params"] == {
        "profile": "FLIGHT",
        "ttl_minutes": 480,
        "mission_label": "walk to work",
    }


def test_status_prints_to_stdout_and_returns_zero(broker, capsys):
    broker.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "DEMO"})
    assert cli.main(["status"]) == 0
    assert "State:" in capsys.readouterr().out


def test_a_failure_goes_to_stderr_so_a_script_can_separate_them(broker, capsys):
    assert cli.main(["status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cubesat@obc" in captured.err


# ── the beacon ──────────────────────────────────────────────────────────────


def test_beacon_on_asks_for_it_and_confirms_from_comms(broker, capsys):
    broker.deliver(TOPICS["comms_status"], {"lora_enabled": True, "lora_listening": True})
    assert cli.main(["beacon", "on"]) == 0
    published = broker.payloads(TOPICS["command"])[-1]
    assert published["command"] == "set_comms_config"
    assert published["params"] == {"lora_enabled": True}
    assert "Beacon on." in capsys.readouterr().out


def test_beacon_off_is_the_same_command_the_radio_sends(broker, capsys):
    broker.deliver(TOPICS["comms_status"], {"lora_enabled": False, "lora_listening": True})
    assert cli.main(["beacon", "off"]) == 0
    assert broker.payloads(TOPICS["command"])[-1]["params"] == {"lora_enabled": False}


def test_beacon_in_a_profile_without_a_radio_says_why_it_cannot(broker, capsys):
    # The profile is the envelope, and this tool does not pretend otherwise:
    # MAINTENANCE frees the serial port, so there is nothing to turn on.
    broker.deliver(TOPICS["comms_status"], {"lora_enabled": False, "lora_listening": False})
    assert cli.main(["beacon", "on"]) == 1
    assert "does not use the radio" in capsys.readouterr().err


def test_beacon_with_no_comms_at_all_names_the_unit(broker, capsys):
    assert cli.main(["beacon", "on"]) == 1
    assert "cubesat@comms" in capsys.readouterr().err


def test_a_beacon_change_that_is_not_confirmed_is_reported_as_unconfirmed(broker, capsys):
    # COMMS is there and the radio is permitted, but no status came back saying
    # the flag moved. Not claimed as success.
    broker.deliver(TOPICS["comms_status"], {"lora_enabled": False, "lora_listening": True})
    assert cli.main(["beacon", "on"]) == 1
    assert "did not confirm" in capsys.readouterr().err


def test_the_old_verb_still_works_from_a_shell_history(broker, capsys):
    # `lora on|off` was renamed to `beacon` on 2026-09-01, because turning it off
    # never turned the radio off. The old spelling is an alias, not in the help:
    # a command that worked last week should not answer "unknown".
    broker.deliver(TOPICS["comms_status"], {"lora_enabled": True, "lora_listening": True})
    assert cli.main(["lora", "on"]) == 0
    assert broker.payloads(TOPICS["command"])[-1]["params"] == {"lora_enabled": True}


def test_the_help_leads_with_beacon_and_shows_the_old_name_as_an_alias(capsys):
    # argparse prints it as `beacon (lora)`, which is the honest rendering: the
    # verb to learn is first, and somebody typing the old one is not left
    # wondering whether it still exists.
    cli.main([])
    printed = capsys.readouterr().out
    assert "beacon (lora)" in printed


# ── restart ─────────────────────────────────────────────────────────────────


def test_restart_goes_through_hostd_rather_than_around_it(broker, capsys):
    # `sudo systemctl restart cubesat@adcs` does the same thing without a broker.
    # Going through HOSTD is what puts the action in the same log, past the same
    # allowlist, and onto host_status like a profile switch.
    broker.deliver(
        TOPICS["host_status"],
        {"profile": "DEMO", "errors": [], "units": {"cubesat@adcs.service": "active"}},
    )
    assert cli.main(["restart", "adcs"]) == 0
    published = broker.payloads(TOPICS["command"])[-1]
    assert published["command"] == "restart_service"
    assert published["params"] == {"service": "adcs"}
    assert "adcs restarted (now active)" in capsys.readouterr().out


def test_restart_of_an_unknown_service_needs_no_broker(capsys):
    assert cli.main(["restart", "telegram"]) == 2
    assert "Unknown service 'telegram'" in capsys.readouterr().err


def test_a_restart_hostd_complained_about_is_not_a_success(broker, capsys):
    broker.deliver(
        TOPICS["host_status"],
        {"profile": "DEMO", "errors": ["restart cubesat@dhs.service: unit failed"], "units": {}},
    )
    assert cli.main(["restart", "dhs"]) == 1
    assert "unit failed" in capsys.readouterr().err


def test_a_restart_nobody_answered_names_the_logs(broker, capsys):
    assert cli.main(["restart", "comms"]) == 1
    assert "journalctl" in capsys.readouterr().err
