import time
import smbus
import lgpio as sbc
from typing import Dict, Optional

# ─── LPS22HB (давление + температура) ──────────────────────────────────────
LPS22HB_I2C_ADDRESS = 0x5C
LPS_CTRL_REG1    = 0x10
LPS_CTRL_REG2    = 0x11
LPS_STATUS       = 0x27
LPS_PRESS_OUT_XL = 0x28
LPS_PRESS_OUT_L  = 0x29
LPS_PRESS_OUT_H  = 0x2A
LPS_TEMP_OUT_L   = 0x2B
LPS_TEMP_OUT_H   = 0x2C

# ─── SHTC3 (влажность + температура) ───────────────────────────────────────
SHTC3_I2C_ADDRESS    = 0x70
CRC_POLYNOMIAL       = 0x0131
SHTC3_WakeUp         = 0x3517
SHTC3_Sleep          = 0xB098
SHTC3_Software_RES   = 0x805D
SHTC3_NM_CD_ReadTH   = 0x7866
SHTC3_NM_CD_ReadRH   = 0x58E0


class SenseHatSensors:
    """
    Коллектор метрик с Sense HAT (C):
    - LPS22HB → давление (hPa), температура (°C)
    - SHTC3   → влажность (%), температура (°C)

    Возвращает усреднённую температуру, если оба датчика дали показания.
    """

    def __init__(self):
        # LPS22HB (smbus)
        self.lps_bus = smbus.SMBus(1)
        self.lps_addr = LPS22HB_I2C_ADDRESS
        self._lps_init()

        # SHTC3 (lgpio)
        self.sbc = sbc
        self.shtc_fd = self.sbc.i2c_open(1, SHTC3_I2C_ADDRESS, 0)
        self._shtc_init()

    def _lps_init(self):
        try:
            # Software reset
            reg = self.lps_bus.read_byte_data(self.lps_addr, LPS_CTRL_REG2)
            self.lps_bus.write_byte_data(self.lps_addr, LPS_CTRL_REG2, reg | 0x04)
            while self.lps_bus.read_byte_data(self.lps_addr, LPS_CTRL_REG2) & 0x04:
                time.sleep(0.001)

            # Настройки: BDU=1, ODR=0 (one-shot), LPF disabled
            self.lps_bus.write_byte_data(self.lps_addr, LPS_CTRL_REG1, 0x02)
        except Exception as e:
            print(f"LPS22HB init error: {e}")

    def _shtc_init(self):
        try:
            # Software reset
            self._shtc_write(SHTC3_Software_RES)
            time.sleep(0.015)
        except Exception as e:
            print(f"SHTC3 init error: {e}")

    # ─── LPS22HB helpers ────────────────────────────────────────────────────────
    def _lps_start_oneshot(self):
        reg = self.lps_bus.read_byte_data(self.lps_addr, LPS_CTRL_REG2)
        self.lps_bus.write_byte_data(self.lps_addr, LPS_CTRL_REG2, reg | 0x01)

    def read_pressure(self) -> Optional[float]:
        try:
            self._lps_start_oneshot()
            time.sleep(0.08)  # типичное время преобразования ~25-50 мс
            status = self.lps_bus.read_byte_data(self.lps_addr, LPS_STATUS)
            if status & 0x01:
                xl = self.lps_bus.read_byte_data(self.lps_addr, LPS_PRESS_OUT_XL)
                l  = self.lps_bus.read_byte_data(self.lps_addr, LPS_PRESS_OUT_L)
                h  = self.lps_bus.read_byte_data(self.lps_addr, LPS_PRESS_OUT_H)
                raw = (h << 16) | (l << 8) | xl
                return round(raw / 4096.0, 2)   # hPa
        except Exception:
            pass
        return None

    def read_lps_temperature(self) -> Optional[float]:
        try:
            self._lps_start_oneshot()
            time.sleep(0.08)
            status = self.lps_bus.read_byte_data(self.lps_addr, LPS_STATUS)
            if status & 0x02:
                l = self.lps_bus.read_byte_data(self.lps_addr, LPS_TEMP_OUT_L)
                h = self.lps_bus.read_byte_data(self.lps_addr, LPS_TEMP_OUT_H)
                raw = (h << 8) | l
                return round(raw / 100.0, 2)
        except Exception:
            pass
        return None

    # ─── SHTC3 helpers ──────────────────────────────────────────────────────────
    def _shtc_write(self, cmd: int):
        self.sbc.i2c_write_byte_data(self.shtc_fd, cmd >> 8, cmd & 0xFF)

    def _shtc_read(self, nbytes: int) -> bytes:
        _, data = self.sbc.i2c_read_device(self.shtc_fd, nbytes)
        return data

    @staticmethod
    def _crc8(data: bytes, length: int, crc_check: int) -> bool:
        crc = 0xFF
        for i in range(length):
            crc ^= data[i]
            for _ in range(8):
                crc = (crc << 1) ^ CRC_POLYNOMIAL if crc & 0x80 else crc << 1
            crc &= 0xFF
        return crc == crc_check

    def read_shtc_temperature(self) -> Optional[float]:
        try:
            self._shtc_write(SHTC3_WakeUp)
            time.sleep(0.01)
            self._shtc_write(SHTC3_NM_CD_ReadTH)
            time.sleep(0.025)
            buf = self._shtc_read(3)
            if len(buf) == 3 and self._crc8(buf, 2, buf[2]):
                raw = (buf[0] << 8) | buf[1]
                return round(raw * 175.0 / 65536.0 - 45.0, 2)
        except Exception:
            pass
        return None

    def read_humidity(self) -> Optional[float]:
        try:
            self._shtc_write(SHTC3_WakeUp)
            time.sleep(0.01)
            self._shtc_write(SHTC3_NM_CD_ReadRH)
            time.sleep(0.025)
            buf = self._shtc_read(3)
            if len(buf) == 3 and self._crc8(buf, 2, buf[2]):
                raw = (buf[0] << 8) | buf[1]
                return round(100.0 * raw / 65536.0, 2)
        except Exception:
            pass
        return None

    # ─── Основная публичная функция ─────────────────────────────────────────────
    def collect(self) -> Dict[str, Optional[float]]:
        """
        Собирает все доступные метрики за один вызов.
        Температура — среднее от двух датчиков, если оба успешны.
        """
        lps_t  = self.read_lps_temperature()
        sht_t  = self.read_shtc_temperature()
        press  = self.read_pressure()
        hum    = self.read_humidity()

        temps = [t for t in (lps_t, sht_t) if t is not None]
        avg_temp = round(sum(temps) / len(temps), 2) if temps else None

        return {
            "temperature": avg_temp,
            "pressure":    press,
            "humidity":    hum,
            # Можно добавить отдельно, если нужно:
            # "lps_temperature": lps_t,
            # "shtc_temperature": sht_t,
        }

    def __del__(self):
        """Закрываем ресурсы при уничтожении объекта"""
        try:
            self.sbc.i2c_close(self.shtc_fd)
        except Exception:
            pass