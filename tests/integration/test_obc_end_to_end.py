"""OBC from process start to a powered-off host, through the real run loop.

The unit tests call ``on_start()`` and ``tick()`` directly. Here ``run()``
drives: signal handling, the cadence loop, the heartbeat thread, the CRITICAL
grace thread and the shutdown path all participate at once, which is where the
interactions between them would break. Everything below is a scenario an
operator can actually produce — a profile switch, a walk that drains the
battery, a subsystem that dies — rather than a method call.
"""

from __future__ import annotations

import threading
import time

from cubesat.common import config, profiles
from cubesat.common.states import MissionState, Profile
from cubesat.common.topics import TOPICS
from cubesat.obc import deploy, power_policy
from cubesat.obc import service as obc_service
from cubesat.obc.service import ObcService

#: Battery levels placed relative to the thresholds rather than spelled out, so
#: that moving one — LOW_POWER went 40 % to 30 % on 2026-09-02 — does not break
#: a test that is about the descent rather than about the number.
THROTTLED = power_policy.LOW_POWER_PERCENT - 1.0
SAVED = power_policy.SAFE_PERCENT - 1.0
DYING = power_policy.CRITICAL_PERCENT - 1.0

#: What DEMO, EXPO and FLIGHT all ask for.
MISSION_SERVICES = ("adcs", "payload", "dhs", "comms")

STATUS_PAYLOADS = {
    "adcs": {"roll": 1.2, "yaw": 178.9, "gnss": {"fix": False, "satellites": 0}},
    "payload": {"temperature": 23.4, "humidity": 45.2},
    "dhs": {"recording": True, "rows": 1},
    "comms": {"beacon_enabled": True},
}


class FakeBus:
    """Every documented address answers. The bring-up is not about the bus here."""

    def present(self, address: int) -> bool:
        return address in (0x20, 0x22, 0x28, 0x36)


class Clock:
    """Injected monotonic clock, so a ten-hour TTL costs no wall time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def launch(service_factory, monkeypatch, clock=None):
    """Start OBC in a thread, ticking fast enough to watch.

    The heartbeat interval is shortened only *after* construction: the health
    monitor captures it once, so this speeds up the publishing loop without also
    shrinking the window a subsystem has to answer in.
    """
    service, client = service_factory(
        ObcService,
        profiles=profiles.load(),
        bus=FakeBus(),
        clock=clock or Clock(),
        wall_clock=clock or Clock(),
    )
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    client.connect_ok()
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 0.02))
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    return service, client, thread


def shut_down(service, thread):
    service.stop()
    thread.join(timeout=5)
    assert not thread.is_alive(), "OBC did not shut down"


def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def state_of(client):
    payloads = client.payloads(TOPICS["obc_status"])
    return payloads[-1]["status"] if payloads else None


def wait_for_state(client, expected):
    assert until(lambda: state_of(client) == expected), (
        f"expected {expected}, last saw {state_of(client)}"
    )


def host_actions(client, action):
    return [p for p in client.payloads(TOPICS["host_command"]) if p["action"] == action]


def report_in(client, *services):
    """A subsystem's own status message — what the bring-up waits for."""
    for name in services:
        client.deliver(TOPICS[deploy.REPORT_TOPICS[name]], dict(STATUS_PAYLOADS[name]))


def beat(client, *services):
    for name in services:
        client.deliver(TOPICS["heartbeat"], {"service": name, "alive": True})


