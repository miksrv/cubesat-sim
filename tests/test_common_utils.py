import logging

from src.common.utils import crc16_ccitt, ensure_dir, json_dumps_pretty, timestamp_iso


class TestJsonDumpsPretty:
    def test_serializes_dict(self):
        result = json_dumps_pretty({"a": 1, "b": "x"})
        assert '"a": 1' in result
        assert '"b": "x"' in result

    def test_preserves_non_ascii(self):
        result = json_dumps_pretty({"msg": "привет"})
        assert "привет" in result

    def test_falls_back_to_str_for_unserializable(self, caplog):
        class Unserializable:
            def __str__(self):
                return "<unserializable>"

        # default=str in json.dumps means even odd objects serialize via str(),
        # so this should NOT hit the except branch.
        result = json_dumps_pretty({"obj": Unserializable()})
        assert "<unserializable>" in result

    def test_falls_back_on_genuine_failure(self, monkeypatch, caplog):
        import json as json_module

        def boom(*_args, **_kwargs):
            raise TypeError("boom")

        monkeypatch.setattr(json_module, "dumps", boom)
        with caplog.at_level(logging.WARNING):
            result = json_dumps_pretty({"a": 1})
        assert result == str({"a": 1})
        assert "Ошибка форматирования JSON" in caplog.text


class TestCrc16Ccitt:
    def test_empty_bytes(self):
        assert crc16_ccitt(b"") == 0xFFFF

    def test_known_value(self):
        # CRC-16-CCITT (poly 0x1021, init 0xFFFF) of ASCII "123456789" is the
        # well-known CCITT-FALSE check value.
        assert crc16_ccitt(b"123456789") == 0x29B1

    def test_different_inputs_differ(self):
        assert crc16_ccitt(b"hello") != crc16_ccitt(b"world")

    def test_deterministic(self):
        data = b"cubesat-packet"
        assert crc16_ccitt(data) == crc16_ccitt(data)

    def test_result_fits_in_16_bits(self):
        assert 0 <= crc16_ccitt(b"\xff" * 50) <= 0xFFFF


class TestTimestampIso:
    def test_format(self):
        ts = timestamp_iso()
        # e.g. 2026-07-31T12:00:00Z
        assert len(ts) == 20
        assert ts.endswith("Z")
        assert ts[4] == "-" and ts[7] == "-" and ts[10] == "T"


class TestEnsureDir:
    def test_creates_missing_directory(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        assert not target.exists()
        ensure_dir(target)
        assert target.is_dir()

    def test_noop_if_already_exists(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        ensure_dir(target)  # must not raise
        assert target.is_dir()

    def test_accepts_string_path(self, tmp_path):
        target = str(tmp_path / "from_string")
        ensure_dir(target)
        assert (tmp_path / "from_string").is_dir()
