"""PAYLOAD from process start to published telemetry, through the real run loop.

The unit tests call ``tick()`` and hand the service messages directly. Here
``run()`` drives, and the two real drivers sit behind a fake I2C bus and a fake
``picamera2`` — so what is checked is the part no single layer can show on its
own: that the registers measured on the bench arrive on MQTT as the documented
payload, that OBC's DEPLOY gets its evidence without waiting out a 60 s cadence,
and that a mission's own photography stops when the process does.
"""

from __future__ import annotations

import base64
import sys
import threading
import time
import types

from cubesat.common import config
from cubesat.common.topics import TOPICS
from cubesat.hal.rpi.camera import PiCamera
from cubesat.hal.rpi.sen0501 import SEN0501
from cubesat.payload import camera as camera_module
from cubesat.payload.service import PayloadService
from tests.unit.hal.test_camera import FakePicamera2, FakeTransform
from tests.unit.hal.test_sen0501 import FakeSen0501Bus


def run_briefly(service, seconds=0.2):
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(seconds)
    service.stop()
    thread.join(timeout=5)
    assert not thread.is_alive(), "service did not shut down"


def build(service_factory, monkeypatch, tmp_path, *, registers=None):
    """A PAYLOAD wired to the real drivers, with only the hardware faked."""
    FakePicamera2.instances = []
    picamera2_module = types.ModuleType("picamera2")
    picamera2_module.Picamera2 = FakePicamera2
    libcamera_module = types.ModuleType("libcamera")
    libcamera_module.Transform = FakeTransform
    monkeypatch.setitem(sys.modules, "picamera2", picamera2_module)
    monkeypatch.setitem(sys.modules, "libcamera", libcamera_module)
    monkeypatch.setattr(config, "PHOTOS_DIR", tmp_path / "photos")
    scratch = tmp_path / "run" / "photo"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(config, "PHOTO_SCRATCH_DIR", scratch)
    monkeypatch.setattr(camera_module, "MIN_MISSION_INTERVAL_SEC", 0.001)
    monkeypatch.setattr(config, "PHOTO_MISSION_INTERVAL_SEC", 0.01)
    bus = FakeSen0501Bus(registers)
    service, client = service_factory(
        PayloadService, sensor=SEN0501(bus=bus), camera=PiCamera()
    )
    return service, client, bus


def test_the_bench_registers_arrive_on_mqtt_as_the_documented_payload(
    service_factory, monkeypatch, tmp_path
):
    # The real driver, the numbers the hardware document recorded, and the
    # payload the README specifies — end to end, with nothing faked but the bus.
    service, client, _ = build(service_factory, monkeypatch, tmp_path)
    service.on_start()
    service.tick()

    payload = client.last(TOPICS["payload_data"])
    assert payload["temperature"] == 27.96
    assert payload["humidity"] == 41.21
    assert payload["pressure"] == 1000.0
    assert payload["light"] == 388.96
    # The board revision is unknown on this satellite, so the index is withheld
    # and the raw count is what gets recorded. That is the whole point of the
    # pair travelling together.
    assert payload["uv_index"] is None
    assert payload["uv_raw"] == 14


def test_payload_publishes_science_and_heartbeats_through_the_run_loop(
    service_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    service, client, _ = build(service_factory, monkeypatch, tmp_path)
    client.connect_ok()
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "DEMO"})
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 0.02))

    run_briefly(service)

    assert client.payloads(TOPICS["payload_data"]), "no science telemetry was published"
    beats = [p for p in client.payloads(TOPICS["heartbeat"]) if p["alive"]]
    assert beats and all(beat["service"] == "payload" for beat in beats)
    assert client.payloads(TOPICS["heartbeat"])[-1]["alive"] is False