def wait_out(clock, client, seconds):
    """Let ``seconds`` of satellite time pass with every subsystem still alive.

    In steps well under the heartbeat grace, because a subsystem going quiet is a
    real verdict and jumping the clock past three missed windows would produce
    it — and then the test would be watching the wrong mechanism.
    """
    step = 10.0
    for _ in range(int(seconds // step) + 1):
        clock.advance(step)
        beat(client, "eps", *MISSION_SERVICES)


def hostd_says(client, profile, ttl_expires_at=None, boot=False, previous=None):
    """HOSTD's retained answer. It carries the absolute TTL deadline, because
    HOSTD holds the applied profile and so owns when that profile started.

    ``boot`` and ``previous`` are what a boot publishes: whether anything has
    asked for a profile since the machine came up, and what the run before it
    was doing. See W11 and ``obc/resume.py``.
    """
    client.deliver(
        TOPICS["host_status"],
        {
            "profile": profile,
            "profile_requested": profile,
            "ttl_expires_at": ttl_expires_at,
            "errors": [],
            "boot": boot,
            "previous": previous,
        },
    )


def battery(client, percent, external_power=False, charge_rate=None):
    if charge_rate is None:
        charge_rate = 1.5 if external_power else -2.0
    client.deliver(
        TOPICS["eps_status"],
        {
            "battery_percent": percent,
            "voltage": 3.7,
            "external_power": external_power,
            "charge_rate": charge_rate,
        },
    )


# ── the ordinary day ─────────────────────────────────────────────────────────


def test_a_profile_switch_from_the_ground_brings_the_satellite_up(
    service_factory, monkeypatch
):
    service, client, thread = launch(service_factory, monkeypatch)

    # 1. Fresh boot: idle, and the profile is not assumed.
    wait_for_state(client, MissionState.STANDBY.value)
    assert client.payloads(TOPICS["obc_status"])[-1]["profile"] is None

    # 2. `cubesat profile expo`, or the same command over LoRa.
    client.deliver(TOPICS["command"], {"command": "set_profile", "params": {"profile": "EXPO"},
                                       "request_id": "req_010"})
    assert until(lambda: host_actions(client, "apply_profile"))
    assert host_actions(client, "apply_profile")[-1]["profile"] == "EXPO"

    # 3. HOSTD did the work and reports what it achieved.
    hostd_says(client, "EXPO")
    wait_for_state(client, MissionState.DEPLOY.value)

    # 4. Each subsystem's own heartbeat is the proof it came up.
    report_in(client, *MISSION_SERVICES)
    wait_for_state(client, MissionState.NOMINAL.value)

    # 5. The one retained message carries everything DHS needs to open a mission.
    status = client.payloads(TOPICS["obc_status"])[-1]
    assert status["profile"] == "EXPO"
    # Read from the profile rather than spelled out: what EXPO permits is the
    # profile file's decision and legitimately changed (to `none`, on
    # 2026-09-01, when recording became FLIGHT's privilege). What this test is
    # for is that the value reaches DHS on this one retained message.
    assert status["persistence"] == profiles.load().get(Profile.EXPO).persistence.value
    assert status["cadence_scale"] == 1.0

    # 6. SAFE from the ground, and back with a recover — the pair of commands
    #    that actually move the state machine from outside. (There was a
    #    science_start/stop pair here until 2026-09-02, when the state they
    #    entered was removed for having no content of its own.)
    client.deliver(TOPICS["command"], {"command": "safe_mode"})
    wait_for_state(client, MissionState.SAFE.value)
    client.deliver(TOPICS["command"], {"command": "recover"})
    wait_for_state(client, MissionState.NOMINAL.value)

    shut_down(service, thread)
    beats = [p for p in client.payloads(TOPICS["heartbeat"]) if p["service"] == "obc"]
    assert any(b["alive"] for b in beats)
    # The goodbye is what tells a health monitor this was a clean exit.
    assert beats[-1]["alive"] is False


def test_a_bring_up_that_never_completes_ends_in_safe(service_factory, monkeypatch, caplog):
    # COMMS was in the profile and never started. Publishing NOMINAL anyway would
    # be pretending the satellite has a radio.
    clock = Clock()
    service, client, thread = launch(service_factory, monkeypatch, clock)
    wait_for_state(client, MissionState.STANDBY.value)

    hostd_says(client, "DEMO")
    wait_for_state(client, MissionState.DEPLOY.value)
    report_in(client, "adcs", "payload", "dhs")
    with caplog.at_level("ERROR"):
        wait_out(clock, client, deploy.DEPLOY_TIMEOUT_SEC)
        wait_for_state(client, MissionState.SAFE.value)
    assert "comms never reported" in caplog.text
    shut_down(service, thread)


# ── the walk that runs the battery down ──────────────────────────────────────


def test_a_draining_battery_descends_and_finally_powers_the_host_off(
    service_factory, monkeypatch
):
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 1.0)
    service, client, thread = launch(service_factory, monkeypatch)
    wait_for_state(client, MissionState.STANDBY.value)

    hostd_says(client, "FLIGHT")
    report_in(client, *MISSION_SERVICES)
    wait_for_state(client, MissionState.NOMINAL.value)
    client.deliver(TOPICS["dhs_status"], {"recording": True, "rows": 12})

    battery(client, THROTTLED)
    wait_for_state(client, MissionState.LOW_POWER.value)
    # LOW_POWER is not a label: the governor drops with it.
    assert host_actions(client, "set_governor")[-1]["params"] == {"governor": "powersave"}
    battery(client, SAVED)
    wait_for_state(client, MissionState.SAFE.value)

    battery(client, DYING)
    wait_for_state(client, MissionState.CRITICAL.value)
    # DHS closes its mission on seeing CRITICAL in the retained status — no new
    # command for it — and OBC waits for it to say the recorder is idle.
    assert host_actions(client, "poweroff") == []
    client.deliver(TOPICS["dhs_status"], {"recording": False, "rows": 13})

    assert until(lambda: host_actions(client, "poweroff"))
    assert host_actions(client, "poweroff")[-1]["params"] == {"reason": "battery_critical"}
    shut_down(service, thread)


def test_mains_at_a_desk_brings_a_throttled_satellite_back(service_factory, monkeypatch):
    service, client, thread = launch(service_factory, monkeypatch)
    wait_for_state(client, MissionState.STANDBY.value)
    hostd_says(client, "EXPO")
    report_in(client, *MISSION_SERVICES)
    wait_for_state(client, MissionState.NOMINAL.value)

    battery(client, THROTTLED - 4.0)
    wait_for_state(client, MissionState.LOW_POWER.value)
    battery(client, THROTTLED - 3.0, external_power=True)
    # A state change happens inside a mission; it does not end one, so the
    # profile and the label are untouched by the round trip.
    wait_for_state(client, MissionState.NOMINAL.value)
    assert client.payloads(TOPICS["obc_status"])[-1]["profile"] == "EXPO"
    shut_down(service, thread)


# ── the ways back out of a profile with no Wi-Fi ─────────────────────────────


def test_a_profile_ttl_expires_on_its_own_and_asks_for_the_default(
    service_factory, monkeypatch
):
    # FLIGHT turns Wi-Fi off, which means no SSH. The TTL is the layer that
    # brings both back with no radio uplink and no power cycle.
    clock = Clock()
    service, client, thread = launch(service_factory, monkeypatch, clock)
    wait_for_state(client, MissionState.STANDBY.value)
    # A one-minute TTL, so the scenario is the mechanism rather than the ten
    # hours FLIGHT carries by default.
    client.deliver(
        TOPICS["command"],
        {"command": "set_profile", "params": {"profile": "FLIGHT", "ttl_minutes": 1}},
    )
    # HOSTD turns the requested minute into an absolute deadline and publishes it.
    hostd_says(client, "FLIGHT", ttl_expires_at=clock.now + 60.0)
    report_in(client, *MISSION_SERVICES)
    wait_for_state(client, MissionState.NOMINAL.value)

    wait_out(clock, client, 70.0)
    assert until(
        lambda: any(a["profile"] == "HOSTED" for a in host_actions(client, "apply_profile"))
    ), "the expiry never asked for the default profile"

    # HOSTD does as it is told; the satellite goes idle rather than staying in a
    # mission it no longer has a profile for.
    hostd_says(client, "HOSTED")
    wait_for_state(client, MissionState.STANDBY.value)
    shut_down(service, thread)


def test_a_subsystem_dying_ungracefully_is_noticed_within_milliseconds(
    service_factory, monkeypatch
):
    # The MQTT last will, not three missed heartbeats.
    service, client, thread = launch(service_factory, monkeypatch)
    wait_for_state(client, MissionState.STANDBY.value)
    hostd_says(client, "DIAG")
    report_in(client, "adcs", "payload", "dhs", "comms")
    wait_for_state(client, MissionState.NOMINAL.value)

    client.deliver(TOPICS["heartbeat"], {"service": "adcs", "alive": False})
    wait_for_state(client, MissionState.SAFE.value)

    # A ground `recover` is the only thing that clears a fault the battery cannot
    # fix — and it only sticks once the subsystem is actually back.
    beat(client, "adcs")
    client.deliver(TOPICS["command"], {"command": "recover"})
    wait_for_state(client, MissionState.NOMINAL.value)
    shut_down(service, thread)


def test_coming_home_with_a_flat_pack_and_plugging_in_recovers(service_factory, monkeypatch):
    # The sequence CRITICAL-on-battery-alone got wrong: the pack is flat, the
    # satellite is plugged in at a desk, and powering the host off here would be
    # permanent — the X728 brings the Pi back when mains *returns*, and mains
    # never left.
    monkeypatch.setattr(obc_service, "CRITICAL_FLUSH_GRACE_SEC", 0.05)
    service, client, thread = launch(service_factory, monkeypatch)
    wait_for_state(client, MissionState.STANDBY.value)
    hostd_says(client, "FLIGHT")
    report_in(client, *MISSION_SERVICES)
    wait_for_state(client, MissionState.NOMINAL.value)

    battery(client, SAVED - 4.0)
    wait_for_state(client, MissionState.SAFE.value)
    battery(client, DYING + 2.0, external_power=True, charge_rate=3.2)

    wait_for_state(client, MissionState.NOMINAL.value)
    assert host_actions(client, "poweroff") == []
    shut_down(service, thread)


def test_a_reset_on_a_walk_puts_the_trip_back_together(service_factory, monkeypatch):
    # The scenario W11 exists for, through the real run loop: an hour into a
    # walk something reboots the Pi. HOSTD comes up in HOSTED, as it always
    # does, and what happens next is decided by a measurement — no mains — not
    # by the file that names the profile.
    clock = Clock()
    service, client, thread = launch(service_factory, monkeypatch, clock=clock)
    wait_for_state(client, MissionState.STANDBY.value)

    hostd_says(
        client,
        "HOSTED",
        boot=True,
        previous={
            "profile": "FLIGHT",
            "ttl_expires_at": clock.now + 540 * 60.0,
            "mission_label": "walk to work",
            "resume_count": 0,
        },
    )
    battery(client, 62.0, external_power=False)

    assert until(lambda: host_actions(client, "apply_profile"))
    asked = host_actions(client, "apply_profile")[-1]
    assert asked["profile"] == "FLIGHT"
    assert asked["resume"] is True
    # Under its own name, and with the remainder of the strap it already had.
    assert asked["mission_label"] == "walk to work"
    assert asked["ttl_minutes"] == 540.0

    # HOSTD obeys, the subsystems come back, and the recording resumes — with
    # the mission row saying it is the second half of a trip.
    hostd_says(client, "FLIGHT", ttl_expires_at=clock.now + 540 * 60.0)
    report_in(client, *MISSION_SERVICES)
    wait_for_state(client, MissionState.NOMINAL.value)
    status = client.payloads(TOPICS["obc_status"])[-1]
    assert status["mission_start_reason"] == "resume"
    assert status["mission_label"] == "walk to work"
    assert status["boot"]["resumed"] is True

    # And once it has outlived the reset that started it, the fence is cleared:
    # three resumes only mean a boot loop when all three were short.
    wait_out(clock, client, config.RESUME_SETTLE_SEC)
    assert until(lambda: host_actions(client, "clear_resume"))

    shut_down(service, thread)


def test_a_reset_at_a_desk_comes_up_reachable(service_factory, monkeypatch):
    # The other half, and the one the whole design protects: the same file, the
    # same interrupted FLIGHT, and mains present. It stays in HOSTED with SSH.
    clock = Clock()
    service, client, thread = launch(service_factory, monkeypatch, clock=clock)
    wait_for_state(client, MissionState.STANDBY.value)

    hostd_says(
        client,
        "HOSTED",
        boot=True,
        previous={
            "profile": "FLIGHT",
            "ttl_expires_at": clock.now + 540 * 60.0,
            "mission_label": "walk to work",
            "resume_count": 0,
        },
    )
    battery(client, 62.0, external_power=True)

    assert until(lambda: client.payloads(TOPICS["obc_status"])[-1].get("boot") is not None)
    assert host_actions(client, "apply_profile") == []
    assert client.payloads(TOPICS["obc_status"])[-1]["boot"]["reason"] == "mains"
    shut_down(service, thread)
