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
class MissionSummary:
    """What a mission's own telemetry says about it, computed once on close."""

    rows: int
    first_fix_at: str | None
    #: None — not zero — when the mission never had a fix. See the module
    #: docstring in schema.py for why that distinction is kept.
    distance_m: float | None


class MissionStore:
    """The ``missions`` table. One instance per open database."""

    def __init__(self, conn: sqlite3.Connection, log: logging.Logger) -> None:
        self._conn = conn
        self._log = log

    # ── lifecycle ───────────────────────────────────────────────────────────

    def open(self, profile: str, label: str | None = None) -> Mission:
        """Start a mission and return it."""
        started_at = utc_iso()
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO missions (label, profile, started_at) VALUES (?, ?, ?)",
                (label, profile, started_at),
            )
        mission_id = int(cursor.lastrowid or 0)
        self._log.info(
            "mission %d opened (profile=%s, label=%s) at %s",
            mission_id,
            profile,
            label,
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
