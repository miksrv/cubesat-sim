"""COMMS from process start to a command back on the bus, through ``run()``.

The unit tests call ``tick()`` and hand the service messages. Here the real run
loop drives — the cadence, the heartbeat thread and the shutdown path all
participate — against the mock HAL's loopback radio. Every scenario is one an
operator can actually produce: a walk in ``FLIGHT`` where the radio is the only
way in or out, a demonstration on the LAN, and a battery that falls far enough
to silence the radio.

What no single layer can show on its own is here. That a beacon assembled from
four subsystems' documented payloads still fits in one Meshtastic message. That
a ``set_profile`` typed into a phone comes out of ``cubesat/command`` in a shape
OBC's own parser accepts — asserted against the real consumer, because a shape
both sides agree on separately is a shape neither side checks. And that the
profile really is an envelope: a ground command sent over the very radio it
would need cannot widen it.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from cubesat.common import config
from cubesat.common.states import MissionState, Profile
from cubesat.common.topics import TOPICS
from cubesat.comms.beacon import PREFIX
from cubesat.comms.service import CommsService
from cubesat.hal.interfaces import MAX_RADIO_MESSAGE_BYTES
from cubesat.hal.mock.radio import MockRadio
from cubesat.obc import commands

EPS = {"battery_percent": 78.24, "voltage": 3.9418, "external_power": False, "charge_rate": -0.2}
SCIENCE = {"temperature": 23.4, "humidity": 45.2, "pressure": 1013.0, "light": 412.0,
           "uv_index": None, "uv_raw": 14}

#: A short walk, as ADCS publishes it: a fix a thousandth of a degree of
#: latitude apart is about 111 m a step.
TRACK = [
    {"lat": 55.7558, "lon": 37.6173, "alt": 156.2, "speed": 0.4, "fix": True, "satellites": 23},
    {"lat": 55.7568, "lon": 37.6173, "alt": 156.4, "speed": 1.2, "fix": True, "satellites": 22},
    {"lat": 55.7578, "lon": 37.6173, "alt": 156.1, "speed": 1.1, "fix": True, "satellites": 22},
]

LOG = logging.getLogger("test-comms")


def adcs(fix: dict) -> dict:
    """The full ADCS payload, nulls and all.

    The key set is fixed whether a device answered or not, and the beacon has to
    cope with every field of it while staying inside one message.
    """
    return {
        "roll": 1.23,
        "pitch": -0.45,
        "yaw": 178.9,
        "quaternion": {"w": 0.99, "x": 0.01, "y": 0.02, "z": 0.03},
        "calib_status": {"sys": 3, "gyro": 3, "accel": 3, "mag": 3},
        "imu_temp": 28.0,
        "accel_g": {"x": 0.01, "y": 0.02, "z": 0.98},
        "gyro_dps": {"x": 0.1, "y": 0.0, "z": -0.1},
        "gnss": fix,
    }


def build(service_factory, monkeypatch, *, radio=None, profiles=None):
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    service, client = service_factory(
        CommsService,
        radio=radio if radio is not None else MockRadio(),
        **({"profiles": profiles} if profiles is not None else {}),
    )
    # A wake every few milliseconds, so a walk fits inside a test rather than
    # inside the 30 s NOMINAL cadence.
    monkeypatch.setattr(type(service), "interval", property(lambda _svc: 0.01))
    # The beacon table, scaled the same way — except SAFE, which stays far
    # longer than any test's lifetime. That preserves the property the real
    # table encodes: SAFE listens on every wake and talks almost never.
    monkeypatch.setattr(
        config,
        "BEACON_INTERVALS",
        {"NOMINAL": 0.0, "LOW_POWER": 0.0, "SAFE": 3600.0},
    )
    client.connect_ok()
    return service, client


def start(service):
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    return thread


def stop(service, thread):
    service.stop()
    thread.join(timeout=5)
    assert not thread.is_alive(), "COMMS did not shut down"


def announce(client, state=MissionState.NOMINAL, profile=Profile.FLIGHT):
    client.deliver(
        TOPICS["obc_status"],
        {"status": state.value, "profile": profile.value, "cadence_scale": 1.0},
    )


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def read(line: str) -> dict[str, str]:
    """A ground station's reader: the prefix, then ``key=value``, and no schema."""
    head, *fields = line.split(" ")
    assert head == PREFIX
    return dict(field.split("=", 1) for field in fields)


