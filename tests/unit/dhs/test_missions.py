"""Mission sessions, against a real database file.

Orphan recovery gets more of this module than the happy path does, in the same
proportion the design gives it: opening and closing a mission is a pair of
statements, while closing one that nothing was left alive to close is the thing
that keeps a year of history queryable after a flat battery.
"""

from __future__ import annotations

import logging

import pytest

from cubesat.common import config
from cubesat.common.states import EndReason
from cubesat.dhs import schema
from cubesat.dhs.missions import MissionStore, haversine_m, track_length_m

LOG = logging.getLogger("test-dhs")

#: Two points about 111 m apart: a thousandth of a degree of latitude. Real
#: enough to catch a haversine written in degrees instead of radians.
NORTH = (55.7558, 37.6173)
NORTH_PLUS_1000TH = (55.7568, 37.6173)

#: Metres per degree of latitude, near enough for building test tracks.
METRES_PER_DEGREE = 111_320.0


def north_of(base, metres):
    """A point ``metres`` north of ``base``, for tracks measured in metres."""
    return (base[0] + metres / METRES_PER_DEGREE, base[1])


@pytest.fixture
def conn(tmp_path):
    connection = schema.connect(tmp_path / "comms.db", LOG)
    yield connection
    connection.close()


@pytest.fixture
def store(conn):
    return MissionStore(conn, LOG)


def add_row(conn, mission_id, timestamp, *, lat=None, lon=None, fix=None):
    """One telemetry row, with only the fields these tests care about."""
    conn.execute(
        "INSERT INTO telemetry (timestamp, mission_id, lat, lon, fix) VALUES (?, ?, ?, ?, ?)",
        (timestamp, mission_id, lat, lon, fix),
    )


def mission_row(conn, mission_id):
    return conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()


# ── opening and closing ─────────────────────────────────────────────────────


def test_opening_a_mission_stamps_it_with_the_profile_and_the_label(store, conn):
    mission = store.open("FLIGHT", "walk to work")

    row = mission_row(conn, mission.id)
    assert row["profile"] == "FLIGHT"
    assert row["label"] == "walk to work"
    assert row["started_at"] == mission.started_at
    # Null while it runs. Anything still null at the next startup is an orphan.
    assert row["ended_at"] is None


def test_a_mission_may_have_no_label_at_all(store, conn):
    # A label is for grouping in a dashboard, not for identity, so a profile
    # applied without one is entirely normal.
    mission = store.open("DEMO")
    assert mission_row(conn, mission.id)["label"] is None


def test_closing_a_mission_records_the_reason_and_what_its_own_rows_say(store, conn):
    mission = store.open("FLIGHT", "walk to work")
    add_row(conn, mission.id, "2026-08-24T07:00:00Z")
    add_row(conn, mission.id, "2026-08-24T07:00:30Z", lat=NORTH[0], lon=NORTH[1], fix=1)
    add_row(
        conn,
        mission.id,
        "2026-08-24T07:01:00Z",
        lat=NORTH_PLUS_1000TH[0],
        lon=NORTH_PLUS_1000TH[1],
        fix=1,
    )

    summary = store.close(mission.id, EndReason.SHUTDOWN)

    row = mission_row(conn, mission.id)
    assert row["end_reason"] == "shutdown"
    assert row["ended_at"] is not None
    # Derived once, on close, and stored: a listing of forty missions must not
    # scan the telemetry table forty times to draw itself.
    assert row["rows"] == summary.rows == 3
    assert row["first_fix_at"] == "2026-08-24T07:00:30Z"
    assert row["distance_m"] == pytest.approx(111.2, abs=1.0)


def test_a_mission_that_never_had_a_fix_gets_a_null_distance_not_a_zero(store, conn):
    # An indoor DEMO did not travel zero metres; it has no track at all, and a
    # chart has to be able to tell those two apart.
    mission = store.open("DEMO")
    add_row(conn, mission.id, "2026-08-24T07:00:00Z", fix=0)

    summary = store.close(mission.id, EndReason.PROFILE_CHANGE)

    assert summary.distance_m is None
    assert summary.first_fix_at is None
    assert mission_row(conn, mission.id)["distance_m"] is None


def test_a_mission_with_a_single_fix_travelled_zero_metres(store, conn):
    # It sat on a windowsill with a fix. That is a track of zero length, which
    # is a different statement from having no track.
    mission = store.open("EXPO")
    add_row(conn, mission.id, "2026-08-24T07:00:00Z", lat=NORTH[0], lon=NORTH[1], fix=1)

    assert store.close(mission.id, EndReason.SHUTDOWN).distance_m == 0.0


def test_a_fixless_row_is_not_part_of_the_track(store, conn):
    # With no fix the receiver reports the last known coordinates, so counting
    # them would draw a line to somewhere the satellite was not.
    mission = store.open("FLIGHT")
    add_row(conn, mission.id, "2026-08-24T07:00:00Z", lat=NORTH[0], lon=NORTH[1], fix=1)
    add_row(conn, mission.id, "2026-08-24T07:00:30Z", lat=0.0, lon=0.0, fix=0)

    assert store.close(mission.id, EndReason.SHUTDOWN).distance_m == 0.0


def test_another_mission_s_rows_are_not_counted_into_this_one(store, conn):
    first = store.open("DEMO")
    second = store.open("DEMO")
    add_row(conn, first.id, "2026-08-24T07:00:00Z")
    add_row(conn, second.id, "2026-08-24T07:00:00Z")
    add_row(conn, second.id, "2026-08-24T07:00:30Z")

    assert store.close(first.id, EndReason.SHUTDOWN).rows == 1
    assert store.close(second.id, EndReason.SHUTDOWN).rows == 2


