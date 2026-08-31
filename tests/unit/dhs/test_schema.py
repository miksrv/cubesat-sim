"""The database file itself: how it is opened, and how it is versioned.

Every test here uses a real SQLite file in ``tmp_path``. This is the persistence
layer, and a mocked database would assert only that the mock was called — not
that a migration ran, that a pragma took, or that a future version was refused,
which are the three things anyone would ever want to know about this module.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from cubesat.dhs import schema

LOG = logging.getLogger("test-dhs")

MISSION_INSERT = "INSERT INTO missions (profile, started_at) VALUES (?, ?)"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "comms.db"


def tables(conn):
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def user_version(conn):
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


# ── migrations ──────────────────────────────────────────────────────────────


def test_opening_an_empty_file_walks_the_whole_migration_path(db_path, caplog):
    # Version 1 is a migration, not a create-if-missing shortcut, so the upgrade
    # path is exercised by every fresh database rather than for the first time
    # on the day a schema change matters.
    with caplog.at_level(logging.INFO):
        conn = schema.connect(db_path, LOG)

    assert {"missions", "telemetry"}.issubset(tables(conn))
    assert user_version(conn) == schema.SCHEMA_VERSION
    for migration in schema.MIGRATIONS:
        assert f"applying schema migration {migration.version}" in caplog.text
    conn.close()


def version_1_database(path):
    """A file exactly as a build that shipped only migration 1 would leave it.

    Built from the shipped statements themselves rather than from a hand-written
    copy of them, so this stays a real version-1 database even if a later step
    changes what version 2 looks like.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    for statement in schema.MIGRATIONS[0].statements:
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(MISSION_INSERT, ("FLIGHT", "2026-08-24T07:00:00Z"))
    for minute in range(3):
        conn.execute(
            "INSERT INTO telemetry (timestamp, mission_id, lat) VALUES (?, 1, 55.7558)",
            (f"2026-08-24T07:0{minute}:00Z",),
        )
    conn.close()


def test_a_version_1_database_is_migrated_in_place_with_its_history_intact(db_path, caplog):
    # The migration path's whole reason for existing, exercised on the shape it
    # was built for: a file with real missions in it, opened by a build that
    # knows one step more than the one that wrote it.
    version_1_database(db_path)

    with caplog.at_level(logging.INFO):
        conn = schema.connect(db_path, LOG)

    assert user_version(conn) == schema.SCHEMA_VERSION == 4
    # Step 1 is not re-run; only the steps this file had never had.
    assert "applying schema migration 1" not in caplog.text
    assert "applying schema migration 2" in caplog.text
    assert "applying schema migration 3" in caplog.text
    assert "applying schema migration 4" in caplog.text
    # And the reason any of this matters: a walk to work happened once.
    assert conn.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()["n"] == 3
    mission = conn.execute("SELECT * FROM missions").fetchone()
    assert mission["profile"] == "FLIGHT"
    assert mission["started_at"] == "2026-08-24T07:00:00Z"
    # The new column arrives null: nothing has been purged, and a migration
    # must not claim otherwise about history it did not witness.
    assert mission["purged_at"] is None
    # The new table arrives empty for the same reason: a trip recorded before
    # attitude existed has no attitude, and inventing rows from the telemetry
    # columns would be a replay of something nobody measured.
    assert conn.execute("SELECT COUNT(*) AS n FROM attitude").fetchone()["n"] == 0
    # And so does radio_log: traffic that predates the table was not observed.
    assert conn.execute("SELECT COUNT(*) AS n FROM radio_log").fetchone()["n"] == 0
    conn.close()


def test_a_migrated_database_is_not_migrated_again_on_the_next_open(db_path, caplog):
    version_1_database(db_path)
    schema.connect(db_path, LOG).close()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        conn = schema.connect(db_path, LOG)

    assert "applying schema migration" not in caplog.text
    assert user_version(conn) == schema.SCHEMA_VERSION
    conn.close()


def test_opening_an_existing_database_a_second_time_changes_nothing(db_path, caplog):
    first = schema.connect(db_path, LOG)
    first.execute(MISSION_INSERT, ("DEMO", "2026-08-24T07:00:00Z"))
    first.close()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        second = schema.connect(db_path, LOG)

    assert "applying schema migration" not in caplog.text
    assert second.execute("SELECT COUNT(*) AS n FROM missions").fetchone()["n"] == 1
    second.close()


def test_a_database_from_a_future_version_is_refused_by_name_and_number(db_path):
    # Opening it read-write would let a build that does not know the newer
    # columns write rows that violate them — trading real history for a restart.
    conn = schema.connect(db_path, LOG)
    conn.execute(f"PRAGMA user_version = {schema.SCHEMA_VERSION + 7}")
    conn.close()

    with pytest.raises(schema.FutureSchemaError) as refused:
        schema.connect(db_path, LOG)

    message = str(refused.value)
    assert str(schema.SCHEMA_VERSION + 7) in message
    assert str(schema.SCHEMA_VERSION) in message
    assert str(db_path) in message


