import base64
import shutil
import threading
import time

import pytest

from cubesat.common import config
from cubesat.common.states import MissionState
from cubesat.common.topics import RETAINED, TOPICS
from cubesat.payload.camera import BYTES_PER_MB
from cubesat.payload.service import PayloadService
from tests.unit.payload.test_camera import BENCH_POSITION, FakeCamera, Usage
from tests.unit.payload.test_science import BENCH_READING, FakeSensor


@pytest.fixture
def payload(service_factory, monkeypatch, tmp_path):
    # The photo root is redirected before the service builds its controller, so
    # a test that takes a photo files it in its own directory and not into the
    # data directory every other test shares.
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    monkeypatch.setattr(config, "MIN_TIMELAPSE_INTERVAL_SEC", 0.001)
    sensor, camera = FakeSensor(), FakeCamera()
    service, client = service_factory(PayloadService, sensor=sensor, camera=camera)
    return service, client, sensor, camera


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
    service, client, _, _ = payload
    nominal(client)
    assert service.interval == 60
    nominal(client, MissionState.LOW_POWER)
    assert service.interval == 300
    nominal(client, MissionState.DEPLOY)
    # DEPLOY has to report in well inside OBC's bring-up window.
    assert service.interval == 2


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
    client.deliver(TOPICS["dhs_status"], {"mission": {"id": 42}})
    status = client.last(TOPICS["payload_status"])
    assert status["mission_id"] == 42
    assert status["photo_dir"] == str(tmp_path / "photos" / "42")


def test_a_mission_id_that_is_not_an_integer_reads_as_no_mission(payload, tmp_path):
    # Withhold rather than fabricate: the id is a row id and every topic
    # carries it as an integer. Anything else is treated as no open mission —
    # photos go to unfiled/ — not coerced into a directory name.
    service, client, _, _ = payload
    for wrong in ("42", True, 4.2):
        client.deliver(TOPICS["dhs_status"], {"mission": {"id": 7}})
        client.deliver(TOPICS["dhs_status"], {"mission": {"id": wrong}})
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
    assert base64.b64decode(message["photo_base64"]) == camera.captures[-1].path.read_bytes()


def test_a_photo_is_filed_under_the_mission_dhs_reported(payload, tmp_path):
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["dhs_status"], {"mission": {"id": 42}})
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    assert photo_messages(client)[-1]["path"].startswith(str(tmp_path / "photos" / "42"))


def test_a_photo_taken_with_no_mission_open_is_filed_not_refused(payload, tmp_path):
    # DHS is not written yet, and a DEMO run may have no mission at all.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    message = photo_messages(client)[-1]
    assert message["status"] == "SUCCESS"
    assert message["mission_id"] is None
    assert "/unfiled/" in message["path"]


def test_a_mission_that_closes_sends_later_photos_back_to_unfiled(payload):
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["dhs_status"], {"mission": {"id": 42}})
    client.deliver(TOPICS["dhs_status"], {"mission": None})
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    assert photo_messages(client)[-1]["mission_id"] is None


def test_a_repeated_dhs_status_does_not_republish_the_payload_status(payload):
    # DHS publishes on its own cadence and the status is retained; only a change
    # of mission is worth saying again.
    service, client, _, _ = payload
    client.deliver(TOPICS["dhs_status"], {"mission": {"id": 42}})
    before = len(client.payloads(TOPICS["payload_status"]))
    client.deliver(TOPICS["dhs_status"], {"mission": {"id": 42}})
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


def test_a_photo_is_permitted_in_science(payload):
    service, client, _, _ = payload
    nominal(client, MissionState.SCIENCE)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    assert photo_messages(client)[-1]["status"] == "SUCCESS"


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
    assert status["timelapse"]["reason"] is None  # none was ever started


def test_a_full_card_does_not_cost_us_the_science(payload, free_space):
    # That reading is small, bounded, and the thing most likely to explain what
    # went wrong. It is the last thing to give up.
    service, client, _, _ = payload
    service.on_start()
    free_space(0)
    service.tick()
    assert client.last(TOPICS["payload_data"])["temperature"] == 27.96


def test_a_timelapse_that_the_card_stopped_says_so_in_the_status(payload, free_space):
    service, client, _, _ = payload
    service.on_start()
    nominal(client)
    free_space(config.PHOTOS_MIN_FREE_MB + 10)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 0.01}}
    )
    assert wait_until(lambda: photo_messages(client, "timelapse"))
    free_space(4)
    assert wait_until(
        lambda: client.last(TOPICS["payload_status"])["timelapse"]["active"] is False
    )
    timelapse = client.last(TOPICS["payload_status"])["timelapse"]
    assert "MB floor" in timelapse["reason"]
    # The frames it did take are still on disk and still counted.
    assert timelapse["frames"] >= 1


def test_nothing_is_deleted_to_make_room(payload, free_space, tmp_path):
    # Retention is DHS's job. A service that writes files and also decides which
    # ones to remove is one bug away from removing the wrong ones.
    service, client, _, _ = payload
    service.on_start()
    nominal(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    existing = sorted((tmp_path / "photos" / "unfiled").iterdir())
    free_space(0)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r2"})
    assert sorted((tmp_path / "photos" / "unfiled").iterdir()) == existing


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


# ── the timelapse ───────────────────────────────────────────────────────────


def test_a_timelapse_frame_never_carries_the_image(payload):
    # Five hundred frames at a few hundred kilobytes each is hundreds of
    # megabytes through a broker that is also carrying the telemetry the
    # satellite exists to collect. The frames are on disk, filed by mission.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 0.01}}
    )
    assert wait_until(lambda: len(photo_messages(client, "timelapse")) >= 2)
    client.deliver(TOPICS["command"], {"command": "stop_timelapse"})
    for frame in photo_messages(client, "timelapse"):
        assert "photo_base64" not in frame
        assert frame["size_bytes"] > 0
        assert frame["path"].endswith(".jpg")
    assert [f["sequence"] for f in photo_messages(client, "timelapse")[:2]] == [1, 2]


