"""DASHBOARD — a transport, and no opinions about the mission.

The satellite carries **no interface code**. This service serves a static build
produced by [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation)
and reads the recorder's database; the interface itself is one React app that
also runs against a recorded mission on ordinary hosting with no backend at all.
One codebase for both, and no PHP or MySQL on a Pi on battery.

**It is deliberately not the live channel.** Browsers subscribe to mosquitto's
own WebSocket listener and get every retained message the moment they connect —
so there is no MQTT-to-WebSocket bridge here to write, to test, or to keep in
step with ``topics.py``. What is left for HTTP is what the broker cannot answer:
the archive, and the host's own CPU, RAM and disk, which live only in rows DHS
wrote down.

**It publishes no status of its own.** Every other service has one because OBC's
``DEPLOY`` is waiting for evidence that a device answered; this one owns no
device. What it owes the bus is a heartbeat, and the base class already sends
that. Adding a status topic would mean adding a subsystem that can fail a
bring-up, for a service whose absence a profile is entitled to intend.

**Which database it reads follows DHS**, from the retained ``dhs_status``: in
``DIAG`` the recorder writes ``diag.db``, and a dashboard still showing
``comms.db`` would be displaying last week's trip during a bench session. Before
DHS says anything — or when no mission is open — it falls back to the mission
database, because the archive of past trips is worth serving whether or not one
is being recorded right now.

**Nothing it does may take the process down.** A request that raises is answered
500 and logged; a database that is not there yet is an empty archive, not an
error. It is a viewer: the satellite is doing something more important than
being looked at.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from cubesat.common import config
from cubesat.common.service import Service
from cubesat.common.topics import TOPICS
from cubesat.dashboard.archive import Archive
from cubesat.dashboard.http import DashboardServer

#: Bound to every interface on purpose. In ``EXPO`` the satellite *is* the
#: network and the audience is on it; in ``DEMO`` it is a phone on the LAN. The
#: fence is the profile — HOSTD only runs this service where a profile asked for
#: it — not a listening address.
BIND_HOST = "0.0.0.0"  # noqa: S104

#: How long ``on_stop`` waits for the server thread. Shutdown closes the socket,
#: so this covers a request already in flight and nothing else.
SHUTDOWN_TIMEOUT_SEC = 5.0


class DashboardService(Service):
    name = "dashboard"
    #: A liveness tick and nothing else — the archive is read on request and the
    #: live data never passes through this process at all.
    cadence_key = "dashboard"
    #: Not needed: this service does not act on the mission state, and reading
    #: it would invite the first "and while we're here" that turns a viewer into
    #: a participant.
    track_mission_state = False
    #: For the database path alone. See the module docstring.
    subscriptions = ("dhs_status",)

    def __init__(
        self,
        *,
        port: int | None = None,
        static_root: Path | None = None,
        photos_root: Path | None = None,
        db_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._port = port if port is not None else config.DASHBOARD_PORT
        self._static_root = static_root if static_root is not None else config.DASHBOARD_ROOT
        self._photos_root = photos_root if photos_root is not None else config.PHOTOS_DIR
        #: The mission database, until DHS says it is writing another.
        self._default_db = db_path if db_path is not None else config.DB_PATH

        # The HTTP thread reads the archive while the broker thread may be
        # swapping it for another file. One lock around both: two threads racing
        # a swap is how a request ends up reading a closed connection.
        self._lock = threading.RLock()
        self._archive = Archive(self._default_db, log=self.log)
        self._server: DashboardServer | None = None
        self._thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_start(self) -> None:
        """Bind the socket, and say plainly if there is nothing to serve.

        The build is deployed separately — `client/dist` copied to
        ``CUBESAT_DASHBOARD_ROOT`` — so an install that skipped that step gets a
        service that answers the API and serves a blank page. Said once, at
        WARNING, because "the dashboard is up and empty" is otherwise a puzzle
        with no clue in it.
        """
        if not (self._static_root / "index.html").is_file():
            self.log.warning(
                "no interface at %s: the build from cubesat-groundstation has not been "
                "deployed, so the API will answer and the page will be blank",
                self._static_root,
            )
        with self._lock:
            self._serve()

    def tick(self) -> None:
        """Nothing periodic. The heartbeat is the base class's, and is the point.

        Kept explicit rather than inherited so the emptiness is a decision on
        the page rather than something a reader has to go and confirm.
        """

    def on_stop(self) -> None:
        with self._lock:
            if self._server is not None:
                # shutdown() returns once serve_forever has stopped; the socket
                # is closed after, so a browser gets a clean refusal rather than
                # a hang while the process is going away.
                self._server.shutdown()
                self._server.server_close()
                self._server = None
            if self._thread is not None:
                self._thread.join(timeout=SHUTDOWN_TIMEOUT_SEC)
                self._thread = None
            self._archive.close()

    # ── inbound ─────────────────────────────────────────────────────────────

    def on_message(self, topic: str, data: dict[str, Any]) -> None:
        if topic != TOPICS["dhs_status"]:
            return
        raw = data.get("database")
        # Null while no mission is open. The archive of past trips is still
        # worth serving then, so this falls back rather than closing.
        target = Path(raw) if isinstance(raw, str) and raw else self._default_db
        with self._lock:
            if target == self._archive.path:
                return
            self.log.info("recorder is writing %s; serving that", target)
            self._archive.close()
            self._archive = Archive(target, log=self.log)
            if self._server is not None:
                self._server.archive = self._archive

    # ── the server ──────────────────────────────────────────────────────────

    def _serve(self) -> None:
        try:
            server = DashboardServer(
                (BIND_HOST, self._port),
                archive=self._archive,
                static_root=self._static_root,
                photos_root=self._photos_root,
            )
        except OSError:
            # A port already taken, most likely a previous instance that has not
            # let go. Logged and survived: the process stays up, keeps its
            # heartbeat, and systemd's Restart=always tries again — which is a
            # better answer than a unit that flaps in a tight loop.
            self.log.exception(
                "could not bind %s:%d; nothing is being served", BIND_HOST, self._port
            )
            return
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="dashboard-http",
                                        daemon=True)
        self._thread.start()
        self.log.info("serving %s on port %d", self._static_root, self._port)
