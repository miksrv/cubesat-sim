"""The break-glass channel, for when the broker itself is down.

Real Unix sockets here — they cost nothing, need no privileges and start no
processes, and what is worth testing is precisely that the plumbing works.
"""

from __future__ import annotations

import json
import shutil
import socket
import stat
import tempfile
import time
from pathlib import Path

import pytest

from cubesat.hostd import control_socket
from cubesat.hostd.control_socket import MAX_REQUEST_BYTES, ControlSocket


class Recorder:
    """Stands in for HostdService.handle — the same callable, minus the host."""

    def __init__(self, raises=None):
        self.actions = []
        self.raises = raises

    def __call__(self, action):
        self.actions.append(action)
        if self.raises is not None:
            raise self.raises
        return {"ok": True, "profile": action.get("profile")}


@pytest.fixture
def sock_path():
    # Not tmp_path: a Unix socket address is capped at ~104 bytes, and pytest's
    # temporary directory names are longer than that on macOS.
    directory = Path(tempfile.mkdtemp(prefix="cubesat-sock-", dir="/tmp"))
    yield directory / "hostd.sock"
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def channel(sock_path):
    handler = Recorder()
    server = ControlSocket(sock_path, handler)
    server.start()
    yield server, handler
    server.stop()


def converse(path, *lines, expected=None):
    """Send lines, read the replies. ``expected`` defaults to one per line."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5.0)
        client.connect(str(path))
        for line in lines:
            client.sendall(line if isinstance(line, bytes) else (line + "\n").encode())
        wanted = len(lines) if expected is None else expected
        buffer = b""
        while buffer.count(b"\n") < wanted:
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer += chunk
    return [json.loads(reply) for reply in buffer.splitlines() if reply]


def test_an_action_object_reaches_the_same_handler_mqtt_uses(channel):
    server, handler = channel
    replies = converse(server.path, json.dumps({"action": "apply_profile", "profile": "EXPO"}))
    assert handler.actions == [{"action": "apply_profile", "profile": "EXPO"}]
    assert replies == [{"ok": True, "profile": "EXPO"}]


def test_several_actions_can_share_one_connection(channel):
    server, handler = channel
    replies = converse(
        server.path,
        json.dumps({"action": "set_governor", "params": {"governor": "powersave"}}),
        json.dumps({"action": "poweroff", "params": {"reason": "battery_critical"}}),
    )
    assert [action["action"] for action in handler.actions] == ["set_governor", "poweroff"]
    assert len(replies) == 2


def test_blank_lines_are_ignored_rather_than_answered(channel):
    server, handler = channel
    replies = converse(server.path, "", "  ", json.dumps({"action": "poweroff"}), expected=1)
    assert len(handler.actions) == 1
    assert replies == [{"ok": True, "profile": None}]


def test_something_that_is_not_json_is_answered_and_not_acted_on(channel):
    server, handler = channel
    replies = converse(server.path, "{not json")
    assert handler.actions == []
    assert replies[0]["ok"] is False
    assert "not a JSON object" in replies[0]["error"]


def test_a_json_array_is_not_an_action(channel):
    server, handler = channel
    replies = converse(server.path, json.dumps([{"action": "poweroff"}]))
    assert handler.actions == []
    assert replies[0]["error"] == "expected a JSON object"


def test_undecodable_bytes_are_answered_rather_than_crashing_the_thread(channel):
    server, _ = channel
    replies = converse(server.path, b"\xff\xfe\n")
    assert replies[0]["ok"] is False


def test_a_client_that_hangs_up_mid_request_leaves_no_action_behind(channel):
    server, handler = channel
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(server.path))
        client.sendall(b'{"action": "poweroff"')  # no newline, then EOF
    assert handler.actions == []
    assert converse(server.path, json.dumps({"action": "poweroff"}))[0]["ok"] is True


def test_a_handler_that_raises_cannot_kill_the_channel_it_is_reached_through(sock_path):
    handler = Recorder(raises=RuntimeError("systemctl exploded"))
    server = ControlSocket(sock_path, handler)
    server.start()
    try:
        replies = converse(server.path, json.dumps({"action": "poweroff"}))
        assert replies[0] == {"ok": False, "error": "systemctl exploded"}
        # Still serving: the emergency door must not be the thing that breaks.
        handler.raises = None
        assert converse(server.path, json.dumps({"action": "poweroff"}))[0]["ok"] is True
    finally:
        server.stop()


def test_an_oversized_request_is_dropped_instead_of_buffered(channel):
    server, handler = channel
    replies = converse(server.path, b"[" * (MAX_REQUEST_BYTES + 10), expected=1)
    assert replies[0]["error"] == "request too large"
    assert handler.actions == []


def test_an_idle_client_is_disconnected_rather_than_holding_the_accept_loop(
    sock_path, monkeypatch
):
    # One conversation at a time is deliberate — actions are serialised anyway —
    # so a client that connects and says nothing must not be able to lock the
    # emergency channel out.
    monkeypatch.setattr(control_socket, "CLIENT_IDLE_TIMEOUT_SEC", 0.05)
    server = ControlSocket(sock_path, Recorder())
    server.start()
    try:
        with socket.socket(socket.AF_UNIX) as idle:
            idle.connect(str(server.path))
            idle.sendall(b'{"action"')
            time.sleep(0.3)
            assert converse(server.path, json.dumps({"action": "poweroff"}))[0]["ok"] is True
    finally:
        server.stop()


def test_the_socket_is_root_only_and_leaves_nothing_behind(sock_path):
    server = ControlSocket(sock_path, Recorder())
    server.start()
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    server.stop()
    # A leftover file would look like a listening socket to the next operator.
    assert not server.path.exists()


def test_a_stale_socket_from_an_ungraceful_death_is_replaced(sock_path):
    sock_path.write_text("left over")
    server = ControlSocket(sock_path, Recorder())
    server.start()
    try:
        assert converse(sock_path, json.dumps({"action": "poweroff"}))[0]["ok"] is True
    finally:
        server.stop()


def test_a_socket_that_cannot_be_bound_is_logged_and_not_fatal(sock_path, caplog):
    # MQTT is the primary channel; losing the emergency door is not a reason to
    # lose the ability to apply a profile at all.
    server = ControlSocket(sock_path.parent / "nowhere" / "hostd.sock", Recorder())
    with caplog.at_level("ERROR"):
        server.start()
    assert "control socket unavailable" in caplog.text
    server.stop()  # tolerates never having started


def test_a_client_that_hung_up_before_reading_its_reply_is_not_an_error(channel, caplog):
    # Called directly: whether the peer's close beats the server's sendall is a
    # race, and this branch has to be exercised deterministically.
    server, _ = channel
    dead = socket.socket(socket.AF_UNIX)
    dead.close()
    with caplog.at_level("INFO"):
        server._send(dead, {"ok": True})
    assert "could not answer" in caplog.text


def test_a_socket_that_disappears_under_the_accept_loop_ends_it_quietly(channel):
    # The listening socket is closed from outside the loop, which is what a
    # crash mid-shutdown looks like. The thread must end, not spin on an error.
    server, _ = channel
    thread = server._thread
    server._server.close()
    deadline = time.monotonic() + 3.0
    while thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not thread.is_alive()


def test_a_queued_client_does_not_lock_the_door_for_the_next_one(channel):
    # The break-glass channel is reached when everything else is broken, so a
    # dead client already in the queue must not be able to have the operator's
    # attempt refused. Connections are served one at a time; the backlog is what
    # absorbs the wait.
    server, handler = channel
    # Silent clients: connected, never speaking. Each one costs the next its
    # first-byte timeout, which is why that timeout is short.
    lingering = [socket.socket(socket.AF_UNIX) for _ in range(4)]
    try:
        for client in lingering:
            client.connect(str(server.path))
        assert converse(server.path, json.dumps({"action": "poweroff"}))[0]["ok"] is True
    finally:
        for client in lingering:
            client.close()
