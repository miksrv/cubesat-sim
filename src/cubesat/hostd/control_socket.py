"""The break-glass channel: a root-owned Unix socket at ``/run/cubesat/hostd.sock``.

Every normal path into HOSTD runs through the broker, which means every normal
path stops working when the broker does — and "the broker is down" is exactly
when someone needs to force the platform back into a profile that has SSH in it.
So there is a second door, and it is deliberately as dumb as a door can be:
line-delimited JSON in, one JSON object out per line.

It is not a second vocabulary and not a second set of checks. It hands the
decoded object to the same ``handle`` callable the MQTT subscription does, so an
action refused over MQTT is refused here in the same words. The only thing this
module knows about HOSTD is that it takes a dict and returns one.

Permissions are 0600 and the socket lives in ``/run/cubesat``, so reaching it
already means being root. That is not a weakness of the design: root can run
``systemctl`` directly anyway. What this buys is doing it *through* HOSTD, so
the action is logged, allowlisted, and reflected on ``host_status`` like any
other.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("hostd.socket")

#: How long ``accept`` blocks before checking whether we are shutting down.
#: Short: systemd waits for this thread on every stop.
ACCEPT_POLL_SEC = 0.2

#: An idle client is disconnected rather than holding the accept loop, which
#: serves one conversation at a time on purpose — actions are serialised anyway.
CLIENT_IDLE_TIMEOUT_SEC = 10.0

#: A request is a single small JSON object. Anything longer is a client that has
#: lost the plot, and this is a privileged process with no reason to buffer it.
MAX_REQUEST_BYTES = 64 * 1024

SOCKET_MODE = 0o600

#: Pending connections the kernel will hold while one is being served.
SOCKET_BACKLOG = 8

#: How long a client that has said *nothing* is given before it is dropped.
#:
#: Much shorter than the idle timeout below, and that gap is the point.
#: Connections are served one at a time, so every silent client costs the next
#: one its full timeout: with a single 10 s figure, four dead sockets delay the
#: operator by forty seconds — on the channel that exists precisely for when
#: everything else is broken. On a local socket a real client sends immediately;
#: one that has not spoken in half a second is not a conversation. Once a client
#: has said something it gets the full idle timeout, because then it is one.
FIRST_BYTE_TIMEOUT_SEC = 0.5


class ControlSocket:
    """Accepts JSON action objects and funnels them through ``handle``."""

    def __init__(
        self,
        path: Path,
        handle: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        log: logging.Logger | None = None,
    ) -> None:
        self.path = Path(path)
        self._handle = handle
        self.log = log or logger
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Bind and serve in a background thread. Never fatal.

        A socket that cannot be bound must not stop HOSTD from starting: MQTT is
        the primary channel, and losing the emergency door is not a reason to
        lose the ability to apply a profile at all.
        """
        try:
            server = self._bind()
        except OSError as exc:
            self.log.error("control socket unavailable at %s: %s", self.path, exc)
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, args=(server,), name="hostd-socket", daemon=True
        )
        self._thread.start()
        self.log.info("control socket listening on %s", self.path)

    def _bind(self) -> socket.socket:
        # A stale socket file survives an ungraceful death, and bind() would then
        # fail forever. Removing it is safe: we are the only writer of this path.
        if self.path.exists():
            self.path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        os.chmod(self.path, SOCKET_MODE)
        # A backlog of one is what a break-glass channel cannot afford: a single
        # dead or slow client sitting in the queue is then enough to have the
        # operator's next attempt refused outright. Connections are still served
        # one at a time — serial handling is what keeps this file auditable — so
        # the queue only has to absorb the wait, not the work.
        server.listen(SOCKET_BACKLOG)
        server.settimeout(ACCEPT_POLL_SEC)
        self._server = server
        return server

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=CLIENT_IDLE_TIMEOUT_SEC + 1.0)
            self._thread = None
        if self._server is not None:
            self._server.close()
            self._server = None
        # Leaving the file behind would look like a listening socket to the next
        # operator who tries it.
        self.path.unlink(missing_ok=True)
        self.log.info("control socket closed")

    # ── serving ─────────────────────────────────────────────────────────────

    def _serve(self, server: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:  # the socket was closed under us by stop()
                return
            with connection:
                connection.settimeout(FIRST_BYTE_TIMEOUT_SEC)
                self._converse(connection)

    def _converse(self, connection: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = connection.recv(4096)
            except (TimeoutError, OSError):
                self.log.info("control socket client went away")
                return
            if not chunk:
                return
            # It has spoken, so it is a conversation now and gets the patient
            # timeout rather than the one that guards the accept loop.
            connection.settimeout(CLIENT_IDLE_TIMEOUT_SEC)
            buffer += chunk
            if len(buffer) > MAX_REQUEST_BYTES:
                self._send(connection, {"ok": False, "error": "request too large"})
                return
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                self._send(connection, self._reply(line))

    def _reply(self, line: bytes) -> dict[str, Any]:
        try:
            action = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"not a JSON object: {exc}"}
        if not isinstance(action, dict):
            return {"ok": False, "error": "expected a JSON object"}
        self.log.info("control socket action: %s", action.get("action"))
        try:
            return self._handle(action)
        except Exception as exc:
            # The emergency channel must not be able to kill the service it
            # exists to reach.
            self.log.exception("control socket action failed")
            return {"ok": False, "error": str(exc)}

    def _send(self, connection: socket.socket, reply: dict[str, Any]) -> None:
        try:
            connection.sendall(json.dumps(reply).encode("utf-8") + b"\n")
        except OSError as exc:
            self.log.info("could not answer the control socket client: %s", exc)
