"""
Global pytest fixtures for the CubeSat Sim test suite.

The project targets Raspberry Pi hardware: several modules import
RPi.GPIO, lgpio, smbus2 and picamera2/libcamera, none of which are
installable (or usable) on a generic CI runner or a contributor's laptop.

Before any ``src.*`` module is imported anywhere in the test session, we
replace those hardware modules in ``sys.modules`` with ``MagicMock``
instances. This must happen at *module import time* (not inside a
fixture function), since conftest.py is imported by pytest before any
test module, which is in turn before any ``src`` import triggered by a
test module.

Individual tests further configure/inspect the relevant mock (e.g.
``bus.read_byte_data.return_value = ...``) or monkeypatch a serial
connection, as needed.
"""

import sys
from unittest.mock import MagicMock

_HARDWARE_MODULES = [
    "RPi",
    "RPi.GPIO",
    "lgpio",
    "smbus2",
    "picamera2",
    "picamera2.encoders",
    "picamera2.outputs",
    "libcamera",
]

for _name in _HARDWARE_MODULES:
    sys.modules[_name] = MagicMock(name=_name)

# Make `import RPi.GPIO as GPIO` and `RPi.GPIO` attribute access consistent.
sys.modules["RPi"].GPIO = sys.modules["RPi.GPIO"]

import src.common as common

# Every subsystem's main.py calls setup_logging(...) at import time, which
# writes to the hardcoded /var/log/cubesat/ directory — not writable without
# root. Replace it with a no-op so importing e.g. src.obc.main is safe in
# any environment; the real function is still exercised directly in
# test_common_logging_setup.py.
common.setup_logging = lambda *_args, **_kwargs: None

import pytest


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Hardware drivers call time.sleep() for real timing budgets (sensor
    settling times, calibration loops, polling). Tests don't need to wait
    for those, so time.sleep is a no-op everywhere by default."""
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)
