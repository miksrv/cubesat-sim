import smbus2
import time
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

I2C_BUS = 1
BATTERY_I2C_ADDR = 0x36
REG_VCELL = 0x02
REG_SOC = 0x04

PLD_GPIO_NUM = 6  # BCM 6 — как в документации X728

class EPSMonitor:
    def __init__(self):
        self.bus = smbus2.SMBus(I2C_BUS)
        self.gpio_path = f"/sys/class/gpio/gpio{PLD_GPIO_NUM}/value"
        self._export_gpio()  # экспортируем пин, если ещё не

    def _export_gpio(self):
        """Экспортируем GPIO6 в sysfs, если ещё не экспортирован"""
        export_path = "/sys/class/gpio/export"
        try:
            if not os.path.exists(self.gpio_path):
                with open(export_path, "w") as f:
                    f.write(str(PLD_GPIO_NUM))
                time.sleep(0.1)  # даём время на создание
                # Устанавливаем направление input
                direction_path = f"/sys/class/gpio/gpio{PLD_GPIO_NUM}/direction"
                with open(direction_path, "w") as f:
                    f.write("in")
                logger.info(f"GPIO{PLD_GPIO_NUM} экспортирован и настроен как input")
        except Exception as e:
            logger.warning(f"Не удалось экспортировать GPIO{PLD_GPIO_NUM}: {e}")

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
        voltage = (raw >> 4) * 0.00125
        return round(voltage, 3)

    def get_battery_percent(self) -> Optional[float]:
        raw = self.read_word(REG_SOC)
        if raw == 0:
            return None
        percent = raw / 256.0
        percent = max(0.0, min(100.0, percent))
        return round(percent, 2)

    def get_external_power(self) -> bool:
        """True = на внешнем питании (AC), False = на батарее"""
        try:
            with open(self.gpio_path, "r") as f:
                pin_value = int(f.read().strip())

            # По документации Geekworm: 0 = AC OK (внешнее есть), 1 = AC Lost
            is_ac_present = (pin_value == 0)
            logger.debug(f"PLD_PIN value = {pin_value}, external_power = {is_ac_present}")
            return is_ac_present
        except Exception as e:
            logger.error(f"Ошибка чтения GPIO sysfs: {e}")
            return True  # безопасный fallback

    def get_status(self) -> Dict:
        status = {
            "timestamp": time.time(),
            "battery": self.get_battery_percent(),
            "voltage": self.get_battery_voltage(),
            "external_power": self.get_external_power(),
            "status": "ok"
        }
        if not status["external_power"]:
            logger.warning("ВНИМАНИЕ: внешнее питание отключено! Работает от батареи")
        return status

    def __del__(self):
        # Можно unexport пин при выходе, но не обязательно
        pass