"""DHS as a service: what it opens, what it records, and what it survives.

Every test writes to a real SQLite file in ``tmp_path``. Mocking the database
would assert that DHS called a mock, which is not the property anyone cares
about — the property is that a row lands, a mission closes, and neither a full
card nor a database from a newer build takes the recorder off the air.
"""

from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from cubesat.common import config
from cubesat.common.states import MissionState, Persistence, Profile
from cubesat.common.topics import RETAINED, TOPICS
from cubesat.dhs import retention, schema
from cubesat.dhs.service import DhsService

LOG = logging.getLogger("test-dhs")

EPS = {"battery_percent": 87.5, "voltage": 4.123, "external_power": True}
ADCS = {
    "roll": 1.23,
    "pitch": -0.45,
    "yaw": 178.9,
    "gnss": {"lat": 55.7558, "lon": 37.6173, "alt": 156.2, "speed": 0.4, "fix": True,
             "satellites": 23},
}
SCIENCE = {"temperature": 23.4, "humidity": 45.2, "pressure": 1013.0, "light": 412.0}


@pytest.fixture
def dhs(service_factory, monkeypatch, tmp_path):
    """A DHS whose databases and photo root are this test's own.

    Redirected before the service is built, so nothing here can reach the data
    directory the rest of the suite shares — a recorder writing into it would
    be the one test failure that shows up somewhere else.
    """
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "comms.db")
    monkeypatch.setattr(config, "DIAG_DB_PATH", tmp_path / "diag.db")
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    service, client = service_factory(DhsService)
    return service, client


def obc(client, state=MissionState.NOMINAL, profile=Profile.FLIGHT,
        persistence=Persistence.MISSION_DB, label=None):
    """Deliver the one retained message DHS opens a mission from."""
    client.deliver(
        TOPICS["obc_status"],
        {
            "status": state.value,
            "profile": profile.value,
            "cadence_scale": 1.0,
            "persistence": persistence.value,
            "mission_label": label,
        },
    )


def status(client):
    return client.last(TOPICS["dhs_status"])


