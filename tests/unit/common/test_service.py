import json
import threading
import time

from cubesat.common.service import IDLE_POLL_SEC, Service
from cubesat.common.states import MissionState, Profile
from cubesat.common.topics import TOPICS


class Probe(Service):
    name = "adcs"
    cadence_key = "adcs"
    subscriptions = ("eps_status",)

    def __init__(self):
        super().__init__()
        self.ticks = 0
        self.messages = []
        self.state_changes = []
        self.started = False
        self.stopped = False

    def on_start(self):
        self.started = True

    def tick(self):
        self.ticks += 1

    def on_message(self, topic, data):
        self.messages.append((topic, data))

    def on_state_change(self, previous, current):
        self.state_changes.append((previous, current))

    def on_stop(self):
        self.stopped = True


# ── subscriptions ────────────────────────────────────────────────────────────


def test_obc_status_is_subscribed_automatically(service_factory):
    service, client = service_factory(Probe)
    client.connect_ok()
    assert TOPICS["eps_status"] in client.subscribed
    assert TOPICS["obc_status"] in client.subscribed


def test_a_service_that_ignores_mission_state_does_not_subscribe_to_it(service_factory):
    class Standalone(Probe):
        name = "hostd"
        track_mission_state = False
        subscriptions = ("host_command",)

    service, client = service_factory(Standalone)
    client.connect_ok()
    assert TOPICS["obc_status"] not in client.subscribed


def test_obc_status_is_not_subscribed_twice(service_factory):
    class Explicit(Probe):
        subscriptions = ("obc_status",)

    service, client = service_factory(Explicit)
    client.connect_ok()
    assert client.subscribed.count(TOPICS["obc_status"]) == 1


def test_a_refused_connection_subscribes_to_nothing(service_factory):
    service, client = service_factory(Probe)
    client.connect_refused()
    assert client.subscribed == []


# ── publishing ───────────────────────────────────────────────────────────────


def test_retain_is_decided_by_the_topic_not_the_caller(service_factory):
    service, _ = service_factory(Probe)
    service.publish("eps_status", battery=50)  # retained topic
    service.publish("adcs_status", roll=1.0)  # not retained
    retained = {p.topic: p.retain for p in service.client.published}
    assert retained[TOPICS["eps_status"]] is True
    assert retained[TOPICS["adcs_status"]] is False


def test_published_payloads_are_timestamped(service_factory):
    service, client = service_factory(Probe)
    service.publish("adcs_status", roll=1.5)
    payload = client.last(TOPICS["adcs_status"])
    assert payload["roll"] == 1.5
    assert payload["timestamp"] > 0


def test_raw_publish_passes_the_payload_through_untouched(service_factory):
    # COMMS relays uplinked commands verbatim; re-serialising them would be a
    # chance to change them.
    service, client = service_factory(Probe)
    service.publish_raw("command", '{"command":"safe_mode","extra":[1,2]}')
    assert client.published[-1].payload == '{"command":"safe_mode","extra":[1,2]}'


# ── mission state tracking ───────────────────────────────────────────────────


def test_mission_state_and_profile_are_absorbed(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "LOW_POWER", "profile": "EXPO"})
    assert service.mission_state is MissionState.LOW_POWER
    assert service.profile is Profile.EXPO


def test_cadence_follows_the_mission_state(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    assert service.interval == 0.5
    client.deliver(TOPICS["obc_status"], {"status": "LOW_POWER"})
    assert service.interval == 5.0


def test_cadence_scale_from_the_profile_is_applied(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "cadence_scale": 0.2})
    assert service.interval == 0.1


def test_state_change_hook_fires_once_per_change(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    client.deliver(TOPICS["obc_status"], {"status": "SAFE"})
    assert service.state_changes == [
        (None, MissionState.NOMINAL),
        (MissionState.NOMINAL, MissionState.SAFE),
    ]


def test_unknown_state_or_profile_is_ignored_not_fatal(service_factory):
    # A newer OBC publishing a state this build does not know must not take the
    # subsystem down.
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "HYPERDRIVE", "profile": "MARS"})
    assert service.mission_state is None
    assert service.profile is None