# ── orphan recovery ─────────────────────────────────────────────────────────


def test_a_mission_left_open_is_closed_at_its_own_last_row(store, conn, caplog):
    # The satellite died on battery halfway through a trip. The last row that
    # landed is the last moment it is known to have been recording, and that is
    # the honest place to end the mission — not whenever it was plugged back in.
    mission = store.open("FLIGHT", "walk to work")
    add_row(conn, mission.id, "2026-08-24T07:00:00Z")
    add_row(conn, mission.id, "2026-08-24T07:31:00Z", lat=NORTH[0], lon=NORTH[1], fix=1)

    with caplog.at_level(logging.WARNING):
        recovered = MissionStore(conn, LOG).recover_orphans()

    assert recovered == [mission.id]
    row = mission_row(conn, mission.id)
    assert row["ended_at"] == "2026-08-24T07:31:00Z"
    assert row["end_reason"] == EndReason.INTERRUPTED.value
    # The derived values are filled in too: nothing else will ever compute them.
    assert row["rows"] == 2
    assert row["first_fix_at"] == "2026-08-24T07:31:00Z"
    assert "was never closed" in caplog.text


def test_a_mission_left_open_with_no_rows_at_all_is_closed_where_it_started(store, conn):
    # Ending it "now" would invent a duration out of however long the satellite
    # was switched off — a minute-long session recovered three days later would
    # be recorded as three days. Leaving it open is what recovery exists to
    # prevent. started_at says the true thing: it was opened, nothing happened.
    mission = store.open("DEMO")

    MissionStore(conn, LOG).recover_orphans()

    row = mission_row(conn, mission.id)
    assert row["ended_at"] == row["started_at"] == mission.started_at
    assert row["end_reason"] == EndReason.INTERRUPTED.value
    assert row["rows"] == 0
    assert row["distance_m"] is None


def test_recovery_closes_every_orphan_and_leaves_closed_missions_alone(store, conn):
    # More than one open mission means more than one hard stop, which is exactly
    # the history that must not be quietly overwritten.
    closed = store.open("DEMO")
    store.close(closed.id, EndReason.SHUTDOWN)
    first_orphan = store.open("EXPO")
    second_orphan = store.open("FLIGHT")

    assert MissionStore(conn, LOG).recover_orphans() == [first_orphan.id, second_orphan.id]
    assert mission_row(conn, closed.id)["end_reason"] == EndReason.SHUTDOWN.value


def test_recovery_on_a_database_with_nothing_to_recover_does_nothing(store, conn, caplog):
    mission = store.open("DEMO")
    store.close(mission.id, EndReason.SHUTDOWN)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert MissionStore(conn, LOG).recover_orphans() == []
    # Nothing was recovered, so nothing is worth an operator's attention.
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


# ── the track ───────────────────────────────────────────────────────────────


def test_a_thousandth_of_a_degree_of_latitude_is_about_a_hundred_and_eleven_metres():
    # The arithmetic check that catches a haversine fed degrees where it wanted
    # radians — an error that still produces plausible-looking distances.
    assert haversine_m(NORTH, NORTH_PLUS_1000TH) == pytest.approx(111.2, abs=1.0)


def test_the_same_point_twice_is_no_distance_at_all():
    assert haversine_m(NORTH, NORTH) == pytest.approx(0.0, abs=1e-9)


def test_a_track_of_no_points_has_no_length_rather_than_a_length_of_zero():
    assert track_length_m([]) is None
    assert track_length_m([NORTH]) == 0.0


def test_a_stationary_receiver_s_wander_is_not_a_walk():
    # A consumer receiver left on a windowsill overnight drifts by metres
    # between fixes. Summed unfiltered, that is three kilometres of travel the
    # satellite never made — the same class of fault as a heading from an
    # uncalibrated magnetometer, and this project withholds rather than
    # publishing a confident wrong number.
    jitter = [north_of(NORTH, offset) for offset in (0, 2, -1.5, 1, -2, 0.5) * 20]

    assert track_length_m(jitter, min_segment_m=5.0) == 0.0
    # And without the floor, the same night invents most of a kilometre.
    assert track_length_m(jitter) > 200


def test_movement_slower_than_the_floor_is_deferred_and_not_discarded():
    # The anchor only advances when a hop is counted, so a slow walk crosses the
    # floor eventually instead of being thrown away two metres at a time.
    creep = [north_of(NORTH, 2 * step) for step in range(11)]

    measured = track_length_m(creep, min_segment_m=5.0)
    assert measured is not None
    # Twenty metres were walked; what is counted lags by less than one floor.
    assert 15 < measured <= 20


def test_the_floor_a_closed_mission_measures_against_is_the_configured_one(
    store, conn, monkeypatch
):
    monkeypatch.setattr(config, "DHS_MIN_SEGMENT_M", 500.0)
    mission = store.open("FLIGHT")
    add_row(conn, mission.id, "2026-08-24T07:00:00Z", lat=NORTH[0], lon=NORTH[1], fix=1)
    add_row(
        conn,
        mission.id,
        "2026-08-24T07:00:30Z",
        lat=NORTH_PLUS_1000TH[0],
        lon=NORTH_PLUS_1000TH[1],
        fix=1,
    )

    # 111 m is under a 500 m floor, so nothing counts — and the mission still
    # has a track, so the answer is zero rather than null.
    assert store.close(mission.id, EndReason.SHUTDOWN).distance_m == 0.0
