import dataclasses

import pytest

from cubesat.common import battery as pack
from cubesat.common import config, profiles
from cubesat.common.states import MissionState, Persistence, Profile
from cubesat.common.topics import TOPICS
from cubesat.obc import deploy, power_policy
from cubesat.obc import service as obc_service
from cubesat.obc.service import ObcService

#: What DEMO, EXPO and FLIGHT all ask for.
MISSION_SERVICES = ("adcs", "payload", "dhs", "comms")

#: Pack voltages placed relative to the thresholds rather than spelled out. The
#: LOW_POWER trigger moved from 40 % to 30 % on 2026-09-02 and from a percentage
#: to 3.64 V on 2026-09-04, and a test that was about the descent rather than
#: about the number should not have noticed either time.
STEP = 0.01
THROTTLED = power_policy.LOW_POWER_VOLTS - STEP
SAVED = power_policy.SAFE_VOLTS - STEP
DYING = power_policy.CRITICAL_VOLTS - STEP
RECOVERED = power_policy.RECOVERY_VOLTS + STEP

#: A plausible first status message per service — enough that the payload is
#: recognisably the real one rather than an empty object.
STATUS_PAYLOADS = {
    "adcs": {"roll": 1.2, "yaw": 178.9, "calib_status": {"sys": 3, "mag": 2},
             "gnss": {"fix": False, "satellites": 0}},
    "payload": {"temperature": 23.4, "humidity": 45.2, "pressure": 1013.25},
    "dhs": {"recording": True, "rows": 1},
    "comms": {"beacon_enabled": True, "lora_listening": True},
}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def advance_minutes(self, minutes):
        self.now += minutes * 60.0


class FakeBus:
    """Every documented address answers, unless a test says otherwise."""

    def __init__(self, present=(0x20, 0x22, 0x28, 0x36)):
        self.answers = set(present)

    def present(self, address):
        return address in self.answers


@pytest.fixture
def clock():
    return Clock()


def pinned_power(cfg, profile, *, governor, cadence_scale):
    """Give one profile the power knobs a test needs, in place of the shipped ones.

    Both values below used to be read straight out of ``config/profiles.yaml``,
    where ``DIAG`` carried ``performance`` and ``0.2``. On 2026-09-01 DIAG became
    a rehearsal of FLIGHT and legitimately went to ``ondemand`` and ``1.0``,
    which left two tests asserting the defaults and therefore proving nothing:
    a delivery mechanism that carries 1.0 correctly would carry a broken 1.0
    just as well. The knobs a test needs belong to the test.
    """
    spec = cfg.get(profile)
    cfg.profiles[profile] = dataclasses.replace(
        spec,
        power=dataclasses.replace(spec.power, governor=governor, cadence_scale=cadence_scale),
    )
    return cfg


@pytest.fixture
def obc(service_factory, clock):
    service, client = service_factory(
        ObcService, profiles=profiles.load(), bus=FakeBus(), clock=clock, wall_clock=clock
    )
    return service, client, clock


# ── helpers ──────────────────────────────────────────────────────────────────


def host_status(
    client, profile, requested=None, ttl_expires_at=None, boot=False, previous=None
):
    """A retained host_status as HOSTD publishes it.

    ``ttl_expires_at`` is absolute, because HOSTD holds the applied profile and
    therefore owns the deadline; OBC only decides what reaching it means.

    ``boot`` and ``previous`` are the two facts the resume rule weighs: whether
    anybody has asked for a profile since the machine came up, and what the run
    before it was doing. Both default to "an ordinary running satellite", so
    every test that is not about W11 reads as it always did.
    """
    client.deliver(
        TOPICS["host_status"],
        {
            "profile": profile,
            "profile_requested": requested or profile,
            "ttl_expires_at": ttl_expires_at,
            "errors": [],
            "boot": boot,
            "previous": previous,
        },
    )


def beat(client, *services, alive=True):
    """A heartbeat: liveness only, never evidence that any hardware answered."""
    for name in services:
        client.deliver(TOPICS["heartbeat"], {"service": name, "alive": alive})


def report_in(client, *services):
    """A subsystem's own status message — what DEPLOY actually waits for."""
    for name in services:
        client.deliver(TOPICS[deploy.REPORT_TOPICS[name]], dict(STATUS_PAYLOADS[name]))


def battery(client, volts, external_power=False, voltage_rate=None):
    """One eps_status at a given pack voltage, shaped the way EPS shapes it.

    The level is published as both the raw sample and the median, because that is
    what EPS does and because the policy reads the median: a test that set only
    one of them would be testing the fallback path rather than the normal one.
    The percentage rides along derived from the same voltage, so that a test can
    see what the log line would have said without any of it deciding anything.
    """
    if voltage_rate is None:
        # Mains held the terminal voltage flat on the hardware; the pack falls.
        voltage_rate = 0.0 if external_power else -100.0
    client.deliver(
        TOPICS["eps_status"],
        {
            "voltage": volts,
            "voltage_median": volts,
            "battery_percent": pack.percent_from_voltage(volts),
            "external_power": external_power,
            "voltage_rate": voltage_rate,
        },
    )


def command(client, name, **fields):
    client.deliver(TOPICS["command"], {"command": name, **fields})


def bring_up(service, client, profile="DEMO", ttl_expires_at=None):
    """Take a fresh OBC all the way to NOMINAL under an active profile."""
    service.on_start()
    host_status(client, profile, ttl_expires_at=ttl_expires_at)
    report_in(client, *MISSION_SERVICES)
    assert service.mission.state is MissionState.NOMINAL
    return service, client


def status(client):
    return client.last(TOPICS["obc_status"])


def states(client):
    return [p["status"] for p in client.payloads(TOPICS["obc_status"])]


def host_commands(client):
    return client.payloads(TOPICS["host_command"])


def host_actions(client, action):
    """Every host_command of one action, in order."""
    return [payload for payload in host_commands(client) if payload.get("action") == action]


# ── wiring ───────────────────────────────────────────────────────────────────


def test_it_subscribes_to_everything_it_decides_on(obc):
    service, client, _ = obc
    client.connect_ok()
    for key in (
        "command", "eps_status", "host_status", "heartbeat",
        "adcs_status", "payload_status", "dhs_status", "comms_status",
    ):
        assert TOPICS[key] in client.subscribed


