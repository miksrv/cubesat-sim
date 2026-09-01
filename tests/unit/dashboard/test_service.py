"""DASHBOARD end to end within one process: a real socket, a real database.

The server is started, requests go over TCP to ``localhost``, and what is
asserted is the bytes that came back. Mocking ``http.server`` would assert that
a mock routes the way the test thinks it does — and routing, path traversal and
the SPA fallback are precisely what could be wrong here.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import urllib.error
import urllib.request

import pytest

from cubesat.common import config
from cubesat.common.states import EndReason
from cubesat.common.topics import TOPICS
from cubesat.dashboard.service import DashboardService
from cubesat.dhs import schema
from cubesat.dhs.missions import MissionStore

LOG = logging.getLogger("test-dashboard")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def roots(tmp_path):
    static = tmp_path / "dashboard"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>CubeSat</title>")
    assets = static / "static"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('hi')")

    photos = tmp_path / "photos"
    (photos / "42").mkdir(parents=True)
    (photos / "42" / "frame_0001.jpg").write_bytes(b"\xff\xd8jpeg")
    (photos / "42" / "notes.txt").write_text("not a photograph")
    (photos / "unfiled").mkdir()

    return {"static": static, "photos": photos, "db": tmp_path / "comms.db"}


@pytest.fixture
def recorded(roots):
    conn = schema.connect(roots["db"], LOG)
    store = MissionStore(conn, LOG)
    mission = store.open("FLIGHT", "walk to work")
    for index in range(3):
        conn.execute(
            "INSERT INTO telemetry (timestamp, mission_id, battery, cpu_percent) "
            "VALUES (?, ?, ?, ?)",
            (f"2026-08-24T07:0{index}:00Z", mission.id, 90 - index, 31 + index),
        )
    conn.executemany(
        "INSERT INTO attitude (mission_id, t, quat_w) VALUES (?, ?, ?)",
        [(mission.id, 1000.0 + step, 0.99) for step in range(4)],
    )
    conn.executemany(
        "INSERT INTO radio_log (mission_id, t, direction, kind, text, bytes, sent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (mission.id, 1000.0, "tx", "beacon", "CSAT t=1000", 24, 1),
            (mission.id, 1005.0, "rx", None, "!ping", 5, None),
        ],
    )
    store.close(mission.id, EndReason.PROFILE_CHANGE)
    conn.close()
    return mission.id


@pytest.fixture
def dashboard(service_factory, roots):
    port = free_port()
    service, client = service_factory(
        DashboardService,
        port=port,
        static_root=roots["static"],
        photos_root=roots["photos"],
        db_path=roots["db"],
    )
    service.on_start()
    yield service, client, port
    service.on_stop()


def get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, response.headers, response.read()


def get_json(port: int, path: str):
    _, _, body = get(port, path)
    return json.loads(body)


def recording(client, mission_id, database=None):
    """A dhs_status saying a mission is open, which is what points
    ``/api/telemetry`` at the database instead of the in-memory ring."""
    client.deliver(
        TOPICS["dhs_status"],
        {
            "recording": True,
            "database": str(database) if database is not None else None,
            "mission": {"id": mission_id},
        },
    )


def published_row(client, **fields):
    """One row as DHS publishes it — assembled, and not necessarily written."""
    client.deliver(TOPICS["dhs_telemetry"], {"row": fields})


# ── what it is and is not ───────────────────────────────────────────────────


def test_it_publishes_no_status_of_its_own(dashboard):
    # Every other service has one because DEPLOY wants evidence a device
    # answered; this one owns no device. A status topic would add a subsystem
    # that can fail a bring-up, for a service a profile may intend to be absent.
    service, client, _ = dashboard
    service.tick()
    assert [p.topic for p in client.published if p.topic != TOPICS["heartbeat"]] == []


def test_it_does_not_track_the_mission_state(dashboard):
    # Reading it would invite the first "and while we're here" that turns a
    # viewer into a participant.
    service, client, _ = dashboard
    client.connect_ok()
    assert service.mission_state is None
    # Both subscriptions come from the recorder: the database path and the open
    # mission from one, the rows for the ring from the other. Neither is a
    # mission-state feed.
    assert client.subscribed == [TOPICS["dhs_status"], TOPICS["dhs_telemetry"]]


# ── the archive over HTTP ───────────────────────────────────────────────────


def test_telemetry_of_an_open_mission_is_served_newest_first(dashboard, recorded):
    _, client, port = dashboard
    recording(client, recorded)
    body = get_json(port, "/api/telemetry")
    assert body["count"] == 3
    assert body["source"] == "mission"
    # The newest row is what a live dashboard reads CPU and RAM from: those are
    # on no status topic at all.
    assert body["records"][0]["cpu_percent"] == 33


def test_the_limit_is_honoured(dashboard, recorded):
    _, client, port = dashboard
    recording(client, recorded)
    assert get_json(port, "/api/telemetry?limit=2")["count"] == 2


def test_a_nonsense_limit_falls_back_rather_than_failing(dashboard, recorded):
    # It arrives from a query string a browser composed.
    _, client, port = dashboard
    recording(client, recorded)
    assert get_json(port, "/api/telemetry?limit=three")["count"] == 3


def test_the_open_mission_is_the_only_one_its_telemetry_comes_from(dashboard, roots, recorded):
    """The bug this endpoint's shape was changed for, pinned.

    Measured on the satellite 2026-09-01: `?limit=60` came back with 33 rows
    from that day's mission and 27 from one two days earlier, and the charts drew
    the two as one continuous line — on the ground track, as one path joining two
    days' positions. "The last N rows of the table" is not "the current session".
    """
    conn = schema.connect(roots["db"], LOG)
    older = MissionStore(conn, LOG).open("DEMO", "two days ago")
    conn.execute(
        "INSERT INTO telemetry (timestamp, mission_id, battery) VALUES (?, ?, ?)",
        ("2026-08-22T09:00:00Z", older.id, 55),
    )
    conn.commit()
    conn.close()

    _, client, port = dashboard
    recording(client, recorded)
    body = get_json(port, "/api/telemetry")
    assert body["count"] == 3
    assert {row["mission_id"] for row in body["records"]} == {recorded}


# ── the ring, for the profiles that record nothing ──────────────────────────


def test_with_no_mission_open_telemetry_comes_from_the_published_rows(dashboard):
    # DEMO and EXPO write nothing to the card (Q7), so the charts' history is
    # what DHS put on the bus and this service kept.
    _, client, port = dashboard
    published_row(client, timestamp="2026-09-01T10:00:00Z", battery=80, cpu_percent=11)
    published_row(client, timestamp="2026-09-01T10:00:30Z", battery=79, cpu_percent=12)

    body = get_json(port, "/api/telemetry")
    assert body["source"] == "live"
    assert body["count"] == 2
    # Newest first, the order the archive uses, for the same reason: the caller's
    # first use of the rows is the latest one.
    assert body["records"][0]["cpu_percent"] == 12
    # Rows carry an id in the database and the interface keys its lists on one.
    assert [row["id"] for row in body["records"]] == [2, 1]


def test_a_row_that_is_not_an_object_never_reaches_a_chart(dashboard):
    # It arrives from the broker. A malformed payload must not become a row of
    # nulls that looks like a measurement.
    _, client, port = dashboard
    client.deliver(TOPICS["dhs_telemetry"], {"row": "not a row"})
    client.deliver(TOPICS["dhs_telemetry"], {})
    assert get_json(port, "/api/telemetry")["count"] == 0


def test_an_open_mission_takes_precedence_over_the_ring(dashboard, recorded):
    # Both hold rows in DIAG: the ring fills from the bus while the recorder
    # writes. The database wins because it survives a restart of this service
    # and carries the whole session rather than its last few hours.
    _, client, port = dashboard
    published_row(client, timestamp="2026-09-01T10:00:00Z", battery=80)
    recording(client, recorded)
    body = get_json(port, "/api/telemetry")
    assert body["source"] == "mission"
    assert body["count"] == 3


def test_a_mission_closing_hands_the_endpoint_back_to_the_ring(dashboard, recorded, caplog):
    _, client, port = dashboard
    recording(client, recorded)
    published_row(client, timestamp="2026-09-01T10:00:00Z", battery=80)
    with caplog.at_level(logging.INFO):
        client.deliver(
            TOPICS["dhs_status"], {"recording": False, "database": None, "mission": None}
        )
    assert "in-memory ring" in caplog.text
    assert get_json(port, "/api/telemetry")["source"] == "live"


def test_missions_are_listed(dashboard, recorded):
    _, _, port = dashboard
    body = get_json(port, "/api/missions")
    assert body["count"] == 1
    assert body["missions"][0]["label"] == "walk to work"


def test_a_mission_carries_the_radio_traffic_of_its_own_trip(dashboard, recorded):
    # Otherwise the Radio Link Log is the one widget on a replaying page still
    # reading the live satellite, while everything beside it reads a recording.
    _, _, port = dashboard
    body = get_json(port, f"/api/missions/{recorded}")
    assert [event["text"] for event in body["radio"]] == ["CSAT t=1000", "!ping"]
    # Oldest first, the order a log is read in and the order attitude uses.
    assert body["radio"][0]["t"] < body["radio"][1]["t"]


def test_a_database_without_the_radio_table_is_an_empty_log(dashboard, roots, recorded):
    # An older file is a perfectly good archive of a trip that happened before
    # the radio log existed. Losing that mission to a schema version would be
    # the worse failure.
    conn = schema.connect(roots["db"], LOG)
    conn.execute("DROP TABLE radio_log")
    conn.commit()
    conn.close()

    _, _, port = dashboard
    assert get_json(port, f"/api/missions/{recorded}")["radio"] == []


def test_one_mission_carries_its_summary_alongside_its_detail(dashboard, recorded):
    # Empty arrays alone cannot say *why* they are empty. `purged_at` and `rows`
    # are what separate "detail aged out" from "recorded nothing".
    _, _, port = dashboard
    body = get_json(port, f"/api/missions/{recorded}")
    assert body["mission"]["id"] == recorded
    assert body["mission"]["purged_at"] is None
    assert len(body["telemetry"]) == 3
    assert [sample["t"] for sample in body["attitude"]] == [1000.0, 1001.0, 1002.0, 1003.0]


def test_mission_rows_are_oldest_first_because_a_timeline_plays_them(dashboard, recorded):
    _, _, port = dashboard
    body = get_json(port, f"/api/missions/{recorded}")
    assert [row["battery"] for row in body["telemetry"]] == [90, 89, 88]


def test_the_export_is_the_same_body_as_a_download(dashboard, recorded):
    # One endpoint backs both "keep a copy of this walk" and "produce the file
    # the public demo replays".
    _, _, port = dashboard
    _, headers, body = get(port, f"/api/missions/{recorded}/export")
    assert "attachment" in headers["Content-Disposition"]
    assert f"mission-{recorded}.json" in headers["Content-Disposition"]
    assert json.loads(body) == get_json(port, f"/api/missions/{recorded}")


def test_a_mission_that_is_not_there_is_404_and_not_a_crash(dashboard, recorded):
    _, _, port = dashboard
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(port, "/api/missions/9999")
    assert refused.value.code == 404


def test_an_empty_archive_is_served_rather_than_refused(dashboard):
    # No `recorded` fixture: the database has never been created, which is what
    # a satellite in HOSTED looks like.
    _, _, port = dashboard
    assert get_json(port, "/api/missions") == {"count": 0, "missions": []}
    # `source` is what separates "nothing has been recorded" from "this session
    # is not being recorded at all" — a viewer that cannot tell them apart
    # eventually decides the recorder is broken.
    assert get_json(port, "/api/telemetry") == {"count": 0, "records": [], "source": "live"}


# ── photographs ─────────────────────────────────────────────────────────────


def test_a_mission_s_photos_are_listed_and_served(dashboard):
    _, _, port = dashboard
    body = get_json(port, "/api/missions/42/photos")
    assert [photo["name"] for photo in body["photos"]] == ["frame_0001.jpg"]
    status, headers, payload = get(port, body["photos"][0]["url"])
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert payload.startswith(b"\xff\xd8")


def test_only_names_the_camera_writes_are_served(dashboard):
    # An allowlist, not a search for "..": a file that is not one the camera
    # writes is not served, whatever it happens to contain.
    _, _, port = dashboard
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(port, "/api/photos/42/notes.txt")
    assert refused.value.code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/photos/42/..%2f..%2f..%2fetc%2fpasswd",
        "/api/photos/unfiled/frame_0001.jpg",
        "/api/photos/42/%2e%2e%2findex.html",
    ],
)
def test_a_photo_path_that_is_not_an_allowlisted_shape_is_refused(dashboard, path):
    # A mission id is a run of digits — the same positive rule retention uses
    # to fence its deletions, and for the same reason.
    _, _, port = dashboard
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(port, path)
    assert refused.value.code == 404


def test_a_mission_with_no_photo_directory_lists_nothing(dashboard):
    _, _, port = dashboard
    assert get_json(port, "/api/missions/77/photos") == {"count": 0, "photos": []}


# ── static files ────────────────────────────────────────────────────────────


def test_the_interface_is_served_at_the_root(dashboard):
    _, _, port = dashboard
    status, headers, body = get(port, "/")
    assert status == 200
    assert b"<title>CubeSat</title>" in body
    assert headers["Content-Type"] == "text/html"


def test_an_unknown_path_falls_back_to_the_app(dashboard):
    # So a reload on a deep link lands in the interface rather than on an error
    # page. There is no router today; this is the whole of the SPA fallback.
    _, _, port = dashboard
    _, _, body = get(port, "/missions/42")
    assert b"<title>CubeSat</title>" in body


def test_hashed_assets_are_cached_hard_and_the_index_is_not(dashboard):
    # index.html is what points at the new hashes, so caching it would pin a
    # browser to the previous build.
    _, _, port = dashboard
    _, asset_headers, _ = get(port, "/static/app.js")
    _, index_headers, _ = get(port, "/")
    assert "max-age=31536000" in asset_headers["Cache-Control"]
    assert index_headers.get("Cache-Control") is None


def test_a_path_that_escapes_the_root_is_refused(dashboard, roots):
    # Checked after resolution rather than by looking for ".." in the request: a
    # symlink and an encoded traversal both survive a textual search, and
    # neither survives asking where the path actually landed.
    secret = roots["static"].parent / "secret.txt"
    secret.write_text("not for the browser")
    _, _, port = dashboard
    # Refused outright rather than quietly answered with the app: an escape
    # attempt is not a deep link, and the two should not look the same in a log.
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(port, "/%2e%2e/secret.txt")
    assert refused.value.code == 404


def test_a_missing_build_is_said_out_loud_rather_than_served_blank(
    service_factory, roots, caplog
):
    # The build is deployed separately, so an install that skipped that step
    # gets an API that answers and a page that is empty — a puzzle with no clue
    # in it unless the service says so.
    empty = roots["static"].parent / "nothing"
    empty.mkdir()
    service, _ = service_factory(
        DashboardService,
        port=free_port(),
        static_root=empty,
        photos_root=roots["photos"],
        db_path=roots["db"],
    )
    with caplog.at_level(logging.WARNING):
        service.on_start()
    service.on_stop()
    assert "has not been deployed" in caplog.text


# ── which database ──────────────────────────────────────────────────────────


def test_it_follows_the_recorder_onto_the_diag_database(dashboard, roots, tmp_path, caplog):
    # In DIAG the recorder writes diag.db, and a dashboard still showing
    # comms.db would be displaying last week's trip during a bench session.
    service, client, port = dashboard
    diag = tmp_path / "diag.db"
    conn = schema.connect(diag, LOG)
    MissionStore(conn, LOG).open("DIAG", "bench")
    conn.close()

    with caplog.at_level(logging.INFO):
        client.deliver(TOPICS["dhs_status"], {"recording": True, "database": str(diag)})

    assert "serving that" in caplog.text
    assert get_json(port, "/api/missions")["missions"][0]["label"] == "bench"


def test_a_recorder_with_no_database_open_still_serves_the_archive(dashboard, recorded):
    # `database` is null while no mission is open. The archive of past trips is
    # still worth serving, so this falls back rather than closing.
    _, client, port = dashboard
    client.deliver(TOPICS["dhs_status"], {"recording": False, "database": None})
    assert get_json(port, "/api/missions")["count"] == 1


def test_the_same_database_twice_is_not_reopened(dashboard, recorded, caplog):
    _, client, port = dashboard
    with caplog.at_level(logging.INFO):
        client.deliver(TOPICS["dhs_status"], {"recording": True, "database": str(config.DB_PATH)})
        client.deliver(TOPICS["dhs_status"], {"recording": True, "database": str(config.DB_PATH)})
    assert caplog.text.count("serving that") <= 1


# ── survivability ───────────────────────────────────────────────────────────


def test_a_port_already_taken_is_survived_rather_than_fatal(service_factory, roots, caplog):
    # Most likely a previous instance that has not let go. The process stays up
    # and keeps its heartbeat; systemd's Restart=always tries again, which beats
    # a unit flapping in a tight loop.
    port = free_port()
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("0.0.0.0", port))
    holder.listen(1)
    try:
        service, _ = service_factory(
            DashboardService,
            port=port,
            static_root=roots["static"],
            photos_root=roots["photos"],
            db_path=roots["db"],
        )
        with caplog.at_level(logging.ERROR):
            service.on_start()
        assert "nothing is being served" in caplog.text
        service.on_stop()
    finally:
        holder.close()


def test_an_endpoint_that_does_not_exist_is_404_and_not_a_crash(dashboard):
    _, _, port = dashboard
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(port, "/api/nothing-like-this")
    assert refused.value.code == 404


def test_a_handler_that_raises_answers_500_and_leaves_the_service_up(dashboard, monkeypatch):
    # Nothing a request can do may take this process down: it is also a
    # subscriber on the bus and a heartbeat OBC is watching.
    service, _, port = dashboard

    def explode(*_args, **_kwargs):
        raise RuntimeError("the card went away")

    monkeypatch.setattr(service._archive, "missions", explode)
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(port, "/api/missions")
    assert refused.value.code == 500
    # Still serving.
    assert get(port, "/")[0] == 200


def test_a_directory_request_serves_the_app_inside_it(dashboard, roots):
    # `/static/` is a directory. Serving its index rather than a listing is what
    # keeps the interface's own file layout out of the response.
    _, _, port = dashboard
    _, _, body = get(port, "/static/")
    assert b"<title>CubeSat</title>" in body


def test_a_file_that_vanishes_between_the_check_and_the_read_is_404(dashboard, roots):
    # The window is real on a satellite: retention deletes photographs while
    # somebody is looking at a gallery.
    _, _, port = dashboard
    vanishing = roots["photos"] / "42" / "frame_0002.jpg"
    vanishing.write_bytes(b"\xff\xd8jpeg")
    listing = get_json(port, "/api/missions/42/photos")
    assert len(listing["photos"]) == 2
    vanishing.unlink()
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(port, "/api/photos/42/frame_0002.jpg")
    assert refused.value.code == 404


def test_a_message_on_another_topic_changes_nothing(dashboard, recorded):
    # Only the recorder's two topics are subscribed, but the guard is explicit:
    # this service must not start acting on the bus at large.
    service, client, port = dashboard
    before = service._archive.path
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "FLIGHT"})
    assert service._archive.path == before
    assert get_json(port, "/api/missions")["count"] == 1


def test_a_browser_that_went_away_mid_response_is_not_a_traceback(dashboard, monkeypatch, caplog):
    # Ordinary on a satellite: somebody navigates away, or walks out of range of
    # an EXPO access point mid-request. Logged at debug — a traceback per
    # abandoned request would be the loudest thing on the card at a science fair.
    service, _, port = dashboard

    def went_away(*_args, **_kwargs):
        raise BrokenPipeError("client closed the connection")

    monkeypatch.setattr(service._archive, "missions", went_away)
    # The connection is dropped with nothing written, so urllib sees an empty
    # reply rather than a status — which is exactly what a browser that left
    # would have seen, had it still been there to see it.
    with caplog.at_level(logging.DEBUG, logger="dashboard.http"), pytest.raises(
        (urllib.error.URLError, http.client.RemoteDisconnected)
    ):
        get(port, "/api/missions")
    assert "client went away" in caplog.text
    assert "Traceback" not in caplog.text
    # Still serving.
    assert get(port, "/")[0] == 200
