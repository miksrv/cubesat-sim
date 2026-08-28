"""Reading the flight recorder's database, and never writing to it.

DHS owns this file. This module opens it **read-only, on its own connection**,
and that is a correctness requirement rather than tidiness: the recorder is
writing a mission while somebody looks at a chart, and a dashboard that took a
write lock would stall the one process whose job is not to miss a row.

Three properties hold everything here up:

* **``mode=ro`` in the URI, not a promise.** A read-only connection cannot start
  a write transaction even by accident — a stray ``INSERT`` fails at SQLite
  rather than in review. WAL is what makes concurrent reading safe at all; DHS
  chose it partly for this.
* **Every query is bounded and every statement is parameterised.** A mission id
  reaches this module from a URL, and a ``limit`` from a query string.
* **A database that is not there is not an error.** A satellite in ``HOSTED``
  has never opened one, and a dashboard that returned 500 for "nothing has been
  recorded yet" would be reporting a fault where there is none. Empty lists.

**A purged mission is not an empty one.** Retention deletes a mission's detail
and stamps ``purged_at``, keeping the row: ``rows`` still says what the trip
recorded. Handing back empty arrays without that stamp would let a viewer draw
an empty chart for a walk that really happened, which is the plausible wrong
number the column exists to prevent — so the summary always travels with the
detail.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

#: Bound on any single request. A mission is thousands of attitude samples and a
#: browser asking for "everything" over an access point is a request that should
#: fail fast rather than serve slowly.
MAX_ROWS = 20_000

#: What ``/api/telemetry`` hands back when the caller does not say.
DEFAULT_TELEMETRY_LIMIT = 200

#: Waiting on a lock at all should be rare — the writer holds one for a single
#: INSERT — but a reader that blocks forever has become the problem it was
#: avoiding.
BUSY_TIMEOUT_SEC = 5.0

logger = logging.getLogger("dashboard.archive")


class Archive:
    """One read-only view of one database file."""

    def __init__(self, path: Path, log: logging.Logger | None = None) -> None:
        self.path = path
        self._log = log or logger
        self._conn: sqlite3.Connection | None = None
        #: Recorded once. Re-opening a database that is not there on every
        #: request would log the same line at whatever rate a browser polls.
        self._missing_reported = False

    def close(self) -> None:
        if self._conn is not None:
            with _suppress_sqlite():
                self._conn.close()
            self._conn = None

    # ── the connection ──────────────────────────────────────────────────────

    def _connection(self) -> sqlite3.Connection | None:
        """The open connection, or ``None`` when there is no database yet."""
        if self._conn is not None:
            return self._conn
        if not self.path.exists():
            if not self._missing_reported:
                self._log.info("no database at %s yet; serving an empty archive", self.path)
                self._missing_reported = True
            return None
        try:
            # mode=ro rather than a convention: a read-only connection cannot
            # take a write lock against the recorder even by mistake.
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=BUSY_TIMEOUT_SEC,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            self._log.exception("could not open %s read-only", self.path)
            return None
        self._conn = conn
        self._missing_reported = False
        self._log.info("archive open: %s", self.path)
        return conn

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run one statement. A failure is logged and answered with nothing.

        Survivable throughout, for the same reason the recorder is: a corrupt
        page or a card pulled mid-read must cost the panel that asked, never the
        service. There is no state here to lose.
        """
        conn = self._connection()
        if conn is None:
            return []
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        except sqlite3.Error:
            self._log.exception("archive query failed: %s", sql.split("\n", 1)[0])
            # Dropped so the next request re-opens: the realistic causes are a
            # file replaced underneath us and a card that went away, and both
            # are cured by opening again rather than by retrying this handle.
            self.close()
            return []

    # ── what the dashboard asks for ─────────────────────────────────────────

    def telemetry(self, limit: int = DEFAULT_TELEMETRY_LIMIT) -> list[dict[str, Any]]:
        """The most recent rows, newest first.

        Newest first because the caller's first use of them is the *latest* row:
        the host's CPU, RAM and disk are on no status topic, so this is where a
        live dashboard reads them from.
        """
        return self._query(
            "SELECT * FROM telemetry ORDER BY timestamp DESC, id DESC LIMIT ?",
            (_bounded(limit),),
        )

    def missions(self) -> list[dict[str, Any]]:
        """Every recorded session, newest first. Cheap: the summaries are stored."""
        return self._query("SELECT * FROM missions ORDER BY started_at DESC, id DESC")

    def mission(self, mission_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM missions WHERE id = ?", (mission_id,))
        return rows[0] if rows else None

    def mission_telemetry(self, mission_id: int, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
        """One mission's rows, oldest first — the order a timeline plays them."""
        return self._query(
            "SELECT * FROM telemetry WHERE mission_id = ? ORDER BY timestamp, id LIMIT ?",
            (mission_id, _bounded(limit)),
        )

    def mission_attitude(self, mission_id: int, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
        """One mission's orientation, oldest first.

        Empty is a real answer and means two different things: a mission
        recorded before the table existed, and one whose detail has been purged.
        Neither is "it never moved" — which is why the caller gets the mission
        summary alongside, with its ``purged_at``.
        """
        if not self._has_attitude():
            return []
        return self._query(
            "SELECT t, quat_w, quat_x, quat_y, quat_z, gyro_x, gyro_y, gyro_z "
            "FROM attitude WHERE mission_id = ? ORDER BY t LIMIT ?",
            (mission_id, _bounded(limit)),
        )

    def _has_attitude(self) -> bool:
        """Whether this file is new enough to have the table.

        Asked rather than assumed. The dashboard reads a database DHS wrote, and
        an older one is a perfectly good archive of a trip that happened before
        attitude was recorded — refusing to open it, or raising on the query,
        would lose a mission to a schema version.
        """
        return bool(
            self._query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'attitude'")
        )


def _bounded(limit: int) -> int:
    """A limit that came from a query string, made safe to interpolate nothing."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_TELEMETRY_LIMIT
    return max(1, min(value, MAX_ROWS))


class _suppress_sqlite:
    """``contextlib.suppress`` for the one exception this module ever ignores."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, *_: Any) -> bool:
        return bool(exc_type is not None and issubclass(exc_type, sqlite3.Error))