def test_it_does_not_subscribe_to_its_own_status(obc):
    # OBC publishes the mission state. Absorbing it back would make the machine
    # a reader of its own output.
    service, client, _ = obc
    client.connect_ok()
    assert TOPICS["obc_status"] not in client.subscribed
    client.deliver(TOPICS["obc_status"], {"status": "SAFE", "profile": "EXPO"})
    assert service.mission_state is None


def test_the_profile_definitions_are_loaded_when_none_are_given(service_factory):
    service, _ = service_factory(ObcService)
    assert service.profile_machine.default is Profile.HOSTED


# ── boot ─────────────────────────────────────────────────────────────────────


def test_boot_settles_in_standby(obc):
    service, client, _ = obc
    service.on_start()
    assert service.mission.state is MissionState.STANDBY
    assert status(client)["status"] == "STANDBY"


def test_the_status_is_retained_at_qos_1(obc):
    # DHS opens a mission from this one message, so a service that starts late
    # must get it immediately and it must not arrive best-effort.
    service, client, _ = obc
    service.on_start()
    published = [p for p in client.published if p.topic == TOPICS["obc_status"]][-1]
    assert published.retain is True and published.qos == 1


def test_before_hostd_speaks_the_profile_is_null_and_nothing_may_be_recorded(obc):
    # Naming the default profile here would be a guess, and a guess that lets DHS
    # write telemetry rows against a profile the platform may not be in.
    service, client, _ = obc
    service.on_start()
    assert status(client) == {
        "timestamp": status(client)["timestamp"],
        "status": "STANDBY",
        "profile": None,
        "cadence_scale": 1.0,
        "persistence": Persistence.NONE.value,
        "mission_label": None,
        # Both new on 2026-09-03 (W11): why this profile is active, and how this
        # run began. Null here because HOSTD has not said anything yet, which is
        # the honest answer rather than "an ordinary boot".
        "mission_start_reason": "command",
        "boot": None,
        "subsystems": {"watched": ["eps"], "lost": []},
    }


def test_nothing_is_self_tested_before_a_profile_says_what_hardware_to_expect(obc):
    service, client, _ = obc
    service.on_start()
    assert service._deploy is None


# ── the profile, and the bring-up it triggers ────────────────────────────────


def test_a_set_profile_command_becomes_an_apply_profile_action(obc):
    service, client, _ = obc
    service.on_start()
    command(client, "set_profile", params={"profile": "EXPO"}, request_id="req_010")
    assert host_commands(client)[-1] == {
        "timestamp": host_commands(client)[-1]["timestamp"],
        "action": "apply_profile",
        "profile": "EXPO",
        "request_id": "req_010",
        "ttl_minutes": None,
        "mission_label": None,
        "resume": False,
    }


def test_a_set_profile_without_a_profile_name_asks_hostd_for_nothing(obc, caplog):
    service, client, _ = obc
    service.on_start()
    with caplog.at_level("ERROR"):
        command(client, "set_profile", params={"ttl_minutes": 10})
    assert host_commands(client) == []
    assert "usable profile name" in caplog.text


def test_entering_an_active_profile_walks_standby_deploy_nominal(obc):
    service, client, _ = obc
    service.on_start()
    host_status(client, "DEMO")
    assert service.mission.state is MissionState.DEPLOY
    report_in(client, *MISSION_SERVICES)
    assert service.mission.state is MissionState.NOMINAL
    assert states(client)[-3:] == ["STANDBY", "DEPLOY", "NOMINAL"]


def test_the_status_carries_what_dhs_needs_to_open_a_mission(obc):
    service, client, _ = obc
    service.on_start()
    command(client, "set_profile", params={"profile": "FLIGHT",
                                          "mission_label": "walk to work"})
    host_status(client, "FLIGHT")
    report_in(client, *MISSION_SERVICES)
    assert status(client)["profile"] == "FLIGHT"
    assert status(client)["persistence"] == Persistence.MISSION_DB.value
    assert status(client)["mission_label"] == "walk to work"


def test_a_standby_profile_does_not_deploy(obc):
    service, client, _ = obc
    service.on_start()
    host_status(client, "HOSTED")
    assert service.mission.state is MissionState.STANDBY


def test_leaving_an_active_profile_returns_to_standby(obc):
    service, client, _ = obc
    bring_up(service, client, "EXPO")
    host_status(client, "HOSTED")
    assert service.mission.state is MissionState.STANDBY


def test_eps_is_not_waited_for_during_deploy(obc):
    # It has been up since boot; the 0x36 probe already proves the gauge answers,
    # and its cadence in DEPLOY is longer than the timeout.
    service, client, _ = obc
    service.on_start()
    host_status(client, "DEMO")
    assert "eps" not in service._deploy.awaited_services


def test_switching_between_two_active_profiles_goes_back_through_deploy(obc):
    # DHS closes its mission on the way out, and the new profile's hardware has
    # not been self-tested yet.
    service, client, _ = obc
    bring_up(service, client, "DEMO")
    host_status(client, "EXPO")
    assert service.mission.state is MissionState.DEPLOY


def test_a_profile_that_is_not_the_one_requested_does_not_deploy(obc, caplog):
    service, client, _ = obc
    service.on_start()
    command(client, "set_profile", params={"profile": "FLIGHT"})
    with caplog.at_level("ERROR"):
        host_status(client, "DEMO", requested="FLIGHT")
    assert service.mission.state is MissionState.STANDBY
    assert "not the profile OBC asked for" in caplog.text


def test_a_profile_running_before_obc_restarted_is_adopted(obc):
    # The retained host_status is how OBC recovers the running profile after
    # `systemctl restart cubesat@obc` mid-demo, without disturbing the AP.
    service, client, _ = obc
    service.on_start()
    host_status(client, "EXPO")
    assert service.mission.state is MissionState.DEPLOY
    assert service.profile_machine.achieved is Profile.EXPO


