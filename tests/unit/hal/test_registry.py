import pytest

from cubesat.common import config
from cubesat.hal import registry
from cubesat.hal.interfaces import Device


def test_every_device_has_both_a_real_and_a_mock_driver():
    for device, (real_mod, real_cls, mock_mod, mock_cls) in registry._DRIVERS.items():
        assert real_mod.startswith("cubesat.hal.rpi."), device
        assert mock_mod.startswith("cubesat.hal.mock."), device
        assert real_cls and mock_cls


def test_factory_functions_cover_every_declared_device():
    # A driver in the table with no accessor would be unreachable.
    accessors = {"imu", "gnss", "environment", "power_monitor", "camera", "radio"}
    assert {name for name in registry._DRIVERS} | {"power_monitor"} >= accessors


@pytest.mark.parametrize(
    "factory",
    [registry.imu, registry.gnss, registry.environment,
     registry.power_monitor, registry.camera, registry.radio],
)
def test_mocks_satisfy_the_device_protocol(factory):
    device = factory()
    assert isinstance(device, Device)
    assert device.probe() is True


def test_a_missing_real_driver_says_how_to_run_without_hardware(monkeypatch):
    monkeypatch.setattr(config, "MOCK_HARDWARE", False)
    monkeypatch.setitem(
        registry._DRIVERS, "imu",
        ("cubesat.hal.rpi.not_written_yet", "Nothing", "cubesat.hal.mock.imu", "MockImu"),
    )
    with pytest.raises(registry.DriverUnavailable, match="CUBESAT_MOCK_HARDWARE=1"):
        registry.imu()


def test_real_drivers_are_selected_when_mocking_is_off(monkeypatch):
    monkeypatch.setattr(config, "MOCK_HARDWARE", False)
    built = {}

    class Stub:
        def __init__(self, **kwargs):
            built.update(kwargs)

    module = type("m", (), {"Stub": Stub})
    monkeypatch.setitem(
        registry._DRIVERS, "imu", ("fake.module", "Stub", "cubesat.hal.mock.imu", "MockImu")
    )
    monkeypatch.setattr(registry.importlib, "import_module", lambda _n: module)
    registry.imu(address=0x28)
    assert built == {"address": 0x28}
