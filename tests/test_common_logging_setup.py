import logging

from src.common import logging_setup


def _redirect_log_dir(monkeypatch, tmp_path):
    """setup_logging() hardcodes /var/log/cubesat/ as the log directory,
    which isn't writable without root. Redirect the Path(...) call inside
    the module to a temp directory so the real function can be exercised
    end-to-end."""
    monkeypatch.setattr(logging_setup, "Path", lambda *_args: tmp_path)


class TestSetupLogging:
    def test_creates_log_file(self, monkeypatch, tmp_path):
        _redirect_log_dir(monkeypatch, tmp_path)
        logging_setup.setup_logging(log_file="unit-test.log")
        logging.getLogger(__name__).info("hello")
        assert (tmp_path / "unit-test.log").exists()

    def test_console_handler_added_when_requested(self, monkeypatch, tmp_path):
        _redirect_log_dir(monkeypatch, tmp_path)
        logging_setup.setup_logging(log_file="console.log", console=True)
        root = logging.getLogger()
        handler_classes = {type(h).__name__ for h in root.handlers}
        assert "StreamHandler" in handler_classes
        assert "RotatingFileHandler" in handler_classes

    def test_console_handler_omitted_when_disabled(self, monkeypatch, tmp_path):
        _redirect_log_dir(monkeypatch, tmp_path)
        logging_setup.setup_logging(log_file="no-console.log", console=False)
        root = logging.getLogger()
        handler_classes = [type(h).__name__ for h in root.handlers]
        assert handler_classes.count("StreamHandler") == 0
        assert "RotatingFileHandler" in handler_classes

    def test_log_level_applied(self, monkeypatch, tmp_path):
        _redirect_log_dir(monkeypatch, tmp_path)
        logging_setup.setup_logging(log_file="level.log", log_level="WARNING")
        assert logging.getLogger().level == logging.WARNING
