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
# Runtime data lives outside the checkout. config/tmpfiles.d/cubesat.conf creates
# these directories, so nothing here ever calls mkdir on a system path.
#
# systemd-tmpfiles rather than the units' StateDirectory/RuntimeDirectory/
# LogsDirectory, and that is deliberate: those re-apply the *starting* unit's own
# User= to the whole tree on every start, and two users share these three paths —
# cubesat-hostd is root, every other service is `cubesat`. Ownership then followed
# whichever unit restarted last, and a HOSTD restart handed all three directories
# to root:root (found on the first hardware run, 2026-08-31). The units name the
# paths in ReadWritePaths= instead, which is what re-opens them under
# ProtectSystem=strict. Point CUBESAT_DATA_DIR at ./data for development on a
# laptop.
DATA_DIR = Path(os.getenv("CUBESAT_DATA_DIR", "/var/lib/cubesat"))
RUN_DIR = Path(os.getenv("CUBESAT_RUN_DIR", "/run/cubesat"))
LOG_DIR = Path(os.getenv("CUBESAT_LOG_DIR", "/var/log/cubesat"))

DB_PATH = DATA_DIR / "comms.db"
DIAG_DB_PATH = DATA_DIR / "diag.db"
PHOTOS_DIR = DATA_DIR / "photos"


def photos_root_for(database: str | Path, root: Path | None = None) -> Path:
    """Which photo root a mission recorded in ``database`` files its frames under.

    There are two databases and they number their missions independently, both
    from 1, so one ``photos/<mission_id>/`` for both is a collision: a DIAG
    bench run and a FLIGHT trip that happen to be mission 3 share a directory,
    and deleting the first takes the second's photographs with it.

    The mission database keeps the bare ``photos/`` it has always had — nothing
    on an existing card moves — and every other database gets a root of its
    own, ``photos-diag/`` for ``diag.db``. That is deliberately a sibling rather
    than ``photos/<db>/<mission_id>/``: the leaf name stays a plain run of
    digits, which is the allowlist ``dhs/retention.py`` fences the most
    destructive code in this project with, and DIAG is a handful of bench runs
    that do not deserve a level of nesting in front of every trip ever taken.

    Derived rather than tabulated, so a third database cannot quietly inherit
    ``comms.db``'s directory by being forgotten here. The name comes from
    ``Path.stem``, which is always a single path segment: a database path
    arriving off the wire — PAYLOAD reads one from ``dhs_status`` — can name a
    root, never a place.

    ``root`` is the mission database's root for callers that hold their own —
    the dashboard is handed one — and every sibling is derived from *that*,
    so a caller with a redirected root has both of its roots redirected rather
    than one of each. It defaults to ``PHOTOS_DIR``, read at call time.
    """
    base = root if root is not None else PHOTOS_DIR
    path = Path(database)
    if path.name == DB_PATH.name:
        return base
    return base.parent / f"{base.name}-{path.stem}"


#: Written by HOSTD after every profile application, as JSON. It never decides
#: whether to restore a profile — only *which* one, and only after the physical
#: evidence has already said yes: no mains at boot. See "the profile is not
#: persisted" in docs/concept.md, and ``obc/resume.py`` for the rule that reads
#: it. Everything else about it is information: ``cubesat status`` prints it.
LAST_PROFILE_FILE = DATA_DIR / "last-profile"
DASHBOARD_ROOT = Path(os.getenv("CUBESAT_DASHBOARD_ROOT", str(DATA_DIR / "dashboard")))

#: All I2C traffic serialises on this lock: four processes share one bus clamped
#: to 10 kHz, where a single read costs tens of milliseconds.
I2C_LOCK_FILE = RUN_DIR / "i2c.lock"
#: HOSTD's break-glass channel, for when the broker itself is down.
HOSTD_SOCKET = RUN_DIR / "hostd.sock"
#: Where a photograph goes when no mission is open. Under RUN_DIR because /run is
#: a tmpfs: in DEMO and EXPO the frame is published as pixels and deleted, and
#: the SD card is never touched. It replaced photos/unfiled/, which retention was
#: never allowed to remove and which therefore only grew (decided 2026-09-01).
PHOTO_SCRATCH_DIR = RUN_DIR / "photo"

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

_dashboard = _yaml.get("dashboard", {})

