import src.payload.science as science_mod
from tests.fakes import FakeI2CBus

ADDR = science_mod.LPS22HB_I2C_ADDRESS


def make_collector(monkeypatch, byte_responses=None, shtc_reads=None):
    bus = FakeI2CBus(byte_responses=byte_responses or {})
    monkeypatch.setattr(science_mod, "smbus", lambda _bus_num: bus)
    # lgpio is globally mocked (see conftest.py); give i2c_read_device a
    # controllable, per-call sequence of (handle, bytes) responses instead
    # of a single MagicMock return value.
    reads = list(shtc_reads or [])

    def fake_read_device(_fd, _nbytes):
        return len(reads[0][1]) if reads else 0, (reads.pop(0)[1] if reads else b"")

    monkeypatch.setattr(science_mod.sbc, "i2c_read_device", fake_read_device)
    monkeypatch.setattr(science_mod.sbc, "i2c_open", lambda *_a, **_k: 3)
    collector = science_mod.ScienceCollector()
    return collector, bus


def crc8(data, crc_init=0xFF, poly=science_mod.CRC_POLYNOMIAL):
    crc = crc_init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) if (crc & 0x80) else (crc << 1)
        crc &= 0xFF
    return crc


def shtc_bytes(raw_value):
    hi, lo = (raw_value >> 8) & 0xFF, raw_value & 0xFF
    check = crc8([hi, lo])
    return bytes([hi, lo, check])


class TestInit:
    def test_lps_reset_polling_loop_waits_for_bit_clear(self, monkeypatch):
        # _lps_init reads LPS_CTRL_REG2 once before the while loop (to OR in
        # the reset bit), then the while condition itself re-reads it on
        # every check. Keep returning "reset in progress" through both of
        # those before flipping to "done", so the loop body actually runs
        # at least once.
        calls = {"n": 0}

        def ctrl_reg2(_length=None):
            calls["n"] += 1
            return 0x04 if calls["n"] <= 2 else 0x00

        collector, bus = make_collector(
            monkeypatch, byte_responses={(ADDR, science_mod.LPS_CTRL_REG2): ctrl_reg2}
        )
        assert calls["n"] >= 3

    def test_lps_init_error_is_swallowed(self, monkeypatch, capsys):
        def boom(_addr, _reg):
            raise OSError("bus not ready")

        bus = FakeI2CBus()
        bus.read_byte_data = boom
        monkeypatch.setattr(science_mod, "smbus", lambda _bus_num: bus)
        monkeypatch.setattr(science_mod.sbc, "i2c_open", lambda *_a, **_k: 3)
        science_mod.ScienceCollector()  # must not raise
        assert "LPS22HB init error" in capsys.readouterr().out

    def test_shtc_init_error_is_swallowed(self, monkeypatch, capsys):
        collector_bus = FakeI2CBus()
        monkeypatch.setattr(science_mod, "smbus", lambda _bus_num: collector_bus)
        monkeypatch.setattr(science_mod.sbc, "i2c_open", lambda *_a, **_k: 3)

        def boom(_fd, _hi, _lo):
            raise OSError("shtc bus not ready")

        monkeypatch.setattr(science_mod.sbc, "i2c_write_byte_data", boom)
        science_mod.ScienceCollector()  # must not raise
        assert "SHTC3 init error" in capsys.readouterr().out


class TestReadPressure:
    def test_returns_none_when_status_bit_not_set(self, monkeypatch):
        collector, _bus = make_collector(monkeypatch)
        assert collector.read_pressure() is None

    def test_converts_raw_reading(self, monkeypatch):
        # raw = 4096 * 1013.25 -> pressure == 1013.25 hPa
        raw = round(4096 * 1013.25)
        collector, bus = make_collector(
            monkeypatch,
            byte_responses={
                (ADDR, science_mod.LPS_STATUS): 0x01,
                (ADDR, science_mod.LPS_PRESS_OUT_XL): raw & 0xFF,
                (ADDR, science_mod.LPS_PRESS_OUT_L): (raw >> 8) & 0xFF,
                (ADDR, science_mod.LPS_PRESS_OUT_H): (raw >> 16) & 0xFF,
            },
        )
        assert collector.read_pressure() == 1013.25

    def test_returns_none_on_bus_error(self, monkeypatch):
        collector, bus = make_collector(monkeypatch)

        def boom(_addr, _reg):
            raise OSError("bus error")

        bus.read_byte_data = boom
        assert collector.read_pressure() is None


