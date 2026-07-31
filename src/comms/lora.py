import logging
from typing import Optional
from smbus2 import SMBus as smbus

from src.common.config import LORA_I2C_ADDRESS
from src.common.utils import crc16_ccitt

logger = logging.getLogger(__name__)

# SC16IS752 I2C↔UART bridge on the 52Pi IoT Node(A) — see docs/hardware-iot-node-a-52pi.md.
# Registers 0x01-0x20 hold the LoRa payload buffer, 0x23 is control/status (write 0x01 to
# trigger TX; bit 0x02 read back on 0x23 signals a received packet). Framing here is
# length-prefixed (1 length byte + payload + 2-byte CRC-16-CCITT) to fit arbitrary JSON
# envelopes into the fixed-size buffer — adjust register offsets against real hardware
# if the vendor's full register map (52Pi wiki) differs.
LORA_REG_LEN     = 0x01
LORA_REG_PAYLOAD = 0x02
LORA_REG_CTRL    = 0x23
LORA_MAX_PAYLOAD = 0x20 - 0x02  # bytes available after the length byte
LORA_TX_TRIGGER  = 0x01
LORA_RX_FLAG     = 0x02


class LoRaModule:
    def __init__(self, i2c_address: int = LORA_I2C_ADDRESS, bus: int = 1):
        self.address = i2c_address
        self.bus = smbus(bus)

    def send(self, payload: bytes) -> None:
        if len(payload) > LORA_MAX_PAYLOAD - 2:
            logger.warning(f"LoRa payload truncated: {len(payload)} bytes exceeds max {LORA_MAX_PAYLOAD - 2}")
            payload = payload[:LORA_MAX_PAYLOAD - 2]

        crc = crc16_ccitt(payload)
        framed = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])

        self.bus.write_byte_data(self.address, LORA_REG_LEN, len(framed))
        for i, b in enumerate(framed):
            self.bus.write_byte_data(self.address, LORA_REG_PAYLOAD + i, b)
        self.bus.write_byte_data(self.address, LORA_REG_CTRL, LORA_TX_TRIGGER)

    def receive(self) -> Optional[bytes]:
        status = self.bus.read_byte_data(self.address, LORA_REG_CTRL)
        if not (status & LORA_RX_FLAG):
            return None

        length = self.bus.read_byte_data(self.address, LORA_REG_LEN)
        framed = bytes(
            self.bus.read_byte_data(self.address, LORA_REG_PAYLOAD + i)
            for i in range(length)
        )
        self.bus.write_byte_data(self.address, LORA_REG_CTRL, 0x00)  # clear RX flag

        if length < 2:
            logger.warning(f"LoRa packet too short to contain a CRC: {length} bytes")
            return None

        payload, received_crc = framed[:-2], (framed[-2] << 8) | framed[-1]
        if crc16_ccitt(payload) != received_crc:
            logger.warning("LoRa packet failed CRC check, discarding")
            return None

        return payload
