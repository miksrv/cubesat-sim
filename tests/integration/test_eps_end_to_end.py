"""EPS from process start to published telemetry, through the real run loop.

The unit tests call ``tick()`` directly. This one lets ``run()`` drive: signal
handling, the cadence loop, the heartbeat thread and the shutdown path all
participate, which is where the interactions between them would break.
"""

from __future__ import annotations

import threading
import time

from cubesat.common import config
from cubesat.common.topics import TOPICS
from cubesat.eps.service import EpsService
from cubesat.hal.mock.power import MockPowerMonitor


def run_briefly(service, seconds=0.2):
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(seconds)
    service.stop()
    thread.join(timeout=5)
    assert not thread.is_alive(), "service did not shut down"


def test_eps_publishes_battery_telemetry_and_heartbeats(service_factory, monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    monkeypatch.setenv("CUBESAT_MOCK_BATTERY", "38")
    service, client = service_factory(EpsService, monitor=MockPowerMonitor())
    client.connect_ok()
    # NOMINAL rather than no state, so the cadence is short enough to observe.
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "DEMO"})
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 0.02))

    run_briefly(service)

    telemetry = client.payloads(TOPICS["eps_status"])
    assert telemetry, "no battery telemetry was published"
    assert telemetry[-1]["battery_percent"] == 38.0

    beats = [p for p in client.payloads(TOPICS["heartbeat"]) if p["alive"]]
    assert beats and all(b["service"] == "eps" for b in beats)

    # The goodbye is what tells OBC this was a clean exit rather than a crash.
    assert client.payloads(TOPICS["heartbeat"])[-1]["alive"] is False


def test_a_draining_battery_is_visible_in_the_telemetry(service_factory, monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 1.0)
    monitor = MockPowerMonitor()
    monitor._discharge_sec = 10.0
    service, client = service_factory(EpsService, monitor=monitor)
    client.connect_ok()
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 0.02))

    run_briefly(service, seconds=0.15)

    levels = [p["battery_percent"] for p in client.payloads(TOPICS["eps_status"])]
    assert len(levels) >= 2
    assert levels[-1] < levels[0], "the mock battery is not actually draining"
    assert all(p["charge_rate"] < 0 for p in client.payloads(TOPICS["eps_status"]))
