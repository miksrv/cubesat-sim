"""Logging setup. Call ``setup_logging()`` before anything else imports and
starts logging, or the first records go nowhere.

Writes to a rotating file under ``LOG_DIR`` plus stderr, so a service is
readable both through ``journalctl`` and in its own log file. If the log
directory is not writable — a development run without the systemd units — the
file handler is skipped rather than crashing the service: losing the log file is
not a reason to lose the satellite.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from cubesat.common import config

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5


def setup_logging(service: str, level: str | None = None) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level or config.LOG_LEVEL)
    root.handlers.clear()

    formatter = logging.Formatter(_FORMAT)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    # transitions logs every callback it runs at INFO: three lines per state
    # change, saying nothing the single "mission state X -> Y" line does not.
    # On a satellite whose log is read after the fact, often over a slow link,
    # that is noise. Logging policy belongs here rather than as a side effect of
    # importing the module that happens to use the library.
    logging.getLogger("transitions").setLevel(logging.WARNING)

    try:
        path = config.LOG_DIR / f"{service}.log"
        handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError as exc:
        root.warning("file logging disabled (%s): %s", config.LOG_DIR, exc)

    return logging.getLogger(service)
