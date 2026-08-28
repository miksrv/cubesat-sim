import threading

import pytest

from cubesat.hal import i2c


class FakeSMBus:
    """A fake ``smbus2.SMBus``: records traffic, can be told to fail."""

    def __init__(self, bus=1):
        self.bus = bus
        self.writes = []
        self.reads = []
        self.registers = {}
        self.fail = False
        self.closed = False

    def _maybe_fail(self):
        if self.fail:
            raise OSError(121, "Remote I/O error")

    def read_byte_data(self, address, register):
        self._maybe_fail()
        self.reads.append((address, register))
        return self.registers.get((address, register), 0x00)

    def read_i2c_block_data(self, address, register, length):
        self._maybe_fail()
        self.reads.append((address, register, length))
        return [self.registers.get((address, register + i), 0) for i in range(length)]

    def write_byte_data(self, address, register, value):
        self._maybe_fail()
        self.writes.append((address, register, value))
        self.registers[(address, register)] = value

    def close(self):
        self.closed = True


@pytest.fixture
def bus(tmp_path, monkeypatch):
    fake_module = type("smbus2", (), {"SMBus": FakeSMBus})
    monkeypatch.setitem(__import__("sys").modules, "smbus2", fake_module)
    return i2c.I2CBus(bus=1, lock_path=tmp_path / "i2c.lock")


def test_read_and_write_round_trip(bus):
    bus.write_byte(0x28, 0x3D, 0x0C)
    assert bus.read_byte(0x28, 0x3D) == 0x0C


def test_block_read_returns_the_requested_length(bus):
    bus.write_byte(0x20, 0x10, 0xAB)
    assert bus.read_block(0x20, 0x10, 4) == [0xAB, 0, 0, 0]


def test_bus_is_opened_lazily(bus):
    assert bus._smbus is None
    bus.read_byte(0x36, 0x02)
    assert bus._smbus is not None


def test_failures_become_i2c_errors_naming_the_register(bus):
    bus.read_byte(0x28, 0x00)
    bus._smbus.fail = True
    with pytest.raises(i2c.I2CError, match=r"read 0x28\[0x01\]"):
        bus.read_byte(0x28, 0x01)
    with pytest.raises(i2c.I2CError, match=r"read 0x28\[0x01:2\]"):
        bus.read_block(0x28, 0x01, 2)
    with pytest.raises(i2c.I2CError, match=r"write 0x28\[0x01\]"):
        bus.write_byte(0x28, 0x01, 0xFF)


def test_present_reports_a_device_that_answers(bus):
    assert bus.present(0x22) is True
    bus._smbus.fail = True
    assert bus.present(0x22) is False


def test_close_releases_the_handle(bus):
    bus.read_byte(0x36, 0x00)
    handle = bus._smbus
    bus.close()
    assert handle.closed and bus._smbus is None


def test_close_is_safe_before_any_traffic(bus):
    bus.close()


def test_missing_smbus2_says_what_to_do(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_smbus(name, *args, **kwargs):
        if name == "smbus2":
            raise ImportError("No module named 'smbus2'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_smbus)
    lonely = i2c.I2CBus(lock_path=tmp_path / "i2c.lock")
    with pytest.raises(i2c.I2CError, match="CUBESAT_MOCK_HARDWARE"):
        lonely.read_byte(0x28, 0x00)


def test_transaction_serialises_concurrent_holders(bus):
    # Two threads must not interleave inside a transaction: at 10 kHz a split
    # multi-byte read is exactly how the BNO055 returns garbage.
    order = []

    def worker(tag):
        with bus.transaction():
            order.append(f"{tag}-in")
            order.append(f"{tag}-out")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [o.split("-")[1] for o in order] == ["in", "out"] * 4


def test_lock_falls_back_when_the_run_directory_is_missing(tmp_path, caplog):
    lock = i2c._FileLock(tmp_path / "absent" / "i2c.lock")
    assert lock._handle is None
    with lock.hold():
        pass  # a process-local lock is enough where there is no real bus


def test_shared_bus_is_a_singleton(monkeypatch):
    monkeypatch.setattr(i2c, "_shared", None)
    assert i2c.shared_bus() is i2c.shared_bus()


def test_transaction_is_reentrant(bus):
    # A driver reading a 16-bit register wraps a transaction around two byte
    # reads, each of which locks too. A non-reentrant lock would either
    # deadlock or, worse, release the bus mid-word.
    with bus.transaction():
        bus.write_byte(0x36, 0x02, 0xCA)
        with bus.transaction():
            assert bus.read_byte(0x36, 0x02) == 0xCA
        assert bus.read_byte(0x36, 0x02) == 0xCA
    assert bus._lock._depth == 0


def test_reentrancy_works_without_a_file_lock(tmp_path):
    lock = i2c._FileLock(tmp_path / "absent" / "i2c.lock")
    with lock.hold(), lock.hold():
        assert lock._depth == 2
    assert lock._depth == 0