def test_a_restart_request_is_relayed_to_hostd_and_nothing_more(obc):
    """OBC adds no check of its own, deliberately.

    Which services exist is `KNOWN_SERVICES` and which units may be touched is
    HOSTD's allowlist — both on HOSTD's side. What OBC contributes is the
    privilege boundary: `cubesat/host/command` is root's inbox and the browser
    ACL denies it, so a ground client asks here instead.
    """
    service, client, _ = obc
    service.on_start()
    command(client, "restart_service", params={"service": "adcs"}, request_id="req_7")

    relayed = host_actions(client, "restart_service")
    assert len(relayed) == 1
    assert relayed[-1]["params"] == {"service": "adcs"}
    # The id travels with it, so the satellite's logs can be read against the
    # command that caused them.
    assert relayed[-1]["request_id"] == "req_7"


def test_a_restart_with_no_service_named_is_not_relayed(obc, caplog):
    service, client, _ = obc
    service.on_start()
    with caplog.at_level("ERROR"):
        command(client, "restart_service", params={})
    assert host_actions(client, "restart_service") == []
    assert "without a service name" in caplog.text


def test_a_restart_does_not_drop_the_satellite_into_safe(obc):
    # The defect this exists to close, found on the hardware 2026-09-01:
    # `cubesat restart comms` made the restarted service publish its goodbye,
    # OBC read it as a lost subsystem and latched SAFE until a ground `recover`
    # — which is exactly what the command was built to avoid.
    service, client, clock = obc
    bring_up(service, client)

    command(client, "restart_service", params={"service": "comms"})
    beat(client, "comms", alive=False)
    service.tick()
    assert service.mission.state is MissionState.NOMINAL
    # And nothing on the wire says otherwise: the retained status kept
    # `lost: [comms]` for seconds on the hardware, which a dashboard believed.
    assert status(client)["subsystems"]["lost"] == []

    clock.advance(2.0)
    beat(client, "comms", *MISSION_SERVICES, "eps")
    service.tick()
    assert service.mission.state is MissionState.NOMINAL


def test_a_restart_that_never_comes_back_still_latches_safe(obc):
    # The waiver postpones the protection, it does not switch it off: a service
    # that does not return inside the loss window is a fault like any other.
    service, client, clock = obc
    bring_up(service, client)

    command(client, "restart_service", params={"service": "comms"})
    beat(client, "comms", alive=False)
    for _ in range(4):
        clock.advance(10.0)
        beat(client, "eps", "adcs", "payload", "dhs")
    service.tick()

    assert service.mission.state is MissionState.SAFE
    assert status(client)["subsystems"]["lost"] == ["comms"]
    # Latched: the battery is fine and cannot clear a subsystem fault.
    battery(client, 80.0, external_power=True)
    assert service.mission.state is MissionState.SAFE


def test_only_the_named_service_is_forgiven_its_departure(obc):
    # A restart is not an amnesty. Another subsystem dying while one is being
    # restarted is still a fault.
    service, client, _ = obc
    bring_up(service, client)

    command(client, "restart_service", params={"service": "comms"})
    beat(client, "adcs", alive=False)
    service.tick()

    assert service.mission.state is MissionState.SAFE


def test_a_restart_of_a_name_obc_does_not_recognise_is_still_relayed(obc):
    # Not OBC's judgement to make: a second copy of the service list here is a
    # second thing to keep in step, and HOSTD refuses it in one place with the
    # allowlist beside it.
    service, client, _ = obc
    service.on_start()
    command(client, "restart_service", params={"service": "telegram"})
    assert host_actions(client, "restart_service")[-1]["params"] == {"service": "telegram"}


def test_the_profile_cadence_scale_reaches_every_service_through_the_status(
    service_factory, clock
):
    # One number in a profile reaches every subsystem through obc_status rather
    # than through a second copy of the cadence table. The scale is pinned here,
    # not read from the shipped profiles — see pinned_power.
    cfg = pinned_power(profiles.load(), Profile.DIAG, governor="ondemand", cadence_scale=0.2)
    service, client = service_factory(
        ObcService, profiles=cfg, bus=FakeBus(), clock=clock, wall_clock=clock
    )
    service.on_start()
    host_status(client, "DIAG")
    assert status(client)["cadence_scale"] == 0.2
    assert service.cadence_scale == 0.2


def test_a_host_status_that_says_nothing_new_republishes_without_deploying(obc):
    service, client, _ = obc
    bring_up(service, client, "DEMO")
    host_status(client, "DEMO")
    assert service.mission.state is MissionState.NOMINAL


def test_an_unusable_host_status_is_ignored(obc):
    service, client, _ = obc
    service.on_start()
    client.deliver(TOPICS["host_status"], {"errors": []})
    assert service.profile_machine.achieved is None
    assert service.mission.state is MissionState.STANDBY


def test_obc_writes_no_file_and_restores_no_profile(
    obc, service_factory, monkeypatch, tmp_path
):
    # The decisive case: a satellite that hit CRITICAL on a trip and is plugged
    # in at a desk hours later must not come back up with Wi-Fi off and no SSH.
    # HOSTD does record last-profile, as information; nothing reads it to decide.
    from cubesat.common import config

    # A path of this test's own. Asserting on the shared data directory would
    # make this pass or fail according to whether some other test legitimately
    # wrote that file first — an assertion about global state, not about OBC.
    marker = tmp_path / "last-profile"
    monkeypatch.setattr(config, "LAST_PROFILE_FILE", marker)

    service, client, _ = obc
    bring_up(service, client, "FLIGHT")
    assert not marker.exists()

    restarted, _ = service_factory(ObcService, profiles=profiles.load(), bus=FakeBus())
    restarted.on_start()
    assert restarted.profile_machine.achieved is None
    assert restarted.mission.state is MissionState.STANDBY


# ── DEPLOY ───────────────────────────────────────────────────────────────────


def test_a_subsystem_that_never_reports_fails_the_bring_up(obc, caplog):
    service, client, clock = obc
    service.on_start()
    host_status(client, "DEMO")
    report_in(client, "adcs", "payload", "dhs")
    clock.advance(deploy.DEPLOY_TIMEOUT_SEC + 1)
    with caplog.at_level("ERROR"):
        service.tick()
    assert service.mission.state is MissionState.SAFE
    assert "comms never reported" in caplog.text


