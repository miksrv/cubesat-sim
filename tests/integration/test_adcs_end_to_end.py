"""ADCS from process start to published telemetry, through the real run loop.

The unit tests call ``tick()`` directly. Here ``run()`` drives, and two of these
tests go further and put the *real* drivers behind a fake I2C bus, because the
two things worth checking end to end are exactly the two that no single layer
can show on its own: that a register block measured on the bench arrives on MQTT
as the documented payload, and that reading two devices never holds the bus
across both of them.
"""

from __future__ import annotations

import threading
import time

from cubesat.adcs.service import AdcsService
from cubesat.common import config
from cubesat.common.topics import TOPICS
from cubesat.hal.i2c import I2CError
from cubesat.hal.mock import gnss as mock_gnss
from cubesat.hal.mock.gnss import MockGnss
from cubesat.hal.mock.imu import MockImu
from cubesat.hal.rpi import bno055, tel0157
from cubesat.hal.rpi.bno055 import BNO055
from cubesat.hal.rpi.tel0157 import TEL0157
from tests.unit.hal.test_bno055 import BENCH_REGISTERS
from tests.unit.hal.test_tel0157 import gnss_registers


class FakeSharedBus:
    """One bus, two devices, and a record of what happened inside each lock.

    The real bus lock is an inter-process ``flock``; what matters at this level
    is which addresses a single held transaction touched, so that is what this
    records.
    """

    def __init__(self) -> None:
        self.devices = {
            # The bench registers, except for the calibration: a reset clears
            # it, so a chip that has just been configured really does report
            # zeros there until someone waves the satellite about.
            bno055.ADDRESS: {**BENCH_REGISTERS, bno055.REG_CALIB_STAT: 0x00},
            tel0157.ADDRESS: gnss_registers(),
        }
        self.depth = 0
        self.max_depth = 0
        #: The set of addresses touched inside each completed transaction.
        self.transactions: list[set[int]] = []
        self._current: set[int] = set()

    class _Txn:
        def __init__(self, bus: FakeSharedBus) -> None:
            self.bus = bus

        def __enter__(self):
            self.bus.depth += 1
            self.bus.max_depth = max(self.bus.max_depth, self.bus.depth)

        def __exit__(self, *_):
            self.bus.depth -= 1
            if self.bus.depth == 0:
                self.bus.transactions.append(self.bus._current)
                self.bus._current = set()
            return False

    def transaction(self):
        return self._Txn(self)

    def read_byte(self, address: int, register: int) -> int:
        if address not in self.devices:
            raise I2CError(f"nothing answers at {address:#04x}")
        self._current.add(address)
        return self.devices[address].get(register, 0)

    def read_block(self, address: int, register: int, length: int) -> list[int]:
        return [self.read_byte(address, register + offset) for offset in range(length)]

    def write_byte(self, address: int, register: int, value: int) -> None:
        if address not in self.devices:
            raise I2CError(f"nothing answers at {address:#04x}")
        self._current.add(address)
        self.devices[address][register] = value


def run_briefly(service, seconds=0.2):
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(seconds)
    service.stop()
    thread.join(timeout=5)
    assert not thread.is_alive(), "service did not shut down"


def test_adcs_publishes_attitude_and_position_and_heartbeats(service_factory, monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 0.01)
    # The mock reports no fix for its first minute, which is the honest default
    # but not what this test is about.
    monkeypatch.setattr(mock_gnss, "_ACQUIRE_SEC", 0.0)
    service, client = service_factory(AdcsService, imu=MockImu(), gnss=MockGnss())
    client.connect_ok()
    client.deliver(TOPICS["obc_status"], {"status": "NOMINAL", "profile": "DEMO"})
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 0.02))

    run_briefly(service)

    telemetry = client.payloads(TOPICS["adcs_status"])
    assert telemetry, "no attitude telemetry was published"
    assert telemetry[-1]["quaternion"]["w"] != 0
    assert telemetry[-1]["gnss"]["fix"] is True

    beats = [p for p in client.payloads(TOPICS["heartbeat"]) if p["alive"]]
    assert beats and all(b["service"] == "adcs" for b in beats)
    assert client.payloads(TOPICS["heartbeat"])[-1]["alive"] is False


