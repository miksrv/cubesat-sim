"""HOSTD: the hands, with no opinions.

Every test here runs unprivileged and starts no process — the executor is the
recording one, so what is asserted is the exact sequence of commands HOSTD would
have run, which is the only thing this service is.
"""

from __future__ import annotations

import pytest

from cubesat.common import config, profiles
from cubesat.common.states import Profile
from cubesat.common.topics import TOPICS
from cubesat.hostd.allowlist import DASHBOARD_UNIT, MDNS_UNIT, Refused, unit_for
from cubesat.hostd.executor import ExecutorError
from cubesat.hostd.service import HostdService
from tests.unit.hostd.test_executor import ScriptedExecutor

MISSION_UNITS = tuple(unit_for(service) for service in ("adcs", "comms", "dhs", "payload"))
EXTERNAL_UNITS = ("starmap.service", "telegram-bot.service")

#: Fixed wall clock, so a ten-hour TTL is an arithmetic assertion.
NOW = 1_700_000_000.0


def profiles_yaml(path, external_units):
    """A profile file identical to the real one but for its unit registry."""
    text = config.PROFILES_FILE.read_text()
    head, _, _ = text.partition("external_units:")
    registry = "\n".join(f"  - unit: {unit}" for unit in external_units)
    _, _, tail = text.partition("\nprofiles:")
    path.write_text(f"{head}external_units:\n{registry}\n\nprofiles:{tail}")
    return profiles.load(path)


@pytest.fixture(autouse=True)
def last_profile(tmp_path, monkeypatch):
    """Keep the informational file out of the shared test data directory.

    OBC's own tests assert that nothing but HOSTD ever creates it, and a stray
    file from here would fail them from the outside.
    """
    path = tmp_path / "last-profile"
    monkeypatch.setattr(config, "LAST_PROFILE_FILE", path)
    return path


@pytest.fixture
def build(service_factory, tmp_path):
    """Build HOSTD over a recording executor, optionally with units already up.

    The default profile document pins the unit registry to ``EXTERNAL_UNITS``
    rather than loading the shipped one: the registry in config/profiles.yaml
    mirrors whatever host the satellite is deployed on, so its unit names are
    deployment data — and a test that reads deployment data breaks the moment
    the deployment legitimately changes (which it did, 2026-08-28).
    """

    def make(states=None, profile_config=None):
        executor = ScriptedExecutor(states=states)
        service, client = service_factory(
            HostdService,
            profiles=profile_config
            or profiles_yaml(tmp_path / "profiles.yaml", EXTERNAL_UNITS),
            executor=executor,
            socket_path=None,
            clock=lambda: NOW,
        )
        return service, client, executor

    return make


@pytest.fixture
def hostd(build):
    """A freshly booted host: nothing of ours is running yet."""
    return build()


def units_touched(executor, verb):
    """Which profile units were started or stopped.

    The mDNS daemon is filtered out: it is the network module's own lever, and
    it is asserted there.
    """
    return [
        call[2]
        for call in executor.calls
        if call[:2] == ("systemctl", verb) and call[2] != MDNS_UNIT
    ]


def test_hostd_never_learns_what_the_mission_is_doing(hostd):
    # The moment it subscribes to obc/status, the privilege split has a hole in
    # it: HOSTD would have a reason to decide something.
    service, client, _ = hostd
    client.connect_ok()
    assert client.subscribed == [TOPICS["host_command"]]
    assert service.mission_state is None


def test_hostd_has_no_periodic_work_of_its_own(hostd):
    service, client, _ = hostd
    service.tick()
    assert client.published == []


# ── startup ─────────────────────────────────────────────────────────────────


