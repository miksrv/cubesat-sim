import smbus2
import time
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# I2C настройки
I2C_BUS = 1
BATTERY_I2C_ADDR = 0x36  # MAX17040/41

# Регистры MAX17040 (из datasheet)
REG_VCELL   = 0x02  # Voltage (12-bit, MSB first)
REG_SOC     = 0x04  # State of Charge (16-bit: 8-bit integer + 8-bit fraction)
REG_MODE    = 0x06  # Mode register
REG_VERSION = 0x08  # Chip version

# Для внешнего питания — используем GPIO (GPIO 6 по умолчанию на X728)
# Предполагаем, что у тебя настроен GPIO 6 как power-loss detect (high = AC present)
POWER_GPIO_CHIP = "gpiochip0"
POWER_GPIO_LINE = 6   # Из документации X728 — GPIO6 (BCM 6)

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
                logger.warning(f"Не удалось инициализировать GPIO для power detect: {e}")
                self.use_gpio = False

    def read_word(self, reg: int) -> int:
        """Читает 16-битное слово (MSB first)"""
        try:
            msb = self.bus.read_byte_data(BATTERY_I2C_ADDR, reg)
            lsb = self.bus.read_byte_data(BATTERY_I2C_ADDR, reg + 1)
            return (msb << 8) | lsb
        except Exception as e:
            logger.error(f"I2C ошибка при чтении регистра 0x{reg:02X}: {e}")
            return 0

    def get_battery_voltage(self) -> Optional[float]:
        raw = self.read_word(REG_VCELL)
        if raw == 0:
            return None
        # 12-bit, 1.25 mV per LSB (по datasheet MAX17040)
        voltage = (raw >> 4) * 0.00125  # сдвигаем 4 бита, умножаем на шаг
        return round(voltage, 3)

    def get_battery_percent(self) -> Optional[float]:
        raw = self.read_word(REG_SOC)
        if raw == 0:
            return None
        # SOC: старшие 8 бит — целая часть, младшие 8 бит — дробная (1/256)
        percent = (raw >> 8) + ((raw & 0xFF) / 256.0)
        return round(percent, 2)

    def get_external_power(self) -> bool:
        """True = на внешнем питании (AC), False = на батарее"""
        if not self.use_gpio or not self.gpio_line:
            # Если GPIO не доступен — предполагаем внешнее питание (fallback)
            return True

        try:
            # High = AC present (по документации X728)
            return self.gpio_line.get_value() == 1
        except Exception as e:
            logger.error(f"GPIO read error: {e}")
            return True  # безопасный fallback

    def get_status(self) -> Dict:
        voltage = self.get_battery_voltage()
        percent = self.get_battery_percent()
        external = self.get_external_power()

        return {
            "timestamp": time.time(),
            "battery": percent if percent is not None else None,
            "voltage": voltage if voltage is not None else None,
            "external_power": external,
            "status": "ok" if percent is not None else "error"
        }

    def __del__(self):
        if self.gpio_line:
            try:
                self.gpio_line.release()
            except:
                pass