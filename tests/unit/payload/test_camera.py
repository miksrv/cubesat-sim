import collections
import json
import shutil
import threading
import time

import pytest

from cubesat.common import config
from cubesat.common.states import MissionState
from cubesat.hal.interfaces import Photo
from cubesat.payload.camera import (
    BYTES_PER_MB,
    KIND_PHOTO,
    KIND_TIMELAPSE,
    UNFILED,
    CameraController,
    CaptureContext,
    StorageFull,
    refusal,
)

#: What shutil.disk_usage hands back.
Usage = collections.namedtuple("Usage", "total used free")

#: A GNSS sub-object as it arrives on adcs_status, with the age PAYLOAD stamps
#: on it — the bench fix from docs/hardware-tel0157-gnss.md.
BENCH_POSITION = {
    "lat": 37.676896,
    "lon": -121.876561,
    "alt": 116.59,
    "speed": 0.0,
    "fix": True,
    "satellites": 23,
    "at": 1741863600.0,
}


class FakeCamera:
    """A camera that writes a real file and notices two captures overlapping."""

    def __init__(self) -> None:
        self.captures: list[Photo] = []
        self.overlays: list[str | None] = []
        self.closed = False
        self.fail = False
        self.delay = 0.0
        self._active = 0
        self._lock = threading.Lock()
        self.max_concurrent = 0

    def probe(self) -> bool:
        return not self.fail

    def capture(self, path, *, overlay=None) -> Photo:
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                raise OSError("camera went away")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xd8" + b"\x00" * 100 + b"\xff\xd9")
            photo = Photo(path=path, width=3280, height=2464, taken_at=time.time())
            self.captures.append(photo)
            self.overlays.append(overlay)
            return photo
        finally:
            with self._lock:
                self._active -= 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fast(monkeypatch):
    """Drop the interval floor so a timelapse test takes milliseconds.

    The floor is a guard against a camera running flat out, not a property any
    of these tests are about — and it has a test of its own below.
    """
    monkeypatch.setattr(config, "MIN_TIMELAPSE_INTERVAL_SEC", 0.001)


@pytest.fixture
def free_space(monkeypatch):
    """Pretend the card has this many megabytes left.

    Patched at ``shutil`` rather than injected into the controller: the point of
    the check is that it asks the real filesystem at the moment of writing, and
    a seam built for the test would let that quietly stop being true.
    """

    def set_to(free_mb):
        monkeypatch.setattr(
            shutil, "disk_usage", lambda _path: Usage(total=0, used=0, free=free_mb * BYTES_PER_MB)
        )

    return set_to


@pytest.fixture
def controller(tmp_path):
    device = FakeCamera()
    return CameraController(device, photos_dir=tmp_path / "photos"), device


def nominal(**kwargs) -> CaptureContext:
    return CaptureContext(state=MissionState.NOMINAL, **kwargs)


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ── the gate ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", [MissionState.NOMINAL, MissionState.SCIENCE])
def test_capture_is_permitted_in_the_two_states_that_have_power_for_it(state):
    assert refusal(state) is None


@pytest.mark.parametrize(
    "state",
    [
        MissionState.BOOT,
        MissionState.STANDBY,
        MissionState.DEPLOY,
        MissionState.LOW_POWER,
        MissionState.SAFE,
        MissionState.CRITICAL,
    ],
)
def test_capture_is_refused_everywhere_else(state):
    # LOW_POWER exists to stop discretionary work, and the camera is the most
    # expensive thing PAYLOAD can do.
    assert refusal(state) is not None


def test_the_refusal_names_the_state_that_refused():
    # This wording reaches the ground in the error payload, and an operator
    # reading it should not have to go and ask what the state was.
    assert refusal(MissionState.LOW_POWER) == (
        "Photo capture not allowed: mission state is 'LOW_POWER'"
    )


def test_an_unknown_state_refuses_rather_than_assumes():
    # Before the first obc/status the state might be SAFE, and a camera is not
    # what SAFE wants.
    assert refusal(None) == "Photo capture not allowed: the mission state is not known yet"


# ── filing ──────────────────────────────────────────────────────────────────


def test_photos_are_filed_under_their_mission(controller, tmp_path):
    control, _ = controller
    capture = control.capture(nominal(mission_id="42"))
    assert capture.photo.path.parent == tmp_path / "photos" / "42"
    assert capture.photo.path.exists()