def test_the_boot_applies_the_default_profile_and_never_the_previous_one(hostd, caplog):
    # The decisive case from docs/concept.md: a satellite that hit CRITICAL on a
    # trip in FLIGHT and is plugged in at a desk hours later must not come back
    # up with Wi-Fi off and no SSH.
    service, client, executor = hostd
    config.LAST_PROFILE_FILE.write_text("FLIGHT\n")

    with caplog.at_level("INFO"):
        service.on_start()

    assert "previous run left profile FLIGHT" in caplog.text
    assert "not restoring it" in caplog.text
    assert config.LAST_PROFILE_FILE.read_text().strip() == Profile.HOSTED.value
    status = client.last(TOPICS["host_status"])
    assert status["profile"] == status["profile_requested"] == Profile.HOSTED.value
    # HOSTED runs the external services and, of the mission services, COMMS
    # alone — the radio listens in every operational profile, so a satellite
    # that rebooted away from home is still reachable over LoRa.
    assert sorted(units_touched(executor, "start")) == sorted(
        [unit_for("comms"), *EXTERNAL_UNITS]
    )
    # Nothing was running, so nothing was stopped: the state of every unit is
    # read before a stop is issued, not after.
    assert units_touched(executor, "stop") == []


def test_an_unrecorded_previous_profile_is_not_an_error(hostd, caplog):
    service, _, _ = hostd
    config.LAST_PROFILE_FILE.unlink(missing_ok=True)
    with caplog.at_level("INFO"):
        service.on_start()
    assert "(unrecorded)" in caplog.text


def test_the_status_is_retained_at_qos_1_so_a_late_subscriber_learns_the_truth(hostd):
    service, client, _ = hostd
    service.on_start()
    published = client.published[-1]
    assert published.topic == TOPICS["host_status"]
    assert published.retain is True
    assert published.qos == 1


# ── apply_profile ───────────────────────────────────────────────────────────


def test_applying_expo_stops_the_external_units_brings_up_the_ap_and_starts_the_mission(build):
    # The transition an operator actually makes: HOSTED is up with the unrelated
    # services running, and the satellite is taken to a science fair.
    service, client, executor = build(dict.fromkeys(EXTERNAL_UNITS, "active"))
    service.handle({"action": "apply_profile", "profile": "EXPO", "request_id": "req_010"})

    assert sorted(units_touched(executor, "start")) == [DASHBOARD_UNIT, *MISSION_UNITS]
    assert sorted(units_touched(executor, "stop")) == list(EXTERNAL_UNITS)
    hotspot = [
        call for call in executor.calls if call[:4] == ("nmcli", "device", "wifi", "hotspot")
    ]
    assert hotspot and hotspot[0][-1] == "cubesat"

    status = client.last(TOPICS["host_status"])
    assert status["profile"] == "EXPO"
    assert status["network"] == {"mode": "ap", "ssid": "cubesat", "clients": 0}
    assert status["governor"] == "ondemand"
    assert status["errors"] == []
    assert status["units"][unit_for("adcs")] == "active"
    assert status["units"]["telegram-bot.service"] == "inactive"


def test_the_order_is_stop_then_network_then_start(build):
    # The units being stopped hold the I2C bus, the camera and the radio the
    # incoming ones are about to want; and the services come up onto a network
    # that is already in the mode they were promised.
    service, _, executor = build(dict.fromkeys(EXTERNAL_UNITS, "active"))
    service.handle({"action": "apply_profile", "profile": "EXPO"})

    verbs = [call for call in executor.calls if call[:1] == ("systemctl",) or call[0] == "nmcli"]
    stopped = max(i for i, call in enumerate(verbs) if call[:2] == ("systemctl", "stop"))
    hotspot = next(i for i, call in enumerate(verbs) if "hotspot" in call)
    started = min(i for i, call in enumerate(verbs) if call[:2] == ("systemctl", "start"))
    assert stopped < hotspot < started


def test_applying_the_active_profile_again_restarts_nothing(hostd):
    # A profile switch during a demonstration must not bounce the service
    # driving the screen.
    service, client, executor = hostd
    service.handle({"action": "apply_profile", "profile": "EXPO"})
    executor.calls.clear()

    service.handle({"action": "apply_profile", "profile": "EXPO"})

    assert units_touched(executor, "start") == []
    assert units_touched(executor, "stop") == []
    # And the access point was not dropped and rebuilt underneath its clients.
    assert not any("hotspot" in call for call in executor.calls)
    assert client.last(TOPICS["host_status"])["profile"] == "EXPO"


