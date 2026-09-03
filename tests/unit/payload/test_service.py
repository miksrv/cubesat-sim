import base64
import shutil
import threading
import time

import pytest

from cubesat.common import config
from cubesat.common.states import MissionState
from cubesat.common.topics import RETAINED, TOPICS
from cubesat.payload import camera as camera_module
from cubesat.payload.camera import BYTES_PER_MB
from cubesat.payload.service import PayloadService
from tests.unit.payload.test_camera import BENCH_POSITION, JPEG_BYTES, FakeCamera, Usage
from tests.unit.payload.test_science import BENCH_READING, FakeSensor


@pytest.fixture
def payload(service_factory, monkeypatch, tmp_path):
    # The photo root is redirected before the service builds its controller, so
    # a test that takes a photo files it in its own directory and not into the
    # data directory every other test shares.
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    # Where a photo goes with no mission open: a tmpfs on the satellite, a
    # directory of this test's own here.
    scratch = tmp_path / "run" / "photo"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(config, "PHOTO_SCRATCH_DIR", scratch)
    # Milliseconds rather than the shipped 300 s, and the floor out of the way.
    monkeypatch.setattr(camera_module, "MIN_MISSION_INTERVAL_SEC", 0.001)
    monkeypatch.setattr(config, "PHOTO_MISSION_INTERVAL_SEC", 0.01)
    sensor, camera = FakeSensor(), FakeCamera()
    service, client = service_factory(PayloadService, sensor=sensor, camera=camera)
    yield service, client, sensor, camera
    # A mission now starts photography on its own, so a test that opens one and
    # says nothing more leaves a frame thread running. Stopped here rather than
    # in each test: a leaked thread fails whichever *later* test looks at
    # threading.enumerate(), which is the hardest kind of failure to read.
    # on_stop is idempotent — a second close stops nothing and closes a closed
    # camera.
    service.on_stop()


@pytest.fixture
def free_space(monkeypatch):
    """Pretend the card has this many megabytes left."""

    def set_to(free_mb):
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _path: Usage(total=0, used=0, free=int(free_mb * BYTES_PER_MB)),
        )

    return set_to


def nominal(client, state=MissionState.NOMINAL):
    client.deliver(TOPICS["obc_status"], {"status": state.value, "profile": "DEMO"})


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def open_mission(client, mission_id=42, database=None):
    """DHS reporting an open mission — what starts a mission's photography.

    The database travels with the id because it does on the wire: both
    databases number their missions from 1, so the pair is what names a
    directory. Defaults to the mission database, which is where a FLIGHT trip
    goes.
    """
    client.deliver(
        TOPICS["dhs_status"],
        {
            "mission": {"id": mission_id},
            "database": str(database if database is not None else config.DB_PATH),
        },
    )


def close_mission(client):
    client.deliver(TOPICS["dhs_status"], {"mission": None, "database": str(config.DB_PATH)})


def photo_messages(client, kind=None):
    return [
        payload
        for payload in client.payloads(TOPICS["payload_photo"])
        if kind is None or payload.get("kind") == kind
    ]


# ── science ─────────────────────────────────────────────────────────────────


def test_a_tick_publishes_the_bench_reading_as_the_documented_payload(payload):
    service, client, _, _ = payload
    service.tick()
    assert client.last(TOPICS["payload_data"]) == {
        "timestamp": pytest.approx(time.time(), abs=5),
        "temperature": 27.96,
        "humidity": 41.21,
        "pressure": 1000.0,
        "light": 388.96,
        "uv_index": None,
        "uv_raw": 14,
    }


def test_the_science_payload_is_not_retained(payload):
    # A late subscriber wants the next reading, not the weather from before the
    # last power cycle. The status is the retained one; the data is not.
    service, client, _, _ = payload
    service.tick()
    assert TOPICS["payload_data"] not in RETAINED
    assert client.published[-1].retain is False


