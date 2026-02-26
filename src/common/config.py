import os
from pathlib import Path
from typing import Dict

# Базовая директория проекта
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# MQTT
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
# MQTT_USERNAME = os.getenv("MQTT_USERNAME", None)
# MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", None)
MQTT_KEEPALIVE = 60

# Топики (централизованно — меняй здесь один раз)
TOPICS: Dict[str, str] = {
    # Команды
    "command":              "cubesat/command",
    "command_photo":        "cubesat/command/photo",
    "command_payload":      "cubesat/command/payload",

    # Статусы подсистем
    "obc_status":           "cubesat/obc/status",
    "eps_status":           "cubesat/eps/status",
    "adcs_status":          "cubesat/adcs/status",
    "payload_status":       "cubesat/payload/status",
    "payload_data":         "cubesat/payload/data",

    # Телеметрия
    "telemetry":            "cubesat/telemetry",
    "telemetry_request":    "cubesat/request/telemetry",
    "telemetry_response":   "cubesat/response/telemetry",

    # Управление подсистемами (от OBC)
    "control":              "cubesat/control/#",

    # Ответы
    "response_photo":       "cubesat/response/photo/#",
}

# Пути к данным
DATA_DIR = BASE_DIR / "data"
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH = DATA_DIR / "telemetry.db"

# Другие константы
PHOTO_RESOLUTION = (1920, 1080)          # для камеры
TELEMETRY_INTERVAL_SEC = 30              # базовый интервал агрегации
LOW_POWER_TELEMETRY_INTERVAL = 300       # в LOW_POWER

def get_config(key: str, default=None):
    """Получить значение из переменных окружения или дефолт"""
    return os.getenv(key.upper(), default)