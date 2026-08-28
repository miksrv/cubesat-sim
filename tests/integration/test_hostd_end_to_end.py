"""HOSTD from process start to a powered-off host, through the real run loop.

The unit tests call ``handle()`` directly. Here ``run()`` drives: the signal
handlers, the heartbeat thread, the real Unix socket and the shutdown path all
participate at once. Everything below is a scenario an operator can actually
produce — a cold boot, a science fair, a walk, a flat battery, a dead broker —
rather than a method call.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from cubesat.common import config, profiles
from cubesat.common.states import Profile
from cubesat.common.topics import TOPICS
from cubesat.hostd.allowlist import DASHBOARD_UNIT, unit_for
from cubesat.hostd.executor import RecordingExecutor
from cubesat.hostd.service import HostdService

MISSION_UNITS = tuple(unit_for(name) for name in ("adcs", "comms", "dhs", "payload"))


@pytest.fixture
def socket_path():
    # Not tmp_path: a Unix socket address is capped at ~104 bytes and pytest's
    # temporary paths are longer than that on macOS.
    directory = Path(tempfile.mkdtemp(prefix="cubesat-hostd-", dir="/tmp"))
    yield directory / "hostd.sock"
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def last_profile(tmp_path, monkeypatch):
    """Keep the informational file out of the shared test data directory: OBC's
    tests assert that nothing but HOSTD ever creates it."""
    monkeypatch.setattr(config, "LAST_PROFILE_FILE", tmp_path / "last-profile")


@pytest.fixture
def hostd(service_factory, monkeypatch, socket_path):
    """HOSTD running for real, against a recording executor and a fake broker."""
    executor = RecordingExecutor()
    service, client = service_factory(
        HostdService, profiles=profiles.load(), executor=executor, socket_path=socket_path
    )
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    client.connect_ok()
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    yield service, client, executor
    service.stop()
    thread.join(timeout=5)
    assert not thread.is_alive(), "HOSTD did not shut down"


def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def ask(path, action):
    """One request over the break-glass socket, one reply."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5.0)
        client.connect(str(path))
        client.sendall(json.dumps(action).encode() + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer += chunk
    return json.loads(buffer.splitlines()[0])


def latest(client):
    return client.last(TOPICS["host_status"])


def test_a_cold_boot_comes_up_idle_reachable_and_in_the_default_profile(hostd, socket_path):
    # The satellite that shut itself down at 9 % battery on a walk and is
    # plugged in at a desk hours later: HOSTED, home network, SSH reachable.
    service, client, executor = hostd
    assert until(lambda: client.payloads(TOPICS["host_status"]))

    status = latest(client)
    assert status["profile"] == status["profile_requested"] == Profile.HOSTED.value
    assert status["network"]["mode"] == "client"
    assert status["errors"] == []
    assert status["ttl_expires_at"] is None
    # Of the mission services only COMMS runs — HOSTED listens on LoRa, so a
    # satellite that rebooted away from home is still reachable by radio.
    for unit in MISSION_UNITS:
        expected = "active" if unit == unit_for("comms") else "inactive"
        assert status["units"][unit] == expected
    assert config.LAST_PROFILE_FILE.read_text().strip() == Profile.HOSTED.value
    # And the emergency door is open.
    assert until(socket_path.exists)


def test_a_science_fair_and_then_a_walk_home(hostd):
    # Two profile switches over MQTT, which is how every UI, bot and uplink does
    # it, and the state each one leaves behind.
    service, client, executor = hostd
    assert until(lambda: client.payloads(TOPICS["host_status"]))

    client.deliver(TOPICS["host_command"], {"action": "apply_profile", "profile": "EXPO"})
    assert until(lambda: latest(client)["profile"] == "EXPO")
    expo = latest(client)
    assert expo["network"] == {"mode": "ap", "ssid": "cubesat", "clients": 0}
    assert expo["units"][DASHBOARD_UNIT] == "active"
    assert all(expo["units"][unit] == "active" for unit in MISSION_UNITS)
    assert expo["units"]["telegram-bot.service"] == "inactive"

    client.deliver(TOPICS["host_command"], {"action": "apply_profile", "profile": "FLIGHT"})
    assert until(lambda: latest(client)["profile"] == "FLIGHT")
    flight = latest(client)
    # Wi-Fi down, so LoRa is the only way in — and the dashboard has no audience.
    assert flight["network"]["mode"] == "off"
    assert flight["units"][DASHBOARD_UNIT] == "inactive"
    assert all(flight["units"][unit] == "active" for unit in MISSION_UNITS)
    assert flight["governor"] == "powersave"
    # The safety net OBC recovers after a restart of its own.
    assert flight["ttl_expires_at"] > time.time()
    assert ("nmcli", "radio", "wifi", "off") in executor.calls


def test_the_broker_being_dead_is_not_the_end_of_the_road(hostd, socket_path):
    # The break-glass path: FLIGHT with no Wi-Fi and no broker, and the operator
    # has physical access. Same vocabulary, same validation, same report.
    service, client, executor = hostd
    assert until(lambda: client.payloads(TOPICS["host_status"]))
    ask(socket_path, {"action": "apply_profile", "profile": "FLIGHT"})

    reply = ask(socket_path, {"action": "apply_profile", "profile": "HOSTED"})

    assert reply["ok"] is True
    assert reply["profile"] == Profile.HOSTED.value
    assert reply["errors"] == []
    assert ("nmcli", "radio", "wifi", "on") in executor.calls


def test_an_action_the_socket_does_not_know_is_refused_there_too(hostd, socket_path):
    service, client, _ = hostd
    assert until(lambda: client.payloads(TOPICS["host_status"]))
    reply = ask(socket_path, {"action": "rm", "params": {"path": "/"}})
    assert reply["ok"] is False
    assert reply["errors"] == ["unknown action 'rm'"]


def test_a_flat_battery_ends_with_the_host_powered_off(hostd):
    # OBC decides that 9 % means CRITICAL; HOSTD only executes it.
    service, client, executor = hostd
    assert until(lambda: client.payloads(TOPICS["host_status"]))

    client.deliver(
        TOPICS["host_command"],
        {"action": "poweroff", "params": {"reason": "battery_critical"}},
    )

    assert until(lambda: ("systemctl", "poweroff") in executor.calls)


def test_shutting_down_takes_the_emergency_door_with_it(service_factory, socket_path):
    # A leftover socket file would look like a listening HOSTD to the next
    # operator who tries it.
    service, client = service_factory(
        HostdService,
        profiles=profiles.load(),
        executor=RecordingExecutor(),
        socket_path=socket_path,
    )
    client.connect_ok()
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    assert until(socket_path.exists)

    service.stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert not socket_path.exists()
    # The goodbye heartbeat is what tells OBC this was a clean exit.
    assert client.payloads(TOPICS["heartbeat"])[-1]["alive"] is False