def test_the_kind_field_is_what_tells_the_two_apart(payload):
    # A consumer should branch on a field, not on whether a base64 blob happens
    # to be present.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(TOPICS["command"], {"command": "take_photo", "request_id": "r1"})
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 0.01}}
    )
    assert wait_until(lambda: photo_messages(client, "timelapse"))
    client.deliver(TOPICS["command"], {"command": "stop_timelapse"})
    kinds = [message.get("kind") for message in photo_messages(client)]
    assert "photo" in kinds and "timelapse" in kinds


def test_a_running_timelapse_is_visible_in_the_status(payload):
    service, client, _, _ = payload
    nominal(client)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 30}}
    )
    timelapse = client.last(TOPICS["payload_status"])["timelapse"]
    assert timelapse["active"] is True
    assert timelapse["interval_sec"] == 30
    client.deliver(TOPICS["command"], {"command": "stop_timelapse"})
    assert client.last(TOPICS["payload_status"])["timelapse"]["active"] is False


def test_a_timelapse_follows_the_same_gate_as_a_capture(payload):
    service, client, _, camera = payload
    nominal(client, MissionState.LOW_POWER)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 1}}
    )
    assert photo_messages(client)[-1]["status"] == "ERROR"
    assert camera.captures == []


def test_a_timelapse_without_a_usable_interval_is_refused(payload):
    # An interval is the whole content of this command; guessing one would mean
    # a garbled uplink silently starting a run at a rate nobody chose.
    service, client, _, _ = payload
    nominal(client)
    for params in ({}, {"interval_sec": 0}, {"interval_sec": -5}, {"interval_sec": "fast"},
                   # True is an int in Python, and a boolean interval is a
                   # garbled command rather than a one-second one.
                   {"interval_sec": True}):
        client.deliver(TOPICS["command"], {"command": "start_timelapse", "params": params})
        assert photo_messages(client)[-1]["status"] == "ERROR"
    assert service._controller.timelapse.active is False


def test_a_second_timelapse_does_not_quietly_replace_the_first(payload):
    # Restarting would abandon a run somebody is watching, and the two intervals
    # would be indistinguishable in the frames afterwards.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 30}}
    )
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 5}}
    )
    assert photo_messages(client)[-1]["reason"] == "a timelapse is already running"
    assert client.last(TOPICS["payload_status"])["timelapse"]["interval_sec"] == 30
    client.deliver(TOPICS["command"], {"command": "stop_timelapse"})


def test_stopping_a_timelapse_is_permitted_from_a_state_that_refuses_captures(payload):
    # Stopping something is never the dangerous direction.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 30}}
    )
    nominal(client, MissionState.SAFE)
    client.deliver(TOPICS["command"], {"command": "stop_timelapse"})
    assert client.last(TOPICS["payload_status"])["timelapse"]["active"] is False


def test_stopping_when_nothing_is_running_is_not_an_error(payload, caplog):
    service, client, _, _ = payload
    with caplog.at_level("INFO"):
        client.deliver(TOPICS["command"], {"command": "stop_timelapse"})
    assert "no timelapse was running" in caplog.text


def test_a_descent_out_of_nominal_stops_a_running_timelapse(payload, caplog):
    # Immediately, rather than at the end of the current interval — which on a
    # slow timelapse could be minutes of a state that wanted the camera off.
    service, client, _, _ = payload
    nominal(client)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 30}}
    )
    with caplog.at_level("WARNING"):
        nominal(client, MissionState.LOW_POWER)
    assert client.last(TOPICS["payload_status"])["timelapse"]["active"] is False
    assert "stopping the timelapse" in caplog.text


def test_a_state_change_with_no_timelapse_running_changes_nothing(payload):
    service, client, _, _ = payload
    nominal(client)
    before = len(client.payloads(TOPICS["payload_status"]))
    nominal(client, MissionState.LOW_POWER)
    assert len(client.payloads(TOPICS["payload_status"])) == before


def test_a_state_change_that_still_permits_the_camera_leaves_it_running(payload):
    service, client, _, _ = payload
    nominal(client)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 30}}
    )
    nominal(client, MissionState.SCIENCE)
    assert client.last(TOPICS["payload_status"])["timelapse"]["active"] is True
    client.deliver(TOPICS["command"], {"command": "stop_timelapse"})


# ── commands in general ─────────────────────────────────────────────────────


def test_commands_addressed_to_other_services_are_ignored_in_silence(payload):
    # OBC's and COMMS' commands share this topic. Warning about each one would
    # make every profile change look like a fault.
    service, client, _, _ = payload
    nominal(client)
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


def test_stopping_gives_the_camera_back_and_ends_the_timelapse(payload):
    service, client, _, camera = payload
    nominal(client)
    client.deliver(
        TOPICS["command"], {"command": "start_timelapse", "params": {"interval_sec": 0.01}}
    )
    assert wait_until(lambda: photo_messages(client, "timelapse"))
    service.on_stop()
    assert camera.closed is True
    assert not any(thread.name == "timelapse" for thread in threading.enumerate())


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
