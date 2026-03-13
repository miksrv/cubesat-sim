import json
import time
import binascii
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def json_dumps_pretty(obj, indent=2) -> str:
    """Красивая печать JSON с обработкой ошибок"""
    try:
        return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)
    except Exception as e:
        logger.warning(f"Ошибка форматирования JSON: {e}")
        return str(obj)

def crc16_ccitt(data: bytes) -> int:
    """CRC-16-CCITT для проверки пакетов LoRa"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc

def timestamp_iso() -> str:
    """Текущее время в ISO 8601 UTC"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def ensure_dir(path):
    """Создаёт директорию, если её нет"""
    Path(path).mkdir(parents=True, exist_ok=True)