def test_a_refused_database_leaves_no_connection_behind(db_path, monkeypatch):
    # The refusal happens after connect() already has a handle. Leaking it would
    # keep a WAL open on a file this build has just declared itself unable to
    # write, which is the opposite of refusing to touch it.
    conn = schema.connect(db_path, LOG)
    conn.execute(f"PRAGMA user_version = {schema.SCHEMA_VERSION + 1}")
    conn.close()

    handles = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        handles.append(real_connect(*args, **kwargs))
        return handles[-1]

    monkeypatch.setattr(schema.sqlite3, "connect", spy)
    with pytest.raises(schema.FutureSchemaError):
        schema.connect(db_path, LOG)

    with pytest.raises(sqlite3.ProgrammingError):
        handles[0].execute("SELECT 1")


def test_a_future_version_is_a_schema_error_so_one_except_clause_catches_both():
    assert issubclass(schema.FutureSchemaError, schema.SchemaError)


def test_a_migration_that_fails_leaves_the_version_where_it_was(db_path, monkeypatch):
    # Each step is one transaction: a half-applied schema with the new version
    # stamped on it would be refused forever by a build that could have fixed it.
    # Numbered from the real head rather than from a literal, so that adding a
    # migration does not silently turn this into a test of an existing step.
    next_version = schema.SCHEMA_VERSION + 1
    broken = schema.Migration(
        version=next_version, statements=("CREATE TABLE ok (a)", "NOT SQL AT ALL")
    )
    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS, broken))
    monkeypatch.setattr(schema, "SCHEMA_VERSION", next_version)

    with pytest.raises(sqlite3.Error):
        schema.connect(db_path, LOG)

    conn = sqlite3.connect(db_path)
    # The steps that did apply are kept; the one that failed left nothing.
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == next_version - 1
    assert "ok" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()


# ── durability settings ─────────────────────────────────────────────────────


def test_the_file_is_opened_in_wal_with_synchronous_normal(db_path):
    # WAL so a torn write fails its checksum instead of corrupting the file, and
    # NORMAL so a recorder writing every 30 s for hours does not fsync an SD
    # card to death. Losing the last rows is what orphan recovery is for; losing
    # the file is losing every mission ever recorded.
    conn = schema.connect(db_path, LOG)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 1 is NORMAL; 2 would be FULL.
    assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 1
    conn.close()


def test_a_row_cannot_point_at_a_mission_that_does_not_exist(db_path):
    # A telemetry row with a dangling mission_id is a row nothing can ever
    # chart. Foreign keys turn that from a silent data bug into a failed write.
    conn = schema.connect(db_path, LOG)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO telemetry (timestamp, mission_id) VALUES ('2026-08-24T07:00:00Z', 99)"
        )
    conn.close()


def test_the_indexes_a_dashboard_needs_are_there(db_path):
    # Forty missions drawn on one page must not be forty scans of the largest
    # table in the file.
    conn = schema.connect(db_path, LOG)
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "idx_telemetry_mission_time" in names
    assert "idx_telemetry_timestamp" in names
    assert "idx_missions_started_at" in names
    conn.close()


def test_a_mission_id_is_never_reused_after_a_delete(db_path):
    # A mission id becomes a photo directory name. Reuse would put one trip's
    # photographs inside another's gallery, which no later query could untangle.
    conn = schema.connect(db_path, LOG)
    conn.execute(MISSION_INSERT, ("DEMO", "2026-08-24T07:00:00Z"))
    conn.execute("DELETE FROM missions")
    conn.execute(MISSION_INSERT, ("DEMO", "2026-08-24T08:00:00Z"))
    assert conn.execute("SELECT id FROM missions").fetchone()["id"] == 2
    conn.close()


# ── helpers ─────────────────────────────────────────────────────────────────


def test_a_transaction_that_raises_rolls_back_and_re_raises(db_path):
    conn = schema.connect(db_path, LOG)
    with pytest.raises(RuntimeError), schema.transaction(conn) as tx:
        tx.execute(MISSION_INSERT, ("DEMO", "2026-08-24T07:00:00Z"))
        raise RuntimeError("something went wrong halfway")
    assert conn.execute("SELECT COUNT(*) AS n FROM missions").fetchone()["n"] == 0
    conn.close()


def test_timestamps_are_iso_8601_utc_to_the_second():
    # The format the schema stores and the README documents. It sorts, it ranges
    # and it needs no conversion at the far end — and the DS1307 RTC makes it
    # trustworthy with no network, which is the point of recording offline.
    assert schema.utc_iso(1_741_863_600.0) == "2025-03-13T11:00:00Z"
    assert schema.utc_iso().endswith("Z")
    assert len(schema.utc_iso()) == len("2026-08-24T07:12:03Z")


def test_a_purged_mission_can_say_so_rather_than_reporting_rows_it_no_longer_holds(db_path):
    # rows keeps its honest historical meaning — what the mission recorded — and
    # purged_at is what explains why asking for that detail returns nothing.
    conn = schema.connect(db_path, LOG)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(missions)")}
    assert {"rows", "purged_at"}.issubset(columns)
    conn.close()


def test_the_insert_statement_and_the_ddl_cannot_drift_apart(db_path):
    # The writer builds its statement from TELEMETRY_COLUMNS, so a column added
    # to one and forgotten in the other is not a thing that can be written.
    conn = schema.connect(db_path, LOG)
    declared = {row["name"] for row in conn.execute("PRAGMA table_info(telemetry)")}
    assert set(schema.TELEMETRY_COLUMNS) | {"id"} == declared
    conn.close()
