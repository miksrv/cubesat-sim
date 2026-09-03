"""Mission sessions: opening one, closing one, and closing the ones nobody could.

A mission is a continuous run of an active profile — the unit that lets a
dashboard answer "show me the walk to work on Tuesday" instead of "show me all
telemetry since March". Every telemetry row carries the id of the mission it
belongs to, and the mission row carries the derived values a listing needs so
that drawing forty of them is forty index lookups and not forty table scans.

**Orphan recovery matters more than the happy path.** Three of the four ways a
mission ends run code: a profile change, a graceful shutdown, and ``CRITICAL``.
The fourth — a flat battery, a watchdog, a kernel panic, a yanked cable — runs
nothing at all, and leaves a row with a null ``ended_at`` that every later query
has to work around forever. So the first thing DHS does with a database is close
those, at the timestamp of each mission's own last telemetry row, with
``end_reason = interrupted``. That timestamp is the honest one: it is the last
moment the satellite is known to have been recording, and it is exactly what the
``synchronous = NORMAL`` trade in ``schema.py`` is designed to leave behind.

**Deleting one is a different act from ageing one out**, and this module holds
both halves of that difference. ``retention.py`` drops a mission's detail and
keeps its row, stamped ``purged_at``, because a trip that happened stays part of
the satellite's history whether or not its rows are still worth the card.
``delete`` below removes the row as well. That is not an inconsistency: retention
is the satellite deciding it can no longer afford a record, and an operator
pressing *delete* is a person saying this trip should not be listed. A delete
that left a ``purged_at`` ghost behind would look, to whoever pressed it, exactly
like a button that does nothing.

**A mission with no rows at all is closed at its own ``started_at``.** The other
two candidates are worse. ``now`` would invent a duration out of however long the
satellite happened to be switched off — a mission opened at 09:00, killed at
09:01 and recovered at a desk three days later would be recorded as a
three-day session, which is a fabrication in the one table that exists to say
what really happened. Leaving it open is precisely the state recovery exists to
remove. ``started_at`` yields a zero-length mission, which is true: it was
opened, and nothing was ever recorded in it.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from cubesat.common import config
from cubesat.common.states import EndReason
from cubesat.dhs.schema import transaction, utc_iso

#: Mean Earth radius, for the haversine below. A sphere is plenty: the error
#: against WGS-84 is a few parts in a thousand over a walk, and the GNSS noise
#: on consecutive fixed points is larger than that by an order of magnitude.
EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class Mission:
    """An open recording session."""

    id: int
    profile: str
    started_at: str
    label: str | None = None


@dataclass(frozen=True)
class Deletion:
    """What removing one mission took with it. Counted for the log and the ack.

    The three detail counts are kept apart for the same reason the retention
    pass keeps them apart: at 1 Hz a mission holds thousands of attitude samples
    against a few dozen telemetry rows, and one total would be a number about
    attitude wearing a telemetry label.
    """

    mission_id: int
    label: str | None
    rows: int
    attitude: int
    radio: int


@dataclass(frozen=True)
class MissionSummary:
    """What a mission's own telemetry says about it, computed once on close."""

    rows: int
    first_fix_at: str | None
    #: None — not zero — when the mission never had a fix. See the module
    #: docstring in schema.py for why that distinction is kept.
    distance_m: float | None


def default_label(started_at: str) -> str:
    """The label a mission gets when nobody supplied one: when it started.

    Decided 2026-09-01. Before this an unlabelled mission listed as its profile
    — "FLIGHT" — which is the same word for every trip ever taken and tells an
    operator scrolling the archive nothing. A trip is identified by when it
    happened, so that is the default, to the minute and in UTC like every other
    timestamp here.

    Derived from ``started_at`` rather than read from the clock a second time, so
    the label and the column cannot disagree across a minute boundary. Anybody
    who wants a real name still passes one — ``cubesat profile flight
    --mission "walk to work"``, or ``mission_label`` on the wire.
    """
    return started_at[:16].replace("T", " ")


