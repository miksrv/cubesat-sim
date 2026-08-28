"""DHS from process start to a closed mission, through the real run loop.

The unit tests call ``tick()`` and hand the service messages. Here ``run()``
drives — the cadence loop, the heartbeat thread and the shutdown path all
participate — against a real SQLite file, and every scenario is one an operator
can actually produce: a walk recorded and ended properly, a battery that ran out
mid-trip, a satellite plugged back in at a desk three days later, and a card
that has been filling up since spring.

What no single layer can show on its own is here: that a row assembled from four
subsystems' documented payloads survives a restart, that a mission nothing was
alive to close is closed at the right timestamp by the next start, and that the
mission id DHS publishes is the one PAYLOAD files a photograph under.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from cubesat.common import config
from cubesat.common.states import EndReason, MissionState, Persistence, Profile
from cubesat.common.topics import TOPICS
from cubesat.dhs import schema
from cubesat.dhs.missions import MissionStore
from cubesat.dhs.service import DhsService
from cubesat.payload.service import PayloadService

EPS = {"battery_percent": 87.5, "voltage": 4.123, "external_power": False, "charge_rate": -0.2}
SCIENCE = {"temperature": 23.4, "humidity": 45.2, "pressure": 1013.0, "light": 412.0,
           "uv_index": None, "uv_raw": 14}

#: A short walk: three fixes a thousandth of a degree of latitude apart, which
#: is about 111 m a step.
TRACK = [
    {"lat": 55.7558, "lon": 37.6173, "alt": 156.2, "speed": 0.4, "fix": True, "satellites": 23},
    {"lat": 55.7568, "lon": 37.6173, "alt": 156.4, "speed": 1.2, "fix": True, "satellites": 22},
    {"lat": 55.7578, "lon": 37.6173, "alt": 156.1, "speed": 1.1, "fix": True, "satellites": 22},
]


def build(service_factory, monkeypatch, tmp_path):
    """A DHS whose databases and photo root are this test's own."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "comms.db")
    monkeypatch.setattr(config, "DIAG_DB_PATH", tmp_path / "diag.db")
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    service, client = service_factory(DhsService)
    client.connect_ok()
    # A tick every few milliseconds, so a walk fits inside a test rather than
    # inside the 30 s NOMINAL cadence.
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 0.01))
    return service, client


def start(service):
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    return thread


def stop(service, thread):
    service.stop()
    thread.join(timeout=5)
    assert not thread.is_alive(), "DHS did not shut down"


def announce(client, state=MissionState.NOMINAL, profile=Profile.FLIGHT,
             persistence=Persistence.MISSION_DB, label=None):
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


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def query(path, sql, *params):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def row_count(path):
    return query(path, "SELECT COUNT(*) AS n FROM telemetry")[0]["n"]


# ── a walk, recorded ────────────────────────────────────────────────────────


def test_a_flight_profile_records_a_track_and_closes_it_on_shutdown(
    service_factory, monkeypatch, tmp_path
):
    service, client = build(service_factory, monkeypatch, tmp_path)
    database = tmp_path / "comms.db"
    thread = start(service)
    announce(client, label="walk to work")
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["payload_data"], SCIENCE)
    for fix in TRACK:
        client.deliver(TOPICS["adcs_status"], {"roll": 1.2, "yaw": 178.9, "gnss": fix})
        assert wait_until(lambda f=fix: any(
            row["lat"] == f["lat"] for row in query(database, "SELECT lat FROM telemetry")
        )), "the fix never reached a row"

    stop(service, thread)

    mission = query(database, "SELECT * FROM missions")[0]
    assert mission["label"] == "walk to work"
    assert mission["profile"] == "FLIGHT"
    assert mission["end_reason"] == EndReason.SHUTDOWN.value
    assert mission["rows"] == row_count(database)
    assert mission["first_fix_at"] is not None
    # Two steps of about 111 m each, measured from the mission's own rows.
    assert 200 < mission["distance_m"] < 260
    # No science_start was ever sent. Under the pre-rewrite gate this file would
    # be empty, which is why that gate is gone.
    assert mission["rows"] > 0


