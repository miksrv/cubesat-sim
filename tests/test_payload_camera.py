from src.payload.camera import PayloadCamera


class FakeRequest:
    def __init__(self, fail=False):
        self.released = False
        self.fail = fail

    def save(self, _name, path):
        if self.fail:
            raise IOError("sensor read error")
        with open(path, "wb") as f:
            f.write(b"fake-jpeg-bytes")

    def release(self):
        self.released = True


class FakePicamera2:
    fail_capture = False
    instances = []

    def __init__(self):
        self.started = False
        self.stopped = False
        self.closed = False
        FakePicamera2.instances.append(self)

    def create_still_configuration(self, **kwargs):
        return kwargs

    def configure(self, _config):
        pass

    def start(self):
        self.started = True

    def capture_request(self):
        return FakeRequest(fail=FakePicamera2.fail_capture)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def make_camera(monkeypatch, tmp_path):
    monkeypatch.setattr("src.payload.camera.PHOTOS_DIR", tmp_path)
    FakePicamera2.instances = []
    FakePicamera2.fail_capture = False
    monkeypatch.setattr("src.payload.camera.Picamera2", FakePicamera2)
    return PayloadCamera()


class TestInit:
    def test_creates_photo_directory(self, monkeypatch, tmp_path):
        photo_dir = tmp_path / "photos"
        monkeypatch.setattr("src.payload.camera.PHOTOS_DIR", photo_dir)
        monkeypatch.setattr("src.payload.camera.Picamera2", FakePicamera2)
        PayloadCamera()
        assert photo_dir.is_dir()


class TestTakePhoto:
    def test_saves_file_and_returns_path(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)
        path = camera.take_photo()
        assert path is not None
        assert path.startswith(str(tmp_path))
        with open(path, "rb") as f:
            assert f.read() == b"fake-jpeg-bytes"

    def test_stops_and_closes_camera_after_success(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)
        camera.take_photo()
        instance = FakePicamera2.instances[-1]
        assert instance.started is True
        assert instance.stopped is True
        assert instance.closed is True

    def test_returns_none_and_still_cleans_up_on_capture_error(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)
        FakePicamera2.fail_capture = True
        path = camera.take_photo()
        assert path is None
        instance = FakePicamera2.instances[-1]
        assert instance.stopped is True
        assert instance.closed is True

    def test_returns_none_when_camera_init_fails(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)

        def boom():
            raise RuntimeError("no camera detected")

        monkeypatch.setattr(camera, "_init_camera", boom)
        assert camera.take_photo() is None


class TestSendAndCleanupPhoto:
    def test_deletes_file_after_send(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"data")
        camera.send_and_cleanup_photo(str(photo))
        assert not photo.exists()

    def test_missing_file_does_not_raise(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)
        camera.send_and_cleanup_photo(str(tmp_path / "does-not-exist.jpg"))  # must not raise


class TestTimelapse:
    def test_start_timelapse_runs_loop_until_stopped(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)
        calls = []

        def fake_take_photo(save_photo=True):
            calls.append(save_photo)
            camera.stop_event.set()
            return None

        monkeypatch.setattr(camera, "take_photo", fake_take_photo)
        camera.start_timelapse(interval_sec=1)
        camera.timelapse_thread.join(timeout=2)

        assert calls == [True]
        assert camera.timelapse_running is True  # only stop_timelapse() flips this

    def test_start_timelapse_is_a_noop_when_already_running(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)
        camera.timelapse_running = True
        camera.timelapse_thread = None

        camera.start_timelapse(interval_sec=1)

        assert camera.timelapse_thread is None  # no new thread spawned

    def test_stop_timelapse_joins_thread_and_resets_flag(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)

        def fake_take_photo(save_photo=True):
            camera.stop_event.set()
            return None

        monkeypatch.setattr(camera, "take_photo", fake_take_photo)
        camera.start_timelapse(interval_sec=1)
        camera.timelapse_thread.join(timeout=2)

        camera.stop_timelapse()
        assert camera.timelapse_running is False

    def test_stop_timelapse_is_a_noop_when_not_running(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)
        camera.stop_timelapse()  # must not raise
        assert camera.timelapse_running is False


class TestCleanup:
    def test_cleanup_stops_timelapse(self, monkeypatch, tmp_path):
        camera = make_camera(monkeypatch, tmp_path)

        def fake_take_photo(save_photo=True):
            camera.stop_event.set()
            return None

        monkeypatch.setattr(camera, "take_photo", fake_take_photo)
        camera.start_timelapse(interval_sec=1)
        camera.timelapse_thread.join(timeout=2)

        camera.cleanup()
        assert camera.timelapse_running is False
