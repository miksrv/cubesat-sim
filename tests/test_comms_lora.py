import src.comms.lora as lora_mod
from src.common.utils import crc16_ccitt
from tests.fakes import FakeI2CBus

ADDR = lora_mod.LORA_I2C_ADDRESS


def make_module(monkeypatch, byte_responses=None):
    bus = FakeI2CBus(byte_responses=byte_responses or {})
    monkeypatch.setattr(lora_mod, "smbus", lambda _bus_num: bus)
    return lora_mod.LoRaModule(), bus


def framed_bytes(payload):
    crc = crc16_ccitt(payload)
    return payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


class TestSend:
    def test_writes_length_payload_and_trigger(self, monkeypatch):
        module, bus = make_module(monkeypatch)
        payload = b"hello"
        module.send(payload)

        framed = framed_bytes(payload)
        assert (ADDR, lora_mod.LORA_REG_LEN, len(framed)) in bus.writes
        for i, b in enumerate(framed):
            assert (ADDR, lora_mod.LORA_REG_PAYLOAD + i, b) in bus.writes
        assert (ADDR, lora_mod.LORA_REG_CTRL, lora_mod.LORA_TX_TRIGGER) in bus.writes

    def test_truncates_oversized_payload(self, monkeypatch):
        module, bus = make_module(monkeypatch)
        max_data = lora_mod.LORA_MAX_PAYLOAD - 2
        payload = bytes(range(max_data + 10))  # 10 bytes over budget

        module.send(payload)

        truncated = payload[:max_data]
        expected_framed = framed_bytes(truncated)
        length_writes = [w for w in bus.writes if w[1] == lora_mod.LORA_REG_LEN]
        assert length_writes == [(ADDR, lora_mod.LORA_REG_LEN, len(expected_framed))]


class TestReceive:
    def test_returns_none_when_rx_flag_not_set(self, monkeypatch):
        module, _bus = make_module(monkeypatch, byte_responses={(ADDR, lora_mod.LORA_REG_CTRL): 0x00})
        assert module.receive() is None

    def test_returns_payload_on_valid_crc(self, monkeypatch):
        payload = b"cmd"
        framed = framed_bytes(payload)
        byte_responses = {
            (ADDR, lora_mod.LORA_REG_CTRL): lora_mod.LORA_RX_FLAG,
            (ADDR, lora_mod.LORA_REG_LEN): len(framed),
        }
        for i, b in enumerate(framed):
            byte_responses[(ADDR, lora_mod.LORA_REG_PAYLOAD + i)] = b

        module, bus = make_module(monkeypatch, byte_responses=byte_responses)
        assert module.receive() == payload
        # RX flag must be cleared after reading
        assert (ADDR, lora_mod.LORA_REG_CTRL, 0x00) in bus.writes

    def test_discards_packet_with_bad_crc(self, monkeypatch):
        payload = b"cmd"
        framed = bytearray(framed_bytes(payload))
        framed[-1] ^= 0xFF  # corrupt the CRC
        byte_responses = {
            (ADDR, lora_mod.LORA_REG_CTRL): lora_mod.LORA_RX_FLAG,
            (ADDR, lora_mod.LORA_REG_LEN): len(framed),
        }
        for i, b in enumerate(framed):
            byte_responses[(ADDR, lora_mod.LORA_REG_PAYLOAD + i)] = b

        module, _bus = make_module(monkeypatch, byte_responses=byte_responses)
        assert module.receive() is None

    def test_discards_packet_shorter_than_crc(self, monkeypatch):
        byte_responses = {
            (ADDR, lora_mod.LORA_REG_CTRL): lora_mod.LORA_RX_FLAG,
            (ADDR, lora_mod.LORA_REG_LEN): 1,
            (ADDR, lora_mod.LORA_REG_PAYLOAD): 0xAB,
        }
        module, _bus = make_module(monkeypatch, byte_responses=byte_responses)
        assert module.receive() is None