def test_a_silent_device_fails_the_bring_up(service_factory, clock, caplog):
    service, client = service_factory(
        ObcService,
        profiles=profiles.load(),
        bus=FakeBus(present=(0x36, 0x22)),
        clock=clock,
        wall_clock=clock,
    )
    service.on_start()
    with caplog.at_level("WARNING"):
        host_status(client, "DEMO")
        # Pending, not failed: the device may be mid-reset under the service
        # that has just been started. The verdict lands when the window closes.
        assert service.mission.state is MissionState.DEPLOY
        clock.advance(21.0)
        service.tick()
    assert service.mission.state is MissionState.SAFE
    assert "0x28" in caplog.text


def test_a_failed_bring_up_is_not_undone_by_a_healthy_battery(obc):
    # Charging the pack does not make a subsystem that never came up report in.
    service, client, clock = obc
    service.on_start()
    host_status(client, "DEMO")
    clock.advance(31.0)
    service.tick()
    assert service.mission.state is MissionState.SAFE
    battery(client, 95.0, external_power=True)
    assert service.mission.state is MissionState.SAFE


def test_the_bring_up_ignores_a_gnss_fix_it_never_gets(obc, caplog):
    # DEMO and EXPO run indoors. Failing on a fix would send every indoor
    # demonstration to SAFE.
    service, client, _ = obc
    service.on_start()
    host_status(client, "DEMO")
    with caplog.at_level("INFO"):
        report_in(client, *MISSION_SERVICES)
    assert service.mission.state is MissionState.NOMINAL
    assert "no GNSS fix yet" in caplog.text


def test_a_fix_that_does_arrive_is_recorded(obc, caplog):
    service, client, _ = obc
    service.on_start()
    host_status(client, "DEMO")
    client.deliver(TOPICS["adcs_status"], {"gnss": {"fix": True, "satellites": 23}})
    with caplog.at_level("INFO"):
        report_in(client, "payload", "dhs", "comms")
    assert "GNSS has a fix" in caplog.text


def test_an_adcs_status_outside_deploy_is_harmless(obc):
    service, client, _ = obc
    bring_up(service, client)
    client.deliver(TOPICS["adcs_status"], {"gnss": None})
    assert service.mission.state is MissionState.NOMINAL


def test_dhs_reports_in_through_its_own_status_topic(obc):
    # DHS has no sensors to probe, but a recorder that never came up is still a
    # failed bring-up in a profile that asked for one.
    service, client, _ = obc
    service.on_start()
    host_status(client, "DEMO")
    report_in(client, "adcs", "payload", "comms")
    assert service.mission.state is MissionState.DEPLOY
    client.deliver(TOPICS["dhs_status"], {"recording": True})
    assert service.mission.state is MissionState.NOMINAL


def test_a_heartbeat_is_not_evidence_that_the_hardware_answered(obc):
    # The defect this guards: every service logs a silent device and stays up —
    # EPS does exactly that with a dead fuel gauge — so a heartbeat proves the
    # process started and nothing about the sensor. Heartbeat-only evidence would
    # make DEPLOY pass for a cable knocked loose during re-assembly.
    service, client, _ = obc
    service.on_start()
    host_status(client, "DEMO")
    beat(client, "adcs", "payload", "dhs", "comms")
    assert service.mission.state is MissionState.DEPLOY
    assert service._deploy.silent == ("adcs", "comms", "dhs", "payload")


def test_a_goodbye_during_deploy_is_not_a_report(obc):
    service, client, _ = obc
    service.on_start()
    host_status(client, "DEMO")
    beat(client, "comms", alive=False)
    assert "comms" in service._deploy.silent


# ── ground commands ──────────────────────────────────────────────────────────


def test_a_retired_command_is_ignored_rather_than_answered(obc):
    # `science_start` and `science_stop` were removed on 2026-09-02 along with
    # the state they entered. An old ground client, a dashboard build that has
    # not been redeployed or somebody's muscle memory can still publish one, and
    # an unknown command on this topic belongs to another service — so it is
    # dropped, not treated as `recover`, which the fall-through would otherwise
    # have made of it.
    service, client, _ = obc
    bring_up(service, client)
    command(client, "safe_mode")
    command(client, "science_start")
    assert service.mission.state is MissionState.SAFE


def test_safe_mode_reaches_safe_from_anywhere(obc):
    service, client, _ = obc
    bring_up(service, client)
    command(client, "safe_mode")
    assert service.mission.state is MissionState.SAFE


def test_recover_leaves_safe(obc):
    service, client, _ = obc
    bring_up(service, client)
    command(client, "safe_mode")
    command(client, "recover")
    assert service.mission.state is MissionState.NOMINAL


def test_a_ground_safe_mode_is_not_undone_by_the_battery(obc):
    # The pack recovering says nothing about why a human put the satellite in
    # SAFE. Only a ground `recover` clears that.
    service, client, _ = obc
    bring_up(service, client)
    command(client, "safe_mode")
    battery(client, 95.0, external_power=True)
    assert service.mission.state is MissionState.SAFE


def test_another_service_s_command_changes_nothing(obc):
    service, client, _ = obc
    bring_up(service, client)
    command(client, "take_photo", request_id="req_001")
    command(client, "get_telemetry", request_id="req_002")
    assert service.mission.state is MissionState.NOMINAL


@pytest.mark.parametrize(
    "payload", ["not json", "[1,2,3]", '{"command": null}', '{"command": {"a": 1}}']
)
def test_a_malformed_command_does_not_take_obc_down(obc, payload):
    service, client, _ = obc
    bring_up(service, client)
    client.deliver(TOPICS["command"], payload)
    assert service.mission.state is MissionState.NOMINAL


# ── the power-driven descent ─────────────────────────────────────────────────


def test_a_draining_battery_throttles_then_saves_itself(obc):
    service, client, _ = obc
    bring_up(service, client)
    battery(client, THROTTLED)
    assert service.mission.state is MissionState.LOW_POWER
    battery(client, SAVED)
    assert service.mission.state is MissionState.SAFE


def test_a_recovering_battery_climbs_back_to_nominal(obc):
    service, client, _ = obc
    bring_up(service, client)
    battery(client, THROTTLED)
    battery(client, RECOVERED)
    assert service.mission.state is MissionState.NOMINAL