#: How many published telemetry rows DASHBOARD holds in memory. This is the
#: charts' whole history in the profiles that do not record — see
#: ``dashboard/live.py`` for why the ring is on the satellite rather than in the
#: browser, and ``config/config.yaml`` for what the number buys.
DASHBOARD_LIVE_ROWS: int = int(
    os.getenv("DASHBOARD_LIVE_ROWS", _dashboard.get("live_history_rows", 720))
)

_eps = _yaml.get("eps", {})

#: The sliding window, in seconds, over which EPS fits the voltage slope. There
#: is no rate register on this gauge; see ``eps/slopes.py`` for why a fitted
#: slope and not a difference. The name is the older one because the published
#: ``charge_rate`` is this same slope expressed in percent per hour.
EPS_CHARGE_RATE_WINDOW_SEC: float = float(
    os.getenv("EPS_CHARGE_RATE_WINDOW_SEC", _eps.get("charge_rate_window_sec", 600.0))
)
#: How much history the window must hold before a rate is published at all.
#: Below this the rate is null and the power policy falls back to the mains pin.
EPS_CHARGE_RATE_MIN_SPAN_SEC: float = float(
    os.getenv("EPS_CHARGE_RATE_MIN_SPAN_SEC", _eps.get("charge_rate_min_span_sec", 300.0))
)
#: The much shorter window over which EPS takes the *median* of the terminal
#: voltage, which is the level every power threshold compares. See
#: ``eps/slopes.py`` -> ``MedianWindow``: the thresholds are volts now, and a
#: voltage moves tens of millivolts when the camera pipeline starts.
EPS_LEVEL_WINDOW_SEC: float = float(
    os.getenv("EPS_LEVEL_WINDOW_SEC", _eps.get("level_window_sec", 120.0))
)

_resume = _yaml.get("resume", {})

#: How many resumes in a row read as a boot loop rather than as a flight. See
#: ``obc/resume.py``; the fence is a lifetime rather than a counter, so this
#: counts *short* sessions in a row and not resumes in general.
RESUME_MAX_CONSECUTIVE: int = int(
    os.getenv("CUBESAT_RESUME_MAX_CONSECUTIVE", _resume.get("max_consecutive", 3))
)

#: How long a resumed session must live before the consecutive count is cleared.
RESUME_SETTLE_SEC: float = float(
    os.getenv("CUBESAT_RESUME_SETTLE_SEC", _resume.get("settle_sec", 300.0))
)

#: How long OBC waits for the first ``eps_status`` before giving up on resuming.
#: A missing measurement is not a measurement of no mains.
RESUME_EVIDENCE_TIMEOUT_SEC: float = float(
    os.getenv("CUBESAT_RESUME_EVIDENCE_TIMEOUT_SEC", _resume.get("evidence_timeout_sec", 60.0))
)

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

#: How many radio events may wait in memory for a write that is failing.
#: Radio traffic is sparse — a beacon a minute in NOMINAL — so a few hundred
#: covers hours; bounded for the same reason the attitude buffer is.
DHS_RADIO_BUFFER: int = int(_dhs.get("radio_buffer", 512))

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

#: How long the camera stays open after its last use, in seconds. An open
#: Picamera2 runs its ISP loops continuously and that is SoC heat for nothing
#: on a satellite that photographs on demand; reopening costs about a second.
#: 0 disables the idle close and keeps the camera open once first used.
CAMERA_IDLE_CLOSE_SEC: float = float(_camera.get("idle_close_sec", 60.0))

#: Free space below which PAYLOAD stops writing images. Paired deliberately with
#: DHS's retention headroom: the camera's floor and the recorder's horizon are
#: the same number seen from two sides, and a floor set below the recorder's
#: needs lets the card fill anyway.
_photos = _yaml.get("photos", {})
PHOTOS_MIN_FREE_MB: int = int(_photos.get("min_free_mb", 512))

#: How often an open mission photographs by itself, in seconds. There is no
#: ground command for this and no interval on the wire: while a mission is open
#: the camera fires on this cadence, and it stops when the mission does. 300 s
#: on foot is a frame every few hundred metres.
PHOTO_MISSION_INTERVAL_SEC: float = float(
    os.getenv("PHOTO_MISSION_INTERVAL_SEC", _photos.get("mission_interval_sec", 300.0))
)

#: SEN0501 board revision, "v1" or "v3". None means the UV index is withheld:
#: the two revisions read one raw register with formulas that disagree by a
#: factor of forty, and guessing would produce a plausible measurement of
#: nothing. See ROADMAP V7.
SEN0501_REVISION: str | None = os.getenv(
    "CUBESAT_SEN0501_REVISION", _science.get("sen0501_revision")
) or None