def test_switching_profiles_stops_what_the_new_one_does_not_ask_for(hostd):
    service, _, executor = hostd
    service.handle({"action": "apply_profile", "profile": "EXPO"})
    executor.calls.clear()

    # FLIGHT runs the same four services but no dashboard: exactly one unit
    # stops, and nothing already running is restarted.
    service.handle({"action": "apply_profile", "profile": "FLIGHT"})

    assert units_touched(executor, "stop") == [DASHBOARD_UNIT]
    assert units_touched(executor, "start") == []


def test_an_unknown_profile_is_refused_and_nothing_is_touched(hostd):
    # A profile that does not exist is not a reason to leave the platform
    # half-way between two that do.
    service, client, executor = hostd
    service.handle({"action": "apply_profile", "profile": "EXPO"})
    executor.calls.clear()

    result = service.handle({"action": "apply_profile", "profile": "PARTY"})

    assert executor.calls == []
    assert result["ok"] is False
    status = client.last(TOPICS["host_status"])
    assert status["profile"] == "EXPO"
    assert status["profile_requested"] == "PARTY"
    assert "unknown profile" in status["errors"][0]


def test_apply_profile_without_a_profile_at_all_is_refused(hostd):
    service, client, executor = hostd
    assert service.handle({"action": "apply_profile"})["ok"] is False
    assert executor.calls == []
    assert client.last(TOPICS["host_status"])["errors"]


def test_a_profile_can_also_be_named_the_way_a_ground_command_names_it(hostd):
    # Tolerance about shape, not a second vocabulary: the actions, the checks
    # and the effects are the same ones.
    service, client, _ = hostd
    service.handle({"action": "apply_profile", "params": {"profile": "DEMO"}})
    assert client.last(TOPICS["host_status"])["profile"] == "DEMO"


# ── partial application ─────────────────────────────────────────────────────


def test_a_unit_that_refuses_to_start_does_not_let_the_profile_claim_success(hostd):
    # OBC refuses to deploy into a profile it did not get; a `profile` field
    # that claimed success would take that guard away.
    service, client, executor = hostd
    service.handle({"action": "apply_profile", "profile": "HOSTED"})
    executor.fails = (("systemctl", "start", unit_for("dhs")),)

    result = service.handle({"action": "apply_profile", "profile": "EXPO"})

    assert result["ok"] is False
    status = client.last(TOPICS["host_status"])
    assert status["profile"] == "HOSTED"
    assert status["profile_requested"] == "EXPO"
    assert any(unit_for("dhs") in error for error in status["errors"])
    # The rest of the profile was still applied: a platform that got its AP but
    # not its recorder is a state worth reaching and seeing.
    assert unit_for("adcs") in units_touched(executor, "start")


def test_the_mdns_daemon_is_not_swept_up_with_the_profiles_units(build):
    # It is permitted, so that the allowlist is the whole inventory of what root
    # can systemctl — but it is HOSTD's own lever, so "stop everything this
    # profile did not ask for" must leave it to the network module.
    service, client, executor = build({MDNS_UNIT: "active"})

    service.handle({"action": "apply_profile", "profile": "EXPO"})  # advertise_mdns: true

    assert ("systemctl", "stop", MDNS_UNIT) not in executor.calls
    assert ("systemctl", "start", MDNS_UNIT) in executor.calls
    # And it is not reported as one of the profile's units either.
    assert MDNS_UNIT not in client.last(TOPICS["host_status"])["units"]


def test_a_boot_profile_that_only_half_applied_still_says_what_it_wanted(hostd):
    # Nothing has ever fully applied, so `profile` is null — and this is then
    # the only message that says why the satellite is sitting in STANDBY. The
    # requested name and the errors have to be in it, or it is not worth logging.
    service, client, executor = hostd
    executor.fails = (("nmcli", "radio"),)

    service.on_start()

    status = client.last(TOPICS["host_status"])
    assert status["profile"] is None
    assert status["profile_requested"] == Profile.HOSTED.value
    assert status["errors"] and all(isinstance(error, str) for error in status["errors"])
    assert any("radio" in error for error in status["errors"])
    # And the rest of the report is still a report: units were read, the mode is
    # not claimed to be the one that failed.
    assert status["units"]
    assert status["network"]["mode"] == "unknown"