def test_a_battery_that_says_nothing_yields_no_verdict(obc, caplog):
    service, client, _ = obc
    bring_up(service, client)
    with caplog.at_level("WARNING"):
        client.deliver(TOPICS["eps_status"], {"battery_percent": 42.0})
    assert service.mission.state is MissionState.NOMINAL
    assert "no pack voltage" in caplog.text


def test_a_healthy_battery_in_a_healthy_state_publishes_nothing_new(obc):
    service, client, _ = obc
    bring_up(service, client)
    before = len(states(client))
    battery(client, 88.0, external_power=True)
    assert len(states(client)) == before


# ── CRITICAL and the power-off ───────────────────────────────────────────────


def flush_thread(service, timeout=2.0):
    service._flush_thread.join(timeout=timeout)
    assert not service._flush_thread.is_alive()


def test_critical_is_published_before_the_power_off_is_asked_for(obc, monkeypatch):
    # DHS closes its mission on seeing CRITICAL in the retained status, so the
    # state has to be out on the bus before the host starts going down.
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 0.05)
    service, client, _ = obc
    bring_up(service, client)
    client.deliver(TOPICS["dhs_status"], {"recording": True})
    battery(client, DYING)
    assert service.mission.state is MissionState.CRITICAL
    assert states(client)[-1] == "CRITICAL"
    flush_thread(service)
    assert host_commands(client)[-1]["action"] == "poweroff"
    assert host_commands(client)[-1]["params"] == {"reason": "battery_critical"}


def test_the_power_off_waits_for_dhs_to_close_its_mission(obc, monkeypatch):
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 5.0)
    service, client, _ = obc
    bring_up(service, client)
    client.deliver(TOPICS["dhs_status"], {"recording": True})
    battery(client, DYING)
    assert [c for c in host_commands(client) if c["action"] == "poweroff"] == []
    client.deliver(TOPICS["dhs_status"], {"recording": False})
    flush_thread(service)
    assert host_commands(client)[-1]["action"] == "poweroff"


def test_the_grace_expires_rather_than_hanging(obc, monkeypatch, caplog):
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 0.05)
    service, client, _ = obc
    bring_up(service, client)
    client.deliver(TOPICS["dhs_status"], {"recording": True})
    with caplog.at_level("WARNING"):
        battery(client, DYING)
        flush_thread(service)
    assert "powering off anyway" in caplog.text
    assert host_commands(client)[-1]["action"] == "poweroff"


def test_a_profile_with_no_recorder_is_not_waited_for(obc, monkeypatch):
    # HOSTED never started DHS. Hanging until a service that was never launched
    # answers would leave the Pi running at under 10 % battery.
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 30.0)
    service, client, _ = obc
    service.on_start()
    host_status(client, "HOSTED")
    battery(client, DYING)
    flush_thread(service, timeout=1.0)
    assert host_commands(client)[-1]["action"] == "poweroff"


def test_critical_before_any_profile_is_known_still_powers_off(obc, monkeypatch):
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 30.0)
    service, client, _ = obc
    service.on_start()
    battery(client, DYING)
    flush_thread(service, timeout=1.0)
    assert host_commands(client)[-1]["action"] == "poweroff"


def test_nothing_leaves_critical(obc, monkeypatch):
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 0.05)
    service, client, _ = obc
    bring_up(service, client)
    battery(client, DYING)
    flush_thread(service)
    command(client, "recover")
    command(client, "safe_mode")
    # Not even a profile change: an active profile arriving now would otherwise
    # walk a satellite that is powering off back into a bring-up.
    host_status(client, "EXPO")
    battery(client, 95.0, external_power=True)
    assert service.mission.state is MissionState.CRITICAL
    assert service._deploy is None


def test_critical_is_entered_only_once(obc, monkeypatch):
    # The power policy already refuses to re-enter CRITICAL; this is the backstop.
    # A second poweroff request, or a second grace thread, would race the
    # shutdown that is already under way.
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 0.05)
    service, client, _ = obc
    bring_up(service, client)
    battery(client, DYING)
    flush_thread(service)
    first = service._flush_thread
    service._enter_critical()
    assert service._flush_thread is first
    assert len([c for c in host_commands(client) if c["action"] == "poweroff"]) == 1


def test_the_flush_is_started_once_per_descent(obc, monkeypatch):
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 0.05)
    service, client, _ = obc
    bring_up(service, client)
    battery(client, DYING)
    flush_thread(service)
    battery(client, DYING - STEP)
    poweroffs = [c for c in host_commands(client) if c["action"] == "poweroff"]
    assert len(poweroffs) == 1


# ── health ───────────────────────────────────────────────────────────────────


def test_a_subsystem_that_dies_ungracefully_drops_the_state_to_safe(obc, caplog):
    # The MQTT last will, so OBC learns in milliseconds instead of waiting out
    # three missed heartbeats.
    service, client, _ = obc
    bring_up(service, client)
    with caplog.at_level("WARNING"):
        beat(client, "adcs", alive=False)
    assert service.mission.state is MissionState.SAFE
    assert "adcs" in caplog.text


def test_a_subsystem_that_goes_quiet_drops_the_state_to_safe(obc):
    service, client, clock = obc
    bring_up(service, client)
    beat(client, "eps", *MISSION_SERVICES)
    clock.advance(31.0)
    service.tick()
    assert service.mission.state is MissionState.SAFE


def test_recover_in_a_standby_profile_lands_in_standby_not_nominal(obc):
    # The first `recover` ever sent to the hardware landed a HOSTED satellite
    # in NOMINAL — a "flying" state on a desk that asked for silence, beaconing
    # every minute. Recovery honours the profile's mission mode.
    service, client, _ = obc
    service.on_start()
    host_status(client, "HOSTED")
    assert service.mission.state is MissionState.STANDBY
    beat(client, "comms", alive=False)
    service.tick()
    assert service.mission.state is MissionState.SAFE

    beat(client, "comms")  # the subsystem is back; only the latch remains
    command(client, "recover")

    assert service.mission.state is MissionState.STANDBY