def test_a_photo_with_no_mission_is_filed_rather_than_refused(controller, tmp_path, caplog):
    # DHS owns missions and may not be running. Inventing an id would put frames
    # into a mission that never existed; refusing would lose a capture over a
    # bookkeeping detail.
    control, _ = controller
    with caplog.at_level("WARNING"):
        capture = control.capture(nominal())
    assert capture.photo.path.parent == tmp_path / "photos" / UNFILED
    assert capture.mission_id is None
    assert UNFILED in caplog.text


def test_the_unfiled_warning_is_said_once_and_then_re_armed(controller, caplog):
    # A timelapse of five hundred frames must not be five hundred warnings, but
    # a later gap in DHS' reporting still has to be said out loud.
    control, _ = controller
    with caplog.at_level("WARNING"):
        control.capture(nominal())
        control.capture(nominal())
        assert caplog.text.count("no mission is open") == 1
        control.capture(nominal(mission_id="42"))
        control.capture(nominal())
    assert caplog.text.count("no mission is open") == 2


def test_asking_where_a_mission_files_creates_nothing(controller, tmp_path):
    # payload_status reports this path on every publish. A satellite that never
    # takes a photo should not grow an unfiled/ directory because of it.
    control, _ = controller
    assert control.path_for(None) == tmp_path / "photos" / UNFILED
    assert not (tmp_path / "photos").exists()


def test_a_single_photo_and_a_timelapse_frame_are_named_apart(controller):
    control, _ = controller
    photo = control.capture(nominal(mission_id="42"))
    frame = control.capture(nominal(mission_id="42"), kind=KIND_TIMELAPSE, sequence=7)
    assert (photo.kind, frame.kind) == (KIND_PHOTO, KIND_TIMELAPSE)
    assert photo.photo.path.name.startswith("photo_")
    assert frame.photo.path.name.startswith("timelapse_")
    # The sequence is in the name so frames a second apart cannot collide, and
    # so a directory listing shows the order they were taken in.
    assert frame.photo.path.name.endswith("_0007.jpg")


def test_a_capture_reports_the_size_of_the_file_it_wrote(controller):
    control, _ = controller
    assert control.capture(nominal()).size_bytes == 104


# ── the overlay sidecar ─────────────────────────────────────────────────────


def test_the_overlay_is_a_sidecar_file_and_not_ink_on_the_photo(controller):
    # No imaging library is added for this, and a science image should not be
    # defaced with text that cannot be removed.
    control, device = controller
    capture = control.capture(
        nominal(mission_id="42", position=BENCH_POSITION, overlay=True)
    )
    sidecar = capture.photo.path.with_suffix(".json")
    assert sidecar.exists()
    assert json.loads(sidecar.read_text()) == capture.sidecar
    assert capture.photo.path.read_bytes()[:2] == b"\xff\xd8"  # the JPEG is untouched


def test_the_sidecar_records_the_moment_the_photo_belongs_to(controller):
    control, _ = controller
    capture = control.capture(
        nominal(mission_id="42", position=BENCH_POSITION, overlay=True)
    )
    sidecar = capture.sidecar
    assert sidecar["mission_id"] == "42"
    assert sidecar["mission_state"] == "NOMINAL"
    assert sidecar["position"]["lat"] == 37.676896
    assert sidecar["captured_at"].endswith("Z")
    assert (sidecar["width"], sidecar["height"]) == (3280, 2464)


def test_the_recorded_position_carries_its_own_age(controller):
    # A last known fix can be minutes old. A coordinate with no age attached is
    # exactly the plausible wrong number this project keeps trying to avoid.
    control, _ = controller
    capture = control.capture(nominal(position=BENCH_POSITION, overlay=True))
    assert capture.sidecar["position"]["at"] == 1741863600.0


def test_a_photo_taken_with_no_fix_still_gets_its_sidecar(controller):
    control, _ = controller
    capture = control.capture(nominal(mission_id="42", overlay=True))
    assert capture.sidecar["position"] is None


def test_nothing_is_written_beside_a_photo_that_asked_for_no_overlay(controller):
    control, _ = controller
    capture = control.capture(nominal(mission_id="42"))
    assert capture.sidecar is None
    assert not capture.photo.path.with_suffix(".json").exists()


def test_the_overlay_text_still_reaches_the_driver(controller):
    # The driver files it rather than drawing it, but it is passed down so that
    # a driver which can draw needs no new plumbing here.
    control, device = controller
    control.capture(nominal(mission_id="42", overlay=True))
    assert "NOMINAL" in device.overlays[-1]
    assert "mission 42" in device.overlays[-1]


def test_an_overlay_with_no_mission_says_so_by_omission(controller):
    control, device = controller
    control.capture(nominal(overlay=True))
    assert "mission" not in device.overlays[-1]