class TestReadLpsTemperature:
    def test_returns_none_when_status_bit_not_set(self, monkeypatch):
        collector, _bus = make_collector(monkeypatch)
        assert collector.read_lps_temperature() is None

    def test_converts_raw_reading(self, monkeypatch):
        # raw = 2500 -> 25.00 C
        collector, _bus = make_collector(
            monkeypatch,
            byte_responses={
                (ADDR, science_mod.LPS_STATUS): 0x02,
                (ADDR, science_mod.LPS_TEMP_OUT_L): 2500 & 0xFF,
                (ADDR, science_mod.LPS_TEMP_OUT_H): (2500 >> 8) & 0xFF,
            },
        )
        assert collector.read_lps_temperature() == 25.0

    def test_returns_none_on_bus_error(self, monkeypatch):
        collector, bus = make_collector(monkeypatch)

        def boom(_addr, _reg):
            raise OSError("bus error")

        bus.read_byte_data = boom
        assert collector.read_lps_temperature() is None


class TestReadShtcTemperature:
    def test_converts_valid_reading(self, monkeypatch):
        # raw * 175/65536 - 45, choose raw so the result is a clean number
        raw = 21299  # -> ~11.0 C
        collector, _bus = make_collector(monkeypatch, shtc_reads=[(3, shtc_bytes(raw))])
        expected = round(raw * 175.0 / 65536.0 - 45.0, 2)
        assert collector.read_shtc_temperature() == expected

    def test_returns_none_on_crc_mismatch(self, monkeypatch):
        good = shtc_bytes(21299)
        corrupted = bytes([good[0], good[1] ^ 0xFF, good[2]])
        collector, _bus = make_collector(monkeypatch, shtc_reads=[(3, corrupted)])
        assert collector.read_shtc_temperature() is None

    def test_returns_none_on_bus_error(self, monkeypatch):
        collector, _bus = make_collector(monkeypatch)

        def boom(_fd, _nbytes):
            raise OSError("i2c error")

        monkeypatch.setattr(science_mod.sbc, "i2c_read_device", boom)
        assert collector.read_shtc_temperature() is None


class TestReadHumidity:
    def test_converts_valid_reading(self, monkeypatch):
        raw = 32768  # -> 50.0 %
        collector, _bus = make_collector(monkeypatch, shtc_reads=[(3, shtc_bytes(raw))])
        assert collector.read_humidity() == round(100.0 * raw / 65536.0, 2)

    def test_returns_none_on_crc_mismatch(self, monkeypatch):
        good = shtc_bytes(32768)
        corrupted = bytes([good[0] ^ 0xFF, good[1], good[2]])
        collector, _bus = make_collector(monkeypatch, shtc_reads=[(3, corrupted)])
        assert collector.read_humidity() is None

    def test_returns_none_on_bus_error(self, monkeypatch):
        collector, _bus = make_collector(monkeypatch)

        def boom(_fd, _nbytes):
            raise OSError("i2c error")

        monkeypatch.setattr(science_mod.sbc, "i2c_read_device", boom)
        assert collector.read_humidity() is None


class TestCollect:
    def test_averages_temperature_from_both_sensors(self, monkeypatch):
        raw_press = round(4096 * 1000.0)
        collector, _bus = make_collector(
            monkeypatch,
            byte_responses={
                (ADDR, science_mod.LPS_STATUS): 0x03,  # pressure + temp ready
                (ADDR, science_mod.LPS_TEMP_OUT_L): 2000 & 0xFF,
                (ADDR, science_mod.LPS_TEMP_OUT_H): (2000 >> 8) & 0xFF,
                (ADDR, science_mod.LPS_PRESS_OUT_XL): raw_press & 0xFF,
                (ADDR, science_mod.LPS_PRESS_OUT_L): (raw_press >> 8) & 0xFF,
                (ADDR, science_mod.LPS_PRESS_OUT_H): (raw_press >> 16) & 0xFF,
            },
            shtc_reads=[(3, shtc_bytes(21299)), (3, shtc_bytes(32768))],
        )
        result = collector.collect()
        lps_temp = 20.0
        sht_temp = round(21299 * 175.0 / 65536.0 - 45.0, 2)
        assert result["temperature"] == round((lps_temp + sht_temp) / 2, 2)
        assert result["pressure"] == 1000.0
        assert result["humidity"] == round(100.0 * 32768 / 65536.0, 2)

    def test_temperature_none_when_both_sensors_fail(self, monkeypatch):
        collector, _bus = make_collector(monkeypatch)
        result = collector.collect()
        assert result == {"temperature": None, "pressure": None, "humidity": None}


class TestDel:
    def test_close_errors_are_swallowed(self, monkeypatch):
        collector, _bus = make_collector(monkeypatch)
        monkeypatch.setattr(
            science_mod.sbc,
            "i2c_close",
            lambda _fd: (_ for _ in ()).throw(RuntimeError("already closed")),
        )
        collector.__del__()  # must not raise
