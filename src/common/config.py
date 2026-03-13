import os
from pathlib import Path
from typing import Dict
from dotenv import load_dotenv

load_dotenv()

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
    "command_telemetry":    "cubesat/command/telemetry",

    # Статусы подсистем
    "obc_status":           "cubesat/obc/status",
    "eps_status":           "cubesat/eps/status",
    "adcs_status":          "cubesat/adcs/status",
    "payload_status":       "cubesat/payload/status",
    "payload_data":         "cubesat/payload/data",
    "payload_photo":        "cubesat/payload/photo",
    "telemetry_data":       "cubesat/telemetry/data",

    # Телеметрия
    "telemetry":            "cubesat/telemetry",

    # Управление подсистемами (от OBC)
    "control":              "cubesat/control/#",

    # Ответы
    "response_photo":       "cubesat/response/photo/#",
}

# Пути к данным
DATA_DIR = BASE_DIR / "data"
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH = DATA_DIR / "telemetry.db"

PHOTO_RESOLUTION = (1920, 1080)          # for camera
TELEMETRY_INTERVAL_SEC = 30              # default aggregation interval
LOW_POWER_TELEMETRY_INTERVAL = 300       # in LOW_POWER

# Remote telemetry API integration
TELEMETRY_API_KEY = os.getenv("TELEMETRY_API_KEY", None)  # API key for remote telemetry server
TELEMETRY_SEND_ENABLED = int(os.getenv("TELEMETRY_SEND_ENABLED", 0))  # Flag to enable sending telemetry to remote API (0 or 1)
TELEMETRY_SEND_INTERVAL_SEC = int(os.getenv("TELEMETRY_SEND_INTERVAL_SEC", 30))  # Send interval in seconds
TELEMETRY_API_URL = os.getenv("TELEMETRY_API_URL", "http://localhost:8080")  # Remote telemetry API base URL

def get_config(key: str, default=None):
    """Получить значение из переменных окружения или дефолт"""
    return os.getenv(key.upper(), default)