def rows_in(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    finally:
        conn.close()


def missions_in(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM missions ORDER BY id")]
    finally:
        conn.close()


# ── reporting in ────────────────────────────────────────────────────────────


def test_the_first_status_goes_out_before_any_tick(dhs):
    # DHS is one of the services OBC's DEPLOY waits on, and its NOMINAL cadence
    # is 30 s — longer than the bring-up window — so the status cannot wait for
    # the first tick.
    service, client = dhs
    service.on_start()

    first = status(client)
    assert first["recording"] is False
    assert first["database"] is None
    assert first["mission"] is None
    assert first["retention_days"] == config.DHS_RETENTION_DAYS


def test_the_status_is_retained_so_payload_learns_the_mission_id_on_connect(dhs):
    service, client = dhs
    service.on_start()
    assert TOPICS["dhs_status"] in RETAINED
    assert client.published[-1].retain is True


def test_dhs_subscribes_to_every_source_it_assembles_a_row_from(dhs):
    _, client = dhs
    client.connect_ok()
    assert set(client.subscribed) == {
        TOPICS["obc_status"],
        TOPICS["eps_status"],
        TOPICS["adcs_status"],
        TOPICS["payload_data"],
        TOPICS["host_status"],
        TOPICS["comms_radio"],
        # Not a source of row data: DHS owns the database, so it is the one
        # service that can act on delete_mission.
        TOPICS["command"],
    }


def test_the_status_reports_the_headroom_payload_is_watching_from_the_other_side(dhs, tmp_path):
    # PAYLOAD's floor and this horizon are the same headroom seen from two
    # sides, so both numbers are in one message and can be compared without ssh.
    service, client = dhs
    service.on_start()

    photos = status(client)["photos"]
    assert photos["min_free_mb"] == config.PHOTOS_MIN_FREE_MB
    assert photos["free_mb"] > 0
    # `unfiled_bytes` was here until 2026-09-01 and is deliberately not replaced:
    # a photograph taken with no mission open never reaches the card now, so
    # there is no longer a pile of files no policy covers.
    assert "unfiled_bytes" not in photos


# ── opening a mission ───────────────────────────────────────────────────────


def test_a_profile_that_permits_persistence_opens_a_mission_on_reaching_nominal(dhs, tmp_path):
    service, client = dhs
    service.on_start()
    obc(client, label="walk to work")

    assert service._mission is not None
    reported = status(client)
    assert reported["recording"] is True
    assert reported["database"] == str(tmp_path / "comms.db")
    assert reported["mission"]["id"] == 1
    assert reported["mission"]["label"] == "walk to work"
    assert missions_in(tmp_path / "comms.db")[0]["profile"] == "FLIGHT"


def test_the_mission_id_is_published_the_moment_the_mission_opens(dhs):
    # PAYLOAD files photographs under this id and learns it from here, so it
    # cannot wait for the next tick — which in LOW_POWER is five minutes away.
    service, client = dhs
    service.on_start()
    before = len(client.payloads(TOPICS["dhs_status"]))
    obc(client)
    assert len(client.payloads(TOPICS["dhs_status"])) == before + 1


def test_a_profile_with_no_persistence_records_nothing(dhs, tmp_path):
    service, client = dhs
    service.on_start()
    obc(client, profile=Profile.HOSTED, persistence=Persistence.NONE)

    service.tick()
    assert status(client)["recording"] is False
    assert not (tmp_path / "comms.db").exists()


def test_a_diag_profile_records_into_its_own_database(dhs, tmp_path):
    # Bench runs are missions too, with the same schema, so one dashboard
    # renders a bench run and a trip with no special case — but not in the same
    # file as a real trip.
    service, client = dhs
    service.on_start()
    obc(client, profile=Profile.DIAG, persistence=Persistence.DIAG_DB)
    service.tick()

    assert status(client)["database"] == str(tmp_path / "diag.db")
    assert rows_in(tmp_path / "diag.db") == 1
    assert not (tmp_path / "comms.db").exists()


def test_a_persistence_value_this_build_does_not_know_records_nothing(dhs, caplog):
    # The conservative direction: DHS does not know which database that profile
    # intends, and inventing one is how a bench run lands inside a real trip.
    service, client = dhs
    service.on_start()
    with caplog.at_level(logging.WARNING):
        client.deliver(
            TOPICS["obc_status"],
            {"status": "NOMINAL", "profile": "DEMO", "persistence": "somewhere_new"},
        )

    assert service._mission is None
    assert "unknown persistence" in caplog.text


def test_a_mission_label_applies_to_the_next_mission_and_not_to_a_running_one(dhs, tmp_path):
    # Labels are for grouping, not identity. Renaming a session halfway through
    # would make the label a claim about a period it did not cover.
    service, client = dhs
    service.on_start()
    obc(client, label="walk to work")
    obc(client, label="something else")

    assert missions_in(tmp_path / "comms.db")[0]["label"] == "walk to work"


# ── the recording gate ──────────────────────────────────────────────────────


def test_nominal_records_with_no_command_from_the_ground(dhs, tmp_path):
    # The pre-rewrite gate — write only while a ground-commanded SCIENCE state
    # was active — is deliberately gone, and so is that state. Keeping it would
    # have meant FLIGHT, the profile whose entire purpose is recording a track,
    # recorded nothing unless somebody remembered a command before leaving the
    # house.
    service, client = dhs
    service.on_start()
    obc(client, state=MissionState.NOMINAL)
    service.tick()

    assert rows_in(tmp_path / "comms.db") == 1


@pytest.mark.parametrize("state", [MissionState.LOW_POWER, MissionState.SAFE])
def test_a_descending_satellite_keeps_recording_it_just_records_less_often(dhs, tmp_path, state):
    # In SAFE the radio goes off while the track must keep going: that use case
    # is the whole reason the recorder is not inside the link service.
    service, client = dhs
    service.on_start()
    obc(client, state=state)
    service.tick()

    assert rows_in(tmp_path / "comms.db") == 1


@pytest.mark.parametrize("state", [MissionState.STANDBY, MissionState.DEPLOY])
def test_nothing_is_recorded_before_the_satellite_has_deployed(dhs, tmp_path, state):
    service, client = dhs
    service.on_start()
    obc(client, state=state)
    service.tick()

    assert not (tmp_path / "comms.db").exists()


def test_a_recorder_that_starts_up_in_low_power_still_opens_a_mission(dhs):
    # Read strictly, "a mission opens on reaching NOMINAL" would leave a DHS
    # that restarted mid-descent writing nothing until the battery recovered —
    # which, on a walk, it never does.
    service, client = dhs
    obc(client, state=MissionState.LOW_POWER)
    service.on_start()

    assert service._mission is not None


def test_the_cadence_follows_the_mission_state(dhs):
    """Read from the shipped table rather than repeated as numbers.

    What is being tested is that the interval *follows the state* — the numbers
    themselves are a tuning decision that may legitimately move, and a test that
    spelled them out would fail on the edit rather than on a defect.
    """
    service, client = dhs
    table = config.CADENCE["dhs"]
    for state in (MissionState.NOMINAL, MissionState.LOW_POWER, MissionState.DEPLOY):
        obc(client, state=state)
        assert service.interval == table[state.value]
    # And the shape of the table is itself a decision: slow down when the battery
    # is low, hurry while OBC's bring-up window is open.
    assert table[MissionState.LOW_POWER.value] > table[MissionState.NOMINAL.value]
    assert table[MissionState.DEPLOY.value] < table[MissionState.NOMINAL.value]


# ── the row ─────────────────────────────────────────────────────────────────


def test_a_tick_assembles_one_row_out_of_the_caches_and_the_host_s_own_health(dhs, tmp_path):
    service, client = dhs
    service.on_start()
    obc(client)
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["adcs_status"], ADCS)
    client.deliver(TOPICS["payload_data"], SCIENCE)

    service.tick()

    conn = sqlite3.connect(tmp_path / "comms.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM telemetry").fetchone()
    assert row["mission_id"] == 1
    assert row["profile"] == "FLIGHT"
    assert row["obc_state"] == "NOMINAL"
    assert row["battery"] == 87.5
    assert (row["lat"], row["fix"]) == (55.7558, 1)
    assert row["temperature"] == 23.4
    # Collected by DHS itself: on a satellite the computer's own temperature and
    # free space are as much telemetry as the battery is.
    assert row["ram_percent"] is not None
    conn.close()


def test_a_database_file_that_has_gone_away_is_reported_as_no_size(dhs, tmp_path):
    # A card pulled out from under a running recorder. The status message is
    # what OBC and the dashboard are reading; an exception here would cost them
    # the message rather than the number.
    service, client = dhs
    service.on_start()
    obc(client)
    service._db_path = tmp_path / "vanished.db"
    service._publish_status()

    assert status(client)["db_size_bytes"] == 0


def test_the_status_counts_what_has_been_written_and_when(dhs):
    service, client = dhs
    service.on_start()
    obc(client)
    service.tick()
    service.tick()

    reported = status(client)
    assert reported["rows"] == 2
    assert reported["mission"]["rows"] == 2
    assert reported["last_write"] == pytest.approx(time.time(), abs=5)
    assert reported["db_size_bytes"] > 0


# ── closing a mission ───────────────────────────────────────────────────────


def test_critical_closes_the_mission_and_says_so_before_obc_gives_up_waiting(dhs, tmp_path):
    # OBC's CRITICAL path waits on recording:false before asking HOSTD to power
    # the host off, with a bounded grace. A late publish there costs the flush
    # it was waiting for, so the close happens on the thread the state arrived
    # on rather than at the next tick.
    service, client = dhs
    service.on_start()
    obc(client)
    service.tick()

    obc(client, state=MissionState.CRITICAL)

    assert status(client)["recording"] is False
    closed = missions_in(tmp_path / "comms.db")[0]
    assert closed["end_reason"] == "battery_critical"
    assert closed["rows"] == 1
    assert closed["ended_at"] is not None


def test_standing_down_for_a_profile_change_closes_the_mission(dhs, tmp_path):
    service, client = dhs
    service.on_start()
    obc(client)
    obc(client, state=MissionState.STANDBY, persistence=Persistence.NONE)

    assert missions_in(tmp_path / "comms.db")[0]["end_reason"] == "profile_change"
    assert status(client)["recording"] is False


def test_a_graceful_shutdown_closes_the_mission_as_a_shutdown(dhs, tmp_path):
    service, client = dhs
    service.on_start()
    obc(client)
    service.tick()

    service.on_stop()

    assert missions_in(tmp_path / "comms.db")[0]["end_reason"] == "shutdown"
    assert status(client)["recording"] is False


def test_stopping_with_no_mission_open_closes_nothing_and_complains_about_nothing(dhs):
    service, client = dhs
    service.on_start()
    service.on_stop()
    assert status(client)["recording"] is False


def test_the_host_reporting_a_different_profile_closes_the_mission(dhs, tmp_path, caplog):
    # A mission is a continuous run of one profile. HOSTD may report the new one
    # before OBC has stood the mission state down, and closing at the earliest
    # evidence keeps that property true.
    service, client = dhs
    service.on_start()
    obc(client)

    with caplog.at_level(logging.WARNING):
        client.deliver(TOPICS["host_status"], {"profile": "EXPO", "profile_requested": "EXPO"})

    assert status(client)["recording"] is False
    assert missions_in(tmp_path / "comms.db")[0]["end_reason"] == "profile_change"
    assert "closing mission 1" in caplog.text


def test_the_host_confirming_the_profile_the_mission_is_recorded_under_changes_nothing(dhs):
    service, client = dhs
    service.on_start()
    obc(client)
    client.deliver(TOPICS["host_status"], {"profile": "FLIGHT"})
    assert status(client)["recording"] is True


def test_host_status_with_no_mission_open_is_ignored(dhs):
    service, client = dhs
    service.on_start()
    client.deliver(TOPICS["host_status"], {"profile": "EXPO"})
    assert status(client)["recording"] is False


# ── orphan recovery ─────────────────────────────────────────────────────────


def test_a_mission_left_open_by_a_power_loss_is_closed_at_startup(dhs, tmp_path, caplog):
    # A satellite that died on battery mid-trip never closed its mission. Every
    # later query would have to work around that row forever.
    conn = schema.connect(tmp_path / "comms.db", LOG)
    conn.execute(
        "INSERT INTO missions (profile, started_at) VALUES ('FLIGHT', '2026-08-24T07:00:00Z')"
    )
    conn.execute(
        "INSERT INTO telemetry (timestamp, mission_id) VALUES ('2026-08-24T07:31:00Z', 1)"
    )
    conn.close()

    service, client = dhs
    with caplog.at_level(logging.INFO):
        obc(client)

    recovered = missions_in(tmp_path / "comms.db")[0]
    assert recovered["end_reason"] == "interrupted"
    assert recovered["ended_at"] == "2026-08-24T07:31:00Z"
    # Named with its file: recovery runs when a database is opened rather than
    # once at startup, so an interrupted DIAG session waits in diag.db until the
    # next DIAG run. This line is where that delay is visible in the log rather
    # than being a fact only the design knows.
    assert f"recovered 1 interrupted mission(s) in {tmp_path / 'comms.db'}: 1" in caplog.text
    # And the new session is a mission of its own, not a resumption: there is a
    # real gap in the data, and stitching across it would draw a straight line
    # through territory where the satellite was switched off.
    assert service._mission is not None and service._mission.id == 2


def test_recovery_that_itself_fails_costs_the_recovery_and_not_the_recorder(
    dhs, monkeypatch, caplog
):
    service, client = dhs
    monkeypatch.setattr(
        "cubesat.dhs.missions.MissionStore.recover_orphans",
        lambda _self: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")),
    )
    with caplog.at_level(logging.ERROR):
        obc(client)

    assert "orphan recovery failed" in caplog.text
    # The recorder came up anyway and opened its mission.
    assert service._mission is not None


# ── surviving the database ──────────────────────────────────────────────────


def test_a_database_from_a_newer_build_is_refused_and_dhs_stays_up(dhs, tmp_path, caplog):
    conn = schema.connect(tmp_path / "comms.db", LOG)
    conn.execute(f"PRAGMA user_version = {schema.SCHEMA_VERSION + 1}")
    conn.close()

    service, client = dhs
    with caplog.at_level(logging.ERROR):
        obc(client)

    assert service.running is True
    assert status(client)["recording"] is False
    assert status(client)["database"] is None
    assert "cannot open" in caplog.text


def test_a_refused_database_is_not_retried_on_every_message(dhs, tmp_path, caplog):
    # Neither cause — a newer build's file, or a card that is not there — is
    # cured by trying again in 30 seconds, and the log would fill with attempts.
    conn = schema.connect(tmp_path / "comms.db", LOG)
    conn.execute(f"PRAGMA user_version = {schema.SCHEMA_VERSION + 1}")
    conn.close()

    service, client = dhs
    with caplog.at_level(logging.ERROR):
        obc(client)
        obc(client)
        service.tick()

    assert caplog.text.count("cannot open") == 1


def test_a_write_that_fails_leaves_the_mission_open_and_the_service_alive(dhs, caplog):
    # A full card or a corrupt page costs one row. DHS going silent is how a
    # trip loses the rest of its track after one bad write.
    service, client = dhs
    service.on_start()
    obc(client)
    service._conn.execute("DROP TABLE telemetry")

    with caplog.at_level(logging.ERROR):
        service.tick()
        service.tick()

    assert service.running is True
    assert service._mission is not None
    assert status(client)["recording"] is True
    # Unable to be counted is reported as unknown rather than as zero rows.
    assert status(client)["rows"] is None
    assert "telemetry write failed" in caplog.text


def test_a_mission_that_cannot_be_closed_is_left_for_orphan_recovery(dhs, monkeypatch, caplog):
    # Self-healing by design: a mission left with a null ended_at is exactly
    # what the next startup closes.
    service, client = dhs
    service.on_start()
    obc(client)
    monkeypatch.setattr(
        "cubesat.dhs.missions.MissionStore.close",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )

    with caplog.at_level(logging.ERROR):
        obc(client, state=MissionState.CRITICAL)

    assert status(client)["recording"] is False
    assert "will be recovered as interrupted" in caplog.text


def test_a_mission_that_cannot_be_opened_records_nothing_and_says_so(dhs, monkeypatch, caplog):
    service, client = dhs
    monkeypatch.setattr(
        "cubesat.dhs.missions.MissionStore.open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    with caplog.at_level(logging.ERROR):
        obc(client)

    assert service._mission is None
    assert "could not open a mission" in caplog.text


def test_a_database_that_will_not_close_cleanly_is_logged_and_let_go(dhs, caplog):
    service, client = dhs
    service.on_start()
    obc(client)

    class Stubborn:
        def close(self):
            raise sqlite3.OperationalError("unfinalised statements")

    service._conn = Stubborn()
    with caplog.at_level(logging.ERROR):
        service.on_stop()

    assert "failed" in caplog.text
    assert service._conn is None


# ── retention ───────────────────────────────────────────────────────────────


def test_a_retention_pass_runs_as_soon_as_a_database_is_opened(dhs, monkeypatch):
    # A session can easily be shorter than the purge interval, and then nothing
    # would ever be purged at all.
    service, client = dhs
    passes = []
    monkeypatch.setattr(retention, "purge", lambda *a, **k: passes.append(k))
    obc(client)

    assert len(passes) == 1
    assert passes[0]["days"] == config.DHS_RETENTION_DAYS
    assert passes[0]["photos_root"] == config.PHOTOS_DIR
    assert passes[0]["purge_photos"] == config.DHS_PURGE_PHOTOS


def test_retention_runs_on_a_horizon_of_hours_and_not_on_every_tick(dhs, monkeypatch):
    # The horizon is measured in days, so a pass per tick is a scan of the
    # largest table in the file to delete nothing.
    service, client = dhs
    passes = []
    monkeypatch.setattr(retention, "purge", lambda *a, **k: passes.append(k))
    obc(client)
    service.tick()
    service.tick()
    assert len(passes) == 1

    monkeypatch.setattr(service, "_next_purge", 0.0)
    service.tick()
    assert len(passes) == 2


def test_with_no_database_open_there_is_nothing_to_purge(dhs, monkeypatch):
    service, client = dhs
    passes = []
    monkeypatch.setattr(retention, "purge", lambda *a, **k: passes.append(k))
    service.on_start()
    service.tick()
    assert passes == []


# ── attitude ────────────────────────────────────────────────────────────────


def attitude_in(path, mission_id=None):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM attitude"
        args: tuple = ()
        if mission_id is not None:
            sql += " WHERE mission_id = ?"
            args = (mission_id,)
        return [dict(row) for row in conn.execute(sql + " ORDER BY t", args)]
    finally:
        conn.close()


def orientation(t, w=0.999):
    """An adcs_status carrying orientation, as ADCS publishes it at 2 Hz."""
    return {
        "timestamp": t,
        "quaternion": {"w": w, "x": 0.01, "y": 0.02, "z": 0.03},
        "gyro_dps": {"x": 0.1, "y": -0.2, "z": 0.05},
        **ADCS,
    }


def test_attitude_arrives_at_the_adcs_cadence_and_lands_in_one_batch(dhs, monkeypatch):
    # The whole point of the table: DHS writes one telemetry row per tick, and
    # a replay of a hand-carried satellite needs more than one orientation per
    # thirty seconds. The samples arrive between ticks and land together on one.
    monkeypatch.setattr(config, "DHS_ATTITUDE_MIN_INTERVAL_SEC", 1.0)
    service, client = dhs
    obc(client)
    for step in range(6):
        client.deliver(TOPICS["adcs_status"], orientation(1000.0 + step * 0.5))
    # Nothing is on the card yet: buffered, not written per sample. Read off the
    # buffer rather than off the status, which is retained and republished only
    # when something changes — six arriving samples deliberately change nothing.
    assert len(service._attitude) == 3
    assert attitude_in(config.DB_PATH) == []
    service.tick()

    stored = attitude_in(config.DB_PATH)
    assert [row["t"] for row in stored] == [1000.0, 1001.0, 1002.0]
    assert stored[0]["quat_w"] == 0.999 and stored[0]["gyro_z"] == 0.05
    # One telemetry row for the same window: the two rates are the reason the
    # table exists.
    assert rows_in(config.DB_PATH) == 1
    assert status(client)["attitude"]["buffered"] == 0
    assert status(client)["attitude"]["written"] == 3


def test_samples_arriving_before_a_mission_is_open_belong_to_nothing_and_are_dropped(dhs):
    # A sample carries the id of the mission that was open when the IMU was
    # read. With none open there is nothing it could truthfully belong to.
    service, client = dhs
    client.deliver(TOPICS["adcs_status"], orientation(1000.0))
    obc(client)
    client.deliver(TOPICS["adcs_status"], orientation(1002.0))
    service.tick()

    assert [row["t"] for row in attitude_in(config.DB_PATH)] == [1002.0]


def test_a_sample_is_filed_under_the_mission_that_was_open_when_it_was_taken(dhs):
    # Stamped on arrival, not on the flush: a profile change between the two
    # would otherwise file the last second of one trip under the next.
    service, client = dhs
    obc(client)
    client.deliver(TOPICS["adcs_status"], orientation(1000.0))
    first = status(client)["mission"]["id"]

    # Standing the mission state down closes the mission; the buffered sample
    # has to land against the one it names, before it stops being open.
    obc(client, state=MissionState.STANDBY)
    obc(client)
    second = status(client)["mission"]["id"]
    client.deliver(TOPICS["adcs_status"], orientation(1010.0))
    service.tick()

    assert second != first
    assert [row["t"] for row in attitude_in(config.DB_PATH, first)] == [1000.0]
    assert [row["t"] for row in attitude_in(config.DB_PATH, second)] == [1010.0]


def test_a_closing_mission_takes_its_buffered_samples_with_it(dhs):
    # Written before the close, so they cannot end up past their own mission's
    # ended_at — which is what a replay would then have to explain.
    service, client = dhs
    obc(client)
    mission_id = status(client)["mission"]["id"]
    client.deliver(TOPICS["adcs_status"], orientation(1000.0))
    obc(client, state=MissionState.STANDBY)

    assert [row["t"] for row in attitude_in(config.DB_PATH, mission_id)] == [1000.0]
    ended = missions_in(config.DB_PATH)[0]["ended_at"]
    assert ended is not None


def test_a_write_that_fails_holds_the_samples_rather_than_dropping_them(dhs, monkeypatch, caplog):
    # A full card that is emptied, or a filesystem remounted read-write, gets
    # the samples on the next tick. Losing them silently is the one outcome a
    # flight recorder must not have.
    service, client = dhs
    obc(client)
    client.deliver(TOPICS["adcs_status"], orientation(1000.0))

    # A card that refuses the write and then comes back. Toggled through a flag
    # rather than undone with monkeypatch, which would also undo the fixture's
    # redirection of DB_PATH and put the second half of this test on the real
    # data directory.
    failing = {"now": True}
    real_write = service._recorder.write_attitude
    monkeypatch.setattr(
        service._recorder,
        "write_attitude",
        lambda batch: False if failing["now"] else real_write(batch),
    )
    with caplog.at_level(logging.ERROR):
        service.tick()
    assert status(client)["attitude"]["buffered"] == 1
    assert attitude_in(config.DB_PATH) == []

    failing["now"] = False
    service.tick()
    assert [row["t"] for row in attitude_in(config.DB_PATH)] == [1000.0]


def test_a_silent_imu_records_no_attitude_at_all(dhs):
    # ADCS publishes when either half answered, so a position-only message is
    # normal. Nine nulls would look on a chart exactly like a satellite that was
    # not moving.
    service, client = dhs
    obc(client)
    client.deliver(TOPICS["adcs_status"], {"timestamp": 1000.0, **ADCS})
    service.tick()

    assert attitude_in(config.DB_PATH) == []
    # The telemetry row still lands: the position half of the same message is
    # exactly what FLIGHT is recording.
    assert rows_in(config.DB_PATH) == 1


def test_the_floor_is_a_ceiling_on_every_profile_including_diag(dhs, monkeypatch):
    # DIAG runs ADCS at 10 Hz (cadence_scale 0.2 against a 0.5 s interval). The
    # floor is one number across every profile and state, so a bench session
    # cannot quietly become ten rows a second on the card.
    monkeypatch.setattr(config, "DHS_ATTITUDE_MIN_INTERVAL_SEC", 1.0)
    service, client = dhs
    monkeypatch.setattr(
        service, "_attitude",
        type(service._attitude)(config.DHS_ATTITUDE_MIN_INTERVAL_SEC, 100, LOG),
    )
    obc(client, profile=Profile.DIAG, persistence=Persistence.DIAG_DB)
    for step in range(20):
        client.deliver(TOPICS["adcs_status"], orientation(2000.0 + step * 0.1))
    service.tick()

    assert [row["t"] for row in attitude_in(config.DIAG_DB_PATH)] == [2000.0, 2001.0]


# ── the radio log ─────────────────────────────────────────────────────────────


def radio_in(path, mission_id=None):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM radio_log"
        args: tuple = ()
        if mission_id is not None:
            sql += " WHERE mission_id = ?"
            args = (mission_id,)
        return [dict(row) for row in conn.execute(sql + " ORDER BY t", args)]
    finally:
        conn.close()


def rx_event(t, text="!pos", snr=6.25, rssi=-96.0, hops=0):
    """A comms_radio event as COMMS publishes one per received message."""
    return {
        "timestamp": t,
        "direction": "rx",
        "text": text,
        "bytes": len(text.encode("utf-8")),
        "sender": "!e2f1a4c8",
        "snr": snr,
        "rssi": rssi,
        "hops": hops,
    }


def tx_event(t, kind="beacon", sent=True, text="CS t=1 st=NOMINAL"):
    return {
        "timestamp": t,
        "direction": "tx",
        "kind": kind,
        "text": text,
        "bytes": len(text.encode("utf-8")),
        "sent": sent,
    }


def test_radio_events_arrive_between_ticks_and_land_in_one_batch(dhs):
    service, client = dhs
    obc(client)
    client.deliver(TOPICS["comms_radio"], rx_event(1000.0))
    client.deliver(TOPICS["comms_radio"], tx_event(1010.0, kind="ack"))
    assert len(service._radio) == 2
    assert radio_in(config.DB_PATH) == []
    service.tick()

    stored = radio_in(config.DB_PATH)
    assert [row["direction"] for row in stored] == ["rx", "tx"]
    assert stored[0]["text"] == "!pos"
    assert stored[0]["snr"] == 6.25 and stored[0]["rssi"] == -96.0 and stored[0]["hops"] == 0
    assert stored[0]["sent"] is None and stored[0]["kind"] is None
    assert stored[1]["kind"] == "ack" and stored[1]["sent"] == 1
    assert status(client)["radio"] == {"written": 2, "buffered": 0}


def test_radio_traffic_outside_a_mission_belongs_to_nothing_and_is_dropped(dhs):
    # The table answers "what did the radio do on this trip"; a HOSTED desk
    # listening has no trip for a row to belong to.
    service, client = dhs
    client.deliver(TOPICS["comms_radio"], rx_event(1000.0))
    obc(client)
    client.deliver(TOPICS["comms_radio"], rx_event(1002.0))
    service.tick()

    assert [row["t"] for row in radio_in(config.DB_PATH)] == [1002.0]


def test_a_closing_mission_takes_its_buffered_radio_events_with_it(dhs):
    service, client = dhs
    obc(client)
    mission_id = status(client)["mission"]["id"]
    client.deliver(TOPICS["comms_radio"], tx_event(1000.0, kind="down"))
    obc(client, state=MissionState.STANDBY)

    assert [row["kind"] for row in radio_in(config.DB_PATH, mission_id)] == ["down"]


def test_a_failed_radio_write_holds_the_events_for_the_next_tick(dhs, monkeypatch):
    service, client = dhs
    obc(client)
    client.deliver(TOPICS["comms_radio"], rx_event(1000.0))

    real_write = service._recorder.write_radio
    monkeypatch.setattr(service._recorder, "write_radio", lambda batch: False)
    service.tick()
    assert status(client)["radio"]["buffered"] == 1
    assert radio_in(config.DB_PATH) == []

    monkeypatch.setattr(service._recorder, "write_radio", real_write)
    service.tick()
    assert [row["t"] for row in radio_in(config.DB_PATH)] == [1000.0]


def test_an_event_with_no_direction_is_noise_and_is_not_buffered(dhs):
    # A session log entry that cannot say whether the satellite was talking or
    # listening is not an entry.
    service, client = dhs
    obc(client)
    client.deliver(TOPICS["comms_radio"], {"timestamp": 1000.0, "text": "?"})
    assert len(service._radio) == 0


# ── deleting a mission ──────────────────────────────────────────────────────


def delete(client, mission_id=1, request_id="req_del"):
    """The one command DHS answers for, exactly as a browser publishes it."""
    payload = {"command": "delete_mission", "request_id": request_id}
    if mission_id is not None:
        payload["params"] = {"mission_id": mission_id}
    client.deliver(TOPICS["command"], payload)


def recorded_mission(dhs, label="walk to work"):
    """One mission with a row in it, closed and on the card."""
    service, client = dhs
    obc(client, label=label)
    mission_id = status(client)["mission"]["id"]
    service.tick()
    obc(client, state=MissionState.STANDBY)
    return mission_id


def photos_of(mission_id):
    directory = config.PHOTOS_DIR / str(mission_id)
    directory.mkdir(parents=True)
    (directory / "photo_20260901_071200.jpg").write_bytes(b"x" * 100)
    return directory


def last_delete(client):
    return status(client)["last_delete"]


def test_deleting_a_mission_removes_its_rows_and_its_photographs(dhs):
    service, client = dhs
    mission_id = recorded_mission(dhs)
    directory = photos_of(mission_id)

    delete(client, mission_id)

    assert missions_in(config.DB_PATH) == []
    assert rows_in(config.DB_PATH) == 0
    assert not directory.exists()


def test_the_status_says_what_the_delete_took_and_which_request_asked(dhs):
    # The ground matches on request_id: dhs_status is retained, so a client
    # that connects later must be able to tell this answer from its own.
    service, client = dhs
    mission_id = recorded_mission(dhs)
    photos_of(mission_id)

    delete(client, mission_id, request_id="req_042")

    result = last_delete(client)
    assert result["ok"] is True
    assert result["error"] is None
    assert (result["mission_id"], result["request_id"]) == (mission_id, "req_042")
    assert (result["rows"], result["photos"], result["bytes_reclaimed"]) == (1, 1, 100)


def test_a_delete_is_refused_in_expo(dhs):
    # The open access point. D4 left commands unauthenticated on the argument
    # that a visitor can do no lasting harm; erasing a recorded flight is not
    # in that class, and the broker's ACL cannot see which profile is applied.
    service, client = dhs
    mission_id = recorded_mission(dhs)
    obc(client, state=MissionState.NOMINAL, profile=Profile.EXPO, persistence=Persistence.NONE)

    delete(client, mission_id)

    assert [row["id"] for row in missions_in(config.DB_PATH)] == [mission_id]
    result = last_delete(client)
    assert result["ok"] is False
    assert "EXPO" in result["error"]


def test_the_mission_being_recorded_cannot_be_deleted_under_the_recorder(dhs):
    service, client = dhs
    obc(client)
    mission_id = status(client)["mission"]["id"]

    delete(client, mission_id)

    assert [row["id"] for row in missions_in(config.DB_PATH)] == [mission_id]
    assert "being recorded" in last_delete(client)["error"]
    # And the recorder carried on with it.
    service.tick()
    assert rows_in(config.DB_PATH) == 1


def test_a_mission_that_is_not_there_is_a_refusal_not_a_silent_success(dhs):
    service, client = dhs
    recorded_mission(dhs)

    delete(client, 404)

    result = last_delete(client)
    assert result["ok"] is False
    assert "no mission 404" in result["error"]


def test_a_delete_without_a_mission_id_is_refused(dhs):
    service, client = dhs
    recorded_mission(dhs)

    delete(client, mission_id=None)

    assert last_delete(client)["ok"] is False
    assert missions_in(config.DB_PATH) != []


def test_a_delete_with_no_database_at_all_does_not_create_one(dhs):
    # HOSTED on a satellite that has never recorded. Opening the file to say
    # "no such mission" would create the empty database the answer is about.
    service, client = dhs

    delete(client, 1)

    assert not config.DB_PATH.exists()
    assert last_delete(client)["ok"] is False


def test_a_delete_outside_a_recording_profile_opens_the_database_and_lets_it_go(dhs):
    # DEMO records nothing, so DHS holds no database — but an operator tidying
    # the archive from the dashboard is exactly the case this exists for.
    service, client = dhs
    mission_id = recorded_mission(dhs)
    service.on_stop()
    service, client = dhs
    obc(client, state=MissionState.NOMINAL, profile=Profile.DEMO, persistence=Persistence.NONE)
    assert service._db_path is None

    delete(client, mission_id)

    assert missions_in(config.DB_PATH) == []
    assert last_delete(client)["ok"] is True
    # And the recorder did not quietly acquire a database it has no business
    # holding in a profile that records nothing.
    assert service._db_path is None
    assert status(client)["recording"] is False


def test_a_delete_acts_on_the_database_the_recorder_is_writing(dhs):
    # The same rule DASHBOARD follows for which archive it serves, so the
    # listing an operator is looking at and the file this deletes from cannot
    # be two different files.
    service, client = dhs
    obc(client, profile=Profile.DIAG, persistence=Persistence.DIAG_DB)
    mission_id = status(client)["mission"]["id"]
    service.tick()
    obc(client, state=MissionState.STANDBY, profile=Profile.DIAG,
        persistence=Persistence.DIAG_DB)

    delete(client, mission_id)

    assert missions_in(config.DIAG_DB_PATH) == []
    assert last_delete(client)["ok"] is True


def test_a_command_that_is_not_ours_leaves_no_trace(dhs):
    # set_profile, take_photo and the rest share this topic. Ignored in
    # silence: a warning per foreign command would make every profile change
    # look like a fault in the recorder.
    service, client = dhs
    obc(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "req_1"})

    assert last_delete(client) is None


def test_there_is_no_delete_result_before_anything_has_been_deleted(dhs):
    service, client = dhs
    service.on_start()
    assert last_delete(client) is None


def test_a_database_that_will_not_open_for_a_delete_is_answered_not_survived_in_silence(
    dhs, monkeypatch
):
    service, client = dhs
    mission_id = recorded_mission(dhs)
    service.on_stop()
    service, client = dhs
    obc(client, state=MissionState.NOMINAL, profile=Profile.DEMO, persistence=Persistence.NONE)

    def refuse(path, log):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(schema, "connect", refuse)
    delete(client, mission_id)

    result = last_delete(client)
    assert result["ok"] is False
    assert "cannot open" in result["error"]


def test_a_delete_that_fails_mid_statement_is_reported_rather_than_raised(dhs, monkeypatch):
    # Survivable like every other database failure here: a recorder that exits
    # on a bad delete takes the rest of the trip's track with it.
    service, client = dhs
    mission_id = recorded_mission(dhs)

    def refuse(_mission_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service._store, "delete", refuse)
    delete(client, mission_id)

    assert "could not be deleted" in last_delete(client)["error"]
    assert [row["id"] for row in missions_in(config.DB_PATH)] == [mission_id]


def test_a_transient_connection_that_will_not_close_still_answers_the_delete(dhs, monkeypatch):
    service, client = dhs
    mission_id = recorded_mission(dhs)
    service.on_stop()
    service, client = dhs
    obc(client, state=MissionState.NOMINAL, profile=Profile.DEMO, persistence=Persistence.NONE)

    real_connect = schema.connect

    class CloseFails:
        """Everything a real connection does, except let go of the card."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(schema, "connect", lambda path, log: CloseFails(real_connect(path, log)))
    delete(client, mission_id)

    assert last_delete(client)["ok"] is True
    assert missions_in(config.DB_PATH) == []
