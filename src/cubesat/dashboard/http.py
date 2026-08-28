"""The HTTP surface: static files, read-only JSON, and nothing else.

**No WebSocket, deliberately.** Live telemetry reaches the browser from
mosquitto's own WebSocket listener, so this project contains no MQTT-to-
WebSocket bridge — none to write, none to test, and none to fall out of step
with ``topics.py``. Subscribing there also replays every retained message, which
is exactly what a page that has just been opened needs. That decision is what
lets this file be ``http.server``: four JSON endpoints and a directory of static
files do not justify pulling ``aiohttp`` or ``fastapi`` onto a Pi on battery.

**Read-only, and the routing table says so.** There is exactly one method, and
it is GET. A command from the dashboard goes onto ``cubesat/command`` over the
browser's own broker connection — the same topic a laptop, the CLI and an uplink
relayed off the radio all use — so nothing downstream knows it came from a
browser, and this service needs no write path at all.

**Serving files from a directory is where path traversal lives**, so both of the
paths this file builds are allowlists rather than denials:

* a static file must resolve to somewhere inside ``DASHBOARD_ROOT``, checked
  after resolution rather than by looking for ``..`` in the request;
* a photo's mission id must be a run of digits and its name must match a plain
  pattern — the same positive rule the recorder's retention uses, and for the
  same reason: an allowlist holds against inputs nobody thought of.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from cubesat.dashboard.archive import DEFAULT_TELEMETRY_LIMIT, Archive

logger = logging.getLogger("dashboard.http")

#: A mission id is an integer, and a photo directory is named after one. Matched
#: positively — the retention module fences its deletions the same way.
MISSION_ID = re.compile(r"^\d+$")

#: What a camera writes. Anything else in that directory is not ours to serve.
PHOTO_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.(jpg|jpeg|png)$")

#: Served for any path that is not a file, so a deep link reloads into the app
#: rather than into a 404. There is no router today, which is why this is the
#: whole of the SPA fallback.
INDEX = "index.html"

#: Static assets are content-hashed by the bundler, so they can be cached hard.
#: ``index.html`` is not, and must not be — it is what points at the new hashes.
IMMUTABLE_MAX_AGE = 31_536_000


class DashboardHandler(BaseHTTPRequestHandler):
    """One request. The archive and the roots arrive on the server object."""

    # Bumped from the 0.9 default: nothing here speaks to a browser that old,
    # and the correct version is what enables keep-alive.
    protocol_version = "HTTP/1.1"

    server_version = "cubesat-dashboard"
    sys_version = ""

    # ── routing ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path.startswith("/api/"):
                self._api(path, parse_qs(parsed.query))
                return
            self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            # A browser navigating away mid-response. Not worth a traceback.
            logger.debug("client went away during %s", path)
        except Exception:
            # Nothing a request can do may take the service down: this process
            # is also a subscriber on the bus and a heartbeat OBC is watching.
            logger.exception("unhandled error serving %s", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        archive: Archive = self.server.archive  # type: ignore[attr-defined]
        parts = [part for part in path.split("/") if part]  # ['api', ...]

        if parts == ["api", "telemetry"]:
            limit = _int(query.get("limit", []), DEFAULT_TELEMETRY_LIMIT)
            records = archive.telemetry(limit)
            self._json({"count": len(records), "records": records})
            return

        if parts == ["api", "missions"]:
            missions = archive.missions()
            self._json({"count": len(missions), "missions": missions})
            return

        if len(parts) in (3, 4) and parts[:2] == ["api", "missions"] and MISSION_ID.match(parts[2]):
            mission_id = int(parts[2])
            if len(parts) == 3:
                self._mission(archive, mission_id, download=False)
                return
            if parts[3] == "export":
                self._mission(archive, mission_id, download=True)
                return
            if parts[3] == "photos":
                self._photo_list(mission_id)
                return

        if len(parts) == 4 and parts[:2] == ["api", "photos"] and MISSION_ID.match(parts[2]):
            self._photo(int(parts[2]), parts[3])
            return

        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    # ── the archive ─────────────────────────────────────────────────────────

    def _mission(self, archive: Archive, mission_id: int, *, download: bool) -> None:
        """One mission, detail and all — the same body an export downloads.

        The summary always travels with the detail, because empty arrays alone
        cannot say *why* they are empty: a mission recorded before the attitude
        table existed, one whose detail retention has purged, and one that never
        recorded anything look identical without ``purged_at`` and ``rows``.
        """
        mission = archive.mission(mission_id)
        if mission is None:
            self._error(HTTPStatus.NOT_FOUND, f"no mission {mission_id}")
            return
        body = {
            "mission": mission,
            "telemetry": archive.mission_telemetry(mission_id),
            "attitude": archive.mission_attitude(mission_id),
        }
        headers = (
            {"Content-Disposition": f'attachment; filename="mission-{mission_id}.json"'}
            if download
            else {}
        )
        self._json(body, headers=headers)

    def _photo_list(self, mission_id: int) -> None:
        directory = self._photos_root() / str(mission_id)
        if not directory.is_dir():
            self._json({"count": 0, "photos": []})
            return
        names = sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_file() and PHOTO_NAME.match(entry.name)
        )
        photos = [
            {"name": name, "url": f"/api/photos/{mission_id}/{name}"} for name in names
        ]
        self._json({"count": len(photos), "photos": photos})

    def _photo(self, mission_id: int, name: str) -> None:
        if not PHOTO_NAME.match(name):
            # An allowlist, not a search for "..": a name that is not one the
            # camera writes is not served, whatever it happens to contain.
            self._error(HTTPStatus.NOT_FOUND, "no such photo")
            return
        path = self._photos_root() / str(mission_id) / name
        self._send_file(path, cache_seconds=IMMUTABLE_MAX_AGE)

    # ── static files ────────────────────────────────────────────────────────

    def _static(self, path: str) -> None:
        root: Path = self.server.static_root  # type: ignore[attr-defined]
        candidate = (root / path.lstrip("/")).resolve() if path != "/" else (root / INDEX)
        # Checked after resolution rather than before: a symlink and an encoded
        # traversal both survive a textual search for "..", and neither survives
        # asking where the path actually landed.
        if not _inside(root, candidate):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if candidate.is_dir():
            candidate = candidate / INDEX
        if not candidate.is_file():
            # Unknown paths fall back to the app rather than 404, so a reload on
            # a deep link lands in the interface instead of on an error page.
            candidate = root / INDEX
        immutable = "/static/" in path
        self._send_file(candidate, cache_seconds=IMMUTABLE_MAX_AGE if immutable else 0)

    def _send_file(self, path: Path, *, cache_seconds: int) -> None:
        try:
            payload = path.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {"Cache-Control": f"public, max-age={cache_seconds}"} if cache_seconds else {}
        self._respond(HTTPStatus.OK, content_type, payload, headers)

    # ── responses ───────────────────────────────────────────────────────────

    def _json(self, body: Any, *, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body, default=str).encode("utf-8")
        self._respond(HTTPStatus.OK, "application/json", payload, headers or {})

    def _error(self, status: HTTPStatus, message: str) -> None:
        payload = json.dumps({"error": message}).encode("utf-8")
        self._respond(status, "application/json", payload, {})

    def _respond(
        self, status: HTTPStatus, content_type: str, payload: bytes, headers: dict[str, str]
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _photos_root(self) -> Path:
        return self.server.photos_root  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        """Into the service's log at DEBUG, not onto stderr.

        The default writes every request to stderr, which on a satellite means
        every request into the journal — a dashboard open on a phone at a
        science fair would be the loudest thing on the card.
        """
        logger.debug("%s - %s", self.address_string(), fmt % args)


class DashboardServer(ThreadingHTTPServer):
    """The socket, plus the three things a handler needs."""

    daemon_threads = True
    #: A browser reconnecting to a satellite that has just come back must not
    #: wait out a TIME_WAIT on the listening socket.
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        archive: Archive,
        static_root: Path,
        photos_root: Path,
    ) -> None:
        super().__init__(address, DashboardHandler)
        self.archive = archive
        self.static_root = static_root
        self.photos_root = photos_root


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _int(values: list[str], fallback: int) -> int:
    if not values:
        return fallback
    try:
        return int(values[0])
    except ValueError:
        return fallback
