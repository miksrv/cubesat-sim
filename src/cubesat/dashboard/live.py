"""The current session's telemetry history, in memory rather than on the card.

Since 2026-09-01 only ``FLIGHT`` and ``DIAG`` write to the database (Q7): a
demonstration on a desk records no track worth an SD-card write. But ``DEMO`` and
``EXPO`` are exactly the profiles that *show* the satellite, and their charts
need a history — so the rows DHS assembles are published on
``cubesat/dhs/telemetry`` and kept here, in a bounded ring, for as long as the
service is up.

**Why here and not in the browser.** The obvious alternative is to let each page
accumulate what it has seen since it was opened. It fails on the case ``EXPO``
exists for: visitors arrive one at a time and open the page one at a time, so
every one of them would see a chart starting from zero points, and "what the
satellite is showing" would become a function of who opened a tab when. A
reload empties it, a phone that sleeps has its tab evicted, and two people
standing side by side compare different graphs of the same satellite. One ring
on the satellite is one answer for everybody, survives a reload, and costs a few
megabytes of a Raspberry Pi's RAM.

**Why not simply keep writing to the database.** Because that is the decision
this exists to implement: the card is the one component here that wears out by
being written to, and a satellite standing still has nothing to record.

**It is not an archive and must not be mistaken for one.** It holds the last
``DASHBOARD_LIVE_ROWS`` rows, it starts empty on every service start, and it is
gone when the process is. A recorded mission is a different thing entirely,
lives in SQLite, and is read through ``archive.py``.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


class LiveHistory:
    """A bounded ring of published telemetry rows, newest first on the way out.

    Thread-safe because the broker thread fills it while HTTP threads read it,
    and ``deque`` is only atomic per operation — a reader taking a snapshot while
    a writer appends needs the lock to see a consistent list.
    """

    def __init__(self, capacity: int) -> None:
        #: ``maxlen`` is the whole bound: the oldest row is dropped by the
        #: deque itself as the newest arrives, so there is no growth path here
        #: for a session that runs for a week.
        self._rows: deque[dict[str, Any]] = deque(maxlen=max(1, capacity))
        self._lock = threading.Lock()
        #: Rows carry an ``id`` in the database, and the interface uses it as a
        #: list key and to tell two samples apart. A published row has none —
        #: the column is SQLite's autoincrement — so one is assigned here.
        #: Monotonic within a session, deliberately unrelated to any database
        #: id: these rows were never written, and pretending otherwise would
        #: invite somebody to look one up.
        self._next_id = 1

    def offer(self, row: Any) -> bool:
        """Take one row from the bus. Returns whether it was kept.

        Anything that is not a non-empty mapping is dropped rather than stored:
        this arrives from the broker, and a malformed payload must not reach a
        chart as a row of nulls that looks like a measurement.
        """
        if not isinstance(row, dict) or not row:
            return False
        with self._lock:
            self._rows.append({"id": self._next_id, **row})
            self._next_id += 1
        return True

    def records(self, limit: int) -> list[dict[str, Any]]:
        """The most recent rows, newest first — the order ``Archive`` returns.

        Same order for the same reason: the caller's first use of them is the
        latest row, because the host's own CPU, RAM and disk are on no status
        topic and this is where a live dashboard reads them from.
        """
        if limit <= 0:
            return []
        with self._lock:
            newest_first = list(self._rows)[-limit:]
        newest_first.reverse()
        return newest_first
