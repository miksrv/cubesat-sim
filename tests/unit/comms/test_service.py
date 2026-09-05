"""COMMS as a service: what it transmits, what it relays, and what it refuses.

Two properties are asserted here more often than anything else, because they are
the ones the rest of the system leans on:

* **A command relayed off the radio is indistinguishable from one published on
  the LAN.** That is what makes ``FLIGHT`` recoverable with Wi-Fi off and no
  SSH, and it only holds if COMMS re-publishes the bytes it received rather than
  something it re-composed.
* **The profile is the envelope and a ground command cannot widen it.** Every
  ``set_comms_config`` test below checks the forbidden direction as well as the
  permitted one, because a rule that is only tested where it says yes is not
  tested at all.

The radio is the mock HAL's loopback node: no serial port, and nothing here
reaches a network.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from cubesat.common import config
from cubesat.common import metrics as metrics_module
from cubesat.common import profiles as profiles_module
from cubesat.common.profiles import ProfileConfig
from cubesat.common.states import MissionState, Profile
from cubesat.common.topics import (
    KIND_MISSION,
    KIND_PHOTO,
    REASON_STATE,
    RETAINED,
    STATUS_ERROR,
    STATUS_SUCCESS,
    TOPICS,
)
from cubesat.comms.service import ACK_DELAY_SEC, MAX_PENDING_ACKS, CommsService
from cubesat.hal.interfaces import MAX_RADIO_MESSAGE_BYTES
from cubesat.hal.mock.radio import MockRadio

EPS = {"battery_percent": 78.24, "voltage": 3.9418, "external_power": False}
FIX = {"lat": 55.75583, "lon": 37.61733, "alt": 156.4, "fix": True, "satellites": 23}
ADCS = {"roll": 1.2, "yaw": 178.9, "gnss": FIX}
SCIENCE = {"temperature": 23.4, "humidity": 45.2, "pressure": 1013.0, "light": 412.0}

#: The shortest accepted uplink there is, and since 2026-09-03 the only shape
#: the radio takes at all: a bare compact verb.
RECOVER = "recover"


class Clock:
    """A monotonic clock a test can wind forward.

    Injected rather than patched because the beacon schedule is the behaviour
    under test and ten minutes of it should not cost ten minutes of suite.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def comms(service_factory):
    """A COMMS with a loopback radio."""

    def build(**kwargs):
        kwargs.setdefault("radio", MockRadio())
        service, client = service_factory(CommsService, **kwargs)
        return service, client

    return build


def obc(client, state=MissionState.NOMINAL, profile=Profile.FLIGHT, boot=None):
    """Deliver the retained message COMMS takes its envelope from."""
    client.deliver(
        TOPICS["obc_status"],
        {
            "status": state.value,
            "profile": profile.value,
            "cadence_scale": 1.0,
            "boot": boot,
        },
    )


def boot_report(resumed=True, previous="FLIGHT", reason=None, at=1.0):
    """OBC's verdict on how this run began — see obc/resume.py."""
    return {"at": at, "previous": previous, "resumed": resumed, "reason": reason}


def status(client):
    return client.last(TOPICS["comms_status"])


def command(client, name, **fields):
    client.deliver(TOPICS["command"], {"command": name, **fields})


def relayed(client):
    return [json.loads(p.payload) for p in client.published if p.topic == TOPICS["command"]]


# ── reporting in ────────────────────────────────────────────────────────────


def test_the_first_status_goes_out_before_any_tick(comms):
    # OBC's DEPLOY waits for this as evidence that the radio answered to the
    # process that owns it, inside a window shorter than a nominal cadence.
    service, client = comms()
    service.on_start()

    first = status(client)
    assert first["radio"]["present"] is True
    assert first["last_uplink"] is None


def test_the_status_is_retained_so_obc_sees_it_whenever_it_connects(comms):
    service, client = comms()
    service.on_start()
    assert TOPICS["comms_status"] in RETAINED
    assert client.published[-1].retain is True


def test_the_radio_is_probed_even_before_a_profile_has_said_anything(comms):
    # Whether the hardware is there is a different question from whether it is
    # allowed to transmit, and DEPLOY is asking the first one.
    service, client = comms()
    service.on_start()
    assert service._mesh.present is True
    assert service.beacon_enabled is False


def test_a_radio_that_never_answered_says_so_rather_than_staying_silent(comms):
    class Silent(MockRadio):
        def probe(self):
            return False

    service, client = comms(radio=Silent())
    service.on_start()
    assert status(client)["radio"]["present"] is False


def test_comms_subscribes_to_everything_the_packet_is_assembled_from(comms):
    service, client = comms()
    client.connect_ok()
    assert set(client.subscribed) == {
        TOPICS["obc_status"],
        TOPICS["command"],
        TOPICS["eps_status"],
        TOPICS["adcs_status"],
        TOPICS["payload_data"],
        # For the !photo ack alone, and the one payload deliberately not cached
        # whole: it carries the image.
        TOPICS["payload_photo"],
        TOPICS["dhs_status"],
    }


# ── the cadence ─────────────────────────────────────────────────────────────


def test_safe_keeps_waking_because_silent_must_not_mean_deaf(comms):
    # The correctness fix this file exists around. An interval of 0 here would
    # make the base class skip the tick entirely, and SAFE is reachable from
    # FLIGHT through a subsystem fault — where the radio is the only way in.
    service, client = comms()
    obc(client, state=MissionState.SAFE)
    assert service.interval > 0


#: How many times SAFE must listen for every time it transmits. A property of
#: the design rather than a number out of the config: listening is a memory read,
#: while transmitting is airtime on a shared duty-cycle-limited mesh and a
#: current spike on a failing battery. The shipped table is far more generous
#: than this floor — which is the point of asserting a floor.
MIN_SAFE_LISTEN_RATIO = 5


def test_safe_listens_far_more_often_than_it_talks(comms):
    """The ratio is the decision; neither number is repeated here.

    `SAFE` is reachable from `FLIGHT`, where the radio is the only way in, so the
    state that most needs a `recover` must not be the one that hears least. If
    the beacon interval is ever tuned below the wake cadence this fails — and it
    should. If it merely moves from 600 s to 300 s, it passes, because nothing
    was broken.
    """
    service, client = comms()
    obc(client, state=MissionState.SAFE)
    beacon = config.BEACON_INTERVALS[MissionState.SAFE.value]

    assert service.interval > 0, "SAFE must keep waking: quiet is not deaf"
    assert beacon >= service.interval * MIN_SAFE_LISTEN_RATIO


def test_the_cadence_follows_the_mission_state(comms):
    # Read from the shipped table, not repeated: the numbers are tuning, the
    # following is the behaviour. See the DHS test for the same treatment.
    service, client = comms()
    table = config.CADENCE["comms"]
    for state in (MissionState.NOMINAL, MissionState.LOW_POWER):
        obc(client, state=state)
        assert service.interval == table[state.value]
    obc(client, state=MissionState.DEPLOY)
    # DEPLOY has to report in well inside OBC's bring-up window.
    assert service.interval == 2


def test_a_satellite_in_safe_can_still_be_recovered_over_the_radio(comms):
    # The whole point. A `recover` typed into a phone has to reach the bus from
    # the state that most needs it, on a profile with no Wi-Fi and no SSH.
    service, client = comms()
    obc(client, state=MissionState.SAFE)
    service._mesh._radio.inject(RECOVER)

    service.tick()

    assert relayed(client) == [{"command": "recover"}]


# ── the envelope ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "listening"),
    [
        (Profile.DEMO, True),
        (Profile.EXPO, True),
        (Profile.FLIGHT, True),
        (Profile.HOSTED, True),
        (Profile.DIAG, True),
        # The only deaf one: MAINTENANCE needs /dev/serial0 free to reflash the
        # Heltec. DIAG was deaf too until it became a rehearsal of FLIGHT
        # (2026-09-01) — and the beacon is one of the things being rehearsed.
        (Profile.MAINTENANCE, False),
    ],
)
def test_the_active_profile_decides_whether_the_radio_may_run(comms, profile, listening):
    # The envelope: whether the radio is part of the mission at all. No ground
    # command can widen this.
    service, client = comms()
    obc(client, profile=profile)
    assert service.lora_listening is listening