def test_an_access_point_that_never_came_up_is_a_partial_application(hostd):
    # The real failure this distinction exists for: services running, no network.
    service, client, executor = hostd
    executor.fails = (("nmcli", "device", "wifi", "hotspot"),)

    service.handle({"action": "apply_profile", "profile": "EXPO"})

    status = client.last(TOPICS["host_status"])
    assert status["profile"] is None
    assert status["profile_requested"] == "EXPO"
    assert status["network"]["mode"] == "unknown"
    assert any("hotspot" in error for error in status["errors"])
    assert unit_for("adcs") in units_touched(executor, "start")


def test_a_network_that_failed_is_retried_on_the_next_application(hostd):
    # Otherwise the "already in this mode" shortcut would make a failed AP
    # permanent until a different profile happened to be applied.
    service, _, executor = hostd
    executor.fails = (("nmcli", "device", "wifi", "hotspot"),)
    service.handle({"action": "apply_profile", "profile": "EXPO"})
    executor.fails = ()
    executor.calls.clear()

    service.handle({"action": "apply_profile", "profile": "EXPO"})

    assert any("hotspot" in call for call in executor.calls)


def test_a_unit_outside_the_allowlist_is_reported_and_never_spawned(hostd, monkeypatch):
    # Defence in depth: the units come from the allowlist itself, so this can
    # only happen if a future change lets one through. It must be reported, and
    # the rest of the profile must still be applied.
    service, client, _ = hostd

    def refuse(unit):
        raise Refused(f"{unit} is not on the allowlist")

    monkeypatch.setattr(service._host, "unit_state", refuse)
    service.handle({"action": "apply_profile", "profile": "EXPO"})

    status = client.last(TOPICS["host_status"])
    assert status["profile"] is None
    assert any("not on the allowlist" in error for error in status["errors"])


def test_a_governor_that_cannot_be_set_is_a_partial_application(hostd):
    service, client, executor = hostd
    executor.governor_error = "sysfs is read-only"
    service.handle({"action": "apply_profile", "profile": "EXPO"})
    status = client.last(TOPICS["host_status"])
    assert status["profile"] is None
    assert status["governor"] is None
    assert any("governor" in error for error in status["errors"])


def test_failing_to_note_the_profile_is_reported_but_does_not_fail_it(hostd, monkeypatch, tmp_path):
    # last-profile is information, never instruction: losing it does not make
    # the platform any less the profile it now is.
    service, client, _ = hostd
    monkeypatch.setattr(config, "LAST_PROFILE_FILE", tmp_path)  # a directory

    service.handle({"action": "apply_profile", "profile": "DEMO"})

    status = client.last(TOPICS["host_status"])
    assert status["profile"] == "DEMO"
    assert any("could not record the profile" in error for error in status["errors"])


SUBSET_YAML = """
default_profile: HOSTED
external_units:
  - unit: telegram-bot.service
  - unit: starmap.service
  - unit: syncthing.service
profiles:
  HOSTED:
    mission: standby
    network: { mode: client }
    external_units: [telegram-bot.service]
    services: [comms]
    persistence: none
  DEMO:
    mission: active
    network: { mode: client }
    external_units: [telegram-bot.service, starmap.service]
    services: [comms]
    persistence: none
"""


