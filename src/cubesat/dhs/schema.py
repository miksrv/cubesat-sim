"""The database itself: how it is opened, and how it is versioned.

Everything else in this project can be rewritten from the code. The contents of
this file's tables cannot: a walk to work happened once, and if the row was not
written or the file was corrupted, no amount of redeployment brings it back.
Three decisions follow from that, and each of them costs something.

**Migrations are versioned, numbered and forward-only, from the first commit.**
``PRAGMA user_version`` says which steps a file has had applied; anything it has
not had is applied in order, each inside its own transaction. There is
deliberately no "create the tables if they are missing" shortcut for version 1 —
version 1 *is* a migration, so the upgrade path is exercised on every fresh
database from day one rather than for the first time on the day it matters, with
a year of missions in the file.

**A database from a future version is refused, not opened.** A file written by a
newer build of DHS may have columns this one does not know about and constraints
it would violate; opening it read-write and carrying on would corrupt real
history to save a restart. ``FutureSchemaError`` says so by name and by number.

**WAL, with ``synchronous = NORMAL``.** The two candidates are not close:

* ``FULL`` fsyncs the WAL on every commit. This recorder commits a row every
  30 seconds for hours at a time on an SD card, and an fsync per commit is how
  a card wears out — the failure it protects against is losing the *last*
  transactions after a power cut, never the file itself.
* ``NORMAL`` in WAL mode syncs at checkpoints instead. A power loss can lose
  the last transactions; it does **not** corrupt the database, because a torn
  WAL frame fails its checksum and is discarded on the next open.

Losing the last row of a track is survivable and is precisely what orphan
recovery in ``missions.py`` is built to tidy up — the mission is closed at the
timestamp of the last row that *did* land. A corrupt database loses every
mission ever recorded. So: ``NORMAL``, deliberately, and the recovery path that
makes it safe is a feature of the design rather than an afterthought.

WAL also lets DASHBOARD read the file for its charts while DHS is writing to it,
which the rollback journal would not.

**Attitude has a table of its own, and it is not tidiness.** ``telemetry`` holds
one wide row per DHS tick — 30 seconds apart in ``NOMINAL`` — while ADCS
publishes orientation at 2 Hz, so every sixtieth sample survives and a timeline
replay of a hand-carried satellite is a slide show. ``attitude`` is the same
track at the rate it was measured: nine narrow columns, decimated to
``config.DHS_ATTITUDE_MIN_INTERVAL_SEC``, buffered in memory and written in one
batch on the tick that was going to open a transaction anyway. The cost is
SD-card writes and nothing else — DHS holds no hardware, so what ADCS puts on
the bus costs the same whether it is recorded or discarded.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: How long a statement waits for another writer before giving up. DASHBOARD
#: reads this file and WAL keeps readers out of the way, so contention should be
#: rare — but a recorder that blocks forever on a lock has stopped recording
#: just as surely as one that crashed, and a timeout produces a logged error
#: with the service still alive.
BUSY_TIMEOUT_SEC = 5.0


class SchemaError(RuntimeError):
    """The database cannot be used as it stands."""


class FutureSchemaError(SchemaError):
    """The file was written by a newer DHS than this one.

    Its own class because the operator action differs from every other failure
    here: this is not a corrupt card or a full disk, it is the wrong build of
    the software, and the fix is to put the newer one back rather than to go
    looking at the hardware.
    """


@dataclass(frozen=True)
class Migration:
    """One numbered, forward-only step, applied in a single transaction."""

    version: int
    statements: tuple[str, ...]


#: Every column of ``telemetry``, in DDL order.
#:
#: The insert statement is built from this tuple rather than written out a
#: second time, so a column added to the schema and forgotten in the writer is
#: not a possible mistake.
TELEMETRY_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "mission_id",
    "profile",
    "obc_state",
    "battery",
    "voltage",
    "external_power",
    "roll",
    "pitch",
    "yaw",
    "quat_w",
    "quat_x",
    "quat_y",
    "quat_z",
    "imu_temp",
    "accel_x",
    "accel_y",
    "accel_z",
    "gyro_x",
    "gyro_y",
    "gyro_z",
    "calib_status",
    "lat",
    "lon",
    "alt",
    "speed",
    "fix",
    "satellites",
    "temperature",
    "humidity",
    "pressure",
    "light",
    "uv_index",
    "cpu_percent",
    "ram_percent",
    "swap_percent",
    "disk_percent",
    "uptime_seconds",
    "cpu_temperature",
    "raw_json",
    # Appended after raw_json because ALTER TABLE adds a column at the end and
    # this tuple is documented as DDL order. The insert binds by name, so the
    # position is documentation rather than mechanism.
    "charge_rate",
)

#: Written in this order, in one executemany per flush.
ATTITUDE_COLUMNS: tuple[str, ...] = (
    "mission_id",
    "t",
    "quat_w",
    "quat_x",
    "quat_y",
    "quat_z",
    "gyro_x",
    "gyro_y",
    "gyro_z",
)

#: Written in this order, in one executemany per flush — same rule as above.
RADIO_COLUMNS: tuple[str, ...] = (
    "mission_id",
    "t",
    "direction",
    "kind",
    "text",
    "bytes",
    "sender",
    "snr",
    "rssi",
    "hops",
    "sent",
)

_MISSIONS_DDL = """
CREATE TABLE missions (
    -- AUTOINCREMENT, not a bare rowid alias: a mission id becomes a photo
    -- directory name, and SQLite reuses the highest rowid after a delete. Rows
    -- are deleted here — retention never does it, but `delete_mission` does,
    -- and so does an operator with sqlite3 — and the consequence of reuse would
    -- be one trip's photographs appearing inside another's gallery. One extra
    -- table is a cheap way not to have to think about that again.
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT,
    profile      TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    -- Null while the mission is running. Any row still null at startup is an
    -- orphan: see missions.py.
    ended_at     TEXT,
    end_reason   TEXT,
    -- Derived on close and stored, not computed on read: a listing of forty
    -- missions must not scan the telemetry table forty times to draw itself.
    "rows"       INTEGER,
    first_fix_at TEXT,
    -- An approximation of path length, summed from straight-line hops between
    -- samples with a noise floor under them; see track_length_m in missions.py.
    -- Null, not zero, for a mission that never had a fix: an indoor DEMO did
    -- not travel zero metres, it has no track at all, and a chart should be
    -- able to tell those apart.
    distance_m   REAL,
    notes        TEXT
)
"""

_TELEMETRY_DDL = """
CREATE TABLE telemetry (
    id              INTEGER PRIMARY KEY,
    -- ISO-8601 UTC, e.g. 2026-08-24T07:12:03Z. A string rather than an epoch
    -- float because it sorts, ranges and reads correctly with no client-side
    -- conversion, and because the DS1307 RTC on the UPS HAT makes it
    -- trustworthy with no network — which is the whole point of recording a
    -- track from a backpack.
    timestamp       TEXT NOT NULL,
    mission_id      INTEGER NOT NULL REFERENCES missions(id),
    profile         TEXT,
    obc_state       TEXT,
    battery         REAL,
    voltage         REAL,
    external_power  INTEGER,
    roll            REAL,
    pitch           REAL,
    yaw             REAL,
    quat_w          REAL,
    quat_x          REAL,
    quat_y          REAL,
    quat_z          REAL,
    imu_temp        REAL,
    accel_x         REAL,
    accel_y         REAL,
    accel_z         REAL,
    gyro_x          REAL,
    gyro_y          REAL,
    gyro_z          REAL,
    calib_status    TEXT,
    lat             REAL,
    lon             REAL,
    alt             REAL,
    speed           REAL,
    fix             INTEGER,
    satellites      INTEGER,
    temperature     REAL,
    humidity        REAL,
    pressure        REAL,
    light           REAL,
    uv_index        REAL,
    cpu_percent     REAL,
    ram_percent     REAL,
    swap_percent    REAL,
    disk_percent    REAL,
    uptime_seconds  REAL,
    cpu_temperature REAL,
    -- The whole assembled packet, including the fields no column holds: the
    -- charge rate, the raw UV count, the per-axis calibration, and each
    -- source's own timestamp. A column set is a decision about what is worth
    -- charting; this is the decision-free copy.
    raw_json        TEXT
)
"""

_ATTITUDE_DDL = """
CREATE TABLE attitude (
    id         INTEGER PRIMARY KEY,
    mission_id INTEGER NOT NULL REFERENCES missions(id),
    -- Epoch seconds, taken from the ADCS payload rather than stamped on write:
    -- this is when the IMU was read, and at 2 Hz the difference between the two
    -- is most of the interval. It is a float where telemetry.timestamp is an
    -- ISO string to the second because that column's resolution is a deliberate
    -- choice for rows 30 seconds apart, and this one has to be finer.
    t          REAL NOT NULL,
    -- The quaternion rather than Euler angles: it is what the BNO055 fuses and
    -- outputs, it interpolates without gimbal trouble, and a viewer replaying a
    -- track has to interpolate — 1 Hz is slower than the eye wants.
    quat_w     REAL,
    quat_x     REAL,
    quat_y     REAL,
    quat_z     REAL,
    -- Angular rate beside it, because "it was pointing there" and "it was being
    -- turned" are different questions and the second one is not derivable from
    -- a decimated series of the first.
    gyro_x     REAL,
    gyro_y     REAL,
    gyro_z     REAL
)
"""

_INDEX_DDL = (
    # The dashboard's two questions: "which missions are there" and "give me
    # this mission between these two times". Without the second index, drawing
    # forty missions is forty full scans of the largest table in the file.
    'CREATE INDEX idx_telemetry_mission_time ON telemetry (mission_id, timestamp)',
    # Retention scans by time across all missions, and so does an export.
    'CREATE INDEX idx_telemetry_timestamp ON telemetry (timestamp)',
    # A mission listing is newest-first, and orphan recovery asks for the open
    # ones, which is a handful of rows out of a partial index-sized table.
    'CREATE INDEX idx_missions_started_at ON missions (started_at)',
)

_ATTITUDE_INDEX_DDL = (
    # The only question this table is ever asked: play this mission's attitude
    # between these two moments. At 1 Hz for a working day it is the longest
    # table in the file, so a scan is not an option.
    'CREATE INDEX idx_attitude_mission_time ON attitude (mission_id, t)',
    # Retention deletes by time across every mission, exactly as it does for
    # telemetry.
    'CREATE INDEX idx_attitude_t ON attitude (t)',
)

_RADIO_DDL = """
CREATE TABLE radio_log (
    id         INTEGER PRIMARY KEY,
    mission_id INTEGER NOT NULL REFERENCES missions(id),
    -- Epoch seconds from the COMMS event's own timestamp — when the radio
    -- transacted, not when DHS got around to writing. A float like attitude.t,
    -- and for the same reason: an ack follows its beacon by seconds.
    t          REAL NOT NULL,
    -- 'rx' or 'tx'. Everything below is nullable because the two directions
    -- observe different things: link quality exists only for what was heard,
    -- an outcome only for what was said.
    direction  TEXT NOT NULL,
    -- tx only: 'beacon', 'ack' or 'down'. What the transmission was for.
    kind       TEXT,
    -- The line as it crossed the air, verbatim — a session log that re-encoded
    -- its traffic would be one more place for two sides to disagree.
    text       TEXT,
    bytes      INTEGER,
    -- rx only: the sending node and what the radio observed about the link.
    -- Null where the node did not report a value, never a substitute.
    sender     TEXT,
    snr        REAL,
    rssi       REAL,
    hops       INTEGER,
    -- tx only, 0/1: whether the transmission actually left. A failed send
    -- spent no airtime but says something about the link worth keeping.
    sent       INTEGER
)
"""

_RADIO_INDEX_DDL = (
    # The dashboard's question: this mission's radio traffic, in order.
    'CREATE INDEX idx_radio_log_mission_time ON radio_log (mission_id, t)',
    # Retention deletes by time across every mission, as for the other tables.
    'CREATE INDEX idx_radio_log_t ON radio_log (t)',
)

_PURGED_AT_DDL = (
    # Set when a mission's last telemetry row passes the retention horizon.
    #
    # `rows` keeps its honest historical meaning — what the mission actually
    # recorded — and this column is what says why querying for that detail now
    # returns nothing. Without it, a purged mission reports 1440 rows while
    # holding none, which is a plausible wrong number: a dashboard would draw an
    # empty chart with no way to tell "aged out" from "never recorded anything".
    "ALTER TABLE missions ADD COLUMN purged_at TEXT",
)

_START_REASON_DDL = (
    # Why the mission was opened: `command` or `resume` (ROADMAP W11, 2026-09-03).
    #
    # A mission opened by a resume is the second half of a trip whose first half
    # is already in this table, closed as `interrupted` by orphan recovery at
    # the timestamp of its own last row. Without this column the two are one
    # label repeated, and nothing says which of them the reset fell between.
    # Null on every mission recorded before the column existed, which is the
    # honest answer for a run whose start nobody was recording.
    "ALTER TABLE missions ADD COLUMN start_reason TEXT",
)

_CHARGE_RATE_DDL = (
    # The signed percent-per-hour EPS fits to the state-of-charge history
    # (eps/charge_rate.py). It has always been published and has always landed
    # in raw_json; this promotes it to a column because it is not one more
    # reading among many — it is the quantity power_policy actually decides on.
    # `on_mains` compares it against DRAINING_PERCENT_PER_HOUR, so it is the
    # number that explains why a descent happened or did not, and a black box
    # that keeps the readings but not the quantity under the decision makes the
    # analyst reconstruct the decision from JSON.
    #
    # Null is a real value here and must stay one: EPS publishes no rate until
    # it has charge_rate_min_span_sec of history, and again for that long after
    # the mains pin changes. A row with a null rate says "not known yet", which
    # is exactly what the policy read at that moment.
    "ALTER TABLE telemetry ADD COLUMN charge_rate REAL",
)

#: Forward-only, in order. Never edit a migration that has shipped — a file that
#: already applied it will not apply it again, so the edit would only ever reach
#: databases created after it, and the two would silently diverge. That is why
#: `purged_at` arrives as step 2 rather than as a line added to step 1, even
#: though no database outside a test has ever held version 1.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, statements=(_MISSIONS_DDL, _TELEMETRY_DDL, *_INDEX_DDL)),
    Migration(version=2, statements=_PURGED_AT_DDL),
    Migration(version=3, statements=(_ATTITUDE_DDL, *_ATTITUDE_INDEX_DDL)),
    Migration(version=4, statements=(_RADIO_DDL, *_RADIO_INDEX_DDL)),
    Migration(version=5, statements=_START_REASON_DDL),
    Migration(version=6, statements=_CHARGE_RATE_DDL),
)

#: What a database this build can write looks like.
SCHEMA_VERSION = MIGRATIONS[-1].version


def utc_iso(epoch: float | None = None) -> str:
    """A wall-clock instant as the ISO-8601 UTC string the schema stores.

    Second resolution, matching the README's example. Rows are seconds apart at
    the fastest cadence, and a fractional part on a timestamp that is also used
    as a range bound is a needless way to make two representations of the same
    second.
    """
    moment = datetime.now(timezone.utc) if epoch is None else datetime.fromtimestamp(
        epoch, timezone.utc
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one explicit transaction.

    Explicit because the connection is opened in autocommit mode, and because
    ``sqlite3``'s implicit transactions never covered DDL in the first place:
    ``with conn:`` around a ``CREATE TABLE`` commits nothing and rolls back
    nothing, which is exactly the wrong property for a migration.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def connect(path: Path, log: logging.Logger) -> sqlite3.Connection:
    """Open ``path``, apply any outstanding migrations, and hand back the connection.

    The caller is responsible for serialising access: DHS writes rows from its
    tick on the main thread and opens and closes missions from the broker's
    thread, and one lock around both is simpler to reason about than a
    connection per thread would be.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_SEC,
        # Autocommit: transactions are opened by hand, see transaction().
        isolation_level=None,
        # Serialised by the caller's lock, not by sqlite3's thread check.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # A telemetry row that points at a mission which does not exist is a row
    # nothing can ever chart. Cheap to enforce, and it turns a silent data bug
    # into a logged write failure.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        migrate(conn, path, log)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection, path: Path, log: logging.Logger) -> int:
    """Bring ``conn`` up to ``SCHEMA_VERSION``. Returns the version it ends at."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise FutureSchemaError(
            f"{path} is at schema version {current}, but this DHS understands "
            f"{SCHEMA_VERSION}: it was written by a newer build and will not be "
            f"opened for writing"
        )
    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        log.info("applying schema migration %d to %s", migration.version, path)
        with transaction(conn):
            for statement in migration.statements:
                conn.execute(statement)
            # PRAGMA takes no parameters, so the version is interpolated. It is
            # an int from a frozen module-level table, never from input.
            conn.execute(f"PRAGMA user_version = {migration.version:d}")
    return SCHEMA_VERSION
