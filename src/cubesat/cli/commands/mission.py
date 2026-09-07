"""``cubesat mission list`` — the trips on the card.

The one command here that does not come off the broker: missions live in SQLite,
and SQLite is on the disk this tool is already sitting on. It reads the file
directly through ``dashboard.archive.Archive`` rather than asking the dashboard
service over HTTP, for two reasons.

``Archive`` opens the database with ``mode=ro`` in the URI — a mode, not a
promise, so it *cannot* write even by mistake, which is the property that makes a
second reader safe while DHS is writing. Reimplementing that here would be a
second place to get it right.

And the dashboard service is not running in ``FLIGHT`` at all, which is exactly
the profile whose missions somebody wants to list. A command that worked only
where the web UI was already available would be a command for the wrong problem.

**Read-only is not enough on its own** (found 2026-09-03, fixed here 2026-09-05).
The databases run in WAL mode, and SQLite creates the ``-wal``/``-shm`` sidecars
beside the file for a *reader* too, owned by whoever opened it. Run from the
operator's shell after a trip — the database closed, no sidecars on disk — this
command would have left two files the operator owns next to a database
``cubesat`` owns, and DHS would have failed its next mission with ``attempt to
write a readonly database`` while the file itself looked perfectly writable. That
is exactly what cost the first ``DIAG`` run its recording, done by hand with
``sqlite3``. So ``owner_mismatch`` says whose file it is, and ``main`` re-runs
the command as that user through ``sudo`` rather than open it as anybody else.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import Any

from cubesat.common import config
from cubesat.dashboard.archive import Archive

#: How many missions a bare `mission list` prints. Enough to cover a month of
#: trips; `--all` is there for the rest.
DEFAULT_LIMIT = 20


def owner_mismatch(database: Path, *, geteuid=os.geteuid) -> str | None:
    """Who owns the database, if that is not who this process runs as.

    ``None`` when it is ours, and ``None`` when there is no file: nothing has
    been recorded, and ``Archive`` reports that without creating anything. The
    owner comes back as a name for ``sudo -u``; an owner with no passwd entry is
    reported by number, which ``sudo`` accepts as ``#uid`` but which mostly means
    the card has been somewhere it should not have been.
    """
    try:
        uid = database.stat().st_uid
    except OSError:
        return None
    if uid == geteuid():
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"#{uid}"


def listing(*, database: Path | None = None, limit: int = DEFAULT_LIMIT) -> tuple[int, list[str]]:
    archive = Archive(database if database is not None else config.DB_PATH)
    try:
        missions = archive.missions()
    finally:
        archive.close()

    if not missions:
        # Not an error. A satellite that has never left the desk has no missions,
        # and since 2026-09-01 a demonstration deliberately records none.
        return 0, [f"No missions recorded in {database or config.DB_PATH}."]

    lines = [f"{len(missions)} mission(s), newest first:"]
    shown = missions if limit <= 0 else missions[:limit]
    lines.extend(_row(mission) for mission in shown)
    if len(shown) < len(missions):
        lines.append(f"… and {len(missions) - len(shown)} older. Use --all to see them.")
    return 0, lines


def _row(mission: dict[str, Any]) -> str:
    parts = [
        f"#{mission.get('id')}",
        str(mission.get("label") or mission.get("started_at") or "?"),
        str(mission.get("profile") or "?"),
    ]
    rows = mission.get("rows")
    if isinstance(rows, int):
        parts.append(f"{rows} rows")
    distance = mission.get("distance_m")
    if isinstance(distance, (int, float)):
        # Null is not zero: an indoor mission has no track at all, and saying
        # "0 m" of one would be the plausible wrong number the column avoids.
        parts.append(f"{distance / 1000:.2f} km" if distance >= 1000 else f"{distance:.0f} m")
    if mission.get("purged_at"):
        parts.append("detail purged")
    elif not mission.get("ended_at"):
        parts.append("still open")
    elif mission.get("end_reason"):
        parts.append(str(mission["end_reason"]))
    return "  " + " · ".join(parts)