def test_a_profile_starts_the_external_units_it_names_and_stops_the_rest(build, tmp_path):
    # The point of naming units rather than saying "start": telegram-bot belongs
    # on the desk, starmap only during a demonstration, syncthing in neither.
    # What a profile does not name is stopped, never left as it was found —
    # otherwise the platform would depend on which profile preceded it.
    path = tmp_path / "profiles.yaml"
    path.write_text(SUBSET_YAML)
    running = dict.fromkeys(
        ["telegram-bot.service", "starmap.service", "syncthing.service"], "active"
    )
    service, _, executor = build(running, profile_config=profiles.load(path))

    service.handle({"action": "apply_profile", "profile": "DEMO"})

    assert units_touched(executor, "stop") == ["syncthing.service"]
    # The two external units it wants were already up, so only the mission
    # service was started: re-applying must not restart a healthy service.
    assert units_touched(executor, "start") == [unit_for("comms")]

    executor.calls.clear()
    service.handle({"action": "apply_profile", "profile": "HOSTED"})

    # HOSTED wants one of the two DEMO left running, and the same mission
    # service — so exactly one unit moves, in one direction.
    assert units_touched(executor, "stop") == ["starmap.service"]
    assert units_touched(executor, "start") == []


# ── the allowlist, from the outside ─────────────────────────────────────────


def test_a_profile_registry_naming_the_branch_hostd_sits_on_can_never_stop_it(build, tmp_path):
    # Every control path in this design runs through the broker, OBC is the only
    # thing that decides anything, and NetworkManager is the way back to a
    # reachable profile. Without the deny all four would be stopped here — and
    # after the fourth there would be no route back to a profile with SSH in it.
    denied = ["mosquitto.service", "cubesat@obc.service", "cubesat-hostd.service",
              "NetworkManager.service"]
    dangerous = profiles_yaml(tmp_path / "profiles.yaml", [*denied, "starmap.service"])
    service, _, executor = build(
        dict.fromkeys([*denied, "starmap.service"], "active"), profile_config=dangerous
    )

    service.handle({"action": "apply_profile", "profile": "EXPO"})  # external_units: stop

    touched = {call[2] for call in executor.calls if call[:1] == ("systemctl",) and len(call) > 2}
    assert not touched & set(denied)
    assert "starmap.service" in touched


# ── the TTL ─────────────────────────────────────────────────────────────────


def test_the_expiry_of_a_flight_profile_is_published_as_an_absolute_moment(hostd):
    # HOSTD holds the applied profile, so an OBC restart recovers the deadline
    # from this retained message instead of forgetting it.
    service, client, _ = hostd
    service.handle({"action": "apply_profile", "profile": "FLIGHT"})
    assert client.last(TOPICS["host_status"])["ttl_expires_at"] == NOW + 600 * 60


def test_a_ttl_on_the_command_wins_over_the_profiles_own(hostd):
    service, client, _ = hostd
    service.handle({"action": "apply_profile", "profile": "FLIGHT", "ttl_minutes": 30})
    assert client.last(TOPICS["host_status"])["ttl_expires_at"] == NOW + 30 * 60


@pytest.mark.parametrize("ttl", [None, 0, -5, True, "soon"])
def test_a_meaningless_ttl_falls_back_to_the_profiles_own(hostd, ttl):
    service, client, _ = hostd
    service.handle({"action": "apply_profile", "profile": "DIAG", "ttl_minutes": ttl})
    assert client.last(TOPICS["host_status"])["ttl_expires_at"] == NOW + 120 * 60


def test_a_profile_with_no_expiry_publishes_null(hostd):
    service, client, _ = hostd
    service.handle({"action": "apply_profile", "profile": "EXPO"})
    assert client.last(TOPICS["host_status"])["ttl_expires_at"] is None


def test_hostd_does_not_act_on_an_expiry_it_has_published(service_factory):
    # What an expiry *means* is a decision, and decisions live in OBC.
    clock = {"now": NOW}
    service, client = service_factory(
        HostdService,
        profiles=profiles.load(),
        executor=ScriptedExecutor(),
        socket_path=None,
        clock=lambda: clock["now"],
    )
    service.handle({"action": "apply_profile", "profile": "FLIGHT"})
    published = len(client.published)

    clock["now"] += 600 * 60 + 1
    service.tick()

    assert len(client.published) == published
    assert client.last(TOPICS["host_status"])["profile"] == "FLIGHT"


# ── restart_service ─────────────────────────────────────────────────────────


