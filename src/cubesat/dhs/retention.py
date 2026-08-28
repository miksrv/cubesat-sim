"""Retention: bounding the recorded tables, and the photographs beside them.

``DHS_RETENTION_DAYS`` (30 by default) bounds ``telemetry`` and ``attitude``
alike, against the same horizon and inside the same transaction. Both belong to
a mission and both are detail, so a rule that aged one out and kept the other
would leave a mission that can be replayed but not charted, or the reverse —
which is a state nobody would think to test and every consumer would have to
handle. Mission rows are never deleted — a trip that happened stays listed even after its detail
has aged out, which costs a few hundred bytes a year and keeps the history of
the satellite honest.

**Photos are the reason this module matters more than a row purge would.** The
camera is the only unbounded writer on this satellite: a telemetry row is small
and now has a horizon, while a timelapse has neither. With nothing bounding the
images, the card fills, and the first write to fail on a full card is the
telemetry row the mission exists to record. PAYLOAD's free-space floor
(``config.PHOTOS_MIN_FREE_MB``, 512 MB) and this horizon are the same headroom
seen from two sides: PAYLOAD refuses to write below the floor, and this is what
keeps the floor from being reached in the first place. Both numbers are reported
in ``dhs_status`` so they can be compared without ssh.

**Photos follow their mission.** A mission's images live exactly as long as the
mission's rows do: when the last telemetry row of a mission passes the horizon,
``photos/<mission_id>/`` goes with it. That rule fits in one sentence, which is
the point — an operator has to be able to predict what this deletes.

One difference the horizon has to account for: ``telemetry.timestamp`` is an
ISO string and ``attitude.t`` is an epoch float, so the same instant is compared
in two forms. That is deliberate — see ``schema.py`` — and it means this module
computes both, from one moment, rather than converting one to the other twice.

**A purged mission says so.** The same pass stamps ``missions.purged_at``, in
the same transaction as the delete. ``rows`` then keeps its honest historical
meaning — what the mission recorded — while ``purged_at`` explains why asking
for that detail returns nothing, so a dashboard can render "detail aged out"
instead of an empty chart. It also makes a mission a purge candidate exactly
once: the photo deletion below can only ever be reached by the pass that stamps
it. The one thing this ordering gives up is a directory left behind if the
process dies between the commit and the ``rmtree`` — logged nowhere, since
nothing ran to log it, and never retried. That is the accepted cost of the
database never being readable in a state that lies about what it holds.

Deleting somebody's photographs is the most destructive thing in this codebase,
so it is fenced on every side:

* Only a directory whose name is the id of a mission being purged **in this
  pass** is ever removed. Never a glob, never a pattern, never a sweep of
  ``photos/``, and never a name that came from listing the directory — the ids
  come from the database, and a name that is not one of them is not touched.
* ``photos/unfiled/`` is never touched under any circumstances. Those images
  belong to no mission, so no rule here covers when they stop being wanted;
  their size is reported in ``dhs_status`` and a person decides.
* Every deletion is logged at INFO with the mission id, the file count and the
  bytes reclaimed. A deletion an operator cannot find afterwards in the log did
  not happen, as far as they can ever tell.
* A deletion that fails is logged and stepped over. It never aborts the pass and
  never blocks the row purge: the database staying bounded is the more important
  of the two guarantees, and it must not depend on the filesystem cooperating.

Setting ``retention.purge_photos`` to false in ``config.yaml`` turns the last
part off. That is a real choice with a real consequence, and it is worth saying
plainly: **the database stays bounded and the card does not.** Nothing else
removes an image, so on a satellite that takes timelapses the card then fills on
its own schedule, and what stops first is the recorder.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from cubesat.dhs.schema import transaction, utc_iso

SECONDS_PER_DAY = 86_400.0

#: Free space is reported in mebibytes, matching ``photos.min_free_mb`` and what
#: ``df -m`` prints, so the two numbers in ``dhs_status`` compare directly.
BYTES_PER_MB = 1024 * 1024

#: Where PAYLOAD files a photo taken while no mission was open. Named here as
#: well as in ``payload/camera.py`` on purpose: the guard that protects it has
#: to be readable in the file that does the deleting, and the two cannot drift
#: into deleting each other's directory because a mission id is an integer and
#: this is not one.
UNFILED = "unfiled"


@dataclass(frozen=True)
class PurgeResult:
    """What one pass removed. Reported by the caller, asserted by the tests."""

    rows: int = 0
    #: Attitude samples, counted apart from ``rows``: at 1 Hz a mission holds
    #: thousands of them against a few dozen telemetry rows, and one total would
    #: be a number about attitude wearing a telemetry label.
    attitude: int = 0
    missions: tuple[int, ...] = field(default=())
    files: int = 0
    bytes_reclaimed: int = 0


def purge(
    conn: sqlite3.Connection,
    log: logging.Logger,
    *,
    days: int,
    photos_root: Path,
    purge_photos: bool = True,
    now: float | None = None,
) -> PurgeResult:
    """Delete telemetry past the horizon, then the photos of anything fully aged out.

    Survivable throughout: a failure here is a card that keeps growing, which is
    a problem for tomorrow, while an exception escaping would stop the recorder,
    which is a problem for the mission currently being recorded.
    """
    moment = now if now is not None else time.time()
    horizon = moment - days * SECONDS_PER_DAY
    cutoff = utc_iso(horizon)
    stamp = utc_iso(moment)
    try:
        # The deletes and the stamp are one transaction on purpose: the database
        # must never be readable in the state where a mission holds no telemetry
        # and carries no purged_at, because that is exactly the plausible wrong
        # number this column exists to remove. Attitude joins them for the same
        # reason — a mission half aged out is a state no consumer expects.
        with transaction(conn) as tx:
            deleted = tx.execute("DELETE FROM telemetry WHERE timestamp < ?", (cutoff,)).rowcount
            # The same horizon in the form this table stores it. Compared as a
            # float against a float; converting it to the ISO string would
            # compare a number with text and silently match nothing.
            dropped = tx.execute("DELETE FROM attitude WHERE t < ?", (horizon,)).rowcount
            purged = _fully_aged_out(tx, cutoff)
            tx.executemany(
                "UPDATE missions SET purged_at = ? WHERE id = ?",
                [(stamp, mission_id) for mission_id in purged],
            )
    except sqlite3.Error:
        log.exception("retention pass failed; the telemetry table is not bounded this cycle")
        return PurgeResult()

    if deleted or dropped:
        log.info(
            "retention: %d telemetry row(s) and %d attitude sample(s) older than %s deleted",
            deleted,
            dropped,
            cutoff,
        )
    if not purge_photos:
        # Said once per pass that would have deleted something, rather than
        # every pass: the setting is a deliberate choice and the log should
        # remind an operator of its consequence without becoming noise.
        if purged:
            log.info(
                "retention: %d mission(s) fully aged out, photos kept "
                "(retention.purge_photos is off, so the card is unbounded)",
                len(purged),
            )
        return PurgeResult(rows=deleted, attitude=dropped, missions=purged)

    files = 0
    reclaimed = 0
    for mission_id in purged:
        removed, freed = _remove_photos(photos_root, mission_id, log)
        files += removed
        reclaimed += freed
    return PurgeResult(
        rows=deleted,
        attitude=dropped,
        missions=purged,
        files=files,
        bytes_reclaimed=reclaimed,
    )


def _fully_aged_out(conn: sqlite3.Connection, cutoff: str) -> tuple[int, ...]:
    """Missions that ended before the horizon, have no telemetry left, and have
    not been purged already.

    Driven from the ``missions`` table rather than from what the delete touched,
    so that a mission which was opened and closed without ever writing a row —
    but which a photo was filed under — is still covered. An open mission has a
    null ``ended_at`` and can never appear here, which is what keeps the pass
    away from the session currently being recorded.

    "Has no telemetry left" is the test, and attitude is deliberately not part
    of it. Both tables age against the same horizon in the same transaction, so
    they empty together; adding a second NOT EXISTS would be a condition that
    can never independently be false, which reads as though it could.

    ``purged_at IS NULL`` makes a mission a candidate exactly once, ever. That
    is the strongest form of the fence around the photo deletion: a directory
    can only be named by a pass that is stamping its mission for the first time.
    """
    rows = conn.execute(
        "SELECT id FROM missions WHERE purged_at IS NULL AND ended_at IS NOT NULL "
        "AND ended_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM telemetry WHERE telemetry.mission_id = missions.id) "
        "ORDER BY id",
        (cutoff,),
    ).fetchall()
    return tuple(int(row["id"]) for row in rows)


def _remove_photos(root: Path, mission_id: int, log: logging.Logger) -> tuple[int, int]:
    """Delete one mission's photo directory. Returns (files, bytes)."""
    directory = photo_dir(root, mission_id)
    if directory is None or not directory.is_dir():
        return (0, 0)
    files, size = directory_size(directory)
    try:
        shutil.rmtree(directory)
    except OSError:
        log.exception("could not remove %s; its rows are gone but its photos are not", directory)
        return (0, 0)
    log.info(
        "retention: mission %d purged, removed %s (%d file(s), %d bytes reclaimed)",
        mission_id,
        directory,
        files,
        size,
    )
    return (files, size)