@pytest.mark.parametrize(
    ("profile", "transmitting"),
    [
        # Away from its operator: the beacon is the only thing saying it is alive.
        (Profile.FLIGHT, True),
        (Profile.DIAG, True),
        # On the desk, dashboard open, operator a metre away: quiet by default.
        (Profile.DEMO, False),
        (Profile.EXPO, False),
        # Permitted and quiet for a different reason — STANDBY has no row in the
        # beacon table, so HOSTED says nothing on its own schedule regardless.
        (Profile.HOSTED, True),
        (Profile.MAINTENANCE, False),
    ],
)
def test_the_profile_decides_whether_the_beacon_starts_on(comms, profile, transmitting):
    # Inside the envelope, and only the starting state: `lora on` moves it either
    # way afterwards, for every profile whose envelope permits it.
    service, client = comms()
    obc(client, profile=profile)
    assert service.beacon_enabled is transmitting


def test_a_quiet_profile_can_be_asked_to_beacon(comms):
    # The whole point of quiet-by-default: over the radio (which is still being
    # listened to), over SSH, or from the dashboard console — all three end at
    # this one command.
    service, client = comms()
    obc(client, profile=Profile.DEMO)
    assert service.beacon_enabled is False

    command(client, "set_comms_config", params={"beacon_enabled": True})
    assert service.beacon_enabled is True


def test_entering_a_profile_resets_the_beacon_to_that_profile_s_default(comms):
    # The trip ends: FLIGHT beacons, and switching to DEMO on arrival stops it
    # without anybody saying so. Written this way round on purpose — a request
    # carried over from the trip would make "quiet in DEMO" true only until the
    # first time anybody ever turned the beacon on.
    service, client = comms()
    obc(client, profile=Profile.FLIGHT)
    assert service.beacon_enabled is True

    obc(client, profile=Profile.DEMO)
    assert service.beacon_enabled is False
    # Still listening, which is how the next trip is started.
    assert service.lora_listening is True


def test_before_any_profile_is_known_nothing_is_permitted(comms):
    # Assuming the permissive envelope would transmit under a profile that
    # forbids transmitting, which in MAINTENANCE is the whole point.
    service, client = comms()
    assert service.beacon_enabled is False


def test_a_profile_this_deployment_does_not_define_permits_nothing(comms, caplog):
    full = profiles_module.load()
    trimmed = ProfileConfig(
        default=full.default, profiles={Profile.HOSTED: full.profiles[Profile.HOSTED]}
    )
    service, client = comms(profiles=trimmed)
    with caplog.at_level(logging.WARNING):
        obc(client, profile=Profile.FLIGHT)

    assert service.beacon_enabled is False
    assert "no downlink permitted" in caplog.text


def test_the_envelope_is_resolved_once_per_profile_and_not_once_per_message(comms, caplog):
    service, client = comms()
    with caplog.at_level(logging.INFO):
        obc(client)
        obc(client)
        obc(client, state=MissionState.LOW_POWER)
    assert caplog.text.count("permits lora=") == 1


# ── the beacon ──────────────────────────────────────────────────────────────


def test_a_tick_transmits_one_beacon_assembled_from_the_caches(comms):
    service, client = comms()
    radio = service._mesh._radio
    obc(client)
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["adcs_status"], ADCS)
    client.deliver(TOPICS["dhs_status"], {"recording": True, "mission": {"id": 42}})

    service.tick()

    assert len(radio.sent) == 1
    line = radio.sent[0]
    assert line.startswith("CSAT t=")
    assert " st=NOMINAL pr=FLIGHT b=78.2" in line
    assert " lat=55.7558 lon=37.6173" in line
    assert line.endswith(" m=42")
    assert len(line.encode("utf-8")) <= MAX_RADIO_MESSAGE_BYTES


def test_a_profile_that_forbids_lora_transmits_nothing(comms):
    service, client = comms()
    obc(client, profile=Profile.MAINTENANCE)
    service.tick()
    assert service._mesh._radio.sent == []


def test_hosted_listens_in_standby_and_never_beacons(comms):
    # The reachability floor. Every boot lands in HOSTED — a reboot in the
    # field included, where there is no home network and no SSH — so the radio
    # must be a way in. It listens and relays; STANDBY has no row in the beacon
    # table, so it transmits nothing on its own.
    service, client = comms()
    obc(client, state=MissionState.STANDBY, profile=Profile.HOSTED)
    service._mesh._radio.inject("profile flight")

    service.tick()

    assert relayed(client) == [{"command": "set_profile", "params": {"profile": "FLIGHT"}}]
    assert service._mesh._radio.sent == []


def test_a_satellite_that_has_heard_from_nobody_still_beacons_that_it_is_alive(comms):
    service, client = comms()
    obc(client)
    service.tick()
    assert service._mesh._radio.sent == ["CSAT " + service._mesh._radio.sent[0].split(" ", 1)[1]]
    assert " st=NOMINAL" in service._mesh._radio.sent[0]


def test_a_transmit_that_fails_costs_the_beacon_and_not_the_service(comms, caplog):
    class Broken(MockRadio):
        def send(self, payload):
            raise OSError("device disconnected")

    service, client = comms(radio=Broken())
    obc(client)
    with caplog.at_level(logging.ERROR):
        service.tick()

    assert service.running is True
    # And the retained status stops claiming a working radio.
    assert status(client)["radio"]["present"] is False


# ── the beacon schedule ─────────────────────────────────────────────────────


def test_the_first_wake_of_a_permitted_state_transmits_immediately(comms):
    # A satellite that has just come up should say so, rather than waiting out
    # an interval nothing has started yet.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service.tick()
    assert len(service._mesh._radio.sent) == 1


def test_a_wake_inside_the_interval_listens_without_transmitting(comms):
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service.tick()

    clock.advance(config.BEACON_INTERVALS["NOMINAL"] - 1)
    service.tick()
    assert len(service._mesh._radio.sent) == 1

    clock.advance(1)
    service.tick()
    assert len(service._mesh._radio.sent) == 2


def test_safe_beacons_every_ten_minutes_and_not_every_minute(comms):
    # It still beacons: a satellite that goes quiet exactly when something is
    # wrong is a satellite nobody can help. It just talks rarely.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client, state=MissionState.SAFE)
    service.tick()
    sent_at_first_wake = len(service._mesh._radio.sent)

    # Nine wakes at the SAFE cadence: nine minutes of listening, no talking.
    for _ in range(9):
        clock.advance(service.interval)
        service.tick()
    assert len(service._mesh._radio.sent) == sent_at_first_wake

    clock.advance(service.interval)
    service.tick()
    assert len(service._mesh._radio.sent) == sent_at_first_wake + 1


def test_a_state_change_re_evaluates_the_interval_without_waiting_out_the_old_one(comms):
    # A satellite recovering from SAFE to NOMINAL must not be unheard from for
    # the remains of a ten-minute interval it is no longer in.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client, state=MissionState.SAFE)
    service.tick()
    assert len(service._mesh._radio.sent) == 1

    clock.advance(config.BEACON_INTERVALS["NOMINAL"] + 1)
    service.tick()
    assert len(service._mesh._radio.sent) == 1, "SAFE should still be waiting"

    obc(client, state=MissionState.NOMINAL)
    service.tick()
    assert len(service._mesh._radio.sent) == 2


def test_descending_into_low_power_slows_the_beacon_down_at_once(comms):
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service.tick()

    obc(client, state=MissionState.LOW_POWER)
    clock.advance(config.BEACON_INTERVALS["NOMINAL"] + 1)
    service.tick()
    assert len(service._mesh._radio.sent) == 1


@pytest.mark.parametrize(
    "state", [MissionState.BOOT, MissionState.STANDBY, MissionState.DEPLOY,
              MissionState.CRITICAL]
)
def test_a_state_the_beacon_table_does_not_name_does_not_transmit(comms, state):
    # A refusal rather than a default: inventing an interval would be inventing
    # a transmission policy for a state nobody wrote one for.
    service, client = comms()
    obc(client, state=state)
    service.tick()
    assert service._mesh._radio.sent == []
    assert state.value not in config.BEACON_INTERVALS


def test_before_obc_has_said_anything_nothing_is_transmitted(comms):
    service, client = comms()
    service.tick()
    assert service._mesh._radio.sent == []


