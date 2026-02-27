import psutil
import os
import time
from typing import Dict, Optional

class SystemMetricsCollector:
    """Сборщик системных метрик (RPi / Linux oriented)"""

    @staticmethod
    def get_soc_temperature() -> Optional[float]:
        path = "/sys/class/thermal/thermal_zone0/temp"
        try:
            with open(path, 'r') as f:
                value = f.read().strip()
            return round(float(value) / 1000, 1)
        except Exception:
            pass  # fallback

        try:
            res = os.popen('vcgencmd measure_temp').readline().strip()
            if res.startswith("temp="):
                temp_str = res.replace("temp=", "").replace("'C", "")
                return round(float(temp_str), 1)
        except Exception:
            pass

        return None

    @staticmethod
    def get_cpu_usage(interval: float = 0.8) -> float:
        """Загрузка CPU в % (с небольшим интервалом измерения)"""
        try:
            return psutil.cpu_percent(interval=interval)
        except Exception:
            return 0.0

    @staticmethod
    def get_ram_usage() -> float:
        """Использование RAM в %"""
        try:
            return psutil.virtual_memory().percent
        except Exception:
            return 0.0

    @staticmethod
    def get_swap_usage() -> float:
        """Использование swap в %"""
        try:
            return psutil.swap_memory().percent
        except Exception:
            return 0.0

    @staticmethod
    def get_uptime_seconds() -> int:
        """Время работы системы в секундах"""
        try:
            return int(psutil.boot_time())
        except Exception:
            return 0

    @staticmethod
    def get_sd_usage() -> float:
        """Использование SD-карты в %"""
        try:
            return psutil.disk_usage('/').percent
        except Exception:
            return 0.0

    @classmethod
    def collect(cls, with_interval: float = 0.8) -> Dict:
        """
        Собирает все метрики разом.
        Возвращает словарь, готовый к включению в телеметрию.
        """
        cpu_temp = cls.get_soc_temperature()

        return {
            "cpu_percent":   cls.get_cpu_usage(interval=with_interval),
            "ram_percent":   cls.get_ram_usage(),
            "swap_percent":  cls.get_swap_usage(),
            "disk_percent":  cls.get_sd_usage(),
            "uptime_seconds": time.time() - psutil.boot_time(),  # более точно
            "cpu_temperature": cpu_temp,
        }