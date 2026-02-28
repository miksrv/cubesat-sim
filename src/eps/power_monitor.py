import smbus2
import time
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

I2C_BUS = 1
BATTERY_I2C_ADDR = 0x36

REG_VCELL   = 0x02
REG_SOC     = 0x04

POWER_GPIO_CHIP = "gpiochip0"
POWER_GPIO_LINE = 6   # BCM 6 на X728

class EPSMonitor:
    def __init__(self, use_gpio: bool = True):
        self.bus = smbus2.SMBus(I2C_BUS)
        self.use_gpio = use_gpio
        self.gpio_line = None

        if self.use_gpio:
            try:
                import gpiod
                chip = gpiod.Chip(POWER_GPIO_CHIP)
                self.gpio_line = chip.get_line(POWER_GPIO_LINE)
                self.gpio_line.request(consumer="x728-power", type=gpiod.LINE_REQ_DIR_IN)
                logger.info("GPIO power-loss detect инициализирован")
            except Exception as e:
                logger.warning(f"Не удалось инициализировать GPIO: {e}")
                self.use_gpio = False

    def read_word(self, reg: int) -> int:
        try:
            msb = self.bus.read_byte_data(BATTERY_I2C_ADDR, reg)
            lsb = self.bus.read_byte_data(BATTERY_I2C_ADDR, reg + 1)
            return (msb << 8) | lsb
        except Exception as e:
            logger.error(f"I2C ошибка при чтении 0x{reg:02X}: {e}")
            return 0

    def get_battery_voltage(self) -> Optional[float]:
        raw = self.read_word(REG_VCELL)
        if raw == 0:
            return None
        # Правильная формула: 12 бит, 1.25 мВ/LSB
        voltage = (raw >> 4) * 0.00125
        return round(voltage, 3)

    def get_battery_percent(self) -> Optional[float]:
        raw = self.read_word(REG_SOC)
        if raw == 0:
            return None

        # Формула как в примере Geekworm + ограничение
        percent = raw / 256.0
        percent = max(0.0, min(100.0, percent))  # ограничиваем 0–100%
        return round(percent, 2)

    def get_external_power(self) -> bool:
        if not self.use_gpio or not self.gpio_line:
            return True  # fallback: считаем, что на внешнем питании

        try:
            # По документации X728: GPIO6 = 1 → AC present
            return self.gpio_line.get_value() == 1
        except Exception as e:
            logger.error(f"GPIO read error: {e}")
            return True

    def get_status(self) -> Dict:
        return {
            "timestamp": time.time(),
            "battery": self.get_battery_percent(),      # теперь 0–100
            "voltage": self.get_battery_voltage(),
            "external_power": self.get_external_power(),
            "status": "ok"
        }

    def __del__(self):
        if self.gpio_line:
            try:
                self.gpio_line.release()
            except:
                pass