def test_restarting_one_service_touches_only_that_unit(hostd):
    # Why the action exists at all: re-applying a profile to restart one service
    # would stop and start everything else the profile names, which in EXPO means
    # taking the dashboard away from a room full of people.
    service, client, executor = hostd
    service.handle({"action": "apply_profile", "profile": "DEMO"})
    executor.calls.clear()

    result = service.handle({"action": "restart_service", "params": {"service": "adcs"}})

    assert result["ok"] is True
    assert ("systemctl", "restart", "cubesat@adcs.service") in executor.calls
    restarted = [call for call in executor.calls if "restart" in call]
    assert len(restarted) == 1
    # And it is not a change of profile.
    assert client.last(TOPICS["host_status"])["profile"] == "DEMO"


def test_a_service_this_satellite_does_not_have_is_refused(hostd):
    # Checked against the profile model rather than a string pattern, so this
    # command can reach exactly what a profile can and nothing more.
    service, client, executor = hostd
    result = service.handle({"action": "restart_service", "params": {"service": "telegram"}})

    assert result["ok"] is False
    assert executor.calls == []
    assert "unknown service 'telegram'" in client.last(TOPICS["host_status"])["errors"][0]


def test_a_unit_name_instead_of_a_service_name_is_refused(hostd):
    # The vocabulary on the bus names subsystems; a ground client that could name
    # a unit would be reaching past it into systemd.
    service, client, executor = hostd
    result = service.handle(
        {"action": "restart_service", "params": {"service": "cubesat@adcs.service"}}
    )
    assert result["ok"] is False
    assert executor.calls == []


def test_restart_service_with_no_service_named_is_reported(hostd):
    service, client, executor = hostd
    assert service.handle({"action": "restart_service"})["ok"] is False
    assert executor.calls == []
    assert "without a service name" in client.last(TOPICS["host_status"])["errors"][0]


def test_a_restart_that_systemd_refuses_is_reported_not_raised(build, tmp_path):
    # A unit that will not come back is a fact for host_status, not a traceback
    # in the only privileged process on the satellite.
    service, client, _executor = build()
    service._executor.fails = (("systemctl", "restart"),)

    result = service.handle({"action": "restart_service", "params": {"service": "dhs"}})

    assert result["ok"] is False
    assert "restart cubesat@dhs.service" in client.last(TOPICS["host_status"])["errors"][0]


# ── set_governor ────────────────────────────────────────────────────────────


def test_set_governor_changes_the_governor_and_nothing_else(hostd):
    # LOW_POWER lowers the governor inside the profile it is already in.
    service, client, executor = hostd
    service.handle({"action": "apply_profile", "profile": "DEMO"})
    executor.calls.clear()

    result = service.handle({"action": "set_governor", "params": {"governor": "powersave"}})

    assert result["ok"] is True
    assert executor.governors[-1] == "powersave"
    assert executor.calls == []
    status = client.last(TOPICS["host_status"])
    assert status["governor"] == "powersave"
    assert status["profile"] == "DEMO"


def test_an_unknown_governor_is_refused_without_touching_the_host(hostd):
    service, client, executor = hostd
    service.handle({"action": "set_governor", "params": {"governor": "ludicrous"}})
    assert executor.governors == []
    assert "unknown CPU governor" in client.last(TOPICS["host_status"])["errors"][0]


def test_set_governor_with_no_governor_named_is_reported(hostd):
    service, client, executor = hostd
    assert service.handle({"action": "set_governor"})["ok"] is False
    assert executor.governors == []
    assert client.last(TOPICS["host_status"])["errors"]


# ── poweroff ────────────────────────────────────────────────────────────────


def test_poweroff_is_the_one_action_that_changes_host_power(hostd, caplog):
    service, client, executor = hostd
    with caplog.at_level("WARNING"):
        result = service.handle(
            {"action": "poweroff", "params": {"reason": "battery_critical"}}
        )
    assert result["ok"] is True
    assert executor.calls[-1] == ("systemctl", "poweroff")
    assert "battery_critical" in caplog.text
    # The status still goes out: systemctl poweroff returns immediately, and a
    # retained message claiming a healthy platform that is off would be a lie.
    assert client.last(TOPICS["host_status"])["errors"] == []