def test_a_sensor_that_cannot_be_read_publishes_nothing_rather_than_nulls(payload, caplog):
    # A payload of nulls would look downstream like a measured environment of
    # zero. The retained status is where the silence is explained.
    service, client, sensor, _ = payload
    service.on_start()
    sensor.reading = OSError("bus went away")
    with caplog.at_level("ERROR"):
        service.tick()
    assert client.payloads(TOPICS["payload_data"]) == []
    assert client.last(TOPICS["payload_status"])["sensor"]["present"] is False


def test_a_sensor_that_comes_back_starts_publishing_again(payload):
    service, client, sensor, _ = payload
    service.on_start()
    sensor.reading = OSError("bus went away")
    service.tick()
    sensor.reading = BENCH_READING
    service.tick()
    assert client.last(TOPICS["payload_data"])["temperature"] == 27.96
    assert client.last(TOPICS["payload_status"])["sensor"]["present"] is True


def test_the_cadence_follows_the_mission_state(payload):
    # Read from the shipped table, not repeated: the numbers are tuning, the
    # following is the behaviour. See the DHS test for the same treatment.
    service, client, _, _ = payload
    table = config.CADENCE["payload"]
    for state in (MissionState.NOMINAL, MissionState.LOW_POWER, MissionState.DEPLOY):
        nominal(client, state)
        assert service.interval == table[state.value]
    assert table[MissionState.LOW_POWER.value] > table[MissionState.NOMINAL.value]
    assert table[MissionState.DEPLOY.value] < table[MissionState.NOMINAL.value]


# ── reporting in ────────────────────────────────────────────────────────────


def test_the_status_is_published_at_start_and_retained(payload):
    # OBC's DEPLOY waits for this message and the window is bounded, so it
    # cannot wait for the first tick of a 60 s cadence.
    service, client, _, _ = payload
    service.on_start()
    assert TOPICS["payload_status"] in RETAINED
    assert client.published[-1].retain is True


def test_the_status_says_the_sensor_answered_not_that_the_process_started(payload):
    # A message that only proved a process was alive would be a heartbeat with
    # extra steps, and DEPLOY already has heartbeats.
    service, client, _, _ = payload
    service.on_start()
    status = client.last(TOPICS["payload_status"])
    assert status["sensor"] == {
        "device": "SEN0501", "present": True, "readings": 0, "last_read": None
    }
    assert status["camera"]["present"] is True


def test_the_sensor_reports_in_before_the_camera_probe_finishes(payload):
    # The camera probe imports picamera2, which on a cold Pi costs longer than
    # the whole DEPLOY window (35 s measured, 2026-08-28) — so the sensor's
    # answer goes out first, with the camera honestly null: "not probed yet"
    # is a different claim from "absent".
    service, client, _, _ = payload
    service.on_start()
    first, last = (
        client.payloads(TOPICS["payload_status"])[0],
        client.payloads(TOPICS["payload_status"])[-1],
    )
    assert first["sensor"]["present"] is True
    assert first["camera"]["present"] is None
    assert last["camera"]["present"] is True


def test_a_deploy_the_service_survived_republishes_the_status(payload):
    # A DEMO→EXPO switch keeps PAYLOAD running, and a status published only on
    # change would leave that DEPLOY with no fresh evidence inside its window.
    service, client, _, _ = payload
    service.on_start()
    nominal(client)
    before = len(client.payloads(TOPICS["payload_status"]))

    nominal(client, state=MissionState.DEPLOY)

    assert len(client.payloads(TOPICS["payload_status"])) == before + 1


def test_the_status_carries_the_mission_the_photos_are_being_filed_under(payload, tmp_path):
    service, client, _, _ = payload
    open_mission(client)
    status = client.last(TOPICS["payload_status"])
    assert status["mission_id"] == 42
    assert status["photo_dir"] == str(tmp_path / "photos" / "42")