def photo_dir(root: Path, mission_id: int | str) -> Path | None:
    """The directory holding one mission's photos, or None if it must not be touched.

    An allowlist rather than a denylist, which is the only version of this worth
    having: the name must be a run of digits, because that is what a mission id
    out of the database is and what nothing else under ``photos/`` ever is.
    ``unfiled`` fails it, and so would a name carrying a path separator or a
    ``..`` if one ever reached here. Listing what may be deleted is a fence that
    holds against inputs nobody thought of; listing what may not is a fence that
    holds only against the ones somebody did.
    """
    name = str(mission_id)
    if not name.isdigit():
        return None
    return root / name


def directory_size(directory: Path) -> tuple[int, int]:
    """How many files are under ``directory`` and how many bytes they occupy.

    Counted before a deletion so the log can say what was reclaimed, and used
    for the unfiled total in ``dhs_status``. A file that disappears mid-walk is
    skipped rather than raising: the number is for a human to read, and an
    approximate one beats an exception out of a status publish.
    """
    files = 0
    total = 0
    for entry in directory.rglob("*"):
        try:
            if entry.is_file():
                files += 1
                total += entry.stat().st_size
        except OSError:
            continue
    return (files, total)


def unfiled_bytes(root: Path) -> int:
    """Bytes sitting in ``photos/unfiled/``, which retention never removes."""
    directory = root / UNFILED
    if not directory.is_dir():
        return 0
    return directory_size(directory)[1]


def free_mb(path: Path) -> float | None:
    """Free space on the filesystem holding ``path``, in mebibytes.

    Reported beside PAYLOAD's ``photos.min_free_mb`` so the horizon and the
    floor — the same headroom seen from two sides — can be compared in one
    place. Measured at the data directory rather than at ``photos/``, because
    the database and the images share a filesystem and the directory the
    recorder is writing to is the one that exists. None where it cannot be
    interrogated at all: a missing number, not a reason to skip a status
    message that OBC may be waiting on.
    """
    try:
        return shutil.disk_usage(path).free / BYTES_PER_MB
    except OSError:
        return None
