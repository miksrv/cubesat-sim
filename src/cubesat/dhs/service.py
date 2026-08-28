"""DHS — the flight recorder. The Data Handling Subsystem, and the only writer.

DHS subscribes to every subsystem's telemetry, caches the latest of each, and on
its own cadence assembles one row out of those caches plus the host's health
metrics. It holds no hardware, takes no bus lock and makes no decisions about
the mission; it records what the others say, which is a small job that has to
work every single time.

**Persistence lives here rather than in COMMS, and the use case is the reason.**
In ``FLIGHT`` and in ``SAFE`` the radio goes off while the GNSS track must keep
recording. With the database owned by the link service, turning the link off
meant losing the recorder — so the two are separate processes, and COMMS
persists nothing.

**Whether a row may be written is the profile's decision; how often is the
state's.** ``Persistence.MISSION_DB`` writes to ``comms.db`` and ``DIAG_DB`` to
``diag.db``; the cadence table turns 30 s in ``NOMINAL`` into 300 s in
``LOW_POWER``. The pre-rewrite gate — write only while the mission state is
``SCIENCE`` — is deliberately gone, and it is worth remembering why: keeping it
would mean ``FLIGHT``, the profile whose entire purpose is recording a track,
recorded nothing unless somebody remembered to send ``science_start`` before
leaving the house.

**Missions are opened and closed here, and recovered here.** A mission opens
when a profile that permits persistence reaches a recording state, and closes on
a profile change, a graceful shutdown or ``CRITICAL``. Nothing closes it on a
flat battery or a kernel panic, which is why ``missions.py`` runs orphan
recovery every time a database is opened.

**``recording: false`` is published the moment a mission closes.** OBC's
``CRITICAL`` path waits for exactly that flag before asking HOSTD to power the
host off, with a bounded grace — so the close happens on the thread that
received the state change and the status goes out with it, rather than waiting
for the next tick. A late publish there costs the flush it was waiting for.

**The first ``dhs_status`` goes out as soon as the connection is up.** DHS is
one of the services OBC's ``DEPLOY`` waits on, and the bring-up window is
shorter than a nominal cadence, so the status is published in ``on_start`` and
not on the first tick. It is retained, and it is also where PAYLOAD learns the
mission id it files photographs under — which is why any mission change
republishes it immediately.

Nothing in this file is allowed to raise its way out of a callback. A database
that will not open, a mission that will not close, a row that will not write:
each is logged and survived. A recorder that exits on a bad write takes the rest
of the trip's track with it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cubesat.common import config
from cubesat.common import metrics as metrics_module
from cubesat.common.service import Service
from cubesat.common.states import (
    RECORDING_STATES,
    EndReason,
    MissionState,
    Persistence,
    Profile,
)
from cubesat.common.topics import TOPICS
from cubesat.dhs import recorder as recorder_module
from cubesat.dhs import retention, schema
from cubesat.dhs.missions import Mission, MissionStore

#: How often the retention pass runs. Hourly, not per tick: the horizon is
#: measured in days, so anything faster is a scan of the largest table in the
#: file to delete nothing. A pass also runs once when a database is opened,
#: because a session may well be shorter than an hour.
PURGE_INTERVAL_SEC = 3600.0


class DhsService(Service):
    name = "dhs"
    cadence_key = "dhs"
    #: ``obc_status`` is added by the base class and carries four of the things
    #: DHS needs — the mission state, the profile, the persistence the profile
    #: permits and the operator's mission label — which is precisely why OBC
    #: puts them all on one retained message.
    #:
    #: ``host_status`` is not a source of row data. It is the corroborating
    #: signal that the profile a mission is being recorded under has actually
    #: gone away; see ``_on_host_status``.
    subscriptions = ("eps_status", "adcs_status", "payload_data", "host_status")

    def __init__(self) -> None:
        super().__init__()
        # Rows are written from tick() on the main thread while missions open
        # and close from the broker's thread. One lock around both, because two
        # threads racing a close is how a mission ends up with two end reasons
        # or a row pointing at a mission that has already been summarised.
        self._lock = threading.RLock()

        self._conn: sqlite3.Connection | None = None
        self._db_path: Path | None = None
        self._store: MissionStore | None = None
        self._recorder: recorder_module.Recorder | None = None
        #: A path that failed to open. Not retried until the target changes or
        #: the service restarts: the two realistic causes — a database from a
        #: newer build, and a card that is not there — are neither of them cured
        #: by trying again in 30 seconds, and the log would fill with the
        #: attempts.
        self._db_refused: Path | None = None

        self._mission: Mission | None = None
        self._mission_rows = 0
        self._last_write: float | None = None
        self._next_purge = 0.0

        #: What the profile permits, from obc_status. NONE until it says.
        self._persistence = Persistence.NONE
        #: Applied to the next mission opened, never to one already running:
        #: labels are for grouping, not identity.
        self._mission_label: str | None = None

        #: The latest payload from each subsystem. Assembled into a row on the
        #: tick; kept whole in raw_json so nothing measured is lost to a column
        #: set decided today.
        self._eps: dict[str, Any] | None = None
        self._adcs: dict[str, Any] | None = None
        self._science: dict[str, Any] | None = None

        #: Attitude is the one thing kept *between* ticks rather than only at
        #: them: a telemetry row every 30 seconds says where the satellite was,
        #: and answers nothing about which way it was pointing on the way there.
        #: Filled as ADCS publishes, drained on the tick.
        self._attitude = recorder_module.AttitudeBuffer(
            config.DHS_ATTITUDE_MIN_INTERVAL_SEC, config.DHS_ATTITUDE_BUFFER, self.log
        )

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_start(self) -> None:
        """Report in immediately, whatever state the recorder is in.

        Published here rather than on the first tick because OBC's DEPLOY is
        waiting for it inside a bounded window, and because PAYLOAD reads the
        mission id out of it before it can file a photograph.
        """
        with self._lock:
            self._reconcile()
            self._publish_status()

    def tick(self) -> None:
        with self._lock:
            self._reconcile()
            self._flush_attitude()
            self._write_row()
            self._maybe_purge()
            self._publish_status()

    def on_stop(self) -> None:
        with self._lock:
            self._close_mission(EndReason.SHUTDOWN)
            self._close_database()

    # ── inbound ─────────────────────────────────────────────────────────────

    def on_message(self, topic: str, data: dict[str, Any]) -> None:
        with self._lock:
            if topic == TOPICS["obc_status"]:
                # The base class has already absorbed the mission state and the
                # profile from this message by the time it reaches here.
                self._on_obc_status(data)
            elif topic == TOPICS["eps_status"]:
                self._eps = data
            elif topic == TOPICS["adcs_status"]:
                self._adcs = data
                self._offer_attitude(data)
            elif topic == TOPICS["payload_data"]:
                self._science = data
            elif topic == TOPICS["host_status"]:
                self._on_host_status(data)

    def _on_obc_status(self, data: dict[str, Any]) -> None:
        raw = data.get("persistence")
        try:
            persistence = Persistence.NONE if raw is None else Persistence(raw)
        except ValueError:
            # Conservative direction: an unrecognised value means DHS does not
            # know which database this profile intends, and inventing one is how
            # a DIAG bench run ends up interleaved with a real trip.
            self.log.warning("unknown persistence in obc_status: %r; recording nothing", raw)
            persistence = Persistence.NONE
        self._persistence = persistence

        label = data.get("mission_label")
        self._mission_label = label if isinstance(label, str) else None
        self._reconcile()

    def _on_host_status(self, data: dict[str, Any]) -> None:
        """Close a mission whose profile the host says is no longer applied.

        OBC stands the mission state down on every profile change, so this
        normally has nothing to do. It is here for the ordering where HOSTD
        reports the new profile first: a mission is a continuous run of *one*
        profile, and closing it at the earliest evidence keeps that true.
        """
        achieved = data.get("profile")
        if self._mission is None or not isinstance(achieved, str):
            return
        if achieved == self._mission.profile:
            return
        self.log.warning(
            "host is now running %s; closing mission %d, recorded under %s",
            achieved,
            self._mission.id,
            self._mission.profile,
        )
        self._close_mission(EndReason.PROFILE_CHANGE)

    # ── the mission ─────────────────────────────────────────────────────────

    def _reconcile(self) -> None:
        """Bring the open mission into line with the profile and the state."""
        if self._mission is not None and not self._may_record():
            self._close_mission(self._end_reason())
        profile = self.profile
        if not self._may_record() or profile is None:
            return
        store = self._ensure_database()
        if store is None or self._mission is not None:
            return
        self._open_mission(store, profile)

    def _may_record(self) -> bool:
        """Whether a row may be written right now.

        The profile decides whether at all — asked as "is there a database for
        this persistence", so that the gate and the target cannot disagree — and
        the state decides whether now. The
        lifecycle table says a mission opens when the state "reaches
        ``NOMINAL``", and on the normal ascent that is exactly this condition —
        ``NOMINAL`` is the first state in ``RECORDING_STATES``. Written as the
        set rather than as the one state so that a DHS which starts, or
        restarts, into a descent still records: reading it strictly would mean a
        recorder that came back up in ``LOW_POWER`` sat there writing nothing
        until the battery recovered, which on a walk it never will.
        """
        return (
            self._desired_db_path() is not None
            and self.profile is not None
            and self.mission_state in RECORDING_STATES
        )

    def _end_reason(self) -> EndReason:
        """Why the mission that is open is about to close.

        ``CRITICAL`` is the state that powers the host off, so a mission ending
        there ended because the battery ran out. Everything else that leaves the
        recording states does so because the profile stood down.
        """
        if self.mission_state is MissionState.CRITICAL:
            return EndReason.BATTERY_CRITICAL
        return EndReason.PROFILE_CHANGE

    def _open_mission(self, store: MissionStore, profile: Profile) -> None:
        try:
            self._mission = store.open(profile.value, self._mission_label)
        except (sqlite3.Error, OSError):
            self.log.exception("could not open a mission; nothing is being recorded")
            return
        self._mission_rows = 0
        # PAYLOAD files photographs under the id in this message, so it goes out
        # now rather than on the next tick.
        self._publish_status()

    def _close_mission(self, reason: EndReason) -> None:
        mission = self._mission
        if mission is None:
            return
        # Before the mission stops being the open one: these samples are its,
        # and writing them afterwards would place them past its own ended_at.
        self._flush_attitude()
        # Cleared before the write is attempted: whatever the database does
        # next, DHS has stopped recording, and OBC's CRITICAL grace is waiting
        # on that flag rather than on the UPDATE landing.
        self._mission = None
        try:
            if self._store is not None:
                self._store.close(mission.id, reason)
        except (sqlite3.Error, OSError):
            # Survivable, and self-healing: a mission left with a null ended_at
            # is exactly what orphan recovery closes at the next startup.
            self.log.exception(
                "could not close mission %d; it will be recovered as interrupted", mission.id
            )
        self._publish_status()

    # ── the database ────────────────────────────────────────────────────────

    def _desired_db_path(self) -> Path | None:
        if self._persistence is Persistence.MISSION_DB:
            return config.DB_PATH
        if self._persistence is Persistence.DIAG_DB:
            return config.DIAG_DB_PATH
        return None

    def _ensure_database(self) -> MissionStore | None:
        """Open the database this profile calls for, and return its mission store."""
        desired = self._desired_db_path()
        if desired is None or desired == self._db_refused:
            return None
        if self._store is not None and self._db_path == desired:
            return self._store

        self._close_database()
        try:
            conn = schema.connect(desired, self.log)
        except (schema.SchemaError, sqlite3.Error, OSError):
            self.log.exception("cannot open %s; nothing will be recorded", desired)
            self._db_refused = desired
            self._publish_status()
            return None

        self._conn = conn
        self._db_path = desired
        store = self._store = MissionStore(conn, self.log)
        self._recorder = recorder_module.Recorder(conn, self.log)
        self.log.info("recording to %s (schema version %d)", desired, schema.SCHEMA_VERSION)
        self._recover_orphans(store)
        # Once on open as well as hourly: a session can easily be shorter than
        # the purge interval, and then nothing would ever be purged at all.
        self._purge()
        return store

    def _recover_orphans(self, store: MissionStore) -> None:
        """Close whatever the last run of this database left open."""
        try:
            recovered = store.recover_orphans()
        except (sqlite3.Error, OSError):
            self.log.exception("orphan recovery failed on %s", self._db_path)
            return
        if recovered:
            # Named with its file, because recovery runs when a database is
            # opened rather than once at startup: an interrupted DIAG session
            # sits unrecovered in diag.db until the next DIAG run, and this line
            # is where that delay becomes visible to whoever is reading the log
            # instead of being a fact only the design knows.
            self.log.info(
                "recovered %d interrupted mission(s) in %s: %s",
                len(recovered),
                self._db_path,
                ", ".join(str(i) for i in recovered),
            )

    def _close_database(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except sqlite3.Error:
            self.log.exception("closing %s failed", self._db_path)
        self._conn = None
        self._db_path = None
        self._store = None
        self._recorder = None

    # ── attitude ────────────────────────────────────────────────────────────

    def _offer_attitude(self, data: dict[str, Any]) -> None:
        """Buffer one sample, if there is a mission for it to belong to.

        The mission id is stamped here rather than at the flush. A sample
        belongs to the mission that was open when the IMU was read, and a
        profile change between the two would otherwise file the last second of
        one trip under the next.
        """
        if self._mission is None:
            return
        self._attitude.offer(
            recorder_module.build_attitude(
                data, mission_id=self._mission.id, now=time.time()
            )
        )

    def _flush_attitude(self) -> None:
        """Write everything buffered, or put it back if the write failed.

        Called before ``_write_row`` and again before a mission closes, so the
        samples land while the mission they name is still the open one — a batch
        written after the close would sit past its own mission's ``ended_at``.
        """
        if self._recorder is None:
            return
        batch = self._attitude.drain()
        if not batch:
            return
        if not self._recorder.write_attitude(batch):
            # Held rather than dropped: a full card that is emptied, or a
            # filesystem remounted read-write, gets the samples on the next
            # tick. The buffer is bounded, so holding them cannot grow without
            # limit.
            self._attitude.restore(batch)

    # ── the row ─────────────────────────────────────────────────────────────

    def _write_row(self) -> None:
        if self._mission is None or self._recorder is None:
            return
        row = recorder_module.build_row(
            mission_id=self._mission.id,
            profile=self._mission.profile,
            obc_state=self.mission_state.value if self.mission_state else None,
            eps=self._eps,
            adcs=self._adcs,
            science=self._science,
            metrics=metrics_module.collect(str(config.DATA_DIR)),
        )
        if self._recorder.write(row):
            self._mission_rows += 1
            self._last_write = time.time()

    # ── retention ───────────────────────────────────────────────────────────

    def _maybe_purge(self) -> None:
        if time.monotonic() < self._next_purge:
            return
        self._purge()

    def _purge(self) -> None:
        if self._conn is None:
            return
        self._next_purge = time.monotonic() + PURGE_INTERVAL_SEC
        retention.purge(
            self._conn,
            self.log,
            days=config.DHS_RETENTION_DAYS,
            photos_root=config.PHOTOS_DIR,
            purge_photos=config.DHS_PURGE_PHOTOS,
        )

    # ── outbound status ─────────────────────────────────────────────────────

    def _publish_status(self) -> None:
        """The retained status: what is being recorded, where, and with how much room.

        ``mission.id`` is what PAYLOAD files photographs under, ``recording`` is
        what OBC's CRITICAL grace waits on, and the free-space pair is PAYLOAD's
        floor and this service's horizon — the same headroom from two sides —
        reported together so they can be compared without an ssh session.
        """
        mission = None
        if self._mission is not None:
            mission = {
                "id": self._mission.id,
                "label": self._mission.label,
                "started_at": self._mission.started_at,
                "rows": self._mission_rows,
            }
        self.publish(
            "dhs_status",
            qos=1,
            recording=self._mission is not None,
            database=str(self._db_path) if self._db_path is not None else None,
            mission=mission,
            rows=self._total_rows(),
            db_size_bytes=self._db_size_bytes(),
            last_write=self._last_write,
            retention_days=config.DHS_RETENTION_DAYS,
            # Reported apart from `rows`: one telemetry row and sixty attitude
            # samples are not sixty-one of the same thing. `buffered` is the
            # number that says a card has stopped accepting writes while the
            # service is still, correctly, alive.
            attitude={
                "written": self._recorder.attitude_written if self._recorder else 0,
                "buffered": len(self._attitude),
                "min_interval_sec": config.DHS_ATTITUDE_MIN_INTERVAL_SEC,
            },
            photos={
                # Retention never covers these: they belong to no mission, so no
                # rule here can say when they stop being wanted. Reported so a
                # person can decide.
                "unfiled_bytes": retention.unfiled_bytes(config.PHOTOS_DIR),
                "free_mb": _rounded(retention.free_mb(config.DATA_DIR)),
                "min_free_mb": config.PHOTOS_MIN_FREE_MB,
            },
        )

    def _total_rows(self) -> int | None:
        """Rows in the whole telemetry table, or None if it cannot be counted."""
        if self._recorder is None:
            return None
        try:
            return self._recorder.count()
        except sqlite3.Error:
            self.log.exception("could not count the telemetry table")
            return None

    def _db_size_bytes(self) -> int | None:
        """What the recorder is occupying on the card, sidecars included.

        The write-ahead log is counted with the database: in WAL mode the main
        file only grows at a checkpoint, so reporting it alone would understate
        the card usage during exactly the long run where the number matters.
        """
        if self._db_path is None:
            return None
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = self._db_path.with_name(self._db_path.name + suffix)
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