def test_a_mission_id_that_is_not_an_integer_reads_as_no_mission(payload, tmp_path):
    # Withhold rather than fabricate: the id is a row id and every topic
    # carries it as an integer. Anything else is treated as no open mission —
    # the photo goes to the scratch directory — not coerced into a name.
    service, client, _, _ = payload
    for wrong in ("42", True, 4.2):
        open_mission(client, 7)
        client.deliver(
            TOPICS["dhs_status"],
            {"mission": {"id": wrong}, "database": str(config.DB_PATH)},
        )
        status = client.last(TOPICS["payload_status"])
        assert status["mission_id"] is None


def test_on_start_says_which_device_answered(payload, caplog):
    service, _, _, camera = payload
    camera.fail = True
    with caplog.at_level("INFO"):
        service.on_start()
    assert "SEN0501 environment answered" in caplog.text
    assert "Camera Module V2 is not answering" in caplog.text


def test_a_dead_camera_does_not_cost_us_the_science(payload):
    service, client, _, camera = payload
    camera.fail = True
    service.on_start()
    service.tick()
    assert client.last(TOPICS["payload_data"])["temperature"] == 27.96
    assert client.last(TOPICS["payload_status"])["camera"]["present"] is False


def test_a_dead_sensor_does_not_cost_us_the_camera(payload):
    service, client, sensor, _ = payload
    sensor.reading = OSError("bus went away")
    nominal(client)
    service.on_start()
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    assert photo_messages(client)[-1]["status"] == "SUCCESS"


def test_nothing_is_reported_when_neither_device_answered(payload, caplog):
    # Publishing anyway would tell DEPLOY that hardware answered when none of it
    # did — the one lie this message must never carry.
    service, client, sensor, camera = payload
    sensor.answers = False
    camera.fail = True
    with caplog.at_level("ERROR"):
        service.on_start()
    assert client.payloads(TOPICS["payload_status"]) == []
    assert "neither" in caplog.text


def test_a_silent_device_keeps_the_service_up(payload, caplog):
    # A vanished process takes its heartbeat with it, and OBC then cannot tell a
    # broken sensor from a broken service.
    service, _, sensor, camera = payload
    sensor.answers = False
    camera.fail = True
    with caplog.at_level("ERROR"):
        service.on_start()
    assert service.running


def test_a_driver_that_raises_from_probe_is_a_no_rather_than_a_crash(payload, caplog):
    service, client, sensor, _ = payload

    def explode():
        raise OSError("bus went away")

    sensor.probe = explode
    with caplog.at_level("ERROR"):
        service.on_start()
    assert client.last(TOPICS["payload_status"])["sensor"]["present"] is False


# ── take_photo ──────────────────────────────────────────────────────────────