def test_the_row_carries_every_subsystem_that_reported_plus_the_host_s_own_health(
    service_factory, monkeypatch, tmp_path
):
    service, client = build(service_factory, monkeypatch, tmp_path)
    database = tmp_path / "comms.db"
    thread = start(service)
    announce(client)
    client.deliver(TOPICS["eps_status"], EPS)
    client.deliver(TOPICS["adcs_status"], {"roll": 1.2, "yaw": 178.9, "gnss": TRACK[0]})
    client.deliver(TOPICS["payload_data"], SCIENCE)
    # Rows are written from the moment the mission opens, so the early ones are
    # assembled from caches that were still filling. The row this test is about
    # is the first one taken after all three subsystems had reported.
    complete = "battery IS NOT NULL AND lat IS NOT NULL AND temperature IS NOT NULL"
    assert wait_until(
        lambda: query(database, f"SELECT COUNT(*) AS n FROM telemetry WHERE {complete}")[0]["n"]
    ), "no row was assembled from a full set of caches"
    stop(service, thread)

    row = query(database, f"SELECT * FROM telemetry WHERE {complete} LIMIT 1")[0]
    assert (row["battery"], row["voltage"]) == (87.5, 4.123)
    assert (row["roll"], row["yaw"]) == (1.2, 178.9)
    assert (row["lat"], row["fix"]) == (55.7558, 1)
    assert row["temperature"] == 23.4
    assert row["disk_percent"] is not None
    # The raw copy keeps what no column holds, so a field measured today is not
    # lost to a column set decided today.
    assert '"charge_rate": -0.2' in row["raw_json"]
    assert '"uv_raw": 14' in row["raw_json"]


# ── the battery ran out ─────────────────────────────────────────────────────


def test_critical_closes_the_mission_and_reports_it_well_inside_obc_s_grace(
    service_factory, monkeypatch, tmp_path
):
    # OBC waits on recording:false before asking HOSTD to power the host off,
    # and gives up after ten seconds. A recorder that answered late would cost
    # the flush OBC was waiting for.
    service, client = build(service_factory, monkeypatch, tmp_path)
    database = tmp_path / "comms.db"
    thread = start(service)
    announce(client)
    assert wait_until(lambda: row_count(database) >= 1)

    started = time.monotonic()
    announce(client, state=MissionState.CRITICAL)
    answered = time.monotonic() - started

    assert client.last(TOPICS["dhs_status"])["recording"] is False
    assert answered < 1.0
    assert query(database, "SELECT end_reason FROM missions")[0]["end_reason"] == (
        EndReason.BATTERY_CRITICAL.value
    )
    stop(service, thread)


def test_a_power_loss_leaves_an_orphan_that_the_next_start_closes(
    service_factory, monkeypatch, tmp_path
):
    # The satellite died on battery mid-trip: nothing ran, so nothing closed the
    # mission. Plugged in at a desk hours later it comes up idle and reachable,
    # with the interrupted mission properly closed.
    service, client = build(service_factory, monkeypatch, tmp_path)
    database = tmp_path / "comms.db"
    # Whatever ends this process, it will not be a graceful shutdown. Patched on
    # the instance, so the satellite that comes back up afterwards still closes
    # its own mission properly.
    monkeypatch.setattr(service, "on_stop", lambda: None)
    thread = start(service)
    announce(client)
    client.deliver(TOPICS["adcs_status"], {"gnss": TRACK[0]})
    assert wait_until(lambda: row_count(database) >= 2)
    stop(service, thread)

    assert query(database, "SELECT ended_at FROM missions")[0]["ended_at"] is None
    last_row = query(database, "SELECT MAX(timestamp) AS t FROM telemetry")[0]["t"]

    revived, revived_client = build(service_factory, monkeypatch, tmp_path)
    revived_thread = start(revived)
    announce(revived_client)
    assert wait_until(lambda: len(query(database, "SELECT * FROM missions")) == 2)
    stop(revived, revived_thread)

    interrupted, resumed = query(database, "SELECT * FROM missions ORDER BY id")
    assert interrupted["end_reason"] == EndReason.INTERRUPTED.value
    assert interrupted["ended_at"] == last_row
    assert interrupted["rows"] >= 2
    # A trip interrupted by a reset is two missions, not one resumed session:
    # there is a real gap, and stitching across it would draw a straight line
    # through territory where the satellite was switched off.
    assert resumed["id"] == 2
    assert resumed["end_reason"] == EndReason.SHUTDOWN.value