def relayed(client):
    return [json.loads(p.payload) for p in client.published if p.topic == TOPICS["command"]]


def loop_back(client):
    """Hand COMMS its own relayed command, which is the broker's job.

    The fake client records a publish rather than delivering it, so this stands
    in for mosquitto. It is the step that makes ``set_comms_config`` over the
    radio work at all: COMMS does not act on what it relays, it acts on what
    comes back around onto ``cubesat/command`` like any other message.
    """
    last = [p for p in client.published if p.topic == TOPICS["command"]][-1]
    client.deliver(TOPICS["command"], last.payload)


# ── a walk in FLIGHT ────────────────────────────────────────────────────────


def test_a_flight_profile_beacons_a_track_that_always_fits_in_one_message(
    service_factory, monkeypatch
):
    # The size question is what blocked this phase: a full telemetry packet runs
    # to several hundred bytes and a Meshtastic message holds 240. Every line
    # below is assembled from the documented payloads of four subsystems.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["payload_data"], SCIENCE)
    client.deliver(TOPICS["dhs_status"], {"recording": True, "mission": {"id": 42}})

    for fix in TRACK:
        client.deliver(TOPICS["adcs_status"], adcs(fix))
        assert wait_until(
            lambda f=fix: any(
                read(line).get("lat") == f"{f['lat']:.4f}" for line in list(radio.sent)
            )
        ), "the fix never reached the air"

    stop(service, thread)

    assert radio.sent, "nothing was ever transmitted"
    for line in radio.sent:
        # Never truncated, and never over the limit. The mock radio refuses an
        # oversized payload, so a failure here would already have shown up as a
        # failed send — this asserts the margin as well.
        assert len(line.encode("utf-8")) <= MAX_RADIO_MESSAGE_BYTES
    last = read(radio.sent[-1])
    assert last["st"] == "NOMINAL" and last["pr"] == "FLIGHT"
    assert last["b"] == "78.2" and last["ep"] == "0"
    assert last["m"] == "42"


def test_a_beacon_carries_no_position_until_there_is_a_fix(service_factory, monkeypatch):
    # Indoors, and at the start of every walk. lat=0 lon=0 is a real place in
    # the Gulf of Guinea; an absent field is the only honest answer.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["adcs_status"], adcs({**TRACK[0], "fix": False}))
    assert wait_until(lambda: len(radio.sent) >= 2)
    stop(service, thread)

    for line in radio.sent:
        fields = read(line)
        assert "lat" not in fields and "lon" not in fields
        # And the beacon is still worth its airtime: alive, state, battery.
        assert fields["st"] == "NOMINAL" and fields["b"] == "78.2"


def test_a_command_typed_into_a_phone_comes_out_in_a_shape_obc_accepts(
    service_factory, monkeypatch
):
    # The recovery path for FLIGHT: Wi-Fi is down and there is no SSH, so the
    # only way back in is a message from another Meshtastic node. Asserted
    # against OBC's own parser, not against a shape this test made up.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    # Twelve bytes of thumb-typing, against a 240-byte message. Since
    # 2026-09-03 this is the *only* shape the radio takes: JSON on the air was
    # removed, and the compact table is the whole vocabulary.
    uplink = "profile hosted"
    assert len(uplink.encode("utf-8")) < MAX_RADIO_MESSAGE_BYTES

    radio.inject(uplink)
    assert wait_until(lambda: relayed(client)), "the uplink never reached the bus"
    stop(service, thread)

    published = [p for p in client.published if p.topic == TOPICS["command"]]
    parsed = commands.parse(json.loads(published[0].payload))
    assert parsed is not None and parsed.name == commands.SET_PROFILE
    request = commands.profile_request(parsed)
    assert request is not None and request.profile == "HOSTED"
    # And nothing the compact spelling cannot say came along for the ride: no
    # request_id over the air, no TTL, no mission label.
    assert parsed.request_id is None
    assert request.ttl_minutes is None and request.mission_label is None