def test_status_without_a_state_field_is_ignored(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"profile": "DEMO"})
    assert service.profile is Profile.DEMO
    assert service.mission_state is None


def test_bad_cadence_scale_is_ignored(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "cadence_scale": -1})
    assert service.cadence_scale == 1.0


# ── message robustness ───────────────────────────────────────────────────────


def test_undecodable_payload_is_dropped(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["eps_status"], "not json at all")
    assert service.messages == []


def test_an_empty_retained_payload_is_an_erasure_not_a_fault(service_factory, caplog):
    # PAYLOAD clears the last photograph by publishing an empty retained
    # payload on every start, so this arrives at every profile change. Warning
    # about it made a routine erasure read as a decode failure in the COMMS log
    # and buried the real ones.
    service, client = service_factory(Probe)
    with caplog.at_level("WARNING"):
        client.deliver(TOPICS["eps_status"], "")

    assert service.messages == []
    assert "undecodable" not in caplog.text


def test_non_object_payload_is_dropped(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["eps_status"], "[1, 2, 3]")
    assert service.messages == []


def test_a_throwing_handler_does_not_propagate(service_factory):
    class Angry(Probe):
        def on_message(self, topic, data):
            raise RuntimeError("boom")

    service, client = service_factory(Angry)
    client.deliver(TOPICS["eps_status"], {"battery": 1})  # must not raise


def test_a_throwing_state_hook_does_not_propagate(service_factory):
    class Angry(Probe):
        def on_state_change(self, previous, current):
            raise RuntimeError("boom")

    service, client = service_factory(Angry)
    client.deliver(TOPICS["obc_status"], {"status": "SAFE"})
    assert service.mission_state is MissionState.SAFE


def test_deploy_asks_the_service_to_report_in(service_factory):
    # The bring-up self-test counts only fresh status messages, and a service
    # that survived the profile switch produces none on its own — so entering
    # DEPLOY, and only DEPLOY, asks for one.
    class Reporting(Probe):
        reports = 0

        def report_in(self):
            self.reports += 1

    service, client = service_factory(Reporting)
    client.deliver(TOPICS["obc_status"], {"status": "DEPLOY"})
    assert service.reports == 1
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    assert service.reports == 1
    client.deliver(TOPICS["obc_status"], {"status": "DEPLOY"})
    assert service.reports == 2


def test_a_throwing_report_in_does_not_propagate(service_factory):
    class Angry(Probe):
        def report_in(self):
            raise RuntimeError("boom")

    service, client = service_factory(Angry)
    client.deliver(TOPICS["obc_status"], {"status": "DEPLOY"})
    assert service.mission_state is MissionState.DEPLOY


# ── the run loop ─────────────────────────────────────────────────────────────


def test_run_starts_ticking_and_stops_cleanly(service_factory):
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})  # 0.5 s cadence

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    client.connect_ok()
    time.sleep(0.15)
    service.stop()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert service.started and service.stopped
    assert service.ticks >= 1
    assert client.disconnected and not client.loop_running


def test_a_failing_tick_keeps_the_service_alive(service_factory):
    class Broken(Probe):
        def tick(self):
            self.ticks += 1
            raise OSError("sensor gone")

    service, client = service_factory(Broken)
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    client.connect_ok()
    time.sleep(0.15)
    service.stop()
    thread.join(timeout=3)
    # A dead sensor is OBC's decision to act on, and it needs the subsystem to
    # stay on the bus long enough to make it.
    assert service.ticks >= 1
    assert service.stopped


