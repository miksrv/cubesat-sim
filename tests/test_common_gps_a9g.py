import pytest

import src.common.gps_a9g as gps_mod
from tests.fakes import FakeSerial


def make_gps(monkeypatch, lines=None):
    fake_serial = FakeSerial(lines=lines)
    monkeypatch.setattr(gps_mod.serial, "Serial", lambda *a, **kw: fake_serial)
    return gps_mod.GPS(), fake_serial


class TestEmptyFix:
    def test_initial_fix_has_no_data(self, monkeypatch):
        gps, _serial = make_gps(monkeypatch)
        fix = gps.read_position()
        assert fix == {"lat": None, "lon": None, "alt": None, "speed": None, "fix": False}


class TestReadPosition:
    def test_parses_valid_gga_sentence(self, monkeypatch):
        gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
        gps, _serial = make_gps(monkeypatch, lines=[gga])
        fix = gps.read_position()
        assert fix["fix"] is True
        assert fix["lat"] == pytest.approx(48 + 7.038 / 60, abs=1e-4)
        assert fix["lon"] == pytest.approx(11 + 31.000 / 60, abs=1e-4)
        assert fix["alt"] == 545.4

    def test_ignores_gga_without_fix_quality(self, monkeypatch):
        # gps_qual = 0 means "no fix" per NMEA spec
        gga_no_fix = "$GPGGA,123519,4807.038,N,01131.000,E,0,00,,,M,,M,,*4B"
        gps, _serial = make_gps(monkeypatch, lines=[gga_no_fix])
        fix = gps.read_position()
        assert fix["fix"] is False

    def test_parses_valid_rmc_sentence(self, monkeypatch):
        rmc = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        gps, _serial = make_gps(monkeypatch, lines=[rmc])
        fix = gps.read_position()
        assert fix["fix"] is True
        assert fix["speed"] == 22.4

    def test_ignores_rmc_with_void_status(self, monkeypatch):
        rmc_void = "$GPRMC,123519,V,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*67"
        gps, _serial = make_gps(monkeypatch, lines=[rmc_void])
        fix = gps.read_position()
        assert fix["fix"] is False

    def test_skips_non_nmea_lines(self, monkeypatch):
        gps, _serial = make_gps(monkeypatch, lines=["not-a-sentence", "also garbage"])
        fix = gps.read_position()
        assert fix["fix"] is False

    def test_skips_unparseable_nmea_lines(self, monkeypatch):
        gps, _serial = make_gps(monkeypatch, lines=["$GPGGA,not,valid,nmea*00"])
        fix = gps.read_position()
        assert fix["fix"] is False

    def test_retains_last_known_fix_across_calls(self, monkeypatch):
        gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
        gps, serial_conn = make_gps(monkeypatch, lines=[gga])
        first = gps.read_position()
        assert first["fix"] is True

        # No new sentences waiting -> last known fix is returned unchanged.
        second = gps.read_position()
        assert second == first

    def test_never_blocks_when_nothing_waiting(self, monkeypatch):
        gps, _serial = make_gps(monkeypatch, lines=[])
        fix = gps.read_position()
        assert fix["fix"] is False

    def test_stops_reading_on_serial_exception(self, monkeypatch):
        gps, serial_conn = make_gps(monkeypatch, lines=["whatever"])

        def raise_serial_exception():
            raise gps_mod.serial.SerialException("device disconnected")

        serial_conn.readline = raise_serial_exception
        fix = gps.read_position()  # must not raise
        assert fix["fix"] is False


class TestClose:
    def test_closes_underlying_serial_connection(self, monkeypatch):
        gps, serial_conn = make_gps(monkeypatch)
        gps.close()
        assert serial_conn.closed is True
