"""The Picamera2 driver, against a fake ``picamera2`` in ``sys.modules``.

``picamera2`` and ``libcamera`` only install on a Raspberry Pi, and the driver
imports them lazily for exactly that reason. These tests exploit the same seam:
the modules are planted before the first capture, so nothing here needs a real
camera, a real libcamera stack or a privileged call.
"""

from __future__ import annotations

import sys
import types

import pytest

from cubesat.common import config
from cubesat.hal.interfaces import Camera
from cubesat.hal.rpi import camera as camera_module
from cubesat.hal.rpi.camera import CameraError, PiCamera


class FakeRequest:
    """A capture request. Records that it was released, because leaking one is
    the failure that matters: a full-resolution still is 24 MB of CMA."""

    def __init__(self, camera: FakePicamera2) -> None:
        self._camera = camera
        self.released = False

    def save(self, stream: str, path: str) -> None:
        assert stream == "main", "the still comes from the main stream"
        if self._camera.fail_capture:
            raise RuntimeError("no buffer")
        self._camera.saved.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xd9")  # SOI, EOI: a file, and a JPEG-shaped one

    def release(self) -> None:
        self.released = True


class FakePicamera2:
    """The half of Picamera2 this driver touches, and a log of the calls."""

    instances: list[FakePicamera2] = []

    def __init__(self) -> None:
        FakePicamera2.instances.append(self)
        self.configuration = None
        self.started = False
        self.stopped = False
        self.closed = False
        self.saved: list[str] = []
        self.requests: list[FakeRequest] = []
        self.fail_capture = False

    def create_still_configuration(self, **kwargs):
        return kwargs

    def configure(self, configuration) -> None:
        self.configuration = configuration

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def capture_request(self) -> FakeRequest:
        request = FakeRequest(self)
        self.requests.append(request)
        return request


class FakeTransform:
    def __init__(self, hflip: int = 0, vflip: int = 0) -> None:
        self.hflip = hflip
        self.vflip = vflip


@pytest.fixture
def picamera2(monkeypatch):
    """Plant a fake ``picamera2`` and ``libcamera`` for the lazy import."""
    FakePicamera2.instances = []
    picamera2_module = types.ModuleType("picamera2")
    picamera2_module.Picamera2 = FakePicamera2
    libcamera_module = types.ModuleType("libcamera")
    libcamera_module.Transform = FakeTransform
    monkeypatch.setitem(sys.modules, "picamera2", picamera2_module)
    monkeypatch.setitem(sys.modules, "libcamera", libcamera_module)
    return FakePicamera2


@pytest.fixture
def missing_picamera2(monkeypatch):
    """A machine that is not a Raspberry Pi: ``from picamera2 import ...`` on a
    None entry in sys.modules raises ImportError, which is what happens for real
    on anything that is not a Pi."""
    monkeypatch.setitem(sys.modules, "picamera2", None)
    monkeypatch.setitem(sys.modules, "libcamera", None)


def test_it_satisfies_the_camera_protocol():
    assert isinstance(PiCamera(), Camera)


def test_constructing_the_driver_touches_no_hardware(missing_picamera2):
    # The registry builds every driver a service asks for before anything has
    # decided to take a photo, and a profile may run PAYLOAD for its sensor
    # alone. Construction must therefore cost nothing and be unable to fail.
    assert PiCamera() is not None


def test_a_capture_writes_the_file_and_reports_it(picamera2, tmp_path):
    device = PiCamera()
    path = tmp_path / "photo.jpg"
    photo = device.capture(path)
    assert path.exists()
    assert photo.path == path
    assert (photo.width, photo.height) == config.PHOTO_RESOLUTION
    assert photo.taken_at > 0


def test_the_capture_request_is_always_released(picamera2, tmp_path):
    # A full-resolution still is 24 MB, and a timelapse that leaks one per frame
    # exhausts CMA long before it exhausts the SD card.
    device = PiCamera()
    device.capture(tmp_path / "photo.jpg")
    assert all(request.released for request in picamera2.instances[0].requests)


def test_the_request_is_released_even_when_the_save_fails(picamera2, tmp_path):
    device = PiCamera()
    device.probe()  # opens the camera, so there is a fake instance to break
    picamera2.instances[0].fail_capture = True
    with pytest.raises(RuntimeError):
        device.capture(tmp_path / "photo.jpg")
    assert picamera2.instances[0].requests[-1].released is True


def test_the_missing_directory_is_created(picamera2, tmp_path):
    # Photos are filed under <data>/photos/<mission_id>/, and a mission id does
    # not exist until DHS opens a mission — no unit file can create it ahead.
    device = PiCamera()
    device.capture(tmp_path / "42" / "photo.jpg")
    assert (tmp_path / "42" / "photo.jpg").exists()