def test_a_goodbye_during_a_profile_switch_is_the_switch_not_a_fault(obc):
    # OBC asked HOSTD for a new profile, and HOSTD stops the old profile's
    # units to apply it. Their last wills reach OBC before host_status does —
    # by four seconds on the first hardware run — and reading that as a lost
    # subsystem latched a healthy satellite into SAFE mid-switch.
    service, client, _ = obc
    bring_up(service, client)

    command(client, "set_profile", params={"profile": "MAINTENANCE"})
    beat(client, "adcs", alive=False)
    service.tick()

    assert service.mission.state is not MissionState.SAFE
    host_status(client, "MAINTENANCE")
    assert service.mission.state is MissionState.STANDBY


def test_a_switch_that_never_completes_gives_health_its_say_back(obc):
    # The settling window is bounded: a request HOSTD never answers must not
    # suppress the health monitor forever. After the heartbeat-loss window the
    # goodbye counts again.
    service, client, clock = obc
    bring_up(service, client)

    command(client, "set_profile", params={"profile": "MAINTENANCE"})
    beat(client, "adcs", alive=False)
    clock.advance(31.0)
    beat(client, "eps", "payload", "dhs", "comms")
    service.tick()

    assert service.mission.state is MissionState.SAFE


def test_a_service_this_profile_never_started_is_not_missed(obc):
    # MAINTENANCE runs OBC and EPS and nothing else. Putting a healthy satellite
    # in SAFE for not running what it was told not to run would be absurd.
    service, client, clock = obc
    service.on_start()
    host_status(client, "MAINTENANCE")
    for _ in range(10):
        clock.advance(10.0)
        beat(client, "eps")
        service.tick()
    assert service.mission.state is MissionState.STANDBY


def test_the_status_names_what_the_profile_watches(obc):
    # The ground segment's only way to tell "off because the profile never
    # started it" from "expected and silent": without this list a dashboard has
    # to guess, and both guesses are wrong somewhere.
    service, client, _ = obc
    bring_up(service, client, "DEMO")
    assert status(client)["subsystems"] == {
        "watched": ["adcs", "comms", "dhs", "eps", "payload"],
        "lost": [],
    }


def test_a_lost_subsystem_is_named_in_the_status(obc):
    service, client, _ = obc
    bring_up(service, client, "DEMO")
    beat(client, "adcs", alive=False)
    assert status(client)["status"] == "SAFE"
    assert status(client)["subsystems"]["lost"] == ["adcs"]


def test_a_goodbye_during_a_profile_switch_is_not_published_as_lost(obc):
    # The same suppression _check_health applies, on the wire: a dashboard
    # flashing FAIL for every unit HOSTD is deliberately stopping would be
    # reporting the switch as a fault.
    service, client, _ = obc
    bring_up(service, client)
    command(client, "set_profile", params={"profile": "MAINTENANCE"})
    beat(client, "adcs", alive=False)
    service.tick()
    assert status(client)["subsystems"]["lost"] == []


def test_a_lost_subsystem_is_not_recovered_by_the_battery(obc):
    service, client, _ = obc
    bring_up(service, client)
    beat(client, "dhs", alive=False)
    battery(client, 95.0, external_power=True)
    assert service.mission.state is MissionState.SAFE


def test_a_new_profile_clears_the_latched_fault(obc):
    # The subsystems that failed are not the ones the new profile asked for.
    # MAINTENANCE, not HOSTED: HOSTED still asks for COMMS, so a dead radio is
    # a real fault there and would not clear.
    service, client, _ = obc
    bring_up(service, client, "DEMO")
    beat(client, "comms", alive=False)
    assert service.mission.state is MissionState.SAFE
    host_status(client, "MAINTENANCE")
    assert service.mission.state is MissionState.STANDBY
    assert service._fault_latched is False


def test_hosted_still_watches_the_radio_it_asks_for(obc):
    # HOSTED runs COMMS so the satellite stays reachable over LoRa, which makes
    # a dead COMMS a real fault on the desk too: switching home must not read
    # as the fault having been repaired.
    service, client, clock = obc
    bring_up(service, client, "DEMO")
    beat(client, "comms", alive=False)
    assert service.mission.state is MissionState.SAFE
    host_status(client, "HOSTED")
    for _ in range(5):
        clock.advance(10.0)
        beat(client, "eps")
        service.tick()
    assert service.mission.state is MissionState.SAFE


# ── the tick ─────────────────────────────────────────────────────────────────


def test_the_tick_republishes_the_retained_status(obc):
    service, client, _ = obc
    bring_up(service, client)
    before = len(states(client))
    service.tick()
    assert states(client)[-1] == "NOMINAL"
    assert len(states(client)) == before + 1


def test_the_tick_falls_back_to_the_default_profile_when_the_ttl_expires(obc):
    # FLIGHT turns Wi-Fi off, which means no SSH. The TTL is the layer that
    # brings both back without a radio uplink or a power cycle.
    service, client, clock = obc
    bring_up(service, client, "FLIGHT", ttl_expires_at=clock.now + 600 * 60.0)
    clock.advance(601 * 60.0)
    service.tick()
    assert host_commands(client)[-1] == {
        "timestamp": host_commands(client)[-1]["timestamp"],
        "action": "apply_profile",
        "profile": "HOSTED",
        "request_id": None,
        "ttl_minutes": None,
        "mission_label": None,
        "resume": False,
    }


def test_a_live_profile_is_left_alone_by_the_tick(obc):
    service, client, clock = obc
    bring_up(service, client, "FLIGHT")
    clock.advance_minutes(10)
    service.tick()
    assert [c for c in host_commands(client) if c["action"] == "apply_profile"] == []


# ── the CPU governor ─────────────────────────────────────────────────────────


def governors(client):
    return [
        p["params"]["governor"]
        for p in client.payloads(TOPICS["host_command"])
        if p["action"] == "set_governor"
    ]


def test_low_power_asks_hostd_to_drop_the_cpu_governor(obc):
    # LOW_POWER is not a label. The governor is one of the knobs that makes it
    # mean something measurable.
    service, client, _ = obc
    bring_up(service, client, "DEMO")
    battery(client, THROTTLED)
    assert governors(client) == ["powersave"]


