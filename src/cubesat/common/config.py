"""Runtime configuration: YAML defaults, environment overrides, resolved paths.

Three tiers, by how often a value changes and how secret it is:

* ``config/config.yaml``   — runtime defaults, committed
* ``config/profiles.yaml`` — platform profiles, committed (see ``profiles.py``)
* environment / ``.env``   — per-deployment values and every secret, never committed

Environment variables win over YAML. Secrets are environment-only: there is no
YAML key for an API token, deliberately, so one cannot be committed by accident.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()


def _find_config_dir() -> Path:
    """Locate the configuration directory.

    ``CUBESAT_CONFIG_DIR`` wins. Otherwise ``/etc/cubesat`` if it exists (a
    packaged install), else the repo's own ``config/`` (a development checkout
    or an editable install). Resolving relative to ``__file__`` alone would
    break the moment the package is installed as a real wheel into
    site-packages, which is why /etc is checked first.
    """
    override = os.getenv("CUBESAT_CONFIG_DIR")
    if override:
        return Path(override)
    etc = Path("/etc/cubesat")
    if etc.is_dir():
        return etc
    # src/cubesat/common/config.py -> repo root
    return Path(__file__).resolve().parents[3] / "config"


CONFIG_DIR = _find_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.yaml"
PROFILES_FILE = CONFIG_DIR / "profiles.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


_yaml = _load_yaml(CONFIG_FILE)
_mqtt = _yaml.get("mqtt", {})
_hb = _yaml.get("heartbeat", {})

# ── MQTT ────────────────────────────────────────────────────────────────────
MQTT_BROKER: str = os.getenv("MQTT_BROKER", _mqtt.get("broker", "localhost"))
MQTT_PORT: int = int(os.getenv("MQTT_PORT", _mqtt.get("port", 1883)))
MQTT_KEEPALIVE: int = int(_mqtt.get("keepalive", 60))

# ── Liveness ────────────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL_SEC: float = float(_hb.get("interval_sec", 10))
#: Consecutive misses before OBC declares a subsystem lost.
HEARTBEAT_MISS_THRESHOLD: int = int(_hb.get("miss_threshold", 3))

# ── Cadence table (mission state -> poll interval), see cadence.py ──────────
CADENCE: dict[str, dict[str, float]] = _yaml.get("cadence", {})

#: How often the radio transmits, per mission state — distinct from how often
#: COMMS wakes. Silencing the whole service in SAFE also silenced *receiving*,
#: and SAFE is reachable from FLIGHT, where the radio is the only way in.
BEACON_INTERVALS: dict[str, float] = {
    state: float(value) for state, value in (_yaml.get("beacon", {}) or {}).items()
}

# ── Paths ───────────────────────────────────────────────────────────────────
# Runtime data lives outside the checkout. systemd creates these directories
# from the units' StateDirectory/RuntimeDirectory/LogsDirectory, so nothing here
# ever calls mkdir on a system path. Point CUBESAT_DATA_DIR at ./data for
# development on a laptop.
DATA_DIR = Path(os.getenv("CUBESAT_DATA_DIR", "/var/lib/cubesat"))
RUN_DIR = Path(os.getenv("CUBESAT_RUN_DIR", "/run/cubesat"))
LOG_DIR = Path(os.getenv("CUBESAT_LOG_DIR", "/var/log/cubesat"))

DB_PATH = DATA_DIR / "comms.db"
DIAG_DB_PATH = DATA_DIR / "diag.db"
PHOTOS_DIR = DATA_DIR / "photos"
#: Written by HOSTD after every profile application. Informational only —
#: nothing reads it to decide anything. See "the profile is not persisted".
LAST_PROFILE_FILE = DATA_DIR / "last-profile"
DASHBOARD_ROOT = Path(os.getenv("CUBESAT_DASHBOARD_ROOT", str(DATA_DIR / "dashboard")))

#: All I2C traffic serialises on this lock: four processes share one bus clamped
#: to 10 kHz, where a single read costs tens of milliseconds.
I2C_LOCK_FILE = RUN_DIR / "i2c.lock"
#: HOSTD's break-glass channel, for when the broker itself is down.
HOSTD_SOCKET = RUN_DIR / "hostd.sock"

# ── Hardware ────────────────────────────────────────────────────────────────
#: 1 selects the mock HAL: the whole stack then runs with no Raspberry Pi.
MOCK_HARDWARE: bool = os.getenv("CUBESAT_MOCK_HARDWARE", "0") == "1"

#: 1 selects HOSTD's no-op executor: nothing is started, stopped or
#: reconfigured, and every action is only recorded. A separate axis from
#: MOCK_HARDWARE on purpose — mocked sensors and a mocked host are independent
#: choices, and a laptop running the whole stack needs both.
MOCK_HOST: bool = os.getenv("CUBESAT_MOCK_HOST", "0") == "1"

I2C_BUS: int = int(os.getenv("CUBESAT_I2C_BUS", 1))

#: The Meshtastic node. 115200 is not a preference: the meshtastic Python
#: library opens the port hard-coded at that rate.
LORA_PORT: str = os.getenv("LORA_PORT", "/dev/serial0")
LORA_BAUDRATE: int = int(os.getenv("LORA_BAUDRATE", 115200))

#: Meshtastic channel index. If this and the ground station disagree, messages
#: are transmitted and received perfectly and simply never meet — the hardest
#: kind of radio fault to diagnose — so it belongs in configuration next to the
#: port rather than in a driver constant.
LORA_CHANNEL_INDEX: int = int(os.getenv("LORA_CHANNEL_INDEX", 1))

# ── Services ────────────────────────────────────────────────────────────────
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", 8080))

_dhs = _yaml.get("dhs", {})

#: The floor on how often an attitude sample is recorded, in seconds. One
#: ceiling across every profile and state — DIAG runs ADCS at 10 Hz, and the
#: card is what pays for that, not the bus.
DHS_ATTITUDE_MIN_INTERVAL_SEC: float = float(
    os.getenv("DHS_ATTITUDE_MIN_INTERVAL_SEC", _dhs.get("attitude_min_interval_sec", 1.0))
)

#: How many attitude samples may wait in memory for a write that is failing.
#: Bounded because surviving a failing card is this service's job, and an
#: unbounded buffer would turn that into an unbounded process.
DHS_ATTITUDE_BUFFER: int = int(_dhs.get("attitude_buffer", 7200))

_retention = _yaml.get("retention", {})
DHS_RETENTION_DAYS: int = int(os.getenv("DHS_RETENTION_DAYS", _retention.get("days", 30)))

#: Whether purging a mission also removes its photo directory. Off means the
#: database stays bounded and the SD card does not — see the note in
#: config.yaml, and PHOTOS_MIN_FREE_MB, which is the same headroom from the
#: writing side.
DHS_PURGE_PHOTOS: bool = bool(_retention.get("purge_photos", True))

#: Distance floor for one track segment. Receiver noise otherwise accumulates
#: kilometres while the satellite sits still, and a confident wrong number is
#: the failure this telemetry keeps refusing.
DHS_MIN_SEGMENT_M: float = float(_retention.get("min_segment_m", 5.0))

LOG_LEVEL: str = os.getenv("CUBESAT_LOG_LEVEL", _yaml.get("logging", {}).get("level", "INFO"))

# ── Payload ─────────────────────────────────────────────────────────────────
_camera = _yaml.get("camera", {})
_science = _yaml.get("science", {})

_res = _camera.get("resolution", [1920, 1080])
PHOTO_RESOLUTION: tuple[int, int] = (int(_res[0]), int(_res[1]))
MIN_TIMELAPSE_INTERVAL_SEC: float = float(_camera.get("min_timelapse_interval_sec", 1.0))

#: Free space below which PAYLOAD stops writing images. Paired deliberately with
#: DHS's retention headroom: the camera's floor and the recorder's horizon are
#: the same number seen from two sides, and a floor set below the recorder's
#: needs lets the card fill anyway.
PHOTOS_MIN_FREE_MB: int = int(_yaml.get("photos", {}).get("min_free_mb", 512))

#: SEN0501 board revision, "v1" or "v3". None means the UV index is withheld:
#: the two revisions read one raw register with formulas that disagree by a
#: factor of forty, and guessing would produce a plausible measurement of
#: nothing. See ROADMAP V7.
SEN0501_REVISION: str | None = os.getenv(
    "CUBESAT_SEN0501_REVISION", _science.get("sen0501_revision")
) or None