def test_a_photo_request_answers_with_the_image(payload):
    # The base64 blob is how the Telegram bot and the dashboard receive a single
    # photo, and it is what the README documents.
    service, client, _, camera = payload
    nominal(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    message = photo_messages(client)[-1]
    assert message["request_id"] == "r1"
    assert message["status"] == "SUCCESS"
    assert message["kind"] == "photo"
    assert message["file"].startswith("photo_")
    # The bytes on the wire are the bytes on disk, not a re-encoding of them.
    # Compared against what the camera wrote rather than against the file: with
    # no mission open the frame has already been deleted, which is the point.
    assert base64.b64decode(message["photo_base64"]) == JPEG_BYTES


def test_a_photo_is_filed_under_the_mission_dhs_reported(payload, tmp_path):
    service, client, _, _ = payload
    nominal(client)
    open_mission(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    assert photo_messages(client)[-1]["path"].startswith(str(tmp_path / "photos" / "42"))


def test_a_diag_mission_files_beside_the_trips_not_among_them(payload, tmp_path):
    # W3: comms.db and diag.db both number their missions from 1, so DIAG
    # mission 42 and a FLIGHT trip 42 are different missions with the same id.
    # The database DHS reports is what tells them apart, and it arrives on the
    # same message as the id.
    service, client, _, _ = payload
    nominal(client)
    open_mission(client, 42, database=config.DIAG_DB_PATH)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    path = photo_messages(client)[-1]["path"]
    assert path.startswith(str(tmp_path / "photos-diag" / "42"))
    assert not path.startswith(str(tmp_path / "photos" / "42"))


def test_the_root_moves_with_the_database_mid_series(payload, tmp_path):
    # The pair is taken together, never separately: a satellite that went from
    # a DIAG rehearsal to a FLIGHT trip must not keep filing into photos-diag/.
    service, client, _, _ = payload
    nominal(client)
    open_mission(client, 3, database=config.DIAG_DB_PATH)
    assert client.last(TOPICS["payload_status"])["photo_dir"] == str(
        tmp_path / "photos-diag" / "3"
    )
    open_mission(client, 3, database=config.DB_PATH)
    assert client.last(TOPICS["payload_status"])["photo_dir"] == str(tmp_path / "photos" / "3")


def test_a_mission_with_no_database_reads_as_no_mission(payload, caplog):
    # The id's own treatment, for the same reason: an id without the database
    # it belongs to does not name a directory, and the two candidates are two
    # different trips. Withhold rather than guess.
    service, client, _, _ = payload
    open_mission(client, 7)
    with caplog.at_level("WARNING"):
        client.deliver(TOPICS["dhs_status"], {"mission": {"id": 42}})
    assert client.last(TOPICS["payload_status"])["mission_id"] is None
    assert "without a database" in caplog.text


def test_a_photo_with_no_mission_open_is_delivered_and_then_deleted(payload, tmp_path):
    # The DEMO/EXPO case: a photograph is taken because somebody asked, they see
    # it, and nothing is kept — the card is never touched. The pixels are in the
    # message, which is the whole delivery.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    message = photo_messages(client)[-1]
    assert message["status"] == "SUCCESS"
    assert message["mission_id"] is None
    assert message["photo_base64"]
    assert str(tmp_path / "run" / "photo") in message["path"]
    # Nothing left behind, in the scratch directory or on the card.
    assert list((tmp_path / "run" / "photo").iterdir()) == []
    assert not (tmp_path / "photos").exists()


def test_a_photo_taken_inside_a_mission_stays_on_the_card(payload, tmp_path):
    # The other half of the same rule: a trip's photographs are the point of
    # recording one, so these are filed and left alone.
    service, client, _, _ = payload
    nominal(client)
    open_mission(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    # Only the requested photograph: the mission's own frames are named apart,
    # and this test is about where a take_photo lands.
    assert len(list((tmp_path / "photos" / "42").glob("photo_*.jpg"))) == 1


def test_a_mission_that_closes_sends_later_photos_to_the_scratch_directory(payload, tmp_path):
    service, client, _, _ = payload
    nominal(client)
    open_mission(client)
    close_mission(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    assert photo_messages(client)[-1]["mission_id"] is None
    assert str(tmp_path / "run" / "photo") in photo_messages(client)[-1]["path"]


def test_the_retained_photograph_is_cleared_when_the_service_starts(payload):
    # payload_photo is retained so a page opened minutes later still shows the
    # last frame. HOSTD starts this service as a profile is applied, so a start
    # is where one session ends: a visitor at an EXPO stand must not meet the
    # previous demonstration's photograph as though it were current.
    service, client, _, _ = payload
    service.on_start()
    cleared = [p for p in client.published if p.topic == TOPICS["payload_photo"]]
    assert cleared and cleared[0].payload == ""
    assert cleared[0].retain is True


def test_a_repeated_dhs_status_does_not_republish_the_payload_status(payload):
    # DHS publishes on its own cadence and the status is retained; only a change
    # of mission is worth saying again.
    service, client, _, _ = payload
    open_mission(client)
    before = len(client.payloads(TOPICS["payload_status"]))
    open_mission(client)
    assert len(client.payloads(TOPICS["payload_status"])) == before


def test_a_photo_is_refused_with_a_reason_below_nominal(payload):
    # The reason is the whole point: "not allowed in LOW_POWER" is an answer,
    # while no response at all is a fault.
    service, client, _, camera = payload
    nominal(client, MissionState.LOW_POWER)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    message = photo_messages(client)[-1]
    assert message["status"] == "ERROR"
    assert message["reason"] == "Photo capture not allowed: mission state is 'LOW_POWER'"
    assert message["request_id"] == "r1"
    assert camera.captures == []


def test_a_photo_is_refused_before_the_mission_state_is_known(payload):
    service, client, _, _ = payload
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    assert photo_messages(client)[-1]["status"] == "ERROR"


def test_a_capture_that_fails_answers_with_the_error(payload, caplog):
    service, client, _, camera = payload
    service.on_start()
    nominal(client)
    camera.fail = True
    with caplog.at_level("ERROR"):
        client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    message = photo_messages(client)[-1]
    assert message["status"] == "ERROR"
    assert "camera went away" in message["reason"]
    assert client.last(TOPICS["payload_status"])["camera"]["present"] is False


# ── the free-space floor ────────────────────────────────────────────────────


def test_a_photo_is_refused_when_the_card_is_nearly_full(payload, free_space):
    # The camera is the only unbounded writer on this satellite, and the next
    # write to fail on a full card is the telemetry row the mission exists to
    # record.
    service, client, _, camera = payload
    service.on_start()
    nominal(client)
    free_space(8)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    message = photo_messages(client)[-1]
    assert message["status"] == "ERROR"
    assert "8 MB free" in message["reason"]
    assert "MB floor" in message["reason"]
    assert camera.captures == []


def test_a_full_card_is_not_reported_as_a_broken_camera(payload, free_space):
    # The camera is fine; the card is not. Marking the camera absent would send
    # someone to check a ribbon cable.
    service, client, _, _ = payload
    service.on_start()
    nominal(client)
    free_space(8)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    status = client.last(TOPICS["payload_status"])
    assert status["camera"]["present"] is True
    assert status["storage"]["blocked"] is True
    assert status["storage"]["free_mb"] == 8.0


def test_the_status_says_the_card_is_what_is_stopping_the_photos(payload):
    # Visible on the topic that describes the payload, not only in a log nobody
    # is reading at a science fair.
    service, client, _, _ = payload
    service.on_start()
    status = client.last(TOPICS["payload_status"])
    assert status["storage"]["blocked"] is False
    assert status["storage"]["min_free_mb"] == float(config.PHOTOS_MIN_FREE_MB)
    assert status["mission_photos"]["reason"] is None  # none was ever started


def test_a_full_card_does_not_cost_us_the_science(payload, free_space):
    # That reading is small, bounded, and the thing most likely to explain what
    # went wrong. It is the last thing to give up.
    service, client, _, _ = payload
    service.on_start()
    free_space(0)
    service.tick()
    assert client.last(TOPICS["payload_data"])["temperature"] == 27.96


def test_a_series_that_the_card_stopped_says_so_in_the_status(payload, free_space):
    service, client, _, _ = payload
    service.on_start()
    nominal(client)
    free_space(config.PHOTOS_MIN_FREE_MB + 10)
    open_mission(client)
    assert wait_until(lambda: photo_messages(client, "mission_frame"))
    free_space(4)
    assert wait_until(
        lambda: client.last(TOPICS["payload_status"])["mission_photos"]["active"] is False
    )
    reported = client.last(TOPICS["payload_status"])["mission_photos"]
    assert "MB floor" in reported["reason"]
    # The frames it did take are still on disk and still counted.
    assert reported["frames"] >= 1


def test_nothing_is_deleted_to_make_room(payload, free_space, tmp_path):
    # Retention is DHS's job. A service that writes files and also decides which
    # ones to remove is one bug away from removing the wrong ones.
    service, client, _, _ = payload
    service.on_start()
    nominal(client)
    open_mission(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    existing = sorted((tmp_path / "photos" / "42").iterdir())
    free_space(0)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r2"})
    assert sorted((tmp_path / "photos" / "42").iterdir()) == existing


# ── the overlay ─────────────────────────────────────────────────────────────


def test_the_overlay_is_echoed_in_the_response_rather_than_drawn_on_the_photo(payload):
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["adcs_status"], {"timestamp": 1741863600.0, "gnss": BENCH_POSITION})
    client.deliver(
        TOPICS["command"],
        {"command": "take_photo", "request_id": "r1", "params": {"overlay": True}},
    )
    overlay = photo_messages(client)[-1]["overlay"]
    assert overlay["mission_state"] == "NOMINAL"
    assert overlay["position"]["lat"] == 37.676896
    assert overlay["captured_at"].endswith("Z")


def test_no_overlay_is_recorded_when_none_was_asked_for(payload):
    service, client, _, _ = payload
    nominal(client)
    client.deliver(
        TOPICS["command"],
        {"command": "take_photo", "request_id": "r1", "params": {"overlay": False}},
    )
    assert photo_messages(client)[-1]["overlay"] is None


def test_a_photo_taken_before_any_fix_records_no_position(payload):
    # "If one is known" is literal: adcs_status is not retained, so before ADCS
    # publishes there is nothing to record.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(
        TOPICS["command"],
        {"command": "take_photo", "request_id": "r1", "params": {"overlay": True}},
    )
    assert photo_messages(client)[-1]["overlay"]["position"] is None


def test_a_fixless_reading_does_not_overwrite_the_last_known_position(payload):
    # ADCS reports the last known fix with fix=false, and taking it here would
    # only lose the age of the coordinate a photo is stamped with.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["adcs_status"], {"timestamp": 1741863600.0, "gnss": BENCH_POSITION})
    client.deliver(
        TOPICS["adcs_status"],
        {"timestamp": 1741864000.0, "gnss": {**BENCH_POSITION, "fix": False}},
    )
    client.deliver(
        TOPICS["command"],
        {"command": "take_photo", "request_id": "r1", "params": {"overlay": True}},
    )
    assert photo_messages(client)[-1]["overlay"]["position"]["at"] == 1741863600.0


def test_an_adcs_message_with_no_gnss_object_is_ignored(payload):
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["adcs_status"], {"timestamp": 1741863600.0, "gnss": None})
    client.deliver(
        TOPICS["command"],
        {"command": "take_photo", "request_id": "r1", "params": {"overlay": True}},
    )
    assert photo_messages(client)[-1]["overlay"]["position"] is None


# ── the mission's own photography ───────────────────────────────────────────


def test_an_open_mission_starts_photographing_by_itself(payload):
    # No command, and no interval on the wire: a recorded mission is a trip, a
    # trip wants pictures along the way, and nobody was going to remember to ask
    # for them before walking out of the house.
    service, client, _, _ = payload
    nominal(client)
    open_mission(client)
    assert wait_until(lambda: len(photo_messages(client, "mission_frame")) >= 2)
    assert [f["sequence"] for f in photo_messages(client, "mission_frame")[:2]] == [1, 2]
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is True


def test_a_mission_frame_never_carries_the_image(payload):
    # A mission's frames pushed through the broker would be tens of megabytes
    # competing with the telemetry the satellite exists to collect. They are on
    # the card, filed under their mission, and that is where a gallery reads them.
    service, client, _, _ = payload
    nominal(client)
    open_mission(client)
    assert wait_until(lambda: len(photo_messages(client, "mission_frame")) >= 2)
    close_mission(client)
    for frame in photo_messages(client, "mission_frame"):
        assert "photo_base64" not in frame
        assert frame["file"] and frame["size_bytes"] > 0


def test_the_kind_field_is_what_tells_the_two_apart(payload):
    # So nothing has to infer the shape from the presence of a base64 blob.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    open_mission(client)
    assert wait_until(lambda: photo_messages(client, "mission_frame"))
    close_mission(client)
    kinds = {message["kind"] for message in photo_messages(client) if "kind" in message}
    assert kinds == {"photo", "mission_frame"}


def test_the_mission_closing_is_what_ends_the_series(payload):
    service, client, _, _ = payload
    nominal(client)
    open_mission(client)
    assert wait_until(lambda: photo_messages(client, "mission_frame"))
    close_mission(client)
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is False
    assert client.last(TOPICS["payload_status"])["mission_photos"]["reason"] is not None


def test_the_interval_reported_is_the_one_being_used(payload, monkeypatch):
    service, client, _, _ = payload
    monkeypatch.setattr(config, "PHOTO_MISSION_INTERVAL_SEC", 30.0)
    nominal(client)
    open_mission(client)
    reported = client.last(TOPICS["payload_status"])["mission_photos"]
    assert reported["active"] is True
    assert reported["interval_sec"] == 30.0


def test_a_descent_out_of_nominal_stops_the_series(payload, caplog, monkeypatch):
    # The frame loop re-asks the gate too, but at the shipped 300 s interval that
    # would be up to five minutes of a state that wanted the camera off.
    service, client, _, _ = payload
    monkeypatch.setattr(config, "PHOTO_MISSION_INTERVAL_SEC", 30.0)
    nominal(client)
    open_mission(client)
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is True
    with caplog.at_level("WARNING"):
        nominal(client, MissionState.LOW_POWER)
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is False
    assert "stopping photography" in caplog.text


def test_a_recovery_starts_the_series_again(payload, monkeypatch):
    # The loop that would have re-asked the gate has ended by now, so nothing
    # else could restart it — and a mission that photographed the first half of a
    # walk and then quietly stopped after a LOW_POWER dip is the failure here.
    service, client, _, _ = payload
    monkeypatch.setattr(config, "PHOTO_MISSION_INTERVAL_SEC", 30.0)
    nominal(client)
    open_mission(client)
    nominal(client, MissionState.LOW_POWER)
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is False

    nominal(client, MissionState.NOMINAL)
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is True


def test_the_same_state_arriving_again_leaves_the_series_running(payload, monkeypatch):
    # OBC republishes its retained status on its own tick and on every reconnect,
    # so PAYLOAD sees the state it is already in over and over.
    service, client, _, _ = payload
    monkeypatch.setattr(config, "PHOTO_MISSION_INTERVAL_SEC", 30.0)
    nominal(client)
    open_mission(client)
    published = len(client.payloads(TOPICS["payload_status"]))
    nominal(client, MissionState.NOMINAL)
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is True
    # Nothing changed, so nothing was republished: the reconciler is idempotent.
    assert len(client.payloads(TOPICS["payload_status"])) == published


def test_a_state_change_with_no_mission_open_changes_nothing(payload):
    service, client, _, _ = payload
    nominal(client)
    before = len(client.payloads(TOPICS["payload_status"]))
    nominal(client, MissionState.LOW_POWER)
    assert len(client.payloads(TOPICS["payload_status"])) == before


def test_a_mission_that_opens_below_nominal_photographs_nothing(payload):
    # DHS records through LOW_POWER and SAFE — the track is the point of the
    # trip — while the camera is exactly the discretionary work those states
    # exist to stop. So a mission can legitimately be open with no photography.
    service, client, _, _ = payload
    nominal(client, MissionState.LOW_POWER)
    open_mission(client)
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is False
    assert photo_messages(client, "mission_frame") == []


def test_a_camera_that_never_answered_starts_no_series(payload):
    # Three failures in a row would end the run anyway; the point is not to fill
    # the log finding that out on every mission.
    service, client, _, camera = payload
    camera.fail = True
    service.on_start()
    nominal(client)
    open_mission(client)
    assert client.last(TOPICS["payload_status"])["mission_photos"]["active"] is False


# ── commands in general ─────────────────────────────────────────────────────


def test_commands_addressed_to_other_services_are_ignored_in_silence(payload):
    # OBC's and COMMS' commands share this topic. Warning about each one would
    # make every profile change look like a fault.
    service, client, _, _ = payload
    nominal(client)
    # `science_start` is in the list on purpose: it was retired on 2026-09-02,
    # and a stale ground client publishing it must still be a non-event here.
    for command in ("set_profile", "science_start", "get_telemetry", "safe_mode"):
        client.deliver(TOPICS["command"], {"command": command})
    assert client.payloads(TOPICS["payload_photo"]) == []


def test_a_garbled_command_cannot_take_the_service_down(payload):
    # The same command arrives over MQTT and over LoRa, so anything typeable by
    # hand or manglable by a radio eventually shows up here.
    service, client, _, _ = payload
    nominal(client)
    for data in ({}, {"command": 42}, {"command": "take_photo", "params": "overlay"},
                 {"command": "take_photo", "request_id": 7}):
        client.deliver(TOPICS["command"], data)
    assert service.running


def test_a_photo_request_with_no_request_id_is_still_answered(payload):
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["command"], {"command": "take_photo"})
    assert photo_messages(client)[-1]["request_id"] is None


# ── shutdown and wiring ─────────────────────────────────────────────────────


def test_stopping_gives_the_camera_back_and_ends_the_series(payload):
    service, client, _, camera = payload
    nominal(client)
    open_mission(client)
    assert wait_until(lambda: photo_messages(client, "mission_frame"))
    service.on_stop()
    assert camera.closed is True
    assert not any(thread.name == "mission-photos" for thread in threading.enumerate())


def test_stopping_closes_a_sensor_that_has_something_to_close(payload):
    service, _, sensor, _ = payload
    closed = []
    sensor.close = lambda: closed.append(True)
    service.on_stop()
    assert closed == [True]


def test_stopping_a_sensor_with_nothing_to_close_is_fine(payload):
    service, _, sensor, _ = payload
    assert not hasattr(sensor, "close")
    service.on_stop()


def test_the_real_drivers_are_used_when_none_are_given(service_factory):
    # The mock HAL is active in tests, so this proves the wiring to the registry
    # rather than the drivers themselves.
    service, _ = service_factory(PayloadService)
    assert service._sensor.probe() is True
    assert service._controller.probe() is True


def test_it_subscribes_to_the_topics_it_needs_and_no_others(payload):
    service, client, _, _ = payload
    client.connect_ok()
    assert set(client.subscribed) == {
        TOPICS["command"], TOPICS["dhs_status"], TOPICS["adcs_status"], TOPICS["obc_status"]
    }


def test_a_reconnect_republishes_the_retained_status(payload):
    # A broker restart discards retained messages. PAYLOAD publishes only on
    # change, so without this DEPLOY would find no evidence for a subsystem that
    # is working perfectly — it would fail the bring-up of a healthy satellite
    # because the broker bounced.
    service, client, _sensor, _camera = payload
    service.on_start()
    before = len(client.payloads(TOPICS["payload_status"]))
    service.on_connected()
    assert len(client.payloads(TOPICS["payload_status"])) == before + 1


def test_a_reconnect_stays_silent_when_nothing_ever_answered(service_factory):
    from cubesat.payload.service import PayloadService

    class Dead:
        def probe(self):
            return False

        def read(self):
            raise OSError("no device")

        def close(self):
            return None

    service, client = service_factory(PayloadService, sensor=Dead(), camera=Dead())
    service.on_start()
    service.on_connected()
    # The gap is the honest signal: republishing would tell DEPLOY that hardware
    # answered when none of it did.
    assert client.payloads(TOPICS["payload_status"]) == []