def test_the_image_is_flipped_because_of_how_the_module_is_mounted(picamera2, tmp_path):
    # Not a preference: the camera sits upside down in the printed frame, so an
    # un-flipped capture is an upside-down photo.
    device = PiCamera()
    device.probe()
    transform = picamera2.instances[0].configuration["transform"]
    assert (transform.hflip, transform.vflip) == (1, 1)


def test_it_configures_a_full_resolution_still_beside_a_preview_stream(picamera2):
    device = PiCamera()
    device.probe()
    configuration = picamera2.instances[0].configuration
    assert configuration["main"] == {"size": config.PHOTO_RESOLUTION}
    assert configuration["lores"] == {
        "size": camera_module.PREVIEW_SIZE,
        "format": camera_module.PREVIEW_FORMAT,
    }


def test_the_still_size_comes_from_the_configuration(monkeypatch, picamera2, tmp_path):
    # A long timelapse is what decides this number — the sensor's full 3280x2464
    # is 24 MB a frame — so it has to be lowerable without a deploy.
    monkeypatch.setattr(config, "PHOTO_RESOLUTION", (1640, 1232))
    device = PiCamera()
    photo = device.capture(tmp_path / "photo.jpg")
    assert picamera2.instances[0].configuration["main"] == {"size": (1640, 1232)}
    assert (photo.width, photo.height) == (1640, 1232)


def test_the_configured_size_is_below_the_sensor_maximum():
    # Not a rule the driver enforces — asking for more than the sensor has is
    # Picamera2's business — but a default above it would be a broken default.
    assert config.PHOTO_RESOLUTION <= camera_module.SENSOR_MAX_SIZE


def test_the_camera_is_opened_once_however_many_photos_are_taken(picamera2, tmp_path):
    # One owner, one handle. A second Picamera2 in the same process is an error
    # rather than a second camera, and opening costs the better part of a second.
    device = PiCamera()
    device.probe()
    device.capture(tmp_path / "a.jpg")
    device.capture(tmp_path / "b.jpg")
    assert len(picamera2.instances) == 1


def test_closing_gives_the_sensor_back(picamera2, tmp_path):
    device = PiCamera()
    device.capture(tmp_path / "a.jpg")
    device.close()
    assert picamera2.instances[0].stopped is True
    assert picamera2.instances[0].closed is True


def test_closing_twice_is_not_an_error(picamera2, tmp_path):
    # Shutdown paths overlap: on_stop() closes the controller, which closes the
    # camera, and a SIGTERM during a capture can arrive at either.
    device = PiCamera()
    device.capture(tmp_path / "a.jpg")
    device.close()
    device.close()
    assert picamera2.instances[0].closed is True


def test_closing_a_camera_that_never_opened_is_not_an_error(missing_picamera2):
    PiCamera().close()


def test_a_reopened_camera_is_a_new_handle(picamera2, tmp_path):
    device = PiCamera()
    device.capture(tmp_path / "a.jpg")
    device.close()
    device.capture(tmp_path / "b.jpg")
    assert len(picamera2.instances) == 2


def test_probe_answers_yes_when_the_camera_opens(picamera2):
    assert PiCamera().probe() is True


def test_probe_answers_no_on_a_machine_with_no_picamera2(missing_picamera2, caplog):
    # The DEPLOY self-test needs a yes or a no, and "picamera2 is not installed"
    # is a no. The message says how to run without hardware, like hal/i2c.py.
    with caplog.at_level("ERROR"):
        assert PiCamera().probe() is False
    assert "CUBESAT_MOCK_HARDWARE=1" in caplog.text


def test_a_capture_on_a_machine_with_no_picamera2_raises_a_camera_error(
    missing_picamera2, tmp_path
):
    with pytest.raises(CameraError):
        PiCamera().capture(tmp_path / "photo.jpg")


def test_probe_answers_no_when_the_camera_refuses_to_start(monkeypatch, picamera2, caplog):
    # A ribbon cable that is not seated, or a camera already claimed by another
    # process. Picamera2 raises its own types for those and none is documented
    # here, so every one of them has to come back as a plain no.
    def refuse(self):
        raise RuntimeError("no cameras available")

    monkeypatch.setattr(FakePicamera2, "start", refuse)
    with caplog.at_level("ERROR"):
        assert PiCamera().probe() is False
    assert "did not open" in caplog.text


def test_the_overlay_text_is_not_drawn_onto_the_pixels(picamera2, tmp_path, caplog):
    # There is no imaging library in this project, and a science image should
    # not be defaced with text that cannot be removed. PAYLOAD files the same
    # information beside the photo instead; the text still reaches the driver so
    # that a driver which can draw needs no new plumbing.
    device = PiCamera()
    path = tmp_path / "photo.jpg"
    with caplog.at_level("DEBUG"):
        device.capture(path, overlay="2026-08-24T12:00:00Z NOMINAL mission 42")
    assert path.read_bytes() == b"\xff\xd8\xff\xd9"  # untouched by the caption
    assert "not drawn on it" in caplog.text