def test_a_mission_state_this_build_cannot_name_transmits_nothing_but_still_listens(
    comms, caplog
):
    # A newer OBC, or a garbled retained message. The profile still permits the
    # radio, so the inbox is polled — but nothing here knows how often to talk,
    # and guessing a rate is guessing at somebody else's airtime budget.
    service, client = comms()
    with caplog.at_level(logging.WARNING):
        client.deliver(
            TOPICS["obc_status"], {"status": "TRANSLUNAR", "profile": Profile.FLIGHT.value}
        )
    service._mesh._radio.inject(RECOVER)

    service.tick()

    assert service.beacon_enabled is True
    assert service._mesh._radio.sent == []
    assert relayed(client) == [{"command": "recover"}]
    assert "unknown mission state" in caplog.text


def test_a_transmit_that_failed_is_retried_on_the_next_wake(comms):
    # A send that never left spent no airtime, so it has no claim on the budget.
    # Charging it would leave a radio that came back idle for ten more minutes.
    failures = [True]

    class Flaky(MockRadio):
        def send(self, payload):
            if failures:
                failures.pop()
                raise OSError("device disconnected")
            super().send(payload)

    clock = Clock()
    service, client = comms(radio=Flaky(), clock=clock)
    obc(client, state=MissionState.SAFE)
    service.tick()
    assert service._mesh._radio.sent == []

    clock.advance(service.interval)
    service.tick()
    assert len(service._mesh._radio.sent) == 1


# ── the boot beacon ─────────────────────────────────────────────────────────
#
# The counterpart of the going-down beacon: that one explains a disappearance,
# this one explains a reappearance. Both are gated on the profile rather than on
# the beacon flag, for the same reason.


def test_a_resumed_trip_is_announced_once(comms):
    service, client = comms()
    obc(client, boot=boot_report())

    assert len(service._mesh._radio.sent) == 1
    line = service._mesh._radio.sent[0]
    assert " boot=FLIGHT rs=1" in line
    assert "why=" not in line


def test_a_refusal_says_why_rather_than_nothing(comms):
    # A satellite that silently declined to resume is indistinguishable from one
    # that never woke up.
    service, client = comms()
    obc(client, boot=boot_report(resumed=False, reason="mains"))
    assert " boot=FLIGHT rs=0 why=mains" in service._mesh._radio.sent[0]


def test_an_ordinary_reboot_is_not_worth_the_airtime(comms):
    # Nothing resumable was interrupted, so there is nothing to say.
    service, client = comms()
    obc(client, boot=boot_report(resumed=False, previous=None, reason="profile"))
    assert service._mesh._radio.sent == []


def test_the_retained_status_redelivered_does_not_repeat_it(comms):
    # obc_status is retained, so a reconnect replays it — and a satellite that
    # re-announced its own boot on every reconnect would be spending a shared
    # mesh's airtime on old news.
    service, client = comms()
    report = boot_report()
    obc(client, boot=report)
    obc(client, boot=report)
    obc(client, state=MissionState.LOW_POWER, boot=report)
    assert len(service._mesh._radio.sent) == 1


def test_a_second_boot_report_is_a_second_announcement(comms):
    service, client = comms()
    obc(client, boot=boot_report(at=1.0))
    obc(client, boot=boot_report(at=2.0, resumed=False, reason="loop"))
    assert len(service._mesh._radio.sent) == 2


def test_a_silenced_transmitter_still_reports_its_boot(comms):
    # Same rule as the going-down beacon and the acks: `beacon off` rations the
    # schedule, not the messages that explain what the satellite is doing.
    service, client = comms()
    obc(client)
    command(client, "set_comms_config", params={"beacon_enabled": False})
    service._mesh._radio.sent.clear()

    obc(client, boot=boot_report())

    assert service.beacon_enabled is False
    assert len(service._mesh._radio.sent) == 1


def test_a_profile_that_forbids_the_radio_says_nothing(comms):
    service, client = comms()
    obc(client, profile=Profile.MAINTENANCE, boot=boot_report())
    assert service._mesh._radio.sent == []


def test_the_boot_beacon_counts_against_the_airtime_budget(comms):
    # It leaves like any other transmission, so the scheduled beacon does not
    # follow it seconds later saying the same thing more slowly.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client, boot=boot_report())
    sent = len(service._mesh._radio.sent)

    service.tick()

    assert len(service._mesh._radio.sent) == sent


def test_a_boot_beacon_that_will_not_transmit_is_logged_and_stepped_over(comms, caplog):
    # A radio that is not there is not a reason to fail to come up: the trip has
    # already resumed by the time this line is composed.
    class Broken(MockRadio):
        def send(self, payload):
            raise OSError("device disconnected")

    service, client = comms(radio=Broken())
    with caplog.at_level(logging.WARNING):
        obc(client, boot=boot_report())

    assert service.running is True
    assert "could not transmit the boot beacon" in caplog.text


def test_a_boot_field_that_is_not_a_report_is_ignored(comms):
    service, client = comms()
    for junk in (None, "resumed", 7, []):
        obc(client, boot=junk)
    assert service._mesh._radio.sent == []


# ── the going-down beacon ───────────────────────────────────────────────────


def test_entering_critical_transmits_once_immediately(comms):
    # On the thread the state change arrived on, not at the next wake — which in
    # LOW_POWER is a minute away and in CRITICAL never comes, because the host
    # will have powered off by then.
    service, client = comms()
    obc(client)
    client.deliver(TOPICS["eps_status"], {"battery_percent": 8.1, "voltage": 3.2})
    service._mesh._radio.sent.clear()

    obc(client, state=MissionState.CRITICAL)

    assert len(service._mesh._radio.sent) == 1
    line = service._mesh._radio.sent[0]
    assert " st=CRITICAL down=1 " in line
    # Where it was and what the battery was doing, which is the content that
    # makes the message worth the airtime.
    assert " b=8.1" in line


def test_a_deploy_the_service_survived_republishes_the_status(comms):
    # COMMS runs across every profile switch by design, so the DEPLOY after one
    # finds a service whose status has not changed since its own bring-up — and
    # a status published only on change would leave OBC's self-test with no
    # fresh evidence at all. The very first hardware run failed exactly here.
    service, client = comms()
    obc(client)  # the steady state the switch is made from
    before = len(client.payloads(TOPICS["comms_status"]))

    obc(client, state=MissionState.DEPLOY)

    assert len(client.payloads(TOPICS["comms_status"])) == before + 1


def test_critical_never_gets_a_repeating_schedule(comms):
    # There is nothing to repeat in a state that lasts ten seconds, so CRITICAL
    # stays out of the beacon table and the wake loop transmits nothing there.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    obc(client, state=MissionState.CRITICAL)
    sent = len(service._mesh._radio.sent)

    for _ in range(5):
        clock.advance(3600)
        service.tick()

    assert len(service._mesh._radio.sent) == sent
    assert MissionState.CRITICAL.value not in config.BEACON_INTERVALS


def test_a_silenced_transmitter_still_says_it_is_going_down(comms):
    # A runtime flag somebody set an hour ago must not be able to silence the
    # one message that explains a disappearance.
    service, client = comms()
    obc(client)
    command(client, "set_comms_config", params={"beacon_enabled": False})
    service._mesh._radio.sent.clear()

    obc(client, state=MissionState.CRITICAL)

    assert service.beacon_enabled is False
    assert len(service._mesh._radio.sent) == 1
    assert "down=1" in service._mesh._radio.sent[0]


def test_a_profile_with_no_radio_says_nothing_even_on_the_way_down(comms):
    # There the radio is not merely quiet, it is not part of the mission.
    service, client = comms()
    obc(client, profile=Profile.MAINTENANCE)
    obc(client, state=MissionState.CRITICAL, profile=Profile.MAINTENANCE)
    assert service._mesh._radio.sent == []


@pytest.mark.parametrize(
    "state", [MissionState.SAFE, MissionState.LOW_POWER, MissionState.NOMINAL]
)
def test_no_other_state_change_sends_one(comms, state):
    service, client = comms()
    obc(client, state=MissionState.DEPLOY)
    service._mesh._radio.sent.clear()
    obc(client, state=state)
    assert service._mesh._radio.sent == []