def test_recovery_restores_the_profile_s_own_governor(service_factory, clock):
    # A profile that asks for something other than `ondemand` must get it back
    # after LOW_POWER, rather than the hardcoded default: restoring `ondemand`
    # would quietly undo the profile. `performance` is pinned by the test — no
    # shipped profile has to keep asking for it for this to stay a real check.
    cfg = pinned_power(profiles.load(), Profile.DIAG, governor="performance", cadence_scale=1.0)
    service, client = service_factory(
        ObcService, profiles=cfg, bus=FakeBus(), clock=clock, wall_clock=clock
    )
    service.on_start()
    host_status(client, "DIAG")
    report_in(client, "adcs", "payload", "dhs")
    battery(client, THROTTLED)
    battery(client, RECOVERED)
    assert governors(client) == ["powersave", "performance"]


def test_the_governor_is_restored_even_when_safe_came_in_between(obc):
    # The way out of LOW_POWER is not always direct: a battery that keeps falling
    # goes LOW_POWER -> SAFE, and the recovery from there never passes back
    # through LOW_POWER.
    service, client, _ = obc
    bring_up(service, client, "DEMO")
    battery(client, THROTTLED)
    battery(client, SAVED)
    battery(client, RECOVERED)
    assert service.mission.state is MissionState.NOMINAL
    assert governors(client) == ["powersave", "ondemand"]


def test_the_governor_is_asked_for_once_per_descent(obc):
    service, client, _ = obc
    bring_up(service, client)
    battery(client, THROTTLED)
    battery(client, THROTTLED - STEP)
    battery(client, THROTTLED - 2 * STEP)
    assert governors(client) == ["powersave"]


def test_reaching_nominal_without_a_descent_asks_for_nothing(obc):
    service, client, _ = obc
    bring_up(service, client)
    battery(client, 88.0)
    assert governors(client) == []


def test_a_profile_change_leaves_the_governor_to_apply_profile(obc):
    # apply_profile already carries the new profile's governor, so a set_governor
    # on the way out would be a second, racing source of truth.
    service, client, _ = obc
    bring_up(service, client, "DEMO")
    battery(client, THROTTLED)
    host_status(client, "EXPO")
    report_in(client, *MISSION_SERVICES)
    assert governors(client) == ["powersave"]
    assert service._governor_lowered is False


def test_the_governor_is_not_guessed_when_no_profile_is_known(obc):
    service, client, _ = obc
    service.on_start()
    service.mission.fire("begin_deploy")
    service.mission.fire("enter_low_power")
    service.mission.fire("recover")
    # LOW_POWER was requested, but there is no profile to restore from, and
    # inventing a governor name is worse than leaving the host as it is.
    assert governors(client) == ["powersave"]


# ── plugged in, flat ─────────────────────────────────────────────────────────


def test_a_flat_satellite_plugged_in_at_a_desk_is_not_powered_off(obc, monkeypatch):
    # The X728 would never bring it back, because mains never left: the ordinary
    # recovery gesture would brick the unit until someone pulled the plug.
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 0.05)
    service, client, _ = obc
    bring_up(service, client, "EXPO")
    battery(client, SAVED)
    assert service.mission.state is MissionState.SAFE
    battery(client, DYING, external_power=True)
    assert service.mission.state is MissionState.NOMINAL
    assert [c for c in host_commands(client) if c["action"] == "poweroff"] == []


def test_a_charger_that_stopped_charging_does_not_suppress_the_power_off(obc, monkeypatch):
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 0.05)
    service, client, _ = obc
    bring_up(service, client, "EXPO")
    # A charger that has genuinely stopped moves both slopes: the pack is now
    # supplying the load, so its terminal voltage falls with the charge. The
    # gauge's model drifting downwards on its own is a different case, and it
    # deliberately no longer reaches here — tests/unit/obc/test_power_policy.py.
    battery(client, DYING, external_power=True, voltage_rate=-90.0)
    assert service.mission.state is MissionState.CRITICAL
    flush_thread(service)
    assert host_commands(client)[-1]["action"] == "poweroff"


# ── the mission label ────────────────────────────────────────────────────────


def test_relabelling_a_running_profile_updates_the_status(obc):
    # The profile did not change, so DHS keeps the mission it has; only the name
    # on obc/status moves.
    service, client, _ = obc
    service.on_start()
    command(client, "set_profile", params={"profile": "FLIGHT", "mission_label": "walk to work"})
    host_status(client, "FLIGHT")
    report_in(client, *MISSION_SERVICES)
    assert status(client)["mission_label"] == "walk to work"

    command(client, "set_profile", params={"profile": "FLIGHT", "mission_label": "walk home"})
    host_status(client, "FLIGHT")
    assert status(client)["mission_label"] == "walk home"
    assert service.mission.state is MissionState.NOMINAL


# ── resuming an interrupted trip (W11) ───────────────────────────────────────
#
# The failure this closes: a reset mid-trip brings the satellite up in HOSTED,
# where DHS, ADCS and PAYLOAD never start, and nothing says so — STANDBY has no
# beacon row. The rule itself is unit-tested in test_resume.py; what these test
# is the wiring, which is where the two halves of the evidence meet.

RESUMES = config.RESUME_MAX_CONSECUTIVE
SETTLE = config.RESUME_SETTLE_SEC
EVIDENCE_WINDOW = config.RESUME_EVIDENCE_TIMEOUT_SEC


def interrupted(clock, profile="FLIGHT", minutes_left=590, **fields):
    """What HOSTD publishes about a trip a reset cut short."""
    return {
        "profile": profile,
        "written_at": clock.now - 60.0,
        "ttl_expires_at": clock.now + minutes_left * 60.0,
        "mission_label": "walk to work",
        "resume_count": 0,
        **fields,
    }


def boot_into(client, clock, **fields):
    """A boot: HOSTD came up in HOSTED and nothing has asked for anything."""
    host_status(client, "HOSTED", boot=True, previous=interrupted(clock, **fields))


def applies(client):
    return host_actions(client, "apply_profile")