def test_the_first_status_arrives_without_waiting_a_whole_cadence(
    service_factory, monkeypatch, tmp_path
):
    # OBC's DEPLOY treats the first payload_status as the evidence that the
    # sensor answered, and gives the subsystem a bounded window. PAYLOAD's
    # NOMINAL cadence is 60 s — three times that window — so a status that
    # waited for the first tick would fail a healthy satellite's own bring-up.
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 5.0)
    service, client, _ = build(service_factory, monkeypatch, tmp_path)
    client.connect_ok()
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 60.0))

    run_briefly(service, seconds=0.1)

    status = client.payloads(TOPICS["payload_status"])
    # Two, staged: the sensor's answer goes out before the camera probe, which
    # on a cold Pi costs longer than the whole DEPLOY window, with the camera
    # honestly null — "not probed yet" is not the same claim as "absent".
    assert len(status) == 2
    assert status[0]["sensor"]["present"] is True
    assert status[0]["camera"]["present"] is None
    assert status[1]["sensor"]["present"] is True
    assert status[1]["camera"]["present"] is True
    # And it says where the photos would go and whether there is room for them,
    # read from the real filesystem this test is running on.
    assert status[1]["storage"]["blocked"] is False
    assert status[1]["mission_photos"] == {
        "active": False, "interval_sec": None, "frames": 0, "reason": None
    }


def test_a_sensor_that_never_answers_still_leaves_a_reporting_subsystem(
    service_factory, monkeypatch, tmp_path
):
    # One dead device degrades the payload; it does not silence the subsystem.
    # The camera answered, so PAYLOAD reports in — saying which half is missing.
    service, client, bus = build(service_factory, monkeypatch, tmp_path)
    bus.fail = True
    client.connect_ok()

    run_briefly(service, seconds=0.1)

    status = client.last(TOPICS["payload_status"])
    assert status["sensor"]["present"] is False
    assert status["camera"]["present"] is True
    assert client.payloads(TOPICS["payload_data"]) == []


def test_a_ground_photo_request_travels_from_the_broker_to_the_disk_and_back(
    service_factory, monkeypatch, tmp_path
):
    service, client, _ = build(service_factory, monkeypatch, tmp_path)
    client.connect_ok()
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "DEMO"})
    client.deliver(
        TOPICS["dhs_status"], {"mission": {"id": 42}, "database": str(config.DB_PATH)}
    )
    client.deliver(
        TOPICS["command"],
        {"command": "take_photo", "request_id": "req_001", "params": {"overlay": True}},
    )

    message = client.last(TOPICS["payload_photo"])
    assert message["status"] == "SUCCESS"
    assert message["kind"] == "photo"
    on_disk = tmp_path / "photos" / "42" / message["file"]
    assert on_disk.exists()
    assert base64.b64decode(message["photo_base64"]) == on_disk.read_bytes()
    # The overlay went to a file beside the photo, not onto the pixels.
    assert on_disk.with_suffix(".json").exists()
    assert message["overlay"]["mission_state"] == "NOMINAL"

    service.on_stop()


def test_an_open_mission_photographs_itself_and_stops_with_the_service(
    service_factory, monkeypatch, tmp_path
):
    # The asymmetry that keeps a mission's frames off the broker, checked where
    # it actually matters: through the run loop, with the real capture path.
    # Started by DHS reporting a mission rather than by a command — there is no
    # command any more.
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 5.0)
    service, client, _ = build(service_factory, monkeypatch, tmp_path)
    client.connect_ok()
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "FLIGHT"})
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 60.0))

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    client.deliver(
        TOPICS["dhs_status"], {"mission": {"id": 7}, "database": str(config.DB_PATH)}
    )
    deadline = time.monotonic() + 3.0
    frames: list[dict] = []
    while time.monotonic() < deadline and len(frames) < 3:
        frames = [
            p
            for p in client.payloads(TOPICS["payload_photo"])
            if p.get("kind") == "mission_frame"
        ]
        time.sleep(0.005)
    service.stop()
    thread.join(timeout=5)

    assert len(frames) >= 3
    for frame in frames:
        assert "photo_base64" not in frame
        # Filed under the mission, on the card, which is where a gallery reads
        # them from — and the reason the pixels are not on the bus.
        assert (tmp_path / "photos" / "7" / frame["file"]).exists()
    assert [frame["sequence"] for frame in frames[:3]] == [1, 2, 3]
    # The frame thread does not outlive the service, and the camera went back.
    assert not any(t.name == "mission-photos" for t in threading.enumerate())
    assert FakePicamera2.instances[0].closed is True
