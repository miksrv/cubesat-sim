"""Small, dependency-free fakes shared across hardware-driver tests.

These stand in for real I2C/serial hardware peripherals. They are plain
Python objects (not MagicMock) so tests can express "what the sensor
returns" as data, rather than choreographing mock call sequences.
"""


class FakeI2CBus:
    """Minimal stand-in for an smbus2.SMBus instance.

    `byte_responses` maps (addr, reg) -> int (or a zero-arg callable for
    values that should change across calls, e.g. simulating a timeout
    loop). `block_responses` maps (addr, reg) -> list[int] (or a callable
    taking `length`).
    """

    def __init__(self, byte_responses=None, block_responses=None, default_byte=0):
        self.byte_responses = dict(byte_responses or {})
        self.block_responses = dict(block_responses or {})
        self.default_byte = default_byte
        self.writes = []

    def read_byte_data(self, addr, reg):
        key = (addr, reg)
        if key in self.byte_responses:
            value = self.byte_responses[key]
            return value() if callable(value) else value
        return self.default_byte

    def write_byte_data(self, addr, reg, value):
        self.writes.append((addr, reg, value))

    def read_i2c_block_data(self, addr, reg, length):
        key = (addr, reg)
        if key in self.block_responses:
            value = self.block_responses[key]
            return list(value(length) if callable(value) else value)
        return [0] * length


class FakeSerial:
    """Stand-in for a pyserial Serial connection, fed a fixed list of lines."""

    def __init__(self, lines=None):
        self._lines = list(lines or [])
        self.closed = False

    @property
    def in_waiting(self):
        return len(self._lines)

    def readline(self):
        if not self._lines:
            return b""
        line = self._lines.pop(0)
        return (line + "\r\n").encode("ascii")

    def close(self):
        self.closed = True
