"""The shared I2C bus.

Every sensor on this satellite is on bus 1, and the bus is clamped to 10 kHz
because the BNO055 stretches the clock in a way the Pi's controller mishandles
at 100 kHz. At 10 kHz a single byte read costs tens of milliseconds and a GNSS
register block costs far more, while four processes — EPS, ADCS, PAYLOAD and
whatever DEPLOY is sweeping — reach for the bus independently.

So every transaction takes an advisory lock. It is a file lock rather than a
threading lock because the contending parties are separate processes.
"""

from __future__ import annotations

import fcntl
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cubesat.common import config

logger = logging.getLogger(__name__)


class I2CError(OSError):
    """A bus transaction failed."""


class _FileLock:
    """Inter-process lock around bus access.

    Falls back to a process-local lock when the run directory does not exist —
    a development machine without the systemd units. That is safe there because
    a machine with no ``/run/cubesat`` has no real bus to contend for either.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        #: Reentrant, and held across the yield: it serialises threads inside
        #: this process while the flock serialises processes.
        self._guard = threading.RLock()
        self._depth = 0
        self._handle = None
        try:
            self._handle = path.open("w")
        except OSError as exc:
            logger.warning("I2C file lock unavailable (%s): %s; using a local lock", path, exc)

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Acquire the bus. Reentrant.

        Reentrancy is not a nicety: a driver reading a 16-bit register wraps
        ``transaction()`` around two byte reads, and each of those takes the
        lock as well. Without depth counting the inner release would drop the
        bus mid-word, which at 10 kHz is exactly how a split read comes back
        as garbage.
        """
        with self._guard:
            self._depth += 1
            try:
                if self._depth == 1 and self._handle is not None:
                    fcntl.flock(self._handle, fcntl.LOCK_EX)
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._handle is not None:
                    fcntl.flock(self._handle, fcntl.LOCK_UN)


class I2CBus:
    """Thin wrapper over ``smbus2`` that serialises every transaction.

    ``smbus2`` is imported lazily so that importing this module on a laptop —
    which the tests and the mock HAL do constantly — does not require a package
    that only installs on a Raspberry Pi.
    """

    def __init__(self, bus: int | None = None, lock_path: Path | None = None) -> None:
        self._bus_number = config.I2C_BUS if bus is None else bus
        self._lock = _FileLock(lock_path or config.I2C_LOCK_FILE)
        self._smbus = None

    def _open(self):
        if self._smbus is None:
            try:
                import smbus2
            except ImportError as exc:
                raise I2CError(
                    "smbus2 is not installed, so there is no I2C bus here. "
                    "Set CUBESAT_MOCK_HARDWARE=1 to run without hardware."
                ) from exc
            self._smbus = smbus2.SMBus(self._bus_number)
        return self._smbus

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the bus for a sequence of reads and writes that must not be split."""
        with self._lock.hold():
            yield

    def read_byte(self, address: int, register: int) -> int:
        with self._lock.hold():
            try:
                return self._open().read_byte_data(address, register)
            except OSError as exc:
                raise I2CError(f"read {address:#04x}[{register:#04x}] failed: {exc}") from exc

    def read_block(self, address: int, register: int, length: int) -> list[int]:
        with self._lock.hold():
            try:
                return self._open().read_i2c_block_data(address, register, length)
            except OSError as exc:
                raise I2CError(
                    f"read {address:#04x}[{register:#04x}:{length}] failed: {exc}"
                ) from exc

    def write_byte(self, address: int, register: int, value: int) -> None:
        with self._lock.hold():
            try:
                self._open().write_byte_data(address, register, value)
            except OSError as exc:
                raise I2CError(f"write {address:#04x}[{register:#04x}] failed: {exc}") from exc

    def present(self, address: int) -> bool:
        """Whether anything answers at ``address``. The DEPLOY sweep uses this."""
        try:
            self.read_byte(address, 0x00)
            return True
        except I2CError:
            return False

    def close(self) -> None:
        if self._smbus is not None:
            self._smbus.close()
            self._smbus = None


_shared: I2CBus | None = None


def shared_bus() -> I2CBus:
    """The process-wide bus handle. One per process is plenty; the lock is what
    actually keeps peace, and it is shared across processes."""
    global _shared
    if _shared is None:
        _shared = I2CBus()
    return _shared
