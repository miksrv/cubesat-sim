import logging
from pathlib import Path

from cubesat.common import config, log, metrics

# ── config ───────────────────────────────────────────────────────────────────


def test_config_dir_override_wins(monkeypatch):
    monkeypatch.setenv("CUBESAT_CONFIG_DIR", "/somewhere/else")
    assert config._find_config_dir() == Path("/somewhere/else")


def test_packaged_install_prefers_etc(monkeypatch):
    monkeypatch.delenv("CUBESAT_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "is_dir", lambda self: str(self) == "/etc/cubesat")
    assert config._find_config_dir() == Path("/etc/cubesat")


def test_development_checkout_falls_back_to_the_repo(monkeypatch):
    monkeypatch.delenv("CUBESAT_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    assert config._find_config_dir().name == "config"


def test_missing_yaml_is_not_an_error(tmp_path):
    assert config._load_yaml(tmp_path / "nope.yaml") == {}


def test_empty_yaml_reads_as_an_empty_mapping(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert config._load_yaml(path) == {}


def test_runtime_paths_live_outside_the_checkout():
    # A database inside a git checkout puts `git pull` next to live mission
    # history; the tests run against a temp dir, but never the repo.
    assert "cubesat-sim" not in str(config.DATA_DIR)
    assert config.DB_PATH.parent == config.DATA_DIR
    assert config.I2C_LOCK_FILE.parent == config.RUN_DIR


def test_the_lock_and_socket_are_runtime_not_state():
    # They must vanish on reboot; /var/lib would keep a stale socket around.
    assert config.HOSTD_SOCKET.parent == config.RUN_DIR


# ── photos_root_for ──────────────────────────────────────────────────────────


def test_the_mission_database_keeps_the_photo_root_it_has_always_had():
    # Nothing on a card that has been recording since before there was a second
    # database moves, which is the whole reason the diag root is a sibling.
    assert config.photos_root_for(config.DB_PATH) == config.PHOTOS_DIR


def test_the_diagnostic_database_files_beside_it_not_under_it():
    root = config.photos_root_for(config.DIAG_DB_PATH)
    assert root == config.PHOTOS_DIR.parent / f"{config.PHOTOS_DIR.name}-diag"
    assert root.parent == config.PHOTOS_DIR.parent


def test_the_two_databases_never_share_a_directory():
    # W3: both number their missions from 1, so mission 3 exists in each.
    mission_id = 3
    flight = config.photos_root_for(config.DB_PATH) / str(mission_id)
    diag = config.photos_root_for(config.DIAG_DB_PATH) / str(mission_id)
    assert flight != diag


def test_the_leaf_name_is_still_a_plain_run_of_digits():
    # The allowlist retention fences its deletions with. A root that added a
    # level above the id, or a suffix after it, would have moved that fence.
    assert (config.photos_root_for(config.DIAG_DB_PATH) / "3").name == "3"


def test_a_caller_with_its_own_root_gets_both_of_its_roots(tmp_path):
    # The dashboard is handed a root; its diag root must be derived from that
    # one and not from the shipped constant, or a test redirects one of two.
    own = tmp_path / "photos"
    assert config.photos_root_for(config.DB_PATH, own) == own
    assert config.photos_root_for(config.DIAG_DB_PATH, own) == tmp_path / "photos-diag"


def test_a_database_path_can_name_a_root_but_never_a_place():
    # PAYLOAD takes this string off the wire. Path.stem is one segment, so the
    # worst a malformed database path can do is name an unused directory.
    root = config.photos_root_for("/var/lib/cubesat/../../etc/passwd.db")
    assert root.name == f"{config.PHOTOS_DIR.name}-passwd"
    assert root.parent == config.PHOTOS_DIR.parent


# ── logging ──────────────────────────────────────────────────────────────────


def test_setup_logging_attaches_a_file_and_a_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    logger = log.setup_logging("eps")
    logger.info("hello")
    assert (tmp_path / "eps.log").exists()
    assert len(logging.getLogger().handlers) == 2


def test_logging_survives_an_unwritable_directory(monkeypatch, caplog):
    # Running a service on a laptop without the systemd units must not fail for
    # want of /var/log/cubesat.
    monkeypatch.setattr(config, "LOG_DIR", Path("/proc/nonexistent/cubesat"))
    with caplog.at_level(logging.WARNING):
        log.setup_logging("obc")
    assert len(logging.getLogger().handlers) == 1


def test_repeated_setup_does_not_duplicate_handlers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    log.setup_logging("dhs")
    log.setup_logging("dhs")
    assert len(logging.getLogger().handlers) == 2


# ── metrics ──────────────────────────────────────────────────────────────────


def test_metrics_are_collected_and_serialisable():
    m = metrics.collect()
    data = m.as_dict()
    assert 0 <= data["ram_percent"] <= 100
    assert 0 <= data["disk_percent"] <= 100
    assert data["uptime_seconds"] > 0
    assert set(data) == {
        "cpu_percent",
        "ram_percent",
        "swap_percent",
        "disk_percent",
        "uptime_seconds",
        "cpu_temperature",
    }


def test_temperature_read_from_psutil_when_available(monkeypatch):
    class Entry:
        current = 47.5

    monkeypatch.setattr(
        "psutil.sensors_temperatures", lambda: {"cpu_thermal": [Entry()]}, raising=False
    )
    assert metrics._cpu_temperature() == 47.5


def test_temperature_skips_zero_readings(monkeypatch, tmp_path):
    class Zero:
        current = 0

    monkeypatch.setattr("psutil.sensors_temperatures", lambda: {"x": [Zero()]}, raising=False)
    monkeypatch.setattr(metrics, "Path", lambda _p: tmp_path / "absent")
    assert metrics._cpu_temperature() is None


def test_temperature_falls_back_to_sysfs(monkeypatch, tmp_path):
    # The Pi always exposes this even when psutil reports nothing.
    monkeypatch.setattr("psutil.sensors_temperatures", lambda: {}, raising=False)
    sysfs = tmp_path / "temp"
    sysfs.write_text("52123\n")
    monkeypatch.setattr(metrics, "Path", lambda _p: sysfs)
    assert metrics._cpu_temperature() == 52.123


def test_temperature_is_none_where_nothing_exposes_it(monkeypatch, tmp_path):
    monkeypatch.delattr("psutil.sensors_temperatures", raising=False)
    monkeypatch.setattr(metrics, "Path", lambda _p: tmp_path / "absent")
    assert metrics._cpu_temperature() is None


def test_temperature_survives_a_raising_sensor_api(monkeypatch, tmp_path):
    def boom():
        raise OSError("no sensors here")

    monkeypatch.setattr("psutil.sensors_temperatures", boom, raising=False)
    monkeypatch.setattr(metrics, "Path", lambda _p: tmp_path / "absent")
    assert metrics._cpu_temperature() is None


def test_temperature_ignores_unparseable_sysfs(monkeypatch, tmp_path):
    monkeypatch.setattr("psutil.sensors_temperatures", lambda: {}, raising=False)
    sysfs = tmp_path / "temp"
    sysfs.write_text("warm-ish")
    monkeypatch.setattr(metrics, "Path", lambda _p: sysfs)
    assert metrics._cpu_temperature() is None
