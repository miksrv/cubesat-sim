# CubeSat Sim

CubeSat Sim is an educational simulation platform for CubeSat satellite systems. It models the onboard software of a real CubeSat as a set of independent Python services, each representing a physical subsystem, communicating exclusively over MQTT — the same way hardware modules communicate over a spacecraft bus.

The platform runs on a Raspberry Pi with real hardware sensors, or can be adapted for local development by mocking hardware dependencies.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Subsystems](#subsystems)
  - [OBC — On-Board Computer](#obc--on-board-computer)
  - [EPS — Electrical Power System](#eps--electrical-power-system)
  - [ADCS — Attitude Determination and Control System](#adcs--attitude-determination-and-control-system)
  - [Payload](#payload)
  - [Telemetry Aggregator](#telemetry-aggregator)
  - [Common Infrastructure](#common-infrastructure)
- [MQTT Topic Reference](#mqtt-topic-reference)
- [Message Payloads](#message-payloads)
- [Data Flows](#data-flows)
- [Directory Structure](#directory-structure)
- [Hardware](#hardware)
- [Setup and Running](#setup-and-running)
- [Configuration](#configuration)
- [Logs](#logs)
- [Documentation](#documentation)

---

## Architecture Overview

Each subsystem is an independent Python process. All inter-process communication happens over a local MQTT broker (mosquitto). No service calls another directly.

```
┌──────────────────────────────────────────────────────────────────┐
│                     MQTT Broker (mosquitto)                       │
│                                                                  │
│  cubesat/command            cubesat/obc/status    (retained)     │
│  cubesat/command/photo      cubesat/eps/status    (retained)     │
│  cubesat/command/telemetry  cubesat/adcs/status                  │
│                             cubesat/payload/status               │
│                             cubesat/payload/data                 │
│                             cubesat/payload/photo                │
│                             cubesat/telemetry/data               │
└───────────┬──────────────────────────────────────────────────────┘
            │ publish / subscribe
   ┌─────────┴──────────────────────────────────────────────┐
   ▼         ▼          ▼             ▼              ▼
┌──────┐  ┌──────┐  ┌──────┐  ┌───────────┐  ┌───────────┐
│ OBC  │  │ EPS  │  │ ADCS │  │  Payload  │  │ Telemetry │
│      │  │      │  │      │  │           │  │           │
│State │  │Power │  │ IMU  │  │ Camera    │  │ Aggregator│
│Mach. │  │Mon.  │  │ AHRS │  │ Science   │  │ SQLite DB │
└──────┘  └──────┘  └──────┘  └───────────┘  └───────────┘
             │           │           │
          I2C/GPIO      I2C         I2C / CSI
          MAX17048    QMI8658      LPS22HB
          X728 UPS    AK09918      SHTC3
                                   Picamera2
                      Raspberry Pi Hardware
```

---

## Subsystems

### OBC — On-Board Computer

**Path:** `src/obc/` | **MQTT client ID:** `obc`

The central authority of the simulation. It is the only service that manages mission state. All other services react to `cubesat/obc/status` to know the current mission phase.

#### State Machine

```
         ┌──────────────────────────────────────────────────────┐
         │                  enter_safe_mode (any state)          │
         ▼                                                       │
       BOOT ──auto_deploy──► DEPLOY ──deployment_complete──► NOMINAL
                                                          │       ▲
                                              start_science│       │end_science
                                                          ▼       │
                                                       SCIENCE ───┘
                                                          │
                                        enter_low_power (battery < 40%)
                                                          ▼
                                                      LOW_POWER
                                                          │
                                          enter_safe_mode (battery < 20%)
                                                          ▼
                                                        SAFE
                                                          │
                                              recover (external power restored)
                                                          ▼
                                                       NOMINAL
```

**Transition rules** (implemented in `handlers.py`):

| Condition | Trigger | Result |
|-----------|---------|--------|
| Battery < 40% | EPS status message | → `LOW_POWER` (if not already `LOW_POWER` or `SAFE`) |
| Battery < 20% | EPS status message | → `SAFE` (from any state) |
| External power restored | EPS status message | → `NOMINAL` (from `LOW_POWER` or `SAFE`) |
| `science_start` command | Ground command | `NOMINAL` → `SCIENCE` |
| `science_stop` command | Ground command | `SCIENCE` → `NOMINAL` |
| `safe_mode` command | Ground command | any → `SAFE` |
| `recover` command | Ground command | `SAFE` → `NOMINAL` |

**Key files:**

| File | Responsibility |
|------|----------------|
| `main.py` | MQTT setup, heartbeat publish loop (30 s) |
| `state_machine.py` | `CubeSatStateMachine` — state definitions, transitions, state publishing |
| `handlers.py` | `OBCMessageHandlers` — EPS status reactions, ground command parsing |

---

### EPS — Electrical Power System

**Path:** `src/eps/` | **MQTT client ID:** `eps`

Reads battery state from the MAX17048 fuel gauge IC (I2C address `0x36`) and external power state from a GPIO pin connected to the X728 UPS Power Loss Detection (PLD) pin. Publishes status every 30 seconds with `retain=True`.

**Key files:**

| File | Responsibility |
|------|----------------|
| `main.py` | MQTT setup, publish loop (30 s) |
| `power_monitor.py` | `EPSMonitor` — MAX17048 I2C reads, GPIO external-power read |

---

### ADCS — Attitude Determination and Control System

**Path:** `src/adcs/` | **MQTT client ID:** `adcs`

Reads the QMI8658 IMU (accelerometer + gyroscope, I2C) and AK09918 magnetometer (I2C). Fuses the three sensor axes using a Mahony complementary filter to produce roll/pitch/yaw angles. Publishes at 2 Hz (every 500 ms).

Note: this service is currently sensing-only. Actuator control (reaction wheels, magnetorquers) is not implemented.

**Key files:**

| File | Responsibility |
|------|----------------|
| `main.py` | MQTT setup, IMU poll loop (0.5 s) |
| `common/imu_qmi8658_ak09918.py` | `IMU` — QMI8658 + AK09918 I2C driver, Mahony AHRS |

---

### Payload

**Path:** `src/payload/` | **MQTT client ID:** `payload`

Combines two responsibilities:

1. **Camera** — captures a JPEG photo via Picamera2 on demand. Responds to `cubesat/command/photo` messages. Photo capture is gated: it is only permitted when the OBC is in `NOMINAL` state. The JPEG is Base64-encoded and published to `cubesat/payload/photo`.

2. **Science** — polls an LPS22HB barometric pressure + temperature sensor (I2C) and a SHTC3 humidity + temperature sensor (I2C) every 60 seconds and publishes the readings to `cubesat/payload/data`.

**Key files:**

| File | Responsibility |
|------|----------------|
| `main.py` | MQTT wiring, OBC state tracking, command routing, science poll loop (60 s) |
| `camera.py` | `PayloadCamera` — Picamera2 integration, photo storage |
| `science.py` | `ScienceCollector` — LPS22HB + SHTC3 I2C reads with CRC verification |

---

### Telemetry Aggregator

**Path:** `src/telemetry/` | **MQTT client ID:** `telemetry`

A passive aggregator. Subscribes to all subsystem status topics and maintains an in-memory cache of the latest reading from each. Every 30 seconds it assembles a unified telemetry packet from the cache, appends system health metrics (CPU, RAM, disk, temperature), writes the packet to a SQLite database, and publishes the packet to `cubesat/telemetry/data`.

Also responds to on-demand telemetry requests on `cubesat/command/telemetry`.

**SQLite schema** (`data/telemetry.db`, table `telemetry_log`):

| Column group | Fields |
|---|---|
| Timing | `id`, `timestamp`, `iso_time` |
| OBC | `obc_state` |
| EPS | `battery`, `voltage`, `external_power` |
| ADCS | `roll`, `pitch`, `yaw`, `imu_temp`, `accel_x/y/z`, `gyro_x/y/z` |
| Payload science | `temperature`, `humidity`, `pressure` |
| System health | `cpu_percent`, `ram_percent`, `swap_percent`, `disk_percent`, `uptime_seconds`, `cpu_temperature` |
| Raw | `raw_json` (full packet as JSON string) |

**Key files:**

| File | Responsibility |
|------|----------------|
| `main.py` | Entry point, logging setup |
| `aggregator.py` | `TelemetryAggregator` — subscriptions, data cache, packet assembly, SQLite writes, main loop |

---

### Common Infrastructure

**Path:** `src/common/`

Shared code imported by all services.

| File | Responsibility |
|------|----------------|
| `config.py` | All constants: MQTT broker, port, keepalive, all topic strings (`TOPICS` dict), data paths, telemetry intervals |
| `mqtt_client.py` | `get_mqtt_client(client_id)` — MQTTv5 factory with exponential backoff reconnect |
| `logging_setup.py` | `setup_logging(service_name)` — rotating file handler (10 MB × 5 files) + console, writes to `/var/log/cubesat/` |
| `system_metrics.py` | `SystemMetricsCollector` — CPU / RAM / swap / disk / uptime / CPU temperature via `psutil` |
| `utils.py` | `crc16_ccitt()`, `json_dumps_pretty()`, `timestamp_iso()`, `ensure_dir()` |
| `imu_qmi8658_ak09918.py` | `IMU` class — QMI8658 + AK09918 I2C driver and Mahony AHRS (used by ADCS) |

---

## MQTT Topic Reference

All topic strings are defined in `src/common/config.py` (`TOPICS` dict). Always import from there — never hardcode topic strings.

| `TOPICS` key | Topic string | Direction | Publisher | Subscribers |
|---|---|---|---|---|
| `command` | `cubesat/command` | Ground → OBC | Ground station | OBC |
| `command_photo` | `cubesat/command/photo` | Ground → Payload | Ground station | Payload |
| `command_telemetry` | `cubesat/command/telemetry` | Ground → Telemetry | Ground station | Telemetry |
| `obc_status` | `cubesat/obc/status` | OBC → All | OBC | Payload, Telemetry |
| `eps_status` | `cubesat/eps/status` | EPS → OBC, Telemetry | EPS | OBC, Telemetry |
| `adcs_status` | `cubesat/adcs/status` | ADCS → Telemetry | ADCS | Telemetry |
| `payload_status` | `cubesat/payload/status` | Payload → All | Payload | (ground tools) |
| `payload_data` | `cubesat/payload/data` | Payload → Telemetry | Payload | Telemetry |
| `payload_photo` | `cubesat/payload/photo` | Payload → Ground | Payload | (ground tools) |
| `telemetry_data` | `cubesat/telemetry/data` | Telemetry → Ground | Telemetry | (ground tools) |

`obc_status` and `eps_status` are published with `retain=True` so newly connected services immediately receive the last known state.

---

## Message Payloads

### `cubesat/obc/status`
```json
{
  "state": "NOMINAL",
  "ts": "2026-03-13T12:00:00.000000"
}
```

### `cubesat/eps/status`
```json
{
  "timestamp": 1741863600.0,
  "battery": 87.5,
  "voltage": 4.123,
  "external_power": true
}
```

### `cubesat/adcs/status`
```json
{
  "timestamp": 1741863600.0,
  "roll": 1.23,
  "pitch": -0.45,
  "yaw": 178.9,
  "imu_temp": 34.5,
  "accel_g": {"x": 0.01, "y": 0.02, "z": 0.99},
  "gyro_dps": {"x": 0.1, "y": -0.2, "z": 0.05}
}
```

### `cubesat/payload/data`
```json
{
  "timestamp": 1741863600.0,
  "temperature": 23.4,
  "humidity": 45.2,
  "pressure": 1013.25
}
```

### `cubesat/payload/photo` (success)
```json
{
  "request_id": "req_001",
  "status": "SUCCESS",
  "timestamp": 1741863600.0,
  "file": "photo_20260313_120000.jpg",
  "photo_base64": "<base64-encoded JPEG>"
}
```

### `cubesat/payload/photo` (error)
```json
{
  "request_id": "req_001",
  "status": "ERROR",
  "reason": "OBC state is SCIENCE, photo only allowed in NOMINAL"
}
```

### Ground command to `cubesat/command`
```json
{"command": "science_start"}
{"command": "science_stop"}
{"command": "safe_mode"}
{"command": "recover"}
```

### Ground command to `cubesat/command/photo`
```json
{"request_id": "req_001", "overlay": false}
```

---

## Data Flows

### SCIENCE mode cycle

```
1. Ground sends:  {"command": "science_start"}  →  cubesat/command

2. OBC receives command, transitions NOMINAL → SCIENCE
   Publishes: {"state": "SCIENCE", "ts": "..."}  →  cubesat/obc/status  (retain)

3. Payload reads obc_state = "SCIENCE" — science poll continues as normal.
   Every 60 s: reads LPS22HB + SHTC3  →  cubesat/payload/data

4. ADCS: every 500 ms: reads QMI8658 + AK09918, runs AHRS  →  cubesat/adcs/status

5. EPS: every 30 s: reads MAX17048 + GPIO  →  cubesat/eps/status

6. Telemetry aggregator: every 30 s:
   Merges cached OBC/EPS/ADCS/payload/system data → writes row to telemetry.db
   Publishes packet  →  cubesat/telemetry/data

7. Ground sends:  {"command": "science_stop"}  →  cubesat/command
   OBC: SCIENCE → NOMINAL
```

### Photo request

```
1. Ground sends: {"request_id": "req_001", "overlay": false}  →  cubesat/command/photo

2. Payload checks obc_state:
   - Not NOMINAL → publishes error  →  cubesat/payload/photo
   - NOMINAL     → captures JPEG via Picamera2
                   saves to data/photos/
                   Base64-encodes image

3. Publishes full response (with photo_base64)  →  cubesat/payload/photo
```

### Low-power event

```
1. EPS reads battery = 38%  →  cubesat/eps/status

2. OBC handler: 38% < 40% threshold → triggers enter_low_power
   State: NOMINAL → LOW_POWER
   Publishes: {"state": "LOW_POWER", ...}  →  cubesat/obc/status

3. If battery continues to drop to 18%:
   OBC handler: 18% < 20% → triggers enter_safe_mode
   State: LOW_POWER → SAFE
   Publishes: {"state": "SAFE", ...}  →  cubesat/obc/status

4. When external power is connected (GPIO pin HIGH):
   OBC handler: calls recover()
   State: SAFE → NOMINAL
```

---

## Directory Structure

```
cubesat-sim/
├── src/
│   ├── obc/                       # On-Board Computer — state machine + handlers
│   │   ├── __init__.py
│   │   ├── main.py                # Service entry point, MQTT setup, heartbeat loop
│   │   ├── state_machine.py       # CubeSatStateMachine using `transitions` library
│   │   └── handlers.py            # OBCMessageHandlers — EPS reactions, ground commands
│   │
│   ├── eps/                       # Electrical Power System
│   │   ├── __init__.py
│   │   ├── main.py                # Service entry point, 30 s publish loop
│   │   └── power_monitor.py       # EPSMonitor — MAX17048 I2C + X728 GPIO reads
│   │
│   ├── adcs/                      # Attitude Determination and Control
│   │   ├── __init__.py
│   │   └── main.py                # Service entry point, 500 ms IMU poll loop
│   │
│   ├── payload/                   # Camera + science sensors
│   │   ├── __init__.py
│   │   ├── main.py                # Service entry point, command router, science poll loop
│   │   ├── camera.py              # PayloadCamera — Picamera2, photo storage
│   │   └── science.py             # ScienceCollector — LPS22HB + SHTC3 I2C reads
│   │
│   ├── telemetry/                 # Telemetry aggregator
│   │   ├── __init__.py
│   │   ├── main.py                # Service entry point
│   │   └── aggregator.py          # TelemetryAggregator — cache, packet builder, SQLite
│   │
│   └── common/                    # Shared code used by all services
│       ├── __init__.py
│       ├── config.py              # All constants: broker, ports, TOPICS dict, paths
│       ├── mqtt_client.py         # get_mqtt_client() factory — MQTTv5 + reconnect
│       ├── logging_setup.py       # setup_logging() — rotating file + console handler
│       ├── system_metrics.py      # SystemMetricsCollector — CPU/RAM/disk/temp
│       ├── utils.py               # crc16_ccitt, json_dumps_pretty, timestamp_iso
│       └── imu_qmi8658_ak09918.py # IMU driver + Mahony AHRS (used by ADCS)
│
├── systemd/                       # systemd unit files
│   ├── cubesat-obc.service
│   ├── cubesat-eps.service
│   ├── cubesat-adcs.service
│   ├── cubesat-payload.service
│   └── cubesat-telemetry.service
│
├── scripts/
│   ├── install.sh                 # Create venv, install deps, install + start systemd units
│   ├── start.sh                   # Start and enable all services
│   └── stop.sh                    # Stop and disable all services
│
├── data/                          # Runtime data (created on first run)
│   ├── photos/                    # JPEG files from payload camera
│   └── telemetry.db               # SQLite database (telemetry_log table)
│
├── docs/
│   ├── architecture.md            # Detailed architecture reference
│   ├── code_smells.md             # Known issues and technical debt
│   └── refactoring_plan.md        # Prioritised improvement plan with code examples
│
├── ROADMAP.md                     # Feature tracker and improvement backlog
├── CLAUDE.md                      # AI assistant context for this repo
├── requirements.txt
└── README.md
```

---

## Hardware

The simulation targets Raspberry Pi. The following hardware is required for full operation:

| Component | Interface | Used by | Library |
|-----------|-----------|---------|---------|
| MAX17048 fuel gauge (LiPo monitor) | I2C (`0x36`) | EPS | `smbus2` |
| X728 UPS — PLD GPIO pin | GPIO | EPS | `RPi.GPIO` |
| QMI8658 IMU (accel + gyro) | I2C | ADCS | `smbus2` |
| AK09918 magnetometer | I2C | ADCS | `smbus2` |
| LPS22HB barometric pressure + temperature sensor | I2C | Payload | `smbus2` |
| SHTC3 humidity + temperature sensor | I2C | Payload | `lgpio` |
| Camera module (any Picamera2-compatible) | CSI | Payload | `picamera2` |
| Mosquitto MQTT broker | localhost:1883 | All | `paho-mqtt` |

> **Non-Pi development:** All hardware libraries are imported at module level, so services will fail to import on a non-Raspberry Pi machine. Hardware mocking is on the roadmap (see `ROADMAP.md` items H1–H7).

---

## Setup and Running

### Prerequisites

- Raspberry Pi running Raspberry Pi OS
- `mosquitto` MQTT broker installed and running (`sudo apt install mosquitto`)
- Python 3.9+

### Install dependencies and start as systemd services (recommended)

```bash
git clone <repo-url> /home/mik/cubesat-sim
cd /home/mik/cubesat-sim
bash scripts/install.sh
```

`install.sh` creates a virtualenv at `./venv`, installs `requirements.txt`, copies the unit files to `/etc/systemd/system/`, and starts the services.

### Manage services

```bash
bash scripts/start.sh   # start and enable all services
bash scripts/stop.sh    # stop and disable all services
```

### Run services manually (development)

Each service must be launched from the **project root** so that `src` is importable as a package:

```bash
source venv/bin/activate

PYTHONPATH=. python -m src.obc.main
PYTHONPATH=. python -m src.eps.main
PYTHONPATH=. python -m src.adcs.main
PYTHONPATH=. python -m src.payload.main
PYTHONPATH=. python -m src.telemetry.main
```

Run each in a separate terminal or as a background process.

---

## Configuration

Configuration is loaded from environment variables (or a `.env` file in the project root). All defaults are defined in `src/common/config.py`.

Create a `.env` file to override settings:

```ini
# MQTT broker
MQTT_BROKER=localhost
MQTT_PORT=1883

# Remote telemetry API (optional)
TELEMETRY_SEND_ENABLED=0
TELEMETRY_SEND_INTERVAL_SEC=30
TELEMETRY_API_URL=http://localhost:8080
TELEMETRY_API_KEY=your-api-key-here
```

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_BROKER` | `localhost` | Hostname or IP of the MQTT broker |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `TELEMETRY_SEND_ENABLED` | `0` | Set to `1` to POST telemetry packets to a remote API |
| `TELEMETRY_SEND_INTERVAL_SEC` | `30` | How often to POST to the remote API (seconds) |
| `TELEMETRY_API_URL` | `http://localhost:8080` | Base URL of the remote telemetry server |
| `TELEMETRY_API_KEY` | _(none)_ | API key sent as `Authorization` header |

---

## Logs

Each service writes rotating logs to `/var/log/cubesat/<service>.log` (10 MB per file, 5 files retained). When running as systemd units, logs are also available via `journalctl`:

```bash
journalctl -u cubesat-obc.service -f
journalctl -u cubesat-eps.service -f
journalctl -u cubesat-adcs.service -f
journalctl -u cubesat-payload.service -f
journalctl -u cubesat-telemetry.service -f
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/architecture.md](docs/architecture.md) | Detailed runtime architecture, subsystem internals, data flow diagrams |
| [docs/code_smells.md](docs/code_smells.md) | Catalogue of known bugs and technical debt |
| [docs/refactoring_plan.md](docs/refactoring_plan.md) | Prioritised refactoring plan with implementation examples |
| [ROADMAP.md](ROADMAP.md) | Feature tracker: bugs, improvements, new features |

---

## License

See `LICENSE` for details.