def test_a_going_down_beacon_that_will_not_transmit_is_logged_and_stepped_over(comms, caplog):
    # Transmit current spikes can brown out the Heltec, and a pack at 8 % is
    # where that is likeliest. DHS closing its mission cleanly matters more than
    # this being heard, and OBC's flush grace must not be spent on a radio.
    class Broken(MockRadio):
        def send(self, payload):
            raise OSError("device disconnected")

    service, client = comms(radio=Broken())
    obc(client)
    with caplog.at_level(logging.WARNING):
        obc(client, state=MissionState.CRITICAL)

    assert service.running is True
    assert "powering off unheard" in caplog.text
    # And the retained status stops claiming a working radio before the host
    # goes, which is the last thing the ground will see.
    assert status(client)["radio"]["present"] is False


def test_the_going_down_beacon_counts_against_the_airtime_budget(comms):
    # One rule for what spends airtime, rather than a second unstated one.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    obc(client, state=MissionState.CRITICAL)
    assert service._last_beacon == clock.now


# ── reconnecting ────────────────────────────────────────────────────────────


def test_a_reconnect_republishes_the_status_even_though_nothing_changed(comms):
    # A broker restart takes every retained message with it. Without this, OBC
    # would find no comms_status and DEPLOY no evidence — and a healthy
    # satellite would fail its own bring-up because mosquitto bounced.
    service, client = comms()
    service.on_start()
    before = len(client.payloads(TOPICS["comms_status"]))

    client.connect_ok()

    assert len(client.payloads(TOPICS["comms_status"])) == before + 1
    assert status(client)["radio"]["present"] is True


# ── the uplink ──────────────────────────────────────────────────────────────


def test_a_command_off_the_radio_reaches_the_bus_as_the_canonical_json(comms):
    # The load-bearing property: nothing downstream knows or cares which channel
    # a command arrived on, so FLIGHT is recoverable with no SSH. What crosses
    # the air is the compact verb; what reaches the bus is what every other
    # client publishes.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("profile hosted")

    service.tick()

    published = [p for p in client.published if p.topic == TOPICS["command"]]
    assert relayed(client) == [{"command": "set_profile", "params": {"profile": "HOSTED"}}]
    assert published[0].retain is False


def test_json_typed_into_a_phone_is_not_a_command_over_the_radio(comms, caplog):
    """Removed on 2026-09-03: the radio takes compact verbs and nothing else.

    It used to be relayed verbatim, which meant a second parser on the air with
    its own idea of what a command is. What it becomes is an uplink that did not
    parse — logged, because behind the private-channel filter the sender holds
    the key and is likelier an operator reaching for last week's spelling than a
    stranger, and never answered, because a reply is what `!` buys.
    """
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject('{"command":"set_profile","params":{"profile":"HOSTED"}}')
    with caplog.at_level(logging.WARNING):
        service.tick()

    assert relayed(client) == []
    assert "not a command" in caplog.text
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()
    assert sent_replies(service) == []


def test_a_json_uplink_is_still_kept_on_the_record(comms):
    # It reached us on our own channel, so it stays in the radio log even though
    # nothing acts on it: a command somebody typed in the wrong spelling is
    # exactly the traffic worth being able to look back at.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject('{"command": "recover"}')
    service.tick()

    received = [e for e in radio_events(client) if e["direction"] == "rx"]
    assert [e["text"] for e in received] == ['{"command": "recover"}']


def test_an_uplink_is_relayed_before_the_beacon_spends_the_airtime(comms, monkeypatch):
    # A command that has been sitting in the radio's inbox should be acted on
    # this cycle, not after the transmission that is about to cost the airtime.
    order: list[str] = []

    class Noting(MockRadio):
        def send(self, payload):
            order.append("beacon")
            super().send(payload)

    service, client = comms(radio=Noting())
    obc(client)
    monkeypatch.setattr(service, "publish_raw", lambda *_a, **_k: order.append("relay"))
    service._mesh._radio.inject(RECOVER)

    service.tick()

    assert order == ["relay", "beacon"]


def test_mesh_chatter_is_dropped_rather_than_relayed(comms, caplog):
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("anyone out there?")
    with caplog.at_level(logging.WARNING):
        service.tick()

    assert not [p for p in client.published if p.topic == TOPICS["command"]]
    assert "dropping an uplink" in caplog.text


def test_an_uplink_refreshes_the_status_so_the_ground_can_see_it_landed(comms):
    service, client = comms()
    obc(client)
    assert status(client)["last_uplink"] is None
    service._mesh._radio.inject(RECOVER)
    service.tick()
    assert status(client)["last_uplink"] is not None


def sent_replies(service):
    return [line for line in service._mesh._radio.sent if " re=" in line]


def test_an_accepted_uplink_is_answered_ten_seconds_later(comms):
    # The ack rule: one out-of-schedule beacon carrying re=<command>, delayed so
    # the st= and pr= fields it carries are the outcome, not the intention.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject(RECOVER)
    service.tick()
    assert sent_replies(service) == []  # not yet: the effect has to land first

    clock.advance(10.1)
    service.tick()

    replies = sent_replies(service)
    assert len(replies) == 1
    assert " re=recover" in replies[0]


def test_a_compact_line_is_relayed_as_canonical_json(comms):
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("!profile demo")
    service.tick()
    assert relayed(client) == [{"command": "set_profile", "params": {"profile": "DEMO"}}]


def test_the_bare_spelling_works_over_the_radio_too(comms):
    # The same line the dashboard console takes: one command language,
    # whichever way the satellite is reached.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("profile demo")
    service.tick()
    assert relayed(client) == [{"command": "set_profile", "params": {"profile": "DEMO"}}]


def test_a_bare_query_is_answered_like_a_banged_one(comms):
    service, client = comms()
    obc(client, state=MissionState.STANDBY, profile=Profile.HOSTED)
    service._mesh._radio.inject("ping")
    service.tick()

    assert relayed(client) == []
    replies = sent_replies(service)
    assert len(replies) == 1
    assert " re=ping" in replies[0]


def test_chat_containing_a_command_verb_is_still_chat(comms, caplog):
    # "photo of the pad looks great" starts with a verb the table knows, and
    # must neither relay nor earn an err=unknown reply: only a `!` declares
    # intent, and answering stray sentences would spend the transmission
    # budget on other people's conversations.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject("photo of the pad looks great")
    with caplog.at_level(logging.WARNING):
        service.tick()

    assert relayed(client) == []
    clock.advance(10.1)
    service.tick()
    assert sent_replies(service) == []


def test_ping_is_answered_immediately_and_never_relayed(comms):
    # Proof of life on demand — and COMMS is the thing being asked, so there is
    # nothing to relay and nothing to wait for.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client, state=MissionState.STANDBY, profile=Profile.HOSTED)
    service._mesh._radio.inject("!ping")
    service.tick()

    assert relayed(client) == []
    replies = sent_replies(service)
    assert len(replies) == 1
    assert " re=ping" in replies[0]
    # STANDBY has no row in the beacon table, and that is the point: the ack
    # needs only the profile's permission, or a satellite listening in HOSTED
    # could hear a ping and not be allowed to answer it.


def test_a_line_nobody_wrote_is_answered_not_dropped(comms, caplog):
    # The sender is a person in a field wondering why nothing happened.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject("!launch")
    with caplog.at_level(logging.WARNING):
        service.tick()
    assert relayed(client) == []
    assert "unknown compact uplink" in caplog.text

    clock.advance(10.1)
    service.tick()

    replies = sent_replies(service)
    assert len(replies) == 1
    assert " re=? ok=0 err=unknown" in replies[0]


def test_sys_answers_immediately_with_the_hosts_own_health(comms):
    # Local psutil reads: no bus time, no cache, no staleness to admit to.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("!sys")
    service.tick()

    replies = sent_replies(service)
    assert len(replies) == 1
    line = replies[0]
    assert " re=sys " in line
    for key in ("cpu=", "ram=", "disk=", "up="):
        assert f" {key}" in line
    assert line.split(" up=")[1].split()[0].endswith("h")


def test_env_answers_from_the_science_cache_with_its_age(comms):
    service, client = comms()
    obc(client)
    client.deliver(
        TOPICS["payload_data"],
        {"timestamp": time.time() - 30, **SCIENCE},
    )
    service._mesh._radio.inject("!env")
    service.tick()

    line = sent_replies(service)[0]
    assert " re=env " in line
    assert " tc=23.4 " in line
    assert " rh=45 " in line
    assert " hpa=1013 " in line
    assert " lux=412 " in line
    age = int(line.split(" age=")[1].split()[0])
    assert 29 <= age <= 32