def test_a_poweroff_with_no_reason_is_still_executed(hostd, caplog):
    service, _, executor = hostd
    with caplog.at_level("WARNING"):
        service.handle({"action": "poweroff"})
    assert executor.calls[-1] == ("systemctl", "poweroff")
    assert "unspecified" in caplog.text


def test_a_poweroff_that_fails_is_reported_so_obc_knows_the_host_is_still_up(hostd):
    service, client, executor = hostd
    executor.fails = (("systemctl", "poweroff"),)
    assert service.handle({"action": "poweroff"})["ok"] is False
    assert any("poweroff" in error for error in client.last(TOPICS["host_status"])["errors"])


# ── the vocabulary is fixed ─────────────────────────────────────────────────


def test_an_action_hostd_does_not_know_is_reported_and_not_guessed_at(hostd, caplog):
    service, client, executor = hostd
    with caplog.at_level("ERROR"):
        result = service.handle({"action": "reflash_the_radio"})
    assert result["ok"] is False
    assert executor.calls == []
    assert client.last(TOPICS["host_status"])["errors"] == [
        "unknown action 'reflash_the_radio'"
    ]


def test_an_action_arriving_over_mqtt_takes_the_same_path(hostd):
    service, client, _ = hostd
    client.connect_ok()
    client.deliver(TOPICS["host_command"], {"action": "apply_profile", "profile": "MAINTENANCE"})
    assert client.last(TOPICS["host_status"])["profile"] == "MAINTENANCE"


def test_hosted_stops_the_mission_services_a_previous_profile_left_running(build):
    # Coming back from EXPO to the desk: the dashboard and the sensors go, the
    # unrelated ones come back — and COMMS stays up across the switch, because
    # HOSTED keeps listening on LoRa.
    active = dict.fromkeys([DASHBOARD_UNIT, *MISSION_UNITS], "active")
    service, _, executor = build(active)

    service.handle({"action": "apply_profile", "profile": "HOSTED"})

    stopped = sorted(units_touched(executor, "stop"))
    assert unit_for("comms") not in stopped
    assert stopped == sorted(
        [DASHBOARD_UNIT, *(u for u in MISSION_UNITS if u != unit_for("comms"))]
    )
    assert sorted(units_touched(executor, "start")) == list(EXTERNAL_UNITS)


def test_the_real_executor_is_asked_for_when_none_is_given(service_factory, monkeypatch):
    # The wiring, not the executor: unprivileged and without the mock flag, the
    # service refuses to exist rather than reporting profiles it never applied.
    monkeypatch.delenv("CUBESAT_MOCK_HOST", raising=False)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(Exception, match="needs root"):
        service_factory(HostdService, profiles=profiles.load(), socket_path=None)


def test_a_host_action_that_explodes_unexpectedly_is_reported_not_raised(hostd, monkeypatch):
    service, client, _ = hostd

    def explode(_unit):
        raise ExecutorError("dbus is not answering")

    monkeypatch.setattr(service._host, "unit_state", explode)
    service.handle({"action": "apply_profile", "profile": "EXPO"})
    assert client.last(TOPICS["host_status"])["profile"] is None


def test_a_reconnect_before_the_first_profile_publishes_nothing(hostd):
    # The connect callback arrives ahead of on_start. An empty status is not
    # neutral: OBC reads a null profile as "HOSTD has not applied any profile"
    # and says so at ERROR, so publishing one on every boot would announce a
    # fault that resolves itself a moment later — which is how a real one stops
    # being noticed.
    service, client = hostd[0], hostd[1]
    service.on_connected()
    assert client.payloads(TOPICS["host_status"]) == []


def test_a_reconnect_republishes_the_retained_host_status(hostd):
    # OBC learns the active profile only from this retained message. A broker
    # restart discards it, and HOSTD publishes only when it has done something —
    # so without this, OBC would sit in STANDBY under a fully applied profile
    # with no way to find out otherwise.
    service, client = hostd[0], hostd[1]
    service.on_start()
    before = len(client.payloads(TOPICS["host_status"]))
    service.on_connected()
    assert len(client.payloads(TOPICS["host_status"])) == before + 1