def test_a_photo_with_no_overlay_hands_the_driver_nothing(controller):
    control, device = controller
    control.capture(nominal())
    assert device.overlays[-1] is None


def test_no_sidecar_survives_a_capture_that_failed(controller, tmp_path):
    # Written after the capture, so a sidecar never describes a photo that does
    # not exist.
    control, device = controller
    device.fail = True
    with pytest.raises(OSError):
        control.capture(nominal(mission_id="42", overlay=True))
    assert list((tmp_path / "photos" / "42").glob("*.json")) == []


# ── the camera as an exclusive resource ─────────────────────────────────────


def test_two_captures_never_run_at_the_same_time(controller):
    # One sensor, two callers: a ground command on the MQTT thread and the
    # timelapse thread. Overlapping them is not a slow photo, it is an error.
    control, device = controller
    device.delay = 0.05
    threads = [
        threading.Thread(target=lambda: control.capture(nominal(mission_id="42")))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert device.max_concurrent == 1
    assert len(device.captures) == 4


def test_closing_gives_the_camera_back(controller):
    control, device = controller
    control.close()
    assert device.closed is True


# ── the timelapse ───────────────────────────────────────────────────────────


def start(control, frames, finished, interval=0.01, context=None):
    return control.start_timelapse(
        interval,
        context=context or (lambda: nominal(mission_id="42")),
        on_frame=frames.append,
        on_finish=finished.append,
    )


def test_a_timelapse_captures_frames_in_sequence(fast, controller):
    control, _ = controller
    frames, finished = [], []
    start(control, frames, finished)
    assert wait_until(lambda: len(frames) >= 3)
    control.stop_timelapse()
    assert [frame.sequence for frame in frames[:3]] == [1, 2, 3]
    assert all(frame.kind == KIND_TIMELAPSE for frame in frames)


def test_the_first_frame_does_not_wait_out_an_interval(fast, controller):
    # An operator starting a timelapse should see it working, not wonder whether
    # the command arrived.
    control, _ = controller
    frames, finished = [], []
    start(control, frames, finished, interval=30.0)
    assert wait_until(lambda: len(frames) == 1)
    control.stop_timelapse()


def test_every_frame_asks_again_where_the_satellite_is(fast, controller):
    # A timelapse spans hours: the mission, the state and the position all move
    # under it, and each frame should record the truth of its own moment.
    control, _ = controller
    missions = iter(["1", "2", "3", "4", "5", "6", "7", "8"])
    frames, finished = [], []
    start(control, frames, finished, context=lambda: nominal(mission_id=next(missions)))
    assert wait_until(lambda: len(frames) >= 3)
    control.stop_timelapse()
    assert [frame.mission_id for frame in frames[:3]] == ["1", "2", "3"]


def test_a_timelapse_stops_itself_when_the_state_no_longer_permits_it(fast, controller):
    # The gate is re-asked every frame rather than only at the start, because a
    # timelapse outlives the state it was started in.
    control, _ = controller
    state = MissionState.NOMINAL

    def context():
        return CaptureContext(mission_id="42", state=state)

    frames, finished = [], []
    start(control, frames, finished, context=context)
    assert wait_until(lambda: len(frames) >= 1)
    state = MissionState.LOW_POWER
    assert wait_until(lambda: finished)
    assert "LOW_POWER" in finished[0]
    assert control.timelapse.active is False


def test_one_failed_frame_does_not_end_a_long_run(fast, controller):
    control, device = controller
    frames, finished = [], []
    device.fail = True
    start(control, frames, finished)
    assert wait_until(lambda: device.max_concurrent >= 1)
    device.fail = False
    assert wait_until(lambda: len(frames) >= 1)
    assert finished == []
    control.stop_timelapse()


def test_the_frame_count_is_what_is_on_disk_not_what_was_attempted(fast, controller):
    # A number a dashboard shows beside a gallery has to match the gallery. A
    # failed frame also leaves its sequence number free, so the next attempt
    # reuses the slot rather than leaving a hole in the filenames.
    control, device = controller
    frames, finished = [], []
    device.fail = True
    start(control, frames, finished)
    assert wait_until(lambda: finished)
    assert control.timelapse.frames == 0


def test_a_camera_that_has_gone_away_ends_the_run_rather_than_filling_the_log(fast, controller):
    control, device = controller
    device.fail = True
    frames, finished = [], []
    start(control, frames, finished)
    assert wait_until(lambda: finished)
    assert "in a row" in finished[0]
    assert device.max_concurrent >= 1  # it did try, MAX_CONSECUTIVE_FRAME_FAILURES times


def test_a_frame_that_could_not_be_published_stays_on_disk_and_the_run_goes_on(fast, controller):
    # The photos are the deliverable; the broker is how someone hears about them.
    control, _ = controller
    published: list[object] = []

    def explode(capture):
        published.append(capture)
        raise RuntimeError("broker went away")

    finished: list[str] = []
    control.start_timelapse(
        0.01,
        context=lambda: nominal(mission_id="42"),
        on_frame=explode,
        on_finish=finished.append,
    )
    assert wait_until(lambda: len(published) >= 2)
    control.stop_timelapse()
    assert all(capture.photo.path.exists() for capture in published)


def test_a_finish_handler_that_raises_does_not_take_the_thread_down_untidily(fast, controller):
    control, _ = controller

    def explode(_reason):
        raise RuntimeError("nowhere to publish")

    control.start_timelapse(
        0.01,
        context=lambda: nominal(mission_id="42"),
        on_frame=lambda _capture: None,
        on_finish=explode,
    )
    assert wait_until(lambda: control.timelapse.frames >= 1)
    assert control.stop_timelapse() is True


def test_stopping_is_permitted_from_any_state(fast, controller):
    # Stopping something is never the dangerous direction: a gate that refused
    # to stop a timelapse in SAFE would keep the camera running in the one state
    # that most wanted it off.
    control, _ = controller
    frames, finished = [], []
    start(control, frames, finished, interval=30.0)
    assert wait_until(lambda: len(frames) == 1)
    # No state is consulted here at all — that is the point.
    assert control.stop_timelapse() is True
    assert control.timelapse.active is False


def test_stopping_a_timelapse_that_is_not_running_is_not_an_error(controller):
    control, _ = controller
    assert control.stop_timelapse() is False


def test_stopping_waits_for_the_frame_thread_to_actually_stop(fast, controller):
    control, device = controller
    device.delay = 0.05
    frames, finished = [], []
    start(control, frames, finished)
    assert wait_until(lambda: len(frames) >= 1)
    control.stop_timelapse()
    assert not any(thread.name == "timelapse" for thread in threading.enumerate())


def test_an_interval_below_the_floor_is_raised_rather_than_refused(controller, caplog):
    # A full-resolution capture takes the better part of a second, so anything
    # below the floor is not a faster timelapse, it is a camera running flat out
    # with a queue behind it. Losing the exact interval beats refusing the run.
    control, _ = controller
    frames, finished = [], []
    with caplog.at_level("WARNING"):
        interval = start(control, frames, finished, interval=0.001)
    control.stop_timelapse()
    assert interval == config.MIN_TIMELAPSE_INTERVAL_SEC
    assert "floor" in caplog.text


def test_the_reported_timelapse_state_is_what_payload_status_says(controller):
    control, _ = controller
    # Never started: no reason, because nobody has stopped anything.
    assert control.timelapse.as_dict() == {
        "active": False, "interval_sec": None, "frames": 0, "reason": None
    }
    frames, finished = [], []
    start(control, frames, finished, interval=1.0)
    assert wait_until(lambda: control.timelapse.frames >= 1)
    reported = control.timelapse.as_dict()
    assert reported["active"] is True
    assert reported["interval_sec"] == 1.0
    assert reported["frames"] >= 1
    assert reported["reason"] is None  # it has not ended, so there is no why yet
    control.stop_timelapse()
    assert control.timelapse.as_dict()["reason"] == "stopped by command"


def test_closing_stops_the_timelapse_before_giving_the_camera_back(fast, controller):
    # A frame thread that outlived the camera it captures with would be an
    # exception a second after a clean shutdown.
    control, device = controller
    frames, finished = [], []
    start(control, frames, finished)
    assert wait_until(lambda: len(frames) >= 1)
    control.close()
    assert device.closed is True
    assert control.timelapse.active is False
    assert not any(thread.name == "timelapse" for thread in threading.enumerate())


def test_the_photos_directory_defaults_to_the_configured_one(tmp_path):
    from cubesat.common import config

    control = CameraController(FakeCamera())
    assert control.path_for("42") == config.PHOTOS_DIR / "42"


def test_stopping_a_run_that_already_ended_itself_reports_nothing_to_stop(fast, controller):
    # To a caller deciding whether anything changed, "there was never one" and
    # "it ended itself a moment ago" are the same answer — and PAYLOAD uses it
    # to decide whether the retained status needs republishing.
    control, device = controller
    device.fail = True
    frames, finished = [], []
    start(control, frames, finished)
    assert wait_until(lambda: finished)
    assert control.stop_timelapse() is False


# ── the free-space floor ────────────────────────────────────────────────────


def test_a_capture_is_permitted_exactly_at_the_floor(controller, free_space):
    # "Keep 512 MB free" means the reserve is intact until something eats into
    # it. This is the boundary, and it is the least convenient one to get wrong:
    # an off-by-one here only shows up on a card that is already nearly full.
    control, _ = controller
    free_space(config.PHOTOS_MIN_FREE_MB)
    assert control.storage().blocked is False
    assert control.capture(nominal(mission_id="42")).photo.path.exists()


def test_a_capture_is_refused_one_megabyte_below_the_floor(controller, free_space):
    control, _ = controller
    free_space(config.PHOTOS_MIN_FREE_MB - 1)
    assert control.storage().blocked is True
    with pytest.raises(StorageFull):
        control.capture(nominal(mission_id="42"))


def test_a_capture_is_permitted_one_megabyte_above_the_floor(controller, free_space):
    control, device = controller
    free_space(config.PHOTOS_MIN_FREE_MB + 1)
    control.capture(nominal(mission_id="42"))
    assert len(device.captures) == 1


def test_the_refusal_says_how_much_room_is_left_and_what_the_floor_is(controller, free_space):
    # It reaches the ground in the error payload. "Not allowed" without a number
    # sends someone to look at the wrong thing.
    control, _ = controller
    free_space(12)
    with pytest.raises(StorageFull) as refused:
        control.capture(nominal(mission_id="42"))
    assert "12 MB free" in str(refused.value)
    assert f"{config.PHOTOS_MIN_FREE_MB} MB floor" in str(refused.value)


def test_a_refused_capture_leaves_nothing_behind(controller, free_space, tmp_path):
    # Checked before anything is created, so a full card does not even gain an
    # empty mission directory.
    control, device = controller
    free_space(0)
    with pytest.raises(StorageFull):
        control.capture(nominal(mission_id="42"))
    assert not (tmp_path / "photos").exists()
    assert device.captures == []


def test_a_timelapse_stops_itself_rather_than_refusing_every_frame(fast, controller, free_space):
    # The same argument as the consecutive-failure ceiling: a loop that fails
    # forever is a log-flooding machine, and the card will not empty itself —
    # PAYLOAD deletes nothing.
    control, _ = controller
    free_space(config.PHOTOS_MIN_FREE_MB + 10)
    frames, finished = [], []
    start(control, frames, finished)
    assert wait_until(lambda: len(frames) >= 1)
    free_space(3)
    assert wait_until(lambda: finished)
    assert "MB floor" in finished[0]
    assert control.timelapse.active is False
    # And it is not counted as a camera fault: the camera is fine, the card is
    # not, which is why StorageFull is its own exception.
    assert "in a row" not in finished[0]


def test_a_stopped_timelapse_is_distinguishable_from_one_never_started(
    fast, controller, free_space
):
    # A satellite that has stopped taking photos should say why on the topic
    # that describes the payload, not only in a log nobody is reading.
    control, _ = controller
    assert control.timelapse.reason is None  # never started
    free_space(config.PHOTOS_MIN_FREE_MB + 10)
    frames, finished = [], []
    start(control, frames, finished)
    assert wait_until(lambda: len(frames) >= 1)
    free_space(3)
    assert wait_until(lambda: finished)
    assert "MB floor" in control.timelapse.reason


def test_free_space_is_read_from_the_filesystem_the_photos_will_land_on(controller, tmp_path):
    # The root may not exist yet — asking where a mission files creates nothing
    # — and the free space of a directory about to be created is the free space
    # of the filesystem it will be created on.
    control, _ = controller
    assert not (tmp_path / "photos").exists()
    assert control.storage().free_mb > 0


def test_an_unreadable_filesystem_does_not_cost_us_the_photo(controller, monkeypatch, caplog):
    # A check that cannot answer must not be the reason a picture is lost. The
    # card filling up afterwards is the smaller problem.
    def explode(_path):
        raise OSError("statvfs failed")

    control, device = controller
    monkeypatch.setattr(shutil, "disk_usage", explode)
    with caplog.at_level("WARNING"):
        control.capture(nominal(mission_id="42"))
    assert len(device.captures) == 1
    assert "unreadable" in caplog.text


def test_the_reported_storage_is_what_payload_status_says(controller, free_space):
    control, _ = controller
    free_space(1234.56)
    assert control.storage().as_dict() == {
        "free_mb": 1234.6,
        "min_free_mb": float(config.PHOTOS_MIN_FREE_MB),
        "blocked": False,
    }