def test_a_query_with_an_empty_cache_says_nodata_rather_than_zeros(comms):
    # PAYLOAD never reported: a line of zeros would be a measured-looking lie.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("!env")
    service.tick()
    assert " re=env ok=0 err=nodata" in sent_replies(service)[0]


def test_sys_reports_the_cpu_temperature_where_the_host_exposes_one(comms, monkeypatch):
    # The suite runs on machines without a thermal sensor psutil can see, so
    # the tc= branch needs the reading pinned rather than hoped for.
    monkeypatch.setattr(
        metrics_module,
        "collect",
        lambda path: metrics_module.SystemMetrics(
            cpu_percent=12.0,
            ram_percent=40.0,
            swap_percent=0.0,
            disk_percent=55.0,
            uptime_seconds=7200.0,
            cpu_temperature=51.24,
        ),
    )
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("!sys")
    service.tick()
    assert " tc=51.2" in sent_replies(service)[0]


def test_pos_with_an_empty_cache_says_nodata_rather_than_zeros(comms):
    # ADCS never reported: 0.0000, 0.0000 is a real place in the Gulf of
    # Guinea, which is exactly the measured-looking lie nodata exists to avoid.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("!pos")
    service.tick()
    assert " re=pos ok=0 err=nodata" in sent_replies(service)[0]


def test_mission_with_no_mission_open_says_nodata(comms):
    # DHS never reported a mission: m=0 would read as a recorded session.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("!mission")
    service.tick()
    assert " re=mission ok=0 err=nodata" in sent_replies(service)[0]


def test_a_cache_with_no_timestamp_reports_an_unknown_age(comms):
    # age=? rather than a fabricated zero: an age that cannot be computed is
    # withheld the same way a value that cannot be justified is.
    service, client = comms()
    obc(client)
    client.deliver(TOPICS["payload_data"], dict(SCIENCE))
    service._mesh._radio.inject("!env")
    service.tick()
    assert " age=? " in sent_replies(service)[0] + " "


def test_pos_reports_a_fixless_position_with_its_age(comms):
    # The lost-satellite query: unlike the scheduled beacon, a stale or
    # fixless coordinate is reported, because age= and fix= say exactly how
    # much to trust it.
    service, client = comms()
    obc(client)
    stale = {"timestamp": time.time() - 120, "gnss": {**FIX, "fix": False}}
    client.deliver(TOPICS["adcs_status"], stale)
    service._mesh._radio.inject("!pos")
    service.tick()

    line = sent_replies(service)[0]
    assert " re=pos " in line
    assert " lat=55.7558 " in line
    assert " fix=0 " in line
    assert 118 <= int(line.split(" age=")[1].split()[0]) <= 123
    # And the reply's coordinate survives even though the schedule would have
    # withheld a fixless position: the reply fields are never dropped.


def test_mission_answers_with_the_id_and_rows_dhs_reported(comms):
    service, client = comms()
    obc(client)
    client.deliver(TOPICS["dhs_status"], {"mission": {"id": 7, "rows": 42}})
    service._mesh._radio.inject("!mission")
    service.tick()
    line = sent_replies(service)[0]
    assert " re=mission " in line
    assert " m=7 " in line or line.endswith(" m=7")
    assert " rows=42" in line


def test_two_commands_with_an_effect_get_two_replies(comms):
    # The single pending slot lost the first of any two commands sent inside ten
    # seconds — silently, which for `photo` is a picture taken and never
    # mentioned. Two events are two answers: an operator who gets one
    # confirmation for two commands cannot tell which one it is about.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject(RECOVER)
    service._mesh._radio.inject("safe")
    service.tick()
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()
    service.tick()

    replies = sent_replies(service)
    assert [" re=recover" in replies[0], " re=safe" in replies[1]] == [True, True]


def test_two_questions_in_a_row_are_answered_once(comms):
    # A query is a snapshot of the present, so the fresh answer answers the
    # older question too. Five near-identical telemetry lines are airtime spent
    # saying the same thing on a shared mesh.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("!sys")
    service._mesh._radio.inject("!ping")
    service.tick()
    service.tick()

    replies = sent_replies(service)
    assert len(replies) == 1
    assert " re=ping" in replies[0]


def test_only_one_reply_leaves_per_wake(comms):
    # The queue exists so a reply is late rather than lost, not so a burst of
    # commands buys a burst of transmissions on a shared duty-cycle-limited mesh.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject(RECOVER)
    service._mesh._radio.inject("safe")
    service.tick()
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()
    assert len(sent_replies(service)) == 1


def test_a_question_asked_while_an_ack_ripens_is_not_made_to_wait(comms):
    # The first *due* reply goes, not strictly the first: `!ping` answers about
    # the present, and a photo ack still counting out its ten seconds is not a
    # reason to hold the present back.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject(RECOVER)
    service.tick()
    service._mesh._radio.inject("!ping")
    service.tick()

    replies = sent_replies(service)
    assert len(replies) == 1
    assert " re=ping" in replies[0]


def test_the_reply_queue_does_not_grow_without_a_bound(comms, caplog):
    # A flooded channel must not grow this list, and the oldest is what goes:
    # at one reply per wake an answer that far down the queue would describe a
    # satellite that has moved on by the time it left.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    for index in range(MAX_PENDING_ACKS + 3):
        # Distinct commands with an effect, so none of them collapse into
        # another: only a query replaces an unsent query.
        service._mesh._radio.inject("restart " + "a" * (index + 1))
    with caplog.at_level(logging.WARNING):
        service.tick()

    assert len(service._pending_acks) == MAX_PENDING_ACKS
    assert "dropping the oldest unsent reply" in caplog.text


def test_a_satellite_told_to_be_quiet_still_answers_the_question_it_was_asked(comms):
    """An answer is not a beacon (2026-09-03).

    `beacon off` rations the schedule; it must not swallow the reply to a
    command somebody just sent. Met three times inside five minutes on
    2026-09-02 in `DEMO`, where quiet is the profile's own default — the
    commands worked and the phone saw a satellite that was not listening.
    """
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    command(client, "set_comms_config", params={"beacon_enabled": False})
    service._mesh._radio.inject(RECOVER)
    service.tick()
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()

    assert relayed(client) == [{"command": "recover"}]
    assert len(sent_replies(service)) == 1
    # And nothing else: the schedule really is off.
    assert len(service._mesh._radio.sent) == 1


def test_beacon_off_confirms_itself(comms):
    """The worst of the three, because its two outcomes look identical.

    "The transmitter is off now" and "the command never arrived" are the same
    silence, and the operator is the one person who cannot tell them apart.
    """
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client, profile=Profile.FLIGHT)
    service._mesh._radio.inject("beacon off")
    service.tick()
    # The relay goes onto the bus and comes back around the loop as an ordinary
    # MQTT message, which is how a command addressed to COMMS reaches COMMS.
    client.deliver(TOPICS["command"], relayed(client)[0])
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()

    assert service.beacon_enabled is False
    replies = sent_replies(service)
    assert len(replies) == 1
    # And it names the verb that was typed, not the command it became.
    assert " re=beacon" in replies[0]


def test_a_reply_goes_out_in_a_state_with_no_beacon_row(comms):
    # HOSTED sits in STANDBY, which the beacon table does not name, so the
    # satellite never transmits on its own schedule there. It is still every
    # boot's landing profile and a field reboot's only way back in, so a command
    # sent to it has to be answered.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client, state=MissionState.STANDBY, profile=Profile.HOSTED)
    assert config.BEACON_INTERVALS.get(MissionState.STANDBY.value) is None

    service._mesh._radio.inject(RECOVER)
    service.tick()
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()

    assert len(sent_replies(service)) == 1


def test_a_profile_that_forbids_the_radio_answers_nothing_and_keeps_nothing(comms):
    # Where "always" stops. The queue is discarded rather than held: those
    # replies would be answering commands that arrived under another profile.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client, profile=Profile.FLIGHT)
    service._mesh._radio.inject(RECOVER)
    service.tick()
    obc(client, profile=Profile.MAINTENANCE)
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()

    assert sent_replies(service) == []
    assert service._pending_acks == []