class MissionStore:
    """The ``missions`` table. One instance per open database."""

    def __init__(self, conn: sqlite3.Connection, log: logging.Logger) -> None:
        self._conn = conn
        self._log = log

    # ── lifecycle ───────────────────────────────────────────────────────────

    def open(
        self, profile: str, label: str | None = None, start_reason: str | None = None
    ) -> Mission:
        """Start a mission and return it.

        With no label, one is made from the start time — see ``default_label``.
        Done here rather than at the caller so that every path into the archive
        gets it: a mission opened by DHS, and one opened by a future tool.

        ``start_reason`` says whether this run was asked for or resumed after a
        reset (ROADMAP W11). It is stored as given: DHS reads it off
        ``obc_status``, and a value this build does not recognise is still the
        truest thing anybody knows about why the mission exists.
        """
        started_at = utc_iso()
        label = label or default_label(started_at)
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO missions (label, profile, started_at, start_reason) "
                "VALUES (?, ?, ?, ?)",
                (label, profile, started_at, start_reason),
            )
        mission_id = int(cursor.lastrowid or 0)
        self._log.info(
            "mission %d opened (profile=%s, label=%s, start_reason=%s) at %s",
            mission_id,
            profile,
            label,
            start_reason or "command",
            started_at,
        )
        return Mission(id=mission_id, profile=profile, started_at=started_at, label=label)

    def close(
        self,
        mission_id: int,
        reason: EndReason,
        *,
        ended_at: str | None = None,
    ) -> MissionSummary:
        """End a mission, filling in what its own rows say about it."""
        summary = self.summarise(mission_id)
        stamp = ended_at or utc_iso()
        with transaction(self._conn) as conn:
            conn.execute(
                'UPDATE missions SET ended_at = ?, end_reason = ?, "rows" = ?, '
                "first_fix_at = ?, distance_m = ? WHERE id = ?",
                (
                    stamp,
                    reason.value,
                    summary.rows,
                    summary.first_fix_at,
                    summary.distance_m,
                    mission_id,
                ),
            )
        self._log.info(
            "mission %d closed at %s (%s): %d rows, %s",
            mission_id,
            stamp,
            reason.value,
            summary.rows,
            "no track" if summary.distance_m is None else f"{summary.distance_m:.0f} m",
        )
        return summary

    def recover_orphans(self) -> list[int]:
        """Close every mission this database left open. Returns their ids.

        Run when a database is opened, before anything is written to it, so no
        new mission can be mistaken for an old one that was never closed.
        """
        rows = self._conn.execute(
            "SELECT id, started_at FROM missions WHERE ended_at IS NULL ORDER BY id"
        ).fetchall()
        recovered = []
        for row in rows:
            mission_id = int(row["id"])
            last = self._conn.execute(
                "SELECT MAX(timestamp) AS last FROM telemetry WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()["last"]
            # No rows means nothing is known to have happened inside it, so it
            # is closed where it started rather than wherever the satellite
            # happened to be switched back on.
            ended_at = last or row["started_at"]
            self._log.warning(
                "mission %d was never closed; recovering it at %s", mission_id, ended_at
            )
            self.close(mission_id, EndReason.INTERRUPTED, ended_at=ended_at)
            recovered.append(mission_id)
        return recovered

    def delete(self, mission_id: int) -> Deletion | None:
        """Remove one mission and everything in the database that references it.

        Returns what went, or ``None`` when there is no such mission — which the
        caller reports as a refusal rather than as a success that deleted
        nothing. Photographs are not this module's to remove; the caller does
        that after the transaction commits, through the same fenced helper
        retention uses.

        **One transaction over four tables**, in reference order: the detail
        first and the mission row last. Half a delete is the state every reader
        here is written to be confused by — a telemetry row whose ``mission_id``
        names nothing would survive every query in ``archive.py`` and appear in
        no listing, which is the plausible-wrong-data this project spends its
        effort avoiding.

        The row goes rather than being stamped ``purged_at``; see the module
        docstring for why a manual delete deliberately parts company with
        retention there.
        """
        row = self._conn.execute(
            "SELECT label FROM missions WHERE id = ?", (mission_id,)
        ).fetchone()
        if row is None:
            return None
        with transaction(self._conn) as conn:
            rows = conn.execute(
                "DELETE FROM telemetry WHERE mission_id = ?", (mission_id,)
            ).rowcount
            attitude = conn.execute(
                "DELETE FROM attitude WHERE mission_id = ?", (mission_id,)
            ).rowcount
            radio = conn.execute(
                "DELETE FROM radio_log WHERE mission_id = ?", (mission_id,)
            ).rowcount
            conn.execute("DELETE FROM missions WHERE id = ?", (mission_id,))
        deletion = Deletion(
            mission_id=mission_id,
            label=row["label"],
            rows=rows,
            attitude=attitude,
            radio=radio,
        )
        # At INFO with everything it took, for the same reason retention logs
        # its own deletions that way: a deletion an operator cannot find
        # afterwards in the log did not happen, as far as they can ever tell.
        self._log.info(
            "mission %d (%s) deleted: %d telemetry row(s), %d attitude sample(s), "
            "%d radio event(s)",
            mission_id,
            deletion.label,
            rows,
            attitude,
            radio,
        )
        return deletion

    # ── derived values ──────────────────────────────────────────────────────

    def summarise(self, mission_id: int) -> MissionSummary:
        """Count the rows, find the first fix, and measure the track.

        One pass over one mission's rows, on close — the reason these three live
        on the mission row at all is so that nothing has to do this on read.
        """
        counted = self._conn.execute(
            "SELECT COUNT(*) AS rows_written, MIN(CASE WHEN fix = 1 THEN timestamp END) "
            "AS first_fix_at FROM telemetry WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        track = self._conn.execute(
            "SELECT lat, lon FROM telemetry WHERE mission_id = ? AND fix = 1 "
            "AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY timestamp, id",
            (mission_id,),
        )
        return MissionSummary(
            rows=int(counted["rows_written"]),
            first_fix_at=counted["first_fix_at"],
            distance_m=track_length_m(
                ((float(r["lat"]), float(r["lon"])) for r in track),
                min_segment_m=config.DHS_MIN_SEGMENT_M,
            ),
        )


def track_length_m(
    points: Iterable[tuple[float, float]],
    *,
    min_segment_m: float = 0.0,
) -> float | None:
    """Approximate the length of a path through consecutive fixed positions.

    An approximation, and worth calling one: it is the sum of straight-line hops
    between samples taken 30 s apart, so it cuts every corner the satellite
    actually walked around.

    Returns None when there were no fixed positions at all, and ``0.0`` for a
    single one: a satellite that sat on a windowsill with a fix has a track of
    zero length, while one that never saw a satellite has no track to measure.
    Collapsing the two would put a real zero on a chart where the honest answer
    is that nothing is known.

    **Hops shorter than ``min_segment_m`` do not count.** A stationary consumer
    GNSS receiver wanders by metres from one fix to the next, and summing that
    unfiltered reports a walk the satellite never took — the same class of fault
    as an uncalibrated heading or a UV index from an unknown board, and this
    project withholds rather than emitting a confident wrong number. Note that
    the anchor only advances when a hop is *counted*: real movement slower than
    the floor accumulates against the last counted position until it crosses it,
    rather than being discarded a metre at a time.
    """
    total = 0.0
    anchor: tuple[float, float] | None = None
    seen = False
    for point in points:
        seen = True
        if anchor is None:
            anchor = point
            continue
        segment = haversine_m(anchor, point)
        if segment < min_segment_m:
            continue
        total += segment
        anchor = point
    return total if seen else None


def haversine_m(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lon) pairs."""
    lat1, lon1 = math.radians(start[0]), math.radians(start[1])
    lat2, lon2 = math.radians(end[0]), math.radians(end[1])
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