def test_the_first_status_arrives_without_waiting_a_whole_cadence(service_factory, monkeypatch):
    # OBC's DEPLOY treats the first adcs_status as the evidence that the IMU and
    # the receiver answered, and it gives the subsystem a bounded window. An
    # interval far longer than that window must still produce one message
    # immediately, or a healthy satellite fails its own bring-up.
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL_SEC", 5.0)
    service, client = service_factory(AdcsService, imu=MockImu(), gnss=MockGnss())
    client.connect_ok()
    monkeypatch.setattr(type(service), "interval", property(lambda _self: 30.0))

    run_briefly(service, seconds=0.1)

    assert len(client.payloads(TOPICS["adcs_status"])) == 1


def test_the_bench_registers_arrive_on_mqtt_as_the_documented_payload(service_factory):
    # The real drivers, the numbers both hardware documents recorded, and the
    # payload the README specifies — end to end, with nothing mocked but the bus.
    bus = FakeSharedBus()
    service, client = service_factory(
        AdcsService,
        imu=BNO055(bus=bus, sleep=lambda _seconds: None),
        gnss=TEL0157(bus=bus),
    )
    service.on_start()
    service.tick()

    payload = client.last(TOPICS["adcs_status"])
    # The bench registers hold 27.19 in Bosch-roll and 7.06 in Bosch-pitch; the
    # driver remaps them to the aerospace convention (tilt-verified 2026-08-28).
    assert payload["roll"] == 7.0625
    assert payload["pitch"] == -27.1875
    assert payload["imu_temp"] == 31.0
    assert payload["gnss"] == {
        "lat": 37.676896,
        "lon": -121.876561,
        "alt": 116.59,
        "speed": 0.0,
        "fix": True,
        "satellites": 23,
    }
    # The reset cleared the calibration, so the heading is withheld — which is
    # the honest state of a freshly booted BNO055, and the reason a bench that
    # was never waved about publishes no yaw.
    assert payload["calib_status"] == {"sys": 0, "gyro": 0, "accel": 0, "mag": 0}
    assert payload["yaw"] is None


def test_the_bus_is_never_held_across_both_devices(service_factory):
    # Two devices on one 10 kHz bus, and a GNSS block is the longest transaction
    # in the project. Each driver locks for its own reads and lets go before the
    # other device is touched; one transaction spanning both would stall EPS for
    # the length of both blocks together.
    bus = FakeSharedBus()
    service, _ = service_factory(
        AdcsService,
        imu=BNO055(bus=bus, sleep=lambda _seconds: None),
        gnss=TEL0157(bus=bus),
    )
    service.tick()

    assert bus.transactions, "nothing took the bus"
    assert all(len(addresses) == 1 for addresses in bus.transactions)
    assert {bno055.ADDRESS} in bus.transactions
    assert {tel0157.ADDRESS} in bus.transactions


def test_a_receiver_that_falls_off_the_bus_does_not_silence_the_subsystem(service_factory):
    bus = FakeSharedBus()
    del bus.devices[tel0157.ADDRESS]
    service, client = service_factory(
        AdcsService,
        imu=BNO055(bus=bus, sleep=lambda _seconds: None),
        gnss=TEL0157(bus=bus),
    )
    service.on_start()
    service.tick()

    payload = client.last(TOPICS["adcs_status"])
    assert payload["roll"] == 7.0625
    # A read that failed outright, so there is no last known fix to fall back on.
    assert payload["gnss"] == {
        "lat": None, "lon": None, "alt": None, "speed": None, "fix": False, "satellites": 0
    }
