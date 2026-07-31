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
_comms_cfg       = _yaml.get("comms", {})
_camera_cfg      = _yaml.get("camera", {})
_gps_cfg         = _yaml.get("gps", {})

# MQTT — environment variables override YAML values
MQTT_BROKER    = os.getenv("MQTT_BROKER",  _mqtt_cfg.get("broker",    "localhost"))
MQTT_PORT      = int(os.getenv("MQTT_PORT", _mqtt_cfg.get("port",      1883)))
MQTT_KEEPALIVE = _mqtt_cfg.get("keepalive", 60)

# Topics — all topic strings in one place; never hardcode these elsewhere
TOPICS: Dict[str, str] = {
    # Commands (ground → all services). All commands are routed through this
    # single topic; the "command" field in the payload determines the handler.
    "command":              "cubesat/command",

    # Subsystem status
    "obc_status":           "cubesat/obc/status",
    "eps_status":           "cubesat/eps/status",
    "adcs_status":          "cubesat/adcs/status",
    "payload_status":       "cubesat/payload/status",
    "payload_data":         "cubesat/payload/data",
    "payload_photo":        "cubesat/payload/photo",
    "comms_data":           "cubesat/comms/data",
}

# Data paths
DATA_DIR   = BASE_DIR / "data"
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH    = DATA_DIR / "comms.db"

# Camera
PHOTO_RESOLUTION = tuple(_camera_cfg.get("resolution", [1920, 1080]))

# GPS (A9G module, NMEA over UART — see docs/hardware-iot-node-a-52pi.md)
GPS_PORT     = os.getenv("GPS_PORT",     _gps_cfg.get("port",     "/dev/ttySC1"))
GPS_BAUDRATE = int(os.getenv("GPS_BAUDRATE", _gps_cfg.get("baudrate", 9600)))

# COMMS: main loop interval (seconds) — governs SQLite aggregation checks,
# remote API sends/command polling, and LoRa TX/RX, all on the same tick
COMMS_LOOP_INTERVAL_SEC = int(os.getenv("COMMS_LOOP_INTERVAL_SEC", _comms_cfg.get("loop_interval_sec", 30)))

# COMMS: SQLite retention — rows older than this are purged periodically
COMMS_DB_RETENTION_DAYS = int(os.getenv("COMMS_DB_RETENTION_DAYS", _comms_cfg.get("db_retention_days", 30)))

# COMMS: default on/off state of each channel at process startup.
# These are NOT persisted across restarts — a `set_comms_config` ground
# command can flip them at runtime, but a restart always resets to these.
# LoRa defaults OFF: the register-level protocol in comms/lora.py is unverified
# against real hardware (see ROADMAP.md) — flip COMMS_LORA_ENABLED=1 only after
# bench-testing that it actually transmits/receives as expected.
COMMS_API_ENABLED         = int(os.getenv("COMMS_API_ENABLED",         1))
COMMS_LORA_ENABLED        = int(os.getenv("COMMS_LORA_ENABLED",        0))
COMMS_AGGREGATION_ENABLED = int(os.getenv("COMMS_AGGREGATION_ENABLED", 1))

# COMMS: remote API — secrets/URLs via environment variables only
COMMS_API_KEY = os.getenv("COMMS_API_KEY", None)
COMMS_API_URL = os.getenv("COMMS_API_URL", "http://localhost:8080")

# COMMS: LoRa — SC16IS752 I2C bridge on the 52Pi IoT Node(A) (see docs/hardware-iot-node-a-52pi.md)
LORA_I2C_ADDRESS = int(os.getenv("LORA_I2C_ADDRESS", _comms_cfg.get("lora_i2c_address", 0x16)))

def get_config(key: str, default=None):
    """Return a value from environment variables, or default."""
    return os.getenv(key.upper(), default)
