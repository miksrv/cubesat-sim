"""The read-only view of the recorder's database.

Against a real SQLite file built by the real schema, because what is asserted
here is that a reader and a writer can share one file — and mocking either side
would assert only that the mock agrees with itself.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from cubesat.common.states import EndReason
from cubesat.dashboard.archive import DEFAULT_TELEMETRY_LIMIT, MAX_ROWS, Archive
from cubesat.dhs import schema
from cubesat.dhs.missions import MissionStore

LOG = logging.getLogger("test-dashboard")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "comms.db"


@pytest.fixture
def recorded(db_path):
    """A database with one closed mission and one still running."""
    conn = schema.connect(db_path, LOG)
    store = MissionStore(conn, LOG)

    closed = store.open("FLIGHT", "walk to work")
    for index in range(3):
        conn.execute(
            "INSERT INTO telemetry (timestamp, mission_id, battery) VALUES (?, ?, ?)",
            (f"2026-08-24T07:0{index}:00Z", closed.id, 90 - index),
        )
    conn.executemany(
        "INSERT INTO attitude (mission_id, t, quat_w) VALUES (?, ?, ?)",
        [(closed.id, 1000.0 + step, 0.99) for step in range(5)],
    )
    store.close(closed.id, EndReason.PROFILE_CHANGE)

    running = store.open("DEMO")
    conn.execute(
        "INSERT INTO telemetry (timestamp, mission_id, battery) VALUES (?, ?, ?)",
        ("2026-08-24T08:00:00Z", running.id, 74),
    )
    conn.close()
    return {"closed": closed.id, "running": running.id}


@pytest.fixture
def archive(db_path, recorded):
    view = Archive(db_path, log=LOG)
    yield view
    view.close()


# ── the file may not be there ───────────────────────────────────────────────


def test_a_database_that_does_not_exist_is_an_empty_archive(tmp_path, caplog):
    # A satellite in HOSTED has never opened one. Answering 500 for "nothing has
    # been recorded yet" would report a fault where there is none.
    view = Archive(tmp_path / "absent.db", log=LOG)
    with caplog.at_level(logging.INFO):
        assert view.missions() == []
        assert view.telemetry() == []
        assert view.mission(1) is None
    assert "serving an empty archive" in caplog.text


def test_the_missing_database_is_reported_once_and_not_per_request(tmp_path, caplog):
    view = Archive(tmp_path / "absent.db", log=LOG)
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            view.missions()
    assert caplog.text.count("serving an empty archive") == 1


def test_a_database_that_appears_later_is_picked_up(tmp_path, db_path, recorded):
    # The realistic order on a fresh satellite: the dashboard starts under a
    # profile that records nothing, and a mission opens afterwards.
    view = Archive(db_path.parent / "later.db", log=LOG)
    assert view.missions() == []
    db_path.rename(db_path.parent / "later.db")
    assert len(view.missions()) == 2


# ── reading ─────────────────────────────────────────────────────────────────


def test_telemetry_comes_back_newest_first(archive):
    # The caller's first use of these rows is the *latest* one: the host's CPU,
    # RAM and disk are on no status topic, so this is where a live dashboard
    # reads them from.
    rows = archive.telemetry()
    assert [row["battery"] for row in rows] == [74, 88, 89, 90]


def test_the_limit_is_honoured_and_bounded(archive):
    assert len(archive.telemetry(2)) == 2
    # A browser over an access point asking for everything should fail fast
    # rather than serve slowly.
    assert archive.telemetry(MAX_ROWS * 10) == archive.telemetry(MAX_ROWS)


@pytest.mark.parametrize("bad", ["nonsense", None, 0, -5])
def test_a_limit_that_is_not_a_number_falls_back_rather_than_raising(archive, bad):
    # It arrives from a query string.
    assert len(archive.telemetry(bad)) <= DEFAULT_TELEMETRY_LIMIT


def test_missions_come_back_newest_first(archive, recorded):
    assert [row["id"] for row in archive.missions()] == [recorded["running"], recorded["closed"]]


def test_one_mission_carries_its_summary(archive, recorded):
    mission = archive.mission(recorded["closed"])
    assert mission is not None
    assert mission["label"] == "walk to work"
    assert mission["profile"] == "FLIGHT"
    assert mission["ended_at"] is not None


def test_a_mission_that_is_not_there_is_none_and_not_an_error(archive):
    assert archive.mission(9999) is None


def test_a_mission_s_rows_come_back_oldest_first(archive, recorded):
    # The order a timeline plays them.
    rows = archive.mission_telemetry(recorded["closed"])
    assert [row["battery"] for row in rows] == [90, 89, 88]


def test_attitude_comes_back_oldest_first(archive, recorded):
    samples = archive.mission_attitude(recorded["closed"])
    assert [sample["t"] for sample in samples] == [1000.0, 1001.0, 1002.0, 1003.0, 1004.0]
    assert samples[0]["quat_w"] == 0.99


def test_a_mission_with_no_attitude_gets_an_empty_list(archive, recorded):
    assert archive.mission_attitude(recorded["running"]) == []


def test_a_database_older_than_the_attitude_table_is_still_readable(tmp_path, caplog):
    """A trip recorded before attitude existed is still a trip that happened.

    Refusing to open it, or raising on the query, would lose a mission to a
    schema version — so the table is asked for rather than assumed.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    with conn:
        for statement in schema.MIGRATIONS[0].statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO missions (id, profile, started_at) "
            "VALUES (1, 'FLIGHT', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO telemetry (timestamp, mission_id) VALUES ('2026-01-01T00:00:00Z', 1)"
        )
    conn.close()

    view = Archive(path, log=LOG)
    with caplog.at_level(logging.ERROR):
        assert view.mission_attitude(1) == []
    assert len(view.mission_telemetry(1)) == 1
    assert caplog.text == ""
    view.close()


# ── it must not disturb the writer ──────────────────────────────────────────


def test_the_connection_is_read_only(archive, recorded):
    # mode=ro rather than a convention: a stray write fails at SQLite instead of
    # in review, and the recorder is the only process that may hold a lock.
    conn = archive._connection()
    assert conn is not None
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO telemetry (timestamp, mission_id) VALUES ('x', 1)")


def test_reading_does_not_block_the_recorder(db_path, archive, recorded):
    # The property the whole module exists for: DHS keeps writing a mission
    # while somebody looks at a chart.
    archive.missions()
    writer = schema.connect(db_path, LOG)
    with schema.transaction(writer) as tx:
        tx.execute(
            "INSERT INTO telemetry (timestamp, mission_id) VALUES ('2026-08-24T09:00:00Z', ?)",
            (recorded["running"],),
        )
    writer.close()
    assert len(archive.telemetry()) == 5


def test_a_query_against_a_broken_file_is_survived(db_path, archive, recorded, caplog):
    # A card pulled mid-read costs the panel that asked, never the service.
    archive.missions()
    db_path.write_bytes(b"this is not a database")
    with caplog.at_level(logging.ERROR):
        assert archive.missions() == []
    assert "archive query failed" in caplog.text


def test_a_file_that_cannot_be_opened_is_an_empty_archive(tmp_path, caplog):
    # A directory where a database should be — the shape a half-finished deploy
    # leaves behind. Logged and survived, like every other read failure here.
    impostor = tmp_path / "comms.db"
    impostor.mkdir()
    view = Archive(impostor, log=LOG)
    with caplog.at_level(logging.ERROR):
        assert view.missions() == []
    assert "read-only" in caplog.text
