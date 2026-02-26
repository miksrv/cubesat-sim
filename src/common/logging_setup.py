import logging
import logging.config
from pathlib import Path


def setup_logging(
        log_level: str = "INFO",
        log_file: str = "cubesat.log",
        console: bool = True
):
    """
    Единая настройка логирования для всех сервисов.
    """
    log_dir = Path('/var/log/cubesat/')
    log_dir.mkdir(parents=True, exist_ok=True)   # создаём папку, если нет
    log_path = log_dir / log_file

    handlers = {}
    if console:
        handlers["console"] = {
            "class": "logging.StreamHandler",
            "level": log_level,
            "formatter": "standard",
            "stream": "ext://sys.stdout",
        }

    handlers["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "level": log_level,
        "formatter": "standard",
        "filename": str(log_path),
        "maxBytes": 10*1024*1024,  # 10 MB
        "backupCount": 5,
        "encoding": "utf-8",
    }

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": handlers,
        "loggers": {
            "": {
                "handlers": list(handlers.keys()),
                "level": log_level,
                "propagate": True,
            },
        },
    })

    logging.info(f"Логирование настроено: уровень={log_level}, файл={log_path}")