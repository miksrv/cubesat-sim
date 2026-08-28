"""Retention, and the fences around deleting somebody's photographs.

The row purge is arithmetic. The photo purge is the most destructive thing in
this codebase, so most of this module is about what it must *not* touch: not
``unfiled/``, not a mission still inside the horizon, not one still running, and
nothing at all that came from listing the directory rather than from the
database.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from cubesat.common.states import EndReason
from cubesat.dhs import retention, schema
from cubesat.dhs.missions import MissionStore

LOG = logging.getLogger("test-dhs")

#: A fixed "now", so "31 days ago" is an assertion and not a race with the clock.
NOW = 1_800_000_000.0
DAY = retention.SECONDS_PER_DAY


@pytest.fixture
def conn(tmp_path):
    connection = schema.connect(tmp_path / "comms.db", LOG)
    yield connection
    connection.close()


@pytest.fixture
def photos(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    return root


def with_photos(root: Path, name: str, count: int = 2) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    for index in range(count):
        (directory / f"photo_{index}.jpg").write_bytes(b"x" * 100)
    return directory


def recorded(conn, mission_id: int, *, days_ago: float) -> None:
    conn.execute(
        "INSERT INTO telemetry (timestamp, mission_id) VALUES (?, ?)",
        (schema.utc_iso(NOW - days_ago * DAY), mission_id),
    )


def aged_mission(conn, *, days_ago: float, rows: int = 1) -> int:
    """A closed mission whose telemetry is ``days_ago`` old."""
    store = MissionStore(conn, LOG)
    mission = store.open("FLIGHT")
    for _ in range(rows):
        recorded(conn, mission.id, days_ago=days_ago)
    store.close(mission.id, EndReason.SHUTDOWN, ended_at=schema.utc_iso(NOW - days_ago * DAY))
    return mission.id


def sampled(conn, mission_id: int, *, days_ago: float, count: int = 1) -> None:
    """Attitude for a mission, at the epoch float that table stores."""
    conn.executemany(
        "INSERT INTO attitude (mission_id, t, quat_w) VALUES (?, ?, 1.0)",
        [(mission_id, NOW - days_ago * DAY + index) for index in range(count)],
    )


def purge(conn, photos, **kwargs):
    options = {"days": 30, "photos_root": photos, "now": NOW}
    options.update(kwargs)
    return retention.purge(conn, LOG, **options)


# ── the rows ────────────────────────────────────────────────────────────────


def test_rows_past_the_horizon_go_and_rows_inside_it_stay(conn, photos):
    old = aged_mission(conn, days_ago=31)
    recent = aged_mission(conn, days_ago=2)

    result = purge(conn, photos)

    assert result.rows == 1
    remaining = [r["mission_id"] for r in conn.execute("SELECT mission_id FROM telemetry")]
    assert remaining == [recent]
    # The mission row itself is kept: a trip that happened stays listed even
    # after its detail has aged out.
    assert conn.execute("SELECT COUNT(*) AS n FROM missions").fetchone()["n"] == 2
    assert result.missions == (old,)


def test_a_purged_mission_says_so_instead_of_reporting_rows_it_no_longer_holds(conn, photos):
    # rows keeps its honest historical meaning — what the mission recorded — and
    # purged_at is what lets a dashboard render "detail aged out" rather than an
    # empty chart with no explanation. A mission reporting 1440 rows while
    # holding none is exactly the plausible wrong number this column removes.
    mission = aged_mission(conn, days_ago=31, rows=3)

    purge(conn, photos)

    row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission,)).fetchone()
    assert row["rows"] == 3
    assert row["purged_at"] == schema.utc_iso(NOW)
    assert conn.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()["n"] == 0


def test_a_mission_inside_the_horizon_is_not_stamped(conn, photos):
    mission = aged_mission(conn, days_ago=2)
    purge(conn, photos)
    row = conn.execute("SELECT purged_at FROM missions WHERE id = ?", (mission,)).fetchone()
    assert row["purged_at"] is None


def test_a_mission_is_a_purge_candidate_exactly_once(conn, photos):
    # The strongest form of the fence: a photo directory can only ever be named
    # by the pass that stamps its mission for the first time.
    mission = aged_mission(conn, days_ago=31)
    directory = with_photos(photos, str(mission))

    assert purge(conn, photos).missions == (mission,)
    assert not directory.exists()

    # Recreated by hand, standing in for anything that might put a directory of
    # that name back. A second pass must not go near it.
    resurrected = with_photos(photos, str(mission))
    second = purge(conn, photos)

    assert second.missions == ()
    assert second.files == 0
    assert resurrected.exists()


def test_the_stamp_lands_even_when_the_photos_are_deliberately_kept(conn, photos):
    # purged_at is a statement about the telemetry, which is gone either way.
    mission = aged_mission(conn, days_ago=31)
    purge(conn, photos, purge_photos=False)
    row = conn.execute("SELECT purged_at FROM missions WHERE id = ?", (mission,)).fetchone()
    assert row["purged_at"] == schema.utc_iso(NOW)


def test_a_failing_database_costs_a_retention_pass_and_nothing_else(conn, photos, caplog):
    # Unbounded growth is a problem for tomorrow; an exception out of here would
    # stop the recorder, which is a problem for the mission being recorded now.
    conn.close()
    with caplog.at_level(logging.ERROR):
        result = purge(conn, photos)

    assert result == retention.PurgeResult()
    assert "not bounded this cycle" in caplog.text


# ── the photos ──────────────────────────────────────────────────────────────


def test_a_purged_mission_takes_its_photo_directory_with_it(conn, photos, caplog):
    # Photos live exactly as long as the mission they belong to. That is a rule
    # an operator can hold in their head, which is the point of it.
    mission = aged_mission(conn, days_ago=31)
    directory = with_photos(photos, str(mission))

    with caplog.at_level(logging.INFO):
        result = purge(conn, photos)

    assert not directory.exists()
    assert (result.files, result.bytes_reclaimed) == (2, 200)
    # A deletion nobody can find afterwards in a log did not happen, as far as
    # an operator can ever tell.
    assert f"mission {mission} purged" in caplog.text
    assert "2 file(s), 200 bytes reclaimed" in caplog.text


def test_unfiled_photos_are_never_touched(conn, photos):
    # They belong to no mission, so no retention rule here covers when they stop
    # being wanted. Their size is reported in dhs_status and a person decides.
    aged_mission(conn, days_ago=31)
    unfiled = with_photos(photos, retention.UNFILED, count=3)

    purge(conn, photos)

    assert unfiled.exists()
    assert len(list(unfiled.iterdir())) == 3


def test_only_the_directory_of_a_mission_purged_in_this_pass_is_deleted(conn, photos):
    # Never a pattern, never a sweep of photos/, and never a name that came from
    # listing the directory. The ids come from the database.
    old = aged_mission(conn, days_ago=31)
    recent = aged_mission(conn, days_ago=2)
    old_dir = with_photos(photos, str(old))
    recent_dir = with_photos(photos, str(recent))
    stray = with_photos(photos, "not-a-mission-id")

    purge(conn, photos)

    assert not old_dir.exists()
    assert recent_dir.exists()
    assert stray.exists()


def test_a_mission_still_running_is_never_a_purge_candidate(conn, photos):
    # Its ended_at is null, so it cannot match the horizon at all — which is
    # what keeps the pass away from the session being recorded right now.
    store = MissionStore(conn, LOG)
    mission = store.open("FLIGHT")
    directory = with_photos(photos, str(mission.id))

    assert purge(conn, photos).missions == ()
    assert directory.exists()


def test_a_mission_that_recorded_nothing_but_holds_photos_is_still_covered(conn, photos):
    # A photo can be filed under a mission that never wrote a telemetry row.
    # Driving the pass from the missions table rather than from what the delete
    # touched is what makes that directory reachable at all.
    store = MissionStore(conn, LOG)
    mission = store.open("DEMO")
    store.close(mission.id, EndReason.SHUTDOWN, ended_at=schema.utc_iso(NOW - 31 * DAY))
    directory = with_photos(photos, str(mission.id))

    assert purge(conn, photos).missions == (mission.id,)
    assert not directory.exists()


def test_a_mission_with_no_photo_directory_purges_quietly(conn, photos, caplog):
    mission = aged_mission(conn, days_ago=31)

    with caplog.at_level(logging.INFO):
        result = purge(conn, photos)

    assert result.missions == (mission,)
    assert (result.files, result.bytes_reclaimed) == (0, 0)
    assert "purged, removed" not in caplog.text


def test_turning_the_photo_purge_off_bounds_the_database_and_not_the_card(conn, photos, caplog):
    # A real choice with a real consequence: nothing else on this satellite ever
    # removes an image, so the card then fills on its own schedule.
    mission = aged_mission(conn, days_ago=31)
    directory = with_photos(photos, str(mission))

    with caplog.at_level(logging.INFO):
        result = purge(conn, photos, purge_photos=False)

    assert result.rows == 1
    assert directory.exists()
    assert "the card is unbounded" in caplog.text


def test_with_the_photo_purge_off_and_nothing_aged_out_nothing_is_said(conn, photos, caplog):
    aged_mission(conn, days_ago=2)
    with caplog.at_level(logging.INFO):
        assert purge(conn, photos, purge_photos=False) == retention.PurgeResult()
    assert "the card is unbounded" not in caplog.text


def test_a_deletion_that_fails_is_logged_and_does_not_block_the_row_purge(
    conn, photos, monkeypatch, caplog
):
    # The database staying bounded is the more important of the two guarantees,
    # and it must not depend on the filesystem cooperating.
    mission = aged_mission(conn, days_ago=31)
    directory = with_photos(photos, str(mission))
    monkeypatch.setattr(
        shutil, "rmtree", lambda _path: (_ for _ in ()).throw(OSError("read-only filesystem"))
    )

    with caplog.at_level(logging.ERROR):
        result = purge(conn, photos)

    assert result.rows == 1
    assert (result.files, result.bytes_reclaimed) == (0, 0)
    assert directory.exists()
    assert "its rows are gone but its photos are not" in caplog.text


def test_only_a_run_of_digits_can_name_a_directory_this_module_will_delete(photos):
    # The fence, stated as an allowlist: a mission id is digits, and nothing
    # else under photos/ ever is. That holds against inputs nobody thought of,
    # where a list of forbidden names would only hold against the ones somebody
    # did — and being wrong here costs somebody their photographs.
    assert retention.photo_dir(photos, 42) == photos / "42"
    assert retention.photo_dir(photos, retention.UNFILED) is None
    assert retention.photo_dir(photos, "../../etc") is None
    assert retention.photo_dir(photos, "") is None


# ── reporting ───────────────────────────────────────────────────────────────


def test_the_size_of_the_unfiled_directory_is_reported_rather_than_acted_on(photos):
    with_photos(photos, retention.UNFILED, count=4)
    assert retention.unfiled_bytes(photos) == 400


def test_an_absent_unfiled_directory_is_zero_bytes_and_not_an_error(photos):
    assert retention.unfiled_bytes(photos) == 0


def test_a_file_that_vanishes_mid_walk_is_skipped_rather_than_raised(photos, monkeypatch):
    # The number is for a human to read, and an approximate one beats an
    # exception out of a status message OBC may be waiting on.
    directory = with_photos(photos, "5")
    monkeypatch.setattr(
        Path, "is_file", lambda _self: (_ for _ in ()).throw(OSError("gone"))
    )
    assert retention.directory_size(directory) == (0, 0)


def test_free_space_is_reported_in_the_same_unit_as_payload_s_floor(tmp_path):
    # PAYLOAD's photos.min_free_mb and this horizon are the same headroom seen
    # from two sides, so the two numbers have to be comparable as printed.
    free = retention.free_mb(tmp_path)
    assert free is not None and free > 0


def test_a_filesystem_that_cannot_be_interrogated_is_a_missing_number(tmp_path, monkeypatch):
    monkeypatch.setattr(
        retention.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("no such device")),
    )
    assert retention.free_mb(tmp_path) is None


# ── attitude ────────────────────────────────────────────────────────────────


def test_attitude_ages_out_against_the_same_horizon_as_telemetry(conn, photos):
    # Both are a mission's detail and both belong to it. A rule that aged one
    # out and kept the other would leave a mission that can be replayed but not
    # charted — a state nobody would think to test and every consumer would have
    # to handle.
    old = aged_mission(conn, days_ago=31)
    recent = aged_mission(conn, days_ago=2)
    sampled(conn, old, days_ago=31, count=5)
    sampled(conn, recent, days_ago=2, count=3)

    result = purge(conn, photos)

    assert result.attitude == 5
    remaining = [r["mission_id"] for r in conn.execute("SELECT mission_id FROM attitude")]
    assert remaining == [recent] * 3


def test_attitude_is_counted_apart_from_telemetry_rows(conn, photos):
    # At 1 Hz a mission holds thousands of samples against a few dozen rows.
    # One total would be a number about attitude wearing a telemetry label.
    old = aged_mission(conn, days_ago=31, rows=2)
    sampled(conn, old, days_ago=31, count=7)

    result = purge(conn, photos)

    assert (result.rows, result.attitude) == (2, 7)


def test_a_mission_with_only_attitude_left_is_still_purged(conn, photos):
    # The aged-out test asks whether any telemetry is left, deliberately not
    # whether any attitude is: both empty in the same transaction, so a second
    # condition could never independently be false. This is the assertion that
    # keeps that reasoning true.
    old = aged_mission(conn, days_ago=31)
    sampled(conn, old, days_ago=31, count=4)

    result = purge(conn, photos)

    assert result.missions == (old,)
    assert conn.execute("SELECT COUNT(*) AS n FROM attitude").fetchone()["n"] == 0
    assert conn.execute("SELECT purged_at FROM missions WHERE id = ?", (old,)).fetchone()[
        "purged_at"
    ] is not None


def test_the_running_mission_keeps_its_attitude(conn, photos):
    # An open mission has a null ended_at and can never be a purge candidate.
    # Its recent samples are inside the horizon for the same reason its rows are.
    store = MissionStore(conn, LOG)
    running = store.open("FLIGHT")
    sampled(conn, running.id, days_ago=0, count=3)

    result = purge(conn, photos)

    assert result.attitude == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM attitude").fetchone()["n"] == 3


def test_the_horizon_is_compared_as_a_number_and_not_as_the_iso_string(conn, photos):
    # telemetry.timestamp is ISO text and attitude.t is an epoch float. Handing
    # the string cutoff to the attitude delete would compare text with a number
    # and silently match nothing at all — a retention pass that reports success
    # while the table grows forever.
    old = aged_mission(conn, days_ago=100)
    sampled(conn, old, days_ago=100, count=2)

    assert purge(conn, photos).attitude == 2