def test_a_reset_on_battery_puts_the_trip_back(obc):
    # The whole point: an hour into a walk, a jolt reboots the Pi, and the
    # recording resumes without anybody being told.
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    assert applies(client) == []  # nothing yet: the mains has not been read

    battery(client, 62.0, external_power=False)

    assert applies(client)[-1] == {
        "timestamp": applies(client)[-1]["timestamp"],
        "action": "apply_profile",
        "profile": "FLIGHT",
        "request_id": None,
        # The remainder of the strap it already had, not a fresh 600 minutes.
        "ttl_minutes": 590.0,
        # Under the name it was given, so the trip reads as one journey.
        "mission_label": "walk to work",
        "resume": True,
    }


def test_mains_at_boot_means_a_desk_and_not_a_trip(obc):
    # The case the whole design protects: brought home flat, plugged in, and it
    # must come up on the home network with SSH rather than walking off into a
    # profile with no Wi-Fi.
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    battery(client, 62.0, external_power=True)

    assert applies(client) == []
    assert status(client)["boot"] == {
        "at": clock.now,
        "previous": "FLIGHT",
        "resumed": False,
        "reason": "mains",
    }


def test_no_reading_at_all_is_not_a_reading_of_no_mains(obc):
    # An EPS that never published is also a satellite that cannot see its own
    # battery, which is a reason to stay reachable rather than to fly on.
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)

    clock.advance(EVIDENCE_WINDOW + 1)
    service.tick()

    assert applies(client) == []
    assert status(client)["boot"]["reason"] == "noeps"
    # And the window really was a window: a reading arriving afterwards does not
    # quietly resume a trip nobody is watching any more.
    battery(client, 62.0, external_power=False)
    assert applies(client) == []


def test_a_reading_inside_the_window_still_counts(obc):
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    clock.advance(EVIDENCE_WINDOW - 1)
    service.tick()
    battery(client, 62.0, external_power=False)
    assert applies(client)[-1]["profile"] == "FLIGHT"


def test_an_eps_message_without_the_mains_pin_is_not_evidence(obc):
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    client.deliver(TOPICS["eps_status"], {"battery_percent": 62.0, "voltage": 3.8})
    assert applies(client) == []
    # Still waiting, rather than refused: the next message may carry it.
    assert status(client)["boot"] is None


def test_an_ordinary_desk_reboot_says_nothing_at_all(obc):
    # HOSTED interrupted by a reboot is not news, and airtime on a shared mesh
    # is not free — so there is nothing for COMMS to transmit.
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock, profile="HOSTED")
    assert status(client)["boot"] == {
        "at": clock.now,
        "previous": None,
        "resumed": False,
        "reason": "profile",
    }


def test_a_restart_of_obc_under_a_running_profile_resumes_nothing(obc):
    # `systemctl restart cubesat@obc` mid-trip: the retained host_status says a
    # profile is applied and nothing about a boot, so there is nothing to weigh.
    service, client, clock = obc
    service.on_start()
    host_status(client, "FLIGHT", previous=interrupted(clock))
    battery(client, 62.0, external_power=False)
    assert applies(client) == []


def test_a_human_who_has_already_chosen_is_not_argued_with(obc):
    # `boot` goes false the moment anything asks for a profile — including the
    # operator typing `profile hosted` on arrival.
    service, client, clock = obc
    service.on_start()
    host_status(client, "HOSTED", boot=False, previous=interrupted(clock))
    battery(client, 62.0, external_power=False)
    assert applies(client) == []


def test_a_boot_loop_stops_resuming_and_says_why(obc):
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock, resume_count=RESUMES)
    battery(client, 62.0, external_power=False)
    assert applies(client) == []
    assert status(client)["boot"]["reason"] == "loop"


def test_a_trip_whose_ttl_ran_out_during_the_reset_stays_down(obc):
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock, minutes_left=-1)
    assert applies(client) == []
    assert status(client)["boot"]["reason"] == "ttl"


def test_the_question_is_asked_once_per_boot(obc):
    # Two eps_status messages, one apply: the rule runs to an answer and stops.
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    battery(client, 62.0, external_power=False)
    battery(client, 61.0, external_power=False)
    assert len(applies(client)) == 1


def test_a_resumed_session_tells_hostd_when_it_has_outlived_the_reset(obc):
    # The boot-loop fence is a lifetime, not a counter: three resumes only mean
    # a loop when all three were short.
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    battery(client, 62.0, external_power=False)

    service.tick()
    assert host_actions(client, "clear_resume") == []

    clock.advance(SETTLE)
    service.tick()
    assert len(host_actions(client, "clear_resume")) == 1

    # Once, not once per tick: a HOSTD that never answers must not be shouted at.
    clock.advance(SETTLE)
    service.tick()
    assert len(host_actions(client, "clear_resume")) == 1


def test_a_session_that_was_not_resumed_never_clears_anything(obc):
    service, client, clock = obc
    bring_up(service, client, "FLIGHT")
    clock.advance(SETTLE * 2)
    service.tick()
    assert host_actions(client, "clear_resume") == []


def test_the_mission_row_records_that_this_run_was_resumed(obc):
    # DHS opens a mission from obc_status alone, so this is how a trip split in
    # two by a reset is legible as one afterwards.
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    battery(client, 62.0, external_power=False)
    assert status(client)["mission_start_reason"] == "command"  # not applied yet

    host_status(client, "FLIGHT", previous=interrupted(clock))
    assert status(client)["mission_start_reason"] == "resume"
    assert status(client)["mission_label"] == "walk to work"

    # And a profile asked for afterwards is a command again.
    command(client, "set_profile", params={"profile": "DEMO"})
    host_status(client, "DEMO")
    assert status(client)["mission_start_reason"] == "command"


def test_a_resume_hostd_refuses_is_reported_rather_than_assumed(obc, monkeypatch):
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    monkeypatch.setattr(service.profile_machine, "request", lambda *a, **k: False)
    battery(client, 62.0, external_power=False)
    assert status(client)["boot"] == {
        "at": clock.now,
        "previous": "FLIGHT",
        "resumed": False,
        "reason": "profile",
    }


def test_a_resumed_trip_says_so_on_obc_status(obc):
    service, client, clock = obc
    service.on_start()
    boot_into(client, clock)
    battery(client, 62.0, external_power=False)
    assert status(client)["boot"] == {
        "at": clock.now,
        "previous": "FLIGHT",
        "resumed": True,
        "reason": None,
    }