def test_a_second_start_finds_its_history_and_applies_no_migration(
    service_factory, monkeypatch, tmp_path, caplog
):
    service, client = build(service_factory, monkeypatch, tmp_path)
    database = tmp_path / "comms.db"
    thread = start(service)
    announce(client)
    assert wait_until(lambda: row_count(database) >= 1)
    stop(service, thread)
    before = row_count(database)

    second, second_client = build(service_factory, monkeypatch, tmp_path)
    with caplog.at_level("INFO"):
        second_thread = start(second)
        announce(second_client)
        assert wait_until(lambda: row_count(database) > before)
    stop(second, second_thread)

    assert "applying schema migration" not in caplog.text
    assert query(database, "PRAGMA user_version")[0]["user_version"] == schema.SCHEMA_VERSION


# ── the card fills up ───────────────────────────────────────────────────────


def test_retention_takes_an_aged_mission_s_photos_and_never_the_unfiled_ones(
    service_factory, monkeypatch, tmp_path
):
    # The camera is the only unbounded writer on this satellite, and photos
    # follow their mission. The unfiled ones belong to none, so nothing here
    # decides when they stop being wanted.
    database = tmp_path / "comms.db"
    photos = tmp_path / "photos"
    seed_log = logging.getLogger("seed")
    conn = schema.connect(database, seed_log)
    store = MissionStore(conn, seed_log)
    aged = store.open("FLIGHT", "a trip last spring")
    long_ago = schema.utc_iso(time.time() - 60 * 86_400)
    conn.execute(
        "INSERT INTO telemetry (timestamp, mission_id) VALUES (?, ?)", (long_ago, aged.id)
    )
    store.close(aged.id, EndReason.SHUTDOWN, ended_at=long_ago)
    conn.close()

    for name in (str(aged.id), "unfiled"):
        (photos / name).mkdir(parents=True)
        (photos / name / "photo.jpg").write_bytes(b"x" * 1024)

    service, client = build(service_factory, monkeypatch, tmp_path)
    thread = start(service)
    announce(client)
    assert wait_until(lambda: not (photos / str(aged.id)).exists()), "the aged photos survived"
    stop(service, thread)

    assert (photos / "unfiled" / "photo.jpg").exists()
    # The mission row itself stays: a trip that happened is still listed even
    # after its detail has aged out — and it says that is what happened, rather
    # than reporting a row count it no longer holds.
    remembered = query(database, "SELECT * FROM missions WHERE id = ?", aged.id)[0]
    assert remembered["label"] == "a trip last spring"
    assert remembered["rows"] == 1
    assert remembered["purged_at"] is not None
    assert query(database, "SELECT COUNT(*) AS n FROM telemetry WHERE mission_id = ?", aged.id)[
        0
    ]["n"] == 0


# ── the contract PAYLOAD reads ──────────────────────────────────────────────


def test_payload_files_its_photographs_in_the_directory_dhs_names(
    service_factory, monkeypatch, tmp_path
):
    # DHS owns missions and PAYLOAD owns the camera, and the only thing joining
    # them is this retained message. Asserted against the real consumer, because
    # a shape both sides agree on separately is a shape neither side checks.
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    service, client = build(service_factory, monkeypatch, tmp_path)
    thread = start(service)
    announce(client)
    assert wait_until(lambda: client.last(TOPICS["dhs_status"])["recording"] is True)
    published = client.last(TOPICS["dhs_status"])
    stop(service, thread)

    payload, payload_client = service_factory(PayloadService)
    payload_client.deliver(TOPICS["dhs_status"], published)

    reported = payload_client.last(TOPICS["payload_status"])
    assert reported["mission_id"] == str(published["mission"]["id"])
    assert reported["photo_dir"] == str(tmp_path / "photos" / str(published["mission"]["id"]))
