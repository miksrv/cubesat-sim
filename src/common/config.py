import os
import yaml
from pathlib import Path
from typing import Dict
from dotenv import load_dotenv

load_dotenv()

# Project root directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Load config/config.yaml — provides defaults overrideable by environment variables
_CONFIG_FILE = BASE_DIR / "config" / "config.yaml"

def _load_yaml_config() -> dict:
    if _CONFIG_FILE.exists():
        with open(_CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}

_yaml            = _load_yaml_config()
_mqtt_cfg        = _yaml.get("mqtt", {})
_telemetry_cfg   = _yaml.get("telemetry", {})
_camera_cfg      = _yaml.get("camera", {})

# MQTT — environment variables override YAML values
MQTT_BROKER    = os.getenv("MQTT_BROKER",  _mqtt_cfg.get("broker",    "localhost"))
MQTT_PORT      = int(os.getenv("MQTT_PORT", _mqtt_cfg.get("port",      1883)))
MQTT_KEEPALIVE = _mqtt_cfg.get("keepalive", 60)

# Topics — all topic strings in one place; never hardcode these elsewhere
TOPICS: Dict[str, str] = {
    # Commands (ground → subsystem)
    "command":              "cubesat/command",
    "command_photo":        "cubesat/command/photo",
    "command_telemetry":    "cubesat/command/telemetry",

    # Subsystem status
    "obc_status":           "cubesat/obc/status",
    "eps_status":           "cubesat/eps/status",
    "adcs_status":          "cubesat/adcs/status",
    "payload_status":       "cubesat/payload/status",
    "payload_data":         "cubesat/payload/data",
    "payload_photo":        "cubesat/payload/photo",
    "telemetry_data":       "cubesat/telemetry/data",
}

# Data paths
DATA_DIR   = BASE_DIR / "data"
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH    = DATA_DIR / "telemetry.db"

# Camera
PHOTO_RESOLUTION = tuple(_camera_cfg.get("resolution", [1920, 1080]))

# Telemetry intervals (seconds)
TELEMETRY_INTERVAL_SEC       = _telemetry_cfg.get("interval_sec",           30)
LOW_POWER_TELEMETRY_INTERVAL = _telemetry_cfg.get("low_power_interval_sec", 300)

# Remote telemetry API integration — secrets/URLs via environment variables only
TELEMETRY_API_KEY           = os.getenv("TELEMETRY_API_KEY",           None)
TELEMETRY_SEND_ENABLED      = int(os.getenv("TELEMETRY_SEND_ENABLED",  0))
TELEMETRY_SEND_INTERVAL_SEC = int(os.getenv("TELEMETRY_SEND_INTERVAL_SEC", 30))
TELEMETRY_API_URL           = os.getenv("TELEMETRY_API_URL",           "http://localhost:8080")

def get_config(key: str, default=None):
    """Return a value from environment variables, or default."""
    return os.getenv(key.upper(), default)