def test_a_profile_with_no_radio_polls_nothing(comms):
    # There the radio is not merely quiet, it is not part of the mission.
    service, client = comms()
    obc(client, profile=Profile.MAINTENANCE)
    service._mesh._radio.inject(RECOVER)
    service.tick()
    assert relayed(client) == []


def test_silencing_the_transmitter_leaves_the_receiver_listening(comms):
    # Point B: otherwise `set_comms_config {"beacon_enabled": false}` sent over
    # the radio would be a one-way door with the key on the far side of it.
    service, client = comms()
    obc(client)
    command(client, "set_comms_config", params={"beacon_enabled": False})
    service._mesh._radio.inject("beacon on")

    service.tick()

    assert service._mesh._radio.sent == []
    assert relayed(client)[0]["params"] == {"beacon_enabled": True}
    # And the status says which of the two states this is.
    assert status(client)["beacon_enabled"] is False
    assert status(client)["lora_listening"] is True


# ── get_telemetry ───────────────────────────────────────────────────────────


def test_get_telemetry_answers_from_the_cache_with_the_request_id(comms):
    service, client = comms()
    obc(client)
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["adcs_status"], ADCS)
    client.deliver(TOPICS["payload_data"], SCIENCE)
    client.deliver(TOPICS["dhs_status"], {"mission": {"id": 42}})

    command(client, "get_telemetry", request_id="req_007")

    answer = client.last(TOPICS["comms_data"])
    assert answer["request_id"] == "req_007"
    assert answer["obc_state"] == "NOMINAL"
    assert answer["profile"] == "FLIGHT"
    assert answer["mission_id"] == 42
    assert answer["eps"] == EPS
    assert answer["adcs"] == ADCS
    assert answer["payload"] == SCIENCE


def test_get_telemetry_is_answered_whatever_the_state_and_whatever_is_switched_off(comms):
    # A question asked over MQTT and answered over MQTT. Refusing it because the
    # radio is off would make the one diagnostic that always works conditional
    # on the thing being diagnosed.
    service, client = comms()
    obc(client, state=MissionState.SAFE, profile=Profile.MAINTENANCE)
    command(client, "get_telemetry", request_id="req_007")
    assert client.last(TOPICS["comms_data"])["request_id"] == "req_007"


def test_a_request_with_no_id_is_answered_with_a_null_one(comms):
    service, client = comms()
    command(client, "get_telemetry")
    assert client.last(TOPICS["comms_data"])["request_id"] is None


def test_an_empty_cache_answers_with_empty_objects_and_not_with_nulls(comms):
    service, client = comms()
    command(client, "get_telemetry")
    answer = client.last(TOPICS["comms_data"])
    assert (answer["eps"], answer["adcs"], answer["payload"]) == ({}, {}, {})
    assert answer["obc_state"] is None and answer["mission_id"] is None


# ── set_comms_config ────────────────────────────────────────────────────────


def test_a_ground_command_can_turn_a_permitted_channel_off(comms):
    service, client = comms()
    obc(client)
    command(client, "set_comms_config", params={"beacon_enabled": False})

    assert service.beacon_enabled is False
    assert status(client)["beacon_enabled"] is False
    service.tick()
    assert service._mesh._radio.sent == []


def test_a_ground_command_cannot_turn_a_forbidden_channel_on(comms):
    # The profile is the envelope. A command that could widen it would make the
    # profile a suggestion, and MAINTENANCE says lora:false because the serial
    # port is being used to reflash the radio — not as a preference. The request
    # is remembered, so that returning to a profile that permits the radio
    # honours what the ground last asked for.
    service, client = comms()
    obc(client, profile=Profile.MAINTENANCE)
    command(client, "set_comms_config", params={"beacon_enabled": True})

    assert service.beacon_enabled is False
    assert service._beacon_requested is True


def test_a_channel_turned_off_comes_back_when_the_ground_asks(comms):
    service, client = comms()
    obc(client)
    command(client, "set_comms_config", params={"beacon_enabled": False})
    command(client, "set_comms_config", params={"beacon_enabled": True})
    assert service.beacon_enabled is True


def test_the_flags_are_not_persisted_so_a_restart_returns_to_the_profile(comms):
    # Deliberate: a power cycle is the simplest recovery from any state a
    # command left things in, which is the same reasoning that keeps the profile
    # itself unrestored across a boot.
    service, client = comms()
    obc(client)
    command(client, "set_comms_config", params={"beacon_enabled": False})

    restarted, restarted_client = comms()
    obc(restarted_client)
    assert restarted.beacon_enabled is True


def test_a_config_command_with_no_parameters_changes_nothing(comms, caplog):
    service, client = comms()
    # FLIGHT, whose beacon starts on: "nothing changed" has to be visible as
    # *nothing*, and in a quiet-by-default profile it would be indistinguishable
    # from the command being obeyed.
    obc(client, profile=Profile.FLIGHT)
    with caplog.at_level(logging.WARNING):
        command(client, "set_comms_config")
    assert service.beacon_enabled is True
    assert "nothing changed" in caplog.text


@pytest.mark.parametrize(
    ("param", "explanation"),
    [
        ("aggregation_enabled", "DHS owns persistence"),
        ("api_enabled", "the cloud ground station is gone"),
    ],
)
def test_a_retired_flag_gets_an_explanation_rather_than_silence(comms, caplog, param, explanation):
    # An old ground client deserves to be told why its command did nothing.
    # A command that changes nothing and says nothing is the hardest kind to
    # diagnose from the far end of a radio link.
    service, client = comms()
    obc(client)
    with caplog.at_level(logging.WARNING):
        command(client, "set_comms_config", params={param: True})
    assert explanation in caplog.text


def test_a_retired_flag_alongside_a_live_one_does_not_swallow_it(comms, caplog):
    service, client = comms()
    obc(client)
    with caplog.at_level(logging.WARNING):
        command(client, "set_comms_config", params={"api_enabled": True, "beacon_enabled": False})
    assert service.beacon_enabled is False
    assert "the cloud ground station is gone" in caplog.text


@pytest.mark.parametrize("name", ["take_photo", "set_profile", "science_start", 42])
def test_commands_that_belong_to_somebody_else_are_ignored_in_silence(comms, name, caplog):
    # A warning per take_photo would make every photograph look like a fault on
    # the radio's status topic.
    service, client = comms()
    with caplog.at_level(logging.INFO):
        command(client, name)
    assert client.published == []


# ── the photo ack ───────────────────────────────────────────────────────────


def photo(client, **fields):
    """One ``payload_photo`` the way PAYLOAD publishes it, image included."""
    client.deliver(
        TOPICS["payload_photo"],
        {
            "timestamp": time.time(),
            "status": STATUS_SUCCESS,
            "kind": KIND_PHOTO,
            "file": "photo_20260903_110000.jpg",
            "path": "/run/cubesat/photo/photo_20260903_110000.jpg",
            "size_bytes": 218_112,
            "mission_id": None,
            "sequence": None,
            "overlay": None,
            "photo_base64": "/9j/" + "A" * 4096,
            **fields,
        },
    )


def take_photo(service, client, clock, **fields):
    """Uplink `photo`, let PAYLOAD answer, and wind on to the ack."""
    service._mesh._radio.inject("photo")
    service.tick()
    photo(client, **fields)
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()
    return sent_replies(service)


def test_photo_is_answered_with_the_frame_and_not_with_the_weather(comms):
    # The command with a physical side effect, answered on 2026-09-02 with the
    # ordinary telemetry line while the picture was visible in the dashboard.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    replies = take_photo(service, client, clock, size_bytes=218_112, sequence=7)

    assert len(replies) == 1
    assert " re=photo " in replies[0]
    assert " ok=1 " in replies[0]
    assert " kb=213 " in replies[0]
    assert " seq=7 " in replies[0]


def test_the_image_itself_never_settles_in_the_link_service(comms):
    """The trap in the subscription: `payload_photo` carries the whole JPEG.

    COMMS keeps "the latest payload from each subsystem, kept whole", and doing
    that here would park a base64 copy of every frame in the link service's
    memory for the sake of two integers — on a topic that is *retained*, so the
    broker re-delivers the last one on every reconnect.
    """
    service, client = comms()
    obc(client)
    photo(client)

    assert service._photo is not None
    assert "photo_base64" not in service._photo
    assert "/9j/" not in json.dumps(service._photo)


def test_a_mission_frame_is_not_an_answer_to_anybody(comms):
    # A mission photographs itself every 300 s and an ack window is ten seconds
    # wide, so without this roughly one !photo in thirty would be answered with
    # somebody else's frame — a plausible wrong number rather than an error.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject("photo")
    service.tick()
    photo(client, kind=KIND_MISSION, sequence=99, size_bytes=999_999)
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()

    assert " err=noreply" in sent_replies(service)[0]
    assert " seq=99" not in sent_replies(service)[0]


def test_a_refused_photo_is_named_rather_than_reported_as_a_success(comms):
    # "Did it work or not" is the whole request. A refusal reaches the radio as
    # one word, because a beacon field may not contain a space and PAYLOAD's
    # sentence cannot travel.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    replies = take_photo(
        service,
        client,
        clock,
        status=STATUS_ERROR,
        kind=None,
        reason="Photo capture not allowed: mission state is 'LOW_POWER'",
        reason_code=REASON_STATE,
    )

    assert " ok=0 " in replies[0]
    assert f" err={REASON_STATE}" in replies[0]


def test_a_refusal_with_no_code_still_says_it_failed(comms):
    # An older PAYLOAD, or a shape nobody planned: the ack says it failed and
    # does not invent a cause.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    replies = take_photo(service, client, clock, status=STATUS_ERROR, kind=None, reason_code=None)
    assert " ok=0 err=failed" in replies[0]


def test_a_photograph_from_before_the_command_is_not_its_outcome(comms):
    # payload_photo is retained, so the broker replays the last one on every
    # reconnect. An ack that read it would report a frame taken minutes ago as
    # the answer to a command sent afterwards.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    photo(client, timestamp=time.time() - 300, sequence=3)
    service._mesh._radio.inject("photo")
    service.tick()
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()

    assert " err=noreply" in sent_replies(service)[0]


def test_silence_from_payload_is_reported_without_a_verdict(comms):
    # PAYLOAD stopped by the profile, or a camera that never came back, look
    # exactly like this. `ok=0` would be a verdict on a capture COMMS never saw.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject("photo")
    service.tick()
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()

    line = sent_replies(service)[0]
    assert " re=photo err=noreply" in line
    assert " ok=" not in line


def test_a_photo_asked_for_with_a_bang_is_answered_the_same_way(comms):
    # Nothing downstream knows which spelling a command arrived in, and the ack
    # is downstream of the translation too.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject("!photo")
    service.tick()
    photo(client, size_bytes=102_400)
    clock.advance(ACK_DELAY_SEC + 0.1)
    service.tick()

    # And `re=` still names the verb that was typed, `!` and all.
    assert " re=photo ok=1 kb=100" in sent_replies(service)[0]


def test_the_photo_fields_survive_a_line_that_will_not_fit(comms):
    # Reply fields are protected in _fit: the routine telemetry gives way to
    # them rather than the answer being cut. Asserted against the real ceiling,
    # with the caches full enough to have something to drop.
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["adcs_status"], ADCS)
    client.deliver(TOPICS["dhs_status"], {"mission": {"id": 424242}})
    replies = take_photo(service, client, clock, size_bytes=218_112, sequence=7)

    assert len(replies[0].encode("utf-8")) <= MAX_RADIO_MESSAGE_BYTES
    assert " ok=1 kb=213 seq=7" in replies[0]


# ── the status topic ────────────────────────────────────────────────────────


def test_the_status_is_not_republished_when_nothing_has_changed(comms):
    # It is retained, so a republish that says the same thing again buys nothing
    # and would drown the one that says something new.
    service, client = comms()
    obc(client)
    service.tick()
    before = len(client.payloads(TOPICS["comms_status"]))
    service.tick()
    service.tick()
    assert len(client.payloads(TOPICS["comms_status"])) == before


def test_the_status_is_republished_the_moment_a_channel_is_toggled(comms):
    service, client = comms()
    obc(client)
    before = len(client.payloads(TOPICS["comms_status"]))
    command(client, "set_comms_config", params={"beacon_enabled": False})
    assert len(client.payloads(TOPICS["comms_status"])) == before + 1


def test_the_old_spelling_is_still_accepted_on_the_way_in(comms):
    """The dashboard deployed on 2026-09-02 sends `lora_enabled`.

    A satellite that stopped answering its own console on the day of a rename
    would have made the rename the fault.
    """
    service, client = comms()
    obc(client, profile=Profile.FLIGHT)
    command(client, "set_comms_config", params={"lora_enabled": False})
    assert service.beacon_enabled is False
    command(client, "set_comms_config", params={"lora_enabled": True})
    assert service.beacon_enabled is True


def test_when_a_client_sends_both_spellings_the_new_one_wins(comms):
    # A client sending both is a client mid-upgrade, and the spelling it learned
    # last is the one it means.
    service, client = comms()
    obc(client, profile=Profile.FLIGHT)
    command(client, "set_comms_config", params={"lora_enabled": True, "beacon_enabled": False})
    assert service.beacon_enabled is False


def test_the_status_carries_the_old_key_beside_the_new_one(comms):
    # Deprecated, published for the deployed dashboard, and never allowed to
    # disagree with the key that replaced it.
    service, client = comms()
    obc(client, profile=Profile.DEMO)
    reported = status(client)
    assert reported["beacon_enabled"] is False
    assert reported["lora_enabled"] == reported["beacon_enabled"]


def test_the_status_reports_the_effective_channel_and_not_the_raw_flag(comms):
    # A reader wants to know whether the radio is transmitting, not to have to
    # fetch a profiles file in order to work it out.
    service, client = comms()
    obc(client, profile=Profile.FLIGHT)
    reported = status(client)
    assert reported["beacon_enabled"] is True
    assert reported["lora_listening"] is True


def test_the_status_names_the_node_the_ground_should_be_listening_for(comms):
    radio = MockRadio()
    radio.node_id = "!698204b0"
    radio.region = "US"
    service, client = comms(radio=radio)
    service.on_start()
    assert status(client)["radio"] == {"present": True, "node": "!698204b0", "region": "US"}


# ── caches ──────────────────────────────────────────────────────────────────


def test_the_mission_id_comes_from_dhs_which_owns_it(comms):
    service, client = comms()
    client.deliver(TOPICS["dhs_status"], {"recording": True, "mission": {"id": 42}})
    assert service._mission_id == 42
    client.deliver(TOPICS["dhs_status"], {"recording": False, "mission": None})
    assert service._mission_id is None


def test_stopping_gives_the_radio_back(comms):
    closed = []
    radio = MockRadio()
    radio.close = lambda: closed.append(True)
    service, client = comms(radio=radio)
    service.on_start()
    service.on_stop()
    assert closed == [True]


# ── the radio session log ─────────────────────────────────────────────────────


def radio_events(client):
    return [json.loads(p.payload) for p in client.published if p.topic == TOPICS["comms_radio"]]


def test_every_received_message_is_a_radio_event_even_gibberish(comms):
    # Published before the relay decides anything: a session log that only held
    # the messages that parsed would hide exactly the traffic being debugged.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject("not even json", sender="!e2f1a4c8", snr=6.25, rssi=-96.0, hops=0)
    service.tick()

    event = [e for e in radio_events(client) if e["direction"] == "rx"][-1]
    assert event == {
        "timestamp": event["timestamp"],
        "direction": "rx",
        "text": "not even json",
        "bytes": len(b"not even json"),
        "sender": "!e2f1a4c8",
        "snr": 6.25,
        "rssi": -96.0,
        "hops": 0,
    }


def test_the_log_line_carries_the_hop_count(comms, caplog):
    # The walk of 2026-09-05 had to be proved from a public gateway's records,
    # because ``HOSTED`` writes no ``radio_log`` and this line held only the
    # sender and the SNR. A relayed uplink must now say so where it lands.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject(RECOVER, sender="!698204b0", snr=6.5, hops=1)
    with caplog.at_level("INFO"):
        service.tick()

    assert "LoRa message from !698204b0 (snr 6.5, hops 1)" in caplog.text