def test_an_interval_of_zero_skips_the_tick_but_keeps_waking(service_factory, monkeypatch):
    # A zero in the cadence table means "do not act in this state". No shipped
    # profile uses one today — the one that did, `comms` in SAFE, turned out to
    # silence receiving as well as transmitting — but the guard stays, because a
    # zero reaching the loop unguarded is a spin. The table is patched here
    # rather than borrowed from the real config: a test that reads a production
    # value stops testing behaviour the moment that value legitimately changes.
    from cubesat.common import config as config_module

    monkeypatch.setitem(config_module.CADENCE, "silent-service", {"SAFE": 0})

    class Silent(Probe):
        name = "comms"
        cadence_key = "silent-service"

    service, client = service_factory(Silent)
    client.deliver(TOPICS["obc_status"], {"status": "SAFE"})
    assert service.interval == 0.0
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    # Connect, so the loop actually runs and this asserts the zero-interval
    # branch rather than passing because startup never got that far.
    client.connect_ok()
    time.sleep(0.05)
    service.stop()
    thread.join(timeout=3)
    assert service.ticks == 0


def test_a_service_with_no_cadence_key_idles(service_factory):
    class Passive(Probe):
        cadence_key = None

    service, _ = service_factory(Passive)
    assert service.interval == IDLE_POLL_SEC


def test_a_failing_on_stop_still_shuts_down(service_factory):
    class Angry(Probe):
        def on_stop(self):
            raise RuntimeError("boom")

    service, client = service_factory(Angry)
    service.stop()
    service.run()
    assert client.disconnected


# ── liveness ─────────────────────────────────────────────────────────────────


def test_shutdown_announces_that_the_service_is_gone(service_factory):
    service, client = service_factory(Probe)
    service.stop()
    service.run()
    goodbye = client.payloads(TOPICS["heartbeat"])[-1]
    assert goodbye == {"service": "adcs", "alive": False, "timestamp": goodbye["timestamp"]}


def test_heartbeats_are_published_while_connected(service_factory, monkeypatch):
    from cubesat.common import config

    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    service, client = service_factory(Probe)
    client.connect_ok()
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(0.08)
    service.stop()
    thread.join(timeout=3)
    beats = [p for p in client.payloads(TOPICS["heartbeat"]) if p["alive"]]
    assert beats and beats[0]["service"] == "adcs"


def test_no_heartbeat_before_the_broker_is_up(service_factory, monkeypatch):
    from cubesat.common import config

    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    service, client = service_factory(Probe)
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(0.05)
    service.stop()
    thread.join(timeout=3)
    assert [p for p in client.payloads(TOPICS["heartbeat"]) if p["alive"]] == []


def test_disconnect_clears_the_connected_flag(service_factory):
    service, client = service_factory(Probe)
    client.connect_ok()
    assert service._connected.is_set()
    client.on_disconnect(client, None, None, 7, None)
    assert not service._connected.is_set()


def test_the_last_will_announces_an_ungraceful_death():
    from cubesat.common.mqtt import make_client

    client = make_client("dhs")
    assert client._will_topic.decode() == TOPICS["heartbeat"]
    assert json.loads(client._will_payload.decode()) == {"service": "dhs", "alive": False}


def test_connect_starts_the_network_loop_without_blocking():
    # connect_async, not connect: at boot the broker may not be listening yet,
    # and a service must wait for it rather than crash-loop under systemd.
    from cubesat.common import mqtt as mqtt_factory
    from tests.fakes.mqtt import FakeMqttClient

    client = FakeMqttClient()
    mqtt_factory.connect(client)
    assert client.loop_running


def test_an_unreachable_broker_is_logged_not_silent(service_factory, monkeypatch, caplog):
    # paho retries a background connect quietly, so without this a service that
    # can reach nothing looks exactly like a healthy one.
    from cubesat.common import config
    from cubesat.common import service as service_module

    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(service_module, "OFFLINE_WARNING_INTERVAL_SEC", 0.0)
    service, _ = service_factory(Probe)
    thread = threading.Thread(target=service.run, daemon=True)
    with caplog.at_level("WARNING"):
        thread.start()
        time.sleep(0.05)
        service.stop()
        thread.join(timeout=3)
    assert "no broker connection" in caplog.text


