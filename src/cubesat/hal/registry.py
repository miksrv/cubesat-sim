"""The HAL factory: hand out real drivers or mocks, decided once.

``CUBESAT_MOCK_HARDWARE=1`` runs the entire stack with no Raspberry Pi and no
sensors. Services never import a driver directly — they ask here — so the choice
lives in one place and a service is identical on the bench and on a laptop.

Imports are lazy and per-device. Importing this module on a laptop must not
reach for ``smbus2`` or ``picamera2``, and a driver that has not been written
yet must not break the five that have.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from cubesat.common import config
from cubesat.hal.interfaces import (
    Camera,
    EnvironmentSensor,
    Gnss,
    Imu,
    PowerMonitor,
    Radio,
)

logger = logging.getLogger(__name__)

#: device -> (real module, real class, mock module, mock class)
_DRIVERS: dict[str, tuple[str, str, str, str]] = {
    "imu": ("cubesat.hal.rpi.bno055", "BNO055", "cubesat.hal.mock.imu", "MockImu"),
    "gnss": ("cubesat.hal.rpi.tel0157", "TEL0157", "cubesat.hal.mock.gnss", "MockGnss"),
    "environment": (
        "cubesat.hal.rpi.sen0501",
        "SEN0501",
        "cubesat.hal.mock.environment",
        "MockEnvironment",
    ),
    "power": (
        "cubesat.hal.rpi.max17048",
        "PowerMonitorX728",
        "cubesat.hal.mock.power",
        "MockPowerMonitor",
    ),
    "camera": ("cubesat.hal.rpi.camera", "PiCamera", "cubesat.hal.mock.camera", "MockCamera"),
    "radio": (
        "cubesat.hal.rpi.meshtastic_radio",
        "MeshtasticRadio",
        "cubesat.hal.mock.radio",
        "MockRadio",
    ),
}


class DriverUnavailable(RuntimeError):
    """The requested driver is not present on this machine."""


def _build(device: str, **kwargs: Any) -> Any:
    real_mod, real_cls, mock_mod, mock_cls = _DRIVERS[device]
    module_name, class_name = (
        (mock_mod, mock_cls) if config.MOCK_HARDWARE else (real_mod, real_cls)
    )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise DriverUnavailable(
            f"{device}: cannot import {module_name} ({exc}). "
            "Set CUBESAT_MOCK_HARDWARE=1 to run without hardware."
        ) from exc
    logger.debug("%s driver: %s.%s", device, module_name, class_name)
    return getattr(module, class_name)(**kwargs)


def imu(**kwargs: Any) -> Imu:
    return _build("imu", **kwargs)


def gnss(**kwargs: Any) -> Gnss:
    return _build("gnss", **kwargs)


def environment(**kwargs: Any) -> EnvironmentSensor:
    return _build("environment", **kwargs)


def power_monitor(**kwargs: Any) -> PowerMonitor:
    return _build("power", **kwargs)


def camera(**kwargs: Any) -> Camera:
    return _build("camera", **kwargs)


def radio(**kwargs: Any) -> Radio:
    return _build("radio", **kwargs)