def test_a_missing_hop_count_is_not_reported_as_zero(comms, caplog):
    # 0 is "heard directly", which is a measurement; a node that sent neither
    # hop field measured nothing, and the two must not read alike.
    service, client = comms()
    obc(client)
    service._mesh._radio.inject(RECOVER, hops=None)
    with caplog.at_level("INFO"):
        service.tick()

    assert "hops not reported" in caplog.text


def test_link_fields_the_node_did_not_report_are_null_not_invented(comms):
    service, client = comms()
    obc(client)
    service._mesh._radio.inject(RECOVER, rssi=None, hops=None)
    service.tick()

    event = [e for e in radio_events(client) if e["direction"] == "rx"][-1]
    assert event["rssi"] is None and event["hops"] is None
    assert event["snr"] == 6.0


def test_a_beacon_that_left_is_a_tx_event_with_the_line_verbatim(comms):
    service, client = comms()
    obc(client)
    service.tick()

    event = [e for e in radio_events(client) if e["direction"] == "tx"][-1]
    assert event["kind"] == "beacon"
    assert event["sent"] is True
    assert event["text"] == service._mesh._radio.sent[-1]
    assert event["bytes"] == len(event["text"].encode("utf-8"))


def test_an_ack_and_a_going_down_beacon_carry_their_kind(comms):
    clock = Clock()
    service, client = comms(clock=clock)
    obc(client)
    service._mesh._radio.inject(RECOVER)
    service.tick()
    clock.advance(10.1)
    service.tick()
    kinds = [e["kind"] for e in radio_events(client) if e["direction"] == "tx"]
    assert "ack" in kinds

    service.on_state_change(MissionState.NOMINAL, MissionState.CRITICAL)
    assert [e["kind"] for e in radio_events(client) if e["direction"] == "tx"][-1] == "down"


def test_a_failed_transmission_is_on_the_record_as_unsent(comms):
    # It spent no airtime, but it says something about the link that a log
    # without it would silently paper over.
    class Broken(MockRadio):
        def send(self, payload):
            raise OSError("the Heltec browned out")

    service, client = comms(radio=Broken())
    obc(client)
    service.tick()

    event = [e for e in radio_events(client) if e["direction"] == "tx"][-1]
    assert event["sent"] is False
    assert event["kind"] == "beacon"


# ── the command channel ──────────────────────────────────────────────────────
#
# The credential is the channel, not the node. Since the mesh preset changed on
# 2026-09-02 this satellite shares a public primary channel with several hundred
# strangers whose ordinary English — `ping`, `photo`, `safe`, `profile flight` —
# is its command vocabulary, so anything that did not arrive on the private
# channel must reach neither `cubesat/command` nor `cubesat/comms/radio`, and
# must be answered with nothing at all.
#
# Both indices below are named here rather than read from the configuration: the
# rule is "the channel this service was told to accept", and a test that read the
# shipped value would stop testing it the day that value legitimately changes.

#: What the service under test is told to accept.
OUR_CHANNEL = 7
#: A secondary channel that is not ours — somebody else's private traffic.
ANOTHER_CHANNEL = 3
#: The primary. On this node it is the stock public one, and it is also where a
#: direct message lands: a DM is on no channel of ours either way.
PRIMARY_CHANNEL = 0


def test_a_command_on_our_channel_is_relayed_as_it_always_was(comms):
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service._mesh._radio.inject(RECOVER, channel=OUR_CHANNEL)
    service.tick()

    assert relayed(client) == [{"command": "recover"}]


def test_on_our_channel_a_line_that_did_not_parse_still_stays_on_the_record(comms):
    # The other half of the rule, and the reason the filter had to go *before*
    # the publish rather than after: on our own channel everything is recorded,
    # because a malformed command from somebody holding the key is exactly the
    # traffic worth debugging.
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service._mesh._radio.inject("not even json", channel=OUR_CHANNEL)
    service.tick()

    received = [e for e in radio_events(client) if e["direction"] == "rx"]
    assert [e["text"] for e in received] == ["not even json"]


def test_a_command_on_another_channel_reaches_the_bus_not_at_all(comms):
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service._mesh._radio.inject(RECOVER, channel=ANOTHER_CHANNEL)
    service.tick()

    assert relayed(client) == []
    assert status(client)["last_uplink"] is None


def test_a_refused_message_never_reaches_the_radio_log(comms):
    # The order is the point. `comms_radio` is rendered by the dashboard's live
    # Radio Link Log, which `EXPO` puts in front of a room, and DHS writes those
    # rows into `radio_log` on the card in `FLIGHT` and `DIAG`, from where they
    # travel inside a mission export. A stranger's chat must not be displayed to
    # an audience or archived in our own flight record — so it is dropped ahead
    # of the publish, and the text appears in nothing this service says.
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service._mesh._radio.inject("hello mesh, anyone near Sunol?", channel=PRIMARY_CHANNEL)
    service.tick()

    assert [e for e in radio_events(client) if e["direction"] == "rx"] == []
    assert all("Sunol" not in published.payload for published in client.published)


def test_a_direct_message_is_not_a_command_path(comms):
    # Nothing is built for this: a DM is on no channel of ours, so it falls out
    # of the same rule. PKI direct messages were considered as the credential
    # and rejected — they bind the way in to one keypair, and the Heltec's does
    # not survive a reflash.
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service._mesh._radio.inject(RECOVER, channel=PRIMARY_CHANNEL)
    service.tick()

    assert relayed(client) == []
    assert [e for e in radio_events(client) if e["direction"] == "rx"] == []


def test_a_stranger_gets_no_answer_not_even_an_error(comms):
    # `!launch` earns an `err=unknown` from somebody holding the key, because
    # the `!` contract exists so the operator is never left wondering. Off our
    # channel it earns nothing: answering spends airtime on a shared band and
    # teaches a mesh of several hundred nodes that this one talks back.
    clock = Clock()
    service, client = comms(channel=OUR_CHANNEL, clock=clock)
    obc(client)
    service._mesh._radio.inject("!launch", channel=ANOTHER_CHANNEL)
    service.tick()
    clock.advance(10.1)
    service.tick()

    assert sent_replies(service) == []


def test_the_refusal_is_logged_with_the_link_facts_and_never_the_text(comms, caplog):
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service._mesh._radio.inject(
        "meet you at the ridge", sender="!167ff893", snr=0.25, channel=ANOTHER_CHANNEL
    )
    with caplog.at_level(logging.WARNING):
        service.tick()

    assert f"channel {ANOTHER_CHANNEL}" in caplog.text
    assert f"channel {OUR_CHANNEL}" in caplog.text
    assert "!167ff893" in caplog.text
    assert "0.25" in caplog.text
    assert str(len(b"meet you at the ridge")) in caplog.text
    assert "meet you at the ridge" not in caplog.text


def test_a_sender_the_library_did_not_give_is_reported_as_missing(comms, caplog):
    # Both community-mesh messages seen on 2026-09-02 arrived with no `fromId`,
    # mechanism unestablished — and one with no `rxSnr` either. The log has to
    # say so rather than imply an anonymous node was identified, and this is
    # also why filtering by sender is not even available as a shortcut: the
    # field is empty on precisely the traffic that has to be refused.
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service._mesh._radio.inject("relayed chatter", sender=None, snr=None, channel=PRIMARY_CHANNEL)
    with caplog.at_level(logging.WARNING):
        service.tick()

    assert "sender not reported" in caplog.text
    assert "snr not reported" in caplog.text
    assert "None" not in caplog.text


def test_the_inbox_is_still_polled_in_full(comms):
    # The filter is on acting, not on hearing: a message that is refused must
    # not stop the one behind it on our own channel from landing.
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service._mesh._radio.inject("chatter", channel=PRIMARY_CHANNEL)
    service._mesh._radio.inject(RECOVER, channel=OUR_CHANNEL)
    service.tick()

    assert relayed(client) == [{"command": "recover"}]


def test_the_status_says_which_channel_commands_are_taken_from(comms):
    # Hearing and acting are different things now, and nothing else on the wire
    # separates them. A ground station one channel out from the satellite meets
    # perfect transmission, perfect reception and no conversation; this is the
    # one retained field that says which index would have worked.
    service, client = comms(channel=OUR_CHANNEL)
    obc(client)
    service.tick()

    assert status(client)["command_channel"] == OUR_CHANNEL