def test_recovery_is_logged_once(service_factory, monkeypatch, caplog):
    from cubesat.common import config
    from cubesat.common import service as service_module

    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(service_module, "OFFLINE_WARNING_INTERVAL_SEC", 0.0)
    service, client = service_factory(Probe)
    thread = threading.Thread(target=service.run, daemon=True)
    with caplog.at_level("INFO"):
        thread.start()
        time.sleep(0.03)
        client.connect_ok()
        time.sleep(0.03)
        service.stop()
        thread.join(timeout=3)
    assert caplog.text.count("broker reachable again") == 1


def test_the_offline_warning_is_throttled(service_factory, monkeypatch, caplog):
    from cubesat.common import config

    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    service, _ = service_factory(Probe)  # default 60 s throttle
    thread = threading.Thread(target=service.run, daemon=True)
    with caplog.at_level("WARNING"):
        thread.start()
        time.sleep(0.06)
        service.stop()
        thread.join(timeout=3)
    assert caplog.text.count("no broker connection") == 1


def test_the_first_tick_waits_for_the_broker(service_factory, monkeypatch):
    # The first tick carries the first status message, and OBC's DEPLOY
    # self-test is waiting for it. Published before CONNACK, a qos-0 message is
    # dropped and the next one is a whole cadence away — long enough for a
    # healthy subsystem to fail its own bring-up.
    from cubesat.common import service as service_module

    monkeypatch.setattr(service_module, "STARTUP_CONNECT_TIMEOUT_SEC", 5.0)
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(0.05)
    assert service.ticks == 0, "ticked before the broker acknowledged the connection"
    client.connect_ok()
    time.sleep(0.1)
    service.stop()
    thread.join(timeout=3)
    assert service.ticks >= 1


def test_a_missing_broker_only_delays_startup(service_factory, monkeypatch, caplog):
    # Waiting for a broker is a startup courtesy, not a precondition: once
    # running, a tick must never be skipped for want of one, because DHS has a
    # flight recorder to keep writing.
    from cubesat.common import service as service_module

    monkeypatch.setattr(service_module, "STARTUP_CONNECT_TIMEOUT_SEC", 0.02)
    service, client = service_factory(Probe)
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    thread = threading.Thread(target=service.run, daemon=True)
    with caplog.at_level("WARNING"):
        thread.start()
        time.sleep(0.15)
        service.stop()
        thread.join(timeout=3)
    assert "not reachable" in caplog.text
    assert service.ticks >= 1


def test_a_stop_during_startup_is_honoured_immediately(service_factory, monkeypatch):
    # systemd waits for us on every stop otherwise: a SIGTERM arriving while the
    # broker is still unreachable must not sit out the whole startup timeout.
    from cubesat.common import service as service_module

    monkeypatch.setattr(service_module, "STARTUP_CONNECT_TIMEOUT_SEC", 30.0)
    service, _ = service_factory(Probe)
    thread = threading.Thread(target=service.run, daemon=True)
    started = time.monotonic()
    thread.start()
    time.sleep(0.03)
    service.stop()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2.0


def test_a_reconnect_gets_a_chance_to_republish_retained_status(service_factory):
    # A broker restart takes every retained message with it. A service that only
    # publishes its status on change would then stay silent, DEPLOY would find no
    # evidence, and a healthy satellite would fail its bring-up because the
    # broker bounced rather than because anything was wrong with it.
    class Retaining(Probe):
        def __init__(self):
            super().__init__()
            self.republished = 0

        def on_connected(self):
            self.republished += 1

    service, client = service_factory(Retaining)
    client.connect_ok()
    client.on_disconnect(client, None, None, 7, None)
    client.connect_ok()
    assert service.republished == 2


def test_a_throwing_on_connected_does_not_lose_the_subscriptions(service_factory):
    class Angry(Probe):
        def on_connected(self):
            raise RuntimeError("boom")

    service, client = service_factory(Angry)
    client.connect_ok()
    assert TOPICS["eps_status"] in client.subscribed