def test_mesh_chatter_never_reaches_the_bus(service_factory, monkeypatch):
    # The third line is the one that changed on 2026-09-03: hand-composed JSON
    # is no longer a command over the air either, so it sits with the chat.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    radio.inject("anyone out there?")
    radio.inject('{"note": "still not a command"}')
    radio.inject('{"command": "safe_mode"}')
    radio.inject("recover")
    assert wait_until(lambda: relayed(client))
    stop(service, thread)

    assert relayed(client) == [{"command": "recover"}]


# ── the envelope holds ──────────────────────────────────────────────────────


def test_a_ground_command_cannot_switch_on_a_channel_the_profile_forbids(
    service_factory, monkeypatch
):
    # The profile is the envelope; a command inside it must not be able to widen
    # it. MAINTENANCE forbids LoRa outright — the serial port is being used to
    # reflash the radio — so the radio cannot carry the command that would
    # reopen it, and the request arrives over MQTT as a laptop would send it.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client, profile=Profile.MAINTENANCE)
    client.deliver(
        TOPICS["command"],
        {"command": "set_comms_config", "params": {"beacon_enabled": True}},
    )

    assert wait_until(lambda: service._beacon_requested is True), "the command was never handled"
    stop(service, thread)

    # Handled, remembered, and it changed nothing that matters: the ground asked
    # for the transmitter and the profile still refuses it.
    assert service.beacon_enabled is False
    assert radio.sent == []


def test_silencing_the_radio_over_the_radio_is_recoverable_over_the_radio(
    service_factory, monkeypatch
):
    # A ground command can turn a permitted channel off — and because
    # beacon_enabled gates transmitting only, the same radio can turn it back on.
    # Otherwise this would be a one-way door with the key on the far side.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    assert wait_until(lambda: len(radio.sent) >= 2)

    radio.inject("beacon off")
    assert wait_until(lambda: relayed(client))
    loop_back(client)
    assert wait_until(lambda: client.last(TOPICS["comms_status"])["beacon_enabled"] is False)
    quiet_since = len(radio.sent)
    time.sleep(0.1)
    assert len(radio.sent) == quiet_since, "the transmitter did not stop"
    # Still listening, and still saying so.
    assert client.last(TOPICS["comms_status"])["lora_listening"] is True

    radio.inject("beacon on")
    assert wait_until(lambda: len(relayed(client)) >= 2), "the radio had gone deaf"
    loop_back(client)
    assert wait_until(lambda: len(radio.sent) > quiet_since), "the radio did not come back"
    stop(service, thread)


def test_safe_stops_talking_but_never_stops_listening(
    service_factory, monkeypatch
):
    # The fault this whole design turns on. SAFE is reachable from FLIGHT
    # through a subsystem fault, where the radio is the only way in and there is
    # no SSH — so the state that most needs `recover` must be able to hear it.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    assert wait_until(lambda: len(radio.sent) >= 2)

    announce(client, state=MissionState.SAFE)
    assert service.interval > 0, "SAFE must keep waking, or it cannot hear"
    silent_at = len(radio.sent)

    radio.inject("recover")
    assert wait_until(lambda: relayed(client)), "SAFE went deaf"
    stop(service, thread)

    assert relayed(client) == [{"command": "recover"}]
    # And it went quiet while it did so: the beacon interval in SAFE is far
    # longer than this test's lifetime.
    assert len(radio.sent) == silent_at


# ── the last thing it says ──────────────────────────────────────────────────


def test_a_satellite_that_powers_itself_off_says_so_first(service_factory, monkeypatch):
    # Without this, a satellite that shut itself down at 8 % leaves a silence
    # indistinguishable from a crash, a flat radio, or somebody walking out of
    # range. With it, the ground has a recorded event: where it was, what the
    # battery was doing, and that it chose this.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    client.deliver(TOPICS["eps_status"], {**EPS, "battery_percent": 8.1, "voltage": 3.21})
    client.deliver(TOPICS["adcs_status"], adcs(TRACK[-1]))
    assert wait_until(lambda: any(read(line).get("b") == "8.1" for line in list(radio.sent)))

    announce(client, state=MissionState.CRITICAL)
    stop(service, thread)

    final = read(radio.sent[-1])
    assert final["down"] == "1"
    assert final["st"] == "CRITICAL"
    assert final["b"] == "8.1"
    assert final["lat"] == f"{TRACK[-1]['lat']:.4f}"
    # It went out on the state change, not on a later wake — there is no later
    # wake, because the host is about to be powered off.
    assert len(radio.sent[-1].encode("utf-8")) <= MAX_RADIO_MESSAGE_BYTES
    # And exactly one: CRITICAL has no schedule, so nothing repeats it.
    assert sum("down=1" in line for line in radio.sent) == 1


def test_the_going_down_beacon_outlives_a_ground_command_that_silenced_the_radio(
    service_factory, monkeypatch
):
    # beacon_enabled gates transmitting, but not this. A flag somebody set an hour
    # ago must not silence the one message that explains a disappearance.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    radio.inject("beacon off")
    assert wait_until(lambda: relayed(client))
    loop_back(client)
    assert wait_until(lambda: client.last(TOPICS["comms_status"])["beacon_enabled"] is False)
    quiet_since = len(radio.sent)

    announce(client, state=MissionState.CRITICAL)
    stop(service, thread)

    assert len(radio.sent) == quiet_since + 1
    assert read(radio.sent[-1])["down"] == "1"


# ── answering the ground over MQTT ──────────────────────────────────────────


def test_get_telemetry_is_answered_from_the_cache_through_the_running_loop(
    service_factory, monkeypatch
):
    service, client = build(service_factory, monkeypatch)
    thread = start(service)
    announce(client)
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["adcs_status"], adcs(TRACK[0]))
    client.deliver(TOPICS["payload_data"], SCIENCE)
    client.deliver(TOPICS["dhs_status"], {"recording": True, "mission": {"id": 42}})

    client.deliver(TOPICS["command"], {"command": "get_telemetry", "request_id": "req_007"})
    stop(service, thread)

    answer = client.last(TOPICS["comms_data"])
    assert answer["request_id"] == "req_007"
    assert answer["mission_id"] == 42
    assert answer["eps"] == EPS
    assert answer["adcs"]["gnss"] == TRACK[0]


def test_comms_reports_the_radio_answered_before_anything_else_happens(
    service_factory, monkeypatch
):
    # OBC's DEPLOY waits for this message as evidence the radio answered, inside
    # a bounded window that is shorter than a nominal cadence.
    service, client = build(service_factory, monkeypatch)
    thread = start(service)
    assert wait_until(lambda: client.last(TOPICS["comms_status"])["radio"]["present"] is True)
    stop(service, thread)


def test_a_broker_that_bounced_gets_the_retained_status_back(service_factory, monkeypatch):
    # A broker restart takes every retained message with it. COMMS publishes its
    # status only on change, so without a republish on reconnect, DEPLOY would
    # find no evidence at all and a healthy satellite would fail its own
    # bring-up because mosquitto bounced.
    service, client = build(service_factory, monkeypatch)
    thread = start(service)
    announce(client)
    assert wait_until(lambda: client.last(TOPICS["comms_status"])["radio"]["present"] is True)
    settled = len(client.payloads(TOPICS["comms_status"]))

    client.connect_ok()

    assert len(client.payloads(TOPICS["comms_status"])) == settled + 1
    stop(service, thread)
    assert client.last(TOPICS["comms_status"])["radio"]["present"] is True


def test_a_clean_shutdown_gives_the_serial_port_back(service_factory, monkeypatch):
    closed: list[bool] = []
    radio = MockRadio()
    radio.close = lambda: closed.append(True)
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client)
    assert wait_until(lambda: len(radio.sent) >= 1)
    stop(service, thread)

    assert closed == [True]


@pytest.mark.parametrize("profile", [Profile.MAINTENANCE])
def test_a_profile_with_no_lora_transmits_nothing_at_all(service_factory, monkeypatch, profile):
    # MAINTENANCE is the one deliberately deaf profile — everywhere else the
    # radio at least listens. DIAG was the second until it became a rehearsal of
    # FLIGHT (2026-09-01), where the beacon is part of what is being rehearsed.
    # Parametrised still, because the property is about the class of profile and
    # the next one added belongs here rather than in a second copy of the test.
    radio = MockRadio()
    service, client = build(service_factory, monkeypatch, radio=radio)
    thread = start(service)
    announce(client, profile=profile)
    time.sleep(0.1)
    stop(service, thread)

    assert radio.sent == []
