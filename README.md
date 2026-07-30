# CubeSat Sim

CubeSat Sim is a working flight software stack for a real, physical CubeSat — not just a simulation on paper. Five independent Python services (OBC, EPS, ADCS, Payload, Telemetry) each model one satellite subsystem and talk to each other exclusively over MQTT, the same way modules communicate over a real spacecraft bus. A central state machine drives the mission lifecycle (`BOOT → DEPLOY → NOMINAL ↔ SCIENCE → LOW_POWER → SAFE`), reacting to live battery telemetry, IMU orientation data, and ground commands, while a telemetry aggregator logs everything to SQLite and can forward it to a [cloud ground station](https://github.com/miksrv/cubesat-groundstation) in real time.

![CubeSat Sim](hardware/photos/cover.jpg)

It runs today on a Raspberry Pi inside a 3D-printed frame, wired to real sensors — battery fuel gauge, IMU, magnetometer, barometric/humidity sensors, and a camera. Everything needed to build one yourself is in this repo: the code, the [3D models](hardware/models), and the [hardware list](#hardware).

If you're learning satellite software architecture, distributed systems, or embedded Python, feel free to dig in, fork it, and adapt it to your own build. If it's useful to you, a star helps other people find it.

The platform can also be adapted for local development by mocking the hardware dependencies (see [Hardware](#hardware)).

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
  - [Mechanical / Fasteners](#mechanical--fasteners)
  - [3D Models](#3d-models)
  - [Build Photos](#build-photos)
- [Setup and Running](#setup-and-running)
- [Configuration](#configuration)
- [Logs](#logs)
- [Documentation](#documentation)
- [Related Projects](#related-projects)

---

## Architecture Overview

Each subsystem is an independent Python process. All inter-process communication happens over a local MQTT broker (mosquitto). No service calls another directly.

```
┌──────────────────────────────────────────────────────────────────┐
│                     MQTT Broker (mosquitto)                      │
│                                                                  │
│  cubesat/command            cubesat/obc/status      (retained)   │
│                              cubesat/eps/status      (retained)   │
│                              cubesat/adcs/status                  │
│                              cubesat/payload/status  (retained)   │
│                              cubesat/payload/data                 │
│                              cubesat/payload/photo                │
│                              cubesat/telemetry/data  (on-demand)  │
└───────────┬──────────────────────────────────────────────────────┘
            │ publish / subscribe
   ┌────────┴──────────────────────────────────────────────┐
   ▼            ▼           ▼             ▼                ▼
┌──────┐    ┌──────┐    ┌──────┐    ┌───────────┐    ┌───────────┐
│ OBC  │    │ EPS  │    │ ADCS │    │  Payload  │    │ Telemetry │
│      │    │      │    │      │    │           │    │           │
│State │    │Power │    │ IMU  │    │ Camera    │    │ Aggregator│
│Mach. │    │Mon.  │    │ AHRS │    │ Science   │    │ SQLite DB │
└──────┘    └──────┘    └──────┘    └───────────┘    └───────────┘
               │           │              │
            I2C/GPIO      I2C         I2C / CSI
            MAX17048    QMI8658       LPS22HB
            X728 UPS    AK09918       SHTC3
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

1. **Camera** — captures a JPEG photo via Picamera2 on demand. Responds to `take_photo`, `start_timelapse`, and `stop_timelapse` commands on `cubesat/command`. `take_photo` and `start_timelapse` are gated: only permitted when the OBC is in `NOMINAL` state. Photos are Base64-encoded and published to `cubesat/payload/photo`.

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

A passive aggregator. Subscribes to all subsystem status topics and maintains an in-memory cache of the latest reading from each. Every `TELEMETRY_SEND_INTERVAL_SEC` (default 30s), if the cached OBC state is `SCIENCE`, it assembles a unified telemetry packet from the cache, appends system health metrics (CPU, RAM, disk, temperature), and writes the packet to a SQLite database.

**Note:** the aggregator does **not** publish to `cubesat/telemetry/data` on this periodic loop — that topic is only published in response to an on-demand `get_telemetry` command on `cubesat/command`.

**Optional remote API forwarding:** if `TELEMETRY_SEND_ENABLED=1`, on every loop iteration the aggregator also POSTs the current packet to `TELEMETRY_API_URL` (e.g. the [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) backend), independent of OBC state. Internet reachability is checked once at startup only; if unreachable at that point, remote sending stays off for the life of the process.

**SQLite schema** (`data/telemetry.db`, table `telemetry_log`):

| Column group | Fields |
|---|---|
| Timing | `id`, `timestamp` (ISO-8601 UTC string, e.g. `2026-03-11T14:30:00.123456Z` — not a Unix float, unlike the MQTT payloads below) |
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
| `command` | `cubesat/command` | Ground → All | Ground station | OBC, Payload, Telemetry |
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
  "timestamp": 1741863600.0,
  "status": "NOMINAL"
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

### `cubesat/payload/status`
Published (retained) on connect, and again on a photo-processing error.
```json
{
  "state": "IDLE",
  "alive": true,
  "timestamp": 1741863600.0
}
```

### `cubesat/payload/data`
No timestamp field — the aggregator timestamps data itself on ingest.
```json
{
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
  "reason": "Photo capture not allowed: OBC status is 'SCIENCE'"
}
```

### Ground commands to `cubesat/command`

All commands use the same topic. The `"command"` field determines which service handles the message.

```json
{"command": "science_start"}
{"command": "science_stop"}
{"command": "safe_mode"}
{"command": "recover"}
{"command": "take_photo", "request_id": "req_001", "params": {"overlay": false}}
{"command": "start_timelapse", "params": {"interval_sec": 60}}
{"command": "stop_timelapse"}
{"command": "get_telemetry", "request_id": "req_002"}
```

---

## Data Flows

### SCIENCE mode cycle

```
1. Ground sends:  {"command": "science_start"}  →  cubesat/command

2. OBC receives command, transitions NOMINAL → SCIENCE
   Publishes: {"timestamp": <unix_float>, "status": "SCIENCE"}  →  cubesat/obc/status  (retain)

3. Payload reads obc_state = "SCIENCE" — science poll continues as normal.
   Every 60 s: reads LPS22HB + SHTC3  →  cubesat/payload/data

4. ADCS: every 500 ms: reads QMI8658 + AK09918, runs AHRS  →  cubesat/adcs/status

5. EPS: every 30 s: reads MAX17048 + GPIO  →  cubesat/eps/status

6. Telemetry aggregator: every TELEMETRY_SEND_INTERVAL_SEC (default 30 s), while obc_state == "SCIENCE":
   Merges cached OBC/EPS/ADCS/payload/system data → writes row to telemetry.db
   (does NOT publish to cubesat/telemetry/data on this loop — see note below)

7. Ground sends:  {"command": "science_stop"}  →  cubesat/command
   OBC: SCIENCE → NOMINAL
```

> `cubesat/telemetry/data` is only published on-demand (step below) or, if `TELEMETRY_SEND_ENABLED=1`, forwarded to a remote HTTP API — see [Telemetry Aggregator](#telemetry-aggregator).

### Photo request

```
1. Ground sends: {"command": "take_photo", "request_id": "req_001", "params": {"overlay": false}}  →  cubesat/command

2. Payload checks obc_state:
   - Not NOMINAL → publishes error  →  cubesat/payload/photo
   - NOMINAL     → captures JPEG via Picamera2
                   saves to data/photos/
                   Base64-encodes image

3. Publishes full response (with photo_base64)  →  cubesat/payload/photo
```

### On-demand telemetry request

```
1. Ground sends: {"command": "get_telemetry", "request_id": "req_002"}  →  cubesat/command

2. Telemetry aggregator builds a packet from its in-memory cache (independent of OBC state)
   Attaches request_id to the packet

3. Publishes  →  cubesat/telemetry/data  (retained)
```

### Timelapse

```
1. Ground sends: {"command": "start_timelapse", "params": {"interval_sec": 60}}  →  cubesat/command
   Payload: OBC must be NOMINAL; starts background thread capturing every interval_sec seconds.

2. Ground sends: {"command": "stop_timelapse"}  →  cubesat/command
   Payload: stops timelapse thread (allowed from any OBC state).
```

### Low-power event

```
1. EPS reads battery = 38%  →  cubesat/eps/status

2. OBC handler: 38% < 40% threshold → triggers enter_low_power
   State: NOMINAL → LOW_POWER
   Publishes: {"timestamp": <unix_float>, "status": "LOW_POWER"}  →  cubesat/obc/status

3. If battery continues to drop to 18%:
   OBC handler: 18% < 20% → triggers enter_safe_mode
   State: LOW_POWER → SAFE
   Publishes: {"timestamp": <unix_float>, "status": "SAFE"}  →  cubesat/obc/status

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
│   ├── stop.sh                    # Stop and disable all services
│   └── restart.sh                 # Restart all services (e.g. after a system update)
│
├── config/
│   └── config.yaml                # Runtime defaults: MQTT, telemetry intervals, camera resolution
│
├── hardware/                      # Physical build assets (not runtime data)
│   ├── models/                    # 3D-printable/CAD files for the frame and mounts
│   └── photos/                    # Build/assembly photos embedded in this README
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

The simulation targets Raspberry Pi. The following hardware is required for full operation.

**Components** — Raspberry Pi, Pi Camera, 2× 18650 batteries, Sense HAT, etc.

![CubeSat hardware components](hardware/photos/02-hardware-components.jpg)

| Component | Purpose | Interface / Library | Product link | Documentation |
|---|---|---|---|---|
| Raspberry Pi 4 Model B | Main compute — hosts and runs all five services | — | [Raspberry Pi](https://www.raspberrypi.com/products/raspberry-pi-4-model-b/) | [Raspberry Pi Docs](https://www.raspberrypi.com/documentation/) |
| X728 V2.5 UPS HAT | Battery power management: LiPo fuel gauge (MAX17048) + AC-loss detection (PLD pin) — used by EPS | I2C (`0x36`) + GPIO · `smbus2`, `RPi.GPIO` | [AliExpress](https://www.aliexpress.us/item/3256804825472151.html) | [Geekworm Wiki](https://wiki.geekworm.com/X728) |
| Sense HAT (B) | Environmental/orientation sensor HAT: QMI8658 (accel + gyro) + AK09918 (magnetometer) drive ADCS orientation; LPS22HB (pressure) + SHTC3 (humidity) feed Payload science data | I2C · `smbus2`, `lgpio` | [AliExpress](https://www.aliexpress.us/item/3256811354242582.html) | [Waveshare Wiki](<https://www.waveshare.com/wiki/Sense_HAT_(B)>) |
| Raspberry Pi Camera Module V2 (8MP, 1080p) | Photo capture + timelapse — used by Payload | CSI · `picamera2` | [Amazon](https://a.co/d/02oyeWg8) | [Raspberry Pi Docs](https://www.raspberrypi.com/documentation/accessories/camera.html#camera-module-2) |
| IoT Node(A) — 52Pi Docker Pi Series (GSM/GPS/LoRa) | Onboard GSM/GPS/LoRa module, present on the build but not yet wired into any service — reserved for future ground-link/positioning work | — | [AliExpress](https://www.aliexpress.us/item/2251832864586218.html) | [52Pi Wiki](https://wiki.52pi.com/index.php?title=EP-0105) |

> Also requires a `mosquitto` MQTT broker running on the Pi (software, not hardware) — see [Prerequisites](#prerequisites).

> **Non-Pi development:** All hardware libraries are imported at module level, so services will fail to import on a non-Raspberry Pi machine. Hardware mocking is on the roadmap (see `ROADMAP.md` items H1–H7).

### Mechanical / Fasteners

Used to assemble the 3D-printed frame ([`hardware/models/`](hardware/models/)) and mount the boards to it.

| Component | Product link |
|---|---|
| M3 Nuts | https://www.aliexpress.us/item/2255800416538696.html |
| M3 Screws | https://www.aliexpress.us/item/3256805798104626.html |
| M3 Standoffs | https://www.aliexpress.us/item/2251832676215215.html |

### 3D Models

3D-printable STL files for the physical build live in [`hardware/models/`](hardware/models/) (see that folder's README for file conventions):

| File | Purpose |
|---|---|
| `base/CS_MGSE_Tight_Fit.stl` | Ground support stand — holds the assembled CubeSat upright on a bench |
| `frame/Cubesat_Bottom_Frame.stl` | Bottom frame panel |
| `frame/Cubesat_Side_Frame_Plain.stl` | Side frame panel |
| `frame/Cubesat_Adaptor_Mount.stl` | Internal adaptor mount for the electronics stack |
| `frame/Cubesat_RaspbiCam_Frame.stl` | Camera mount for the Raspberry Pi Camera Module |

### Build Photos

Photos of the physical build (full-resolution originals are not kept in the repo — see [`hardware/photos/`](hardware/photos/) for naming conventions).

**3D-printed frame**

![3D-printed CubeSat frame](hardware/photos/01-frame-3d-printed.jpg)

**Assembled — without side panels**

<p align="center">
  <img src="hardware/photos/03-assembled-no-panels-1.jpg" width="32%" alt="Assembled CubeSat without panels — view 1">
  <img src="hardware/photos/03-assembled-no-panels-2.jpg" width="32%" alt="Assembled CubeSat without panels — view 2">
  <img src="hardware/photos/03-assembled-no-panels-3.jpg" width="32%" alt="Assembled CubeSat without panels — view 3">
</p>

**Finished unit — with protective panels**

<p align="center">
  <img src="hardware/photos/04-finished-unit-1.jpg" width="32%" alt="Finished CubeSat with panels — view 1">
  <img src="hardware/photos/04-finished-unit-2.jpg" width="32%" alt="Finished CubeSat with panels — view 2">
  <img src="hardware/photos/04-finished-unit-3.jpg" width="32%" alt="Finished CubeSat with panels — view 3">
</p>

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
bash scripts/start.sh    # start and enable all services
bash scripts/stop.sh     # stop and disable all services
bash scripts/restart.sh  # restart all services (e.g. after a system update)
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

Non-secret defaults live in [`config/config.yaml`](config/config.yaml). `src/common/config.py` loads that file first, then lets specific environment variables (or a `.env` file in the project root) override individual values. Secrets and deployment-specific values (remote API URL/key) are environment-variable only and must never be committed to `config.yaml`.

```yaml
# config/config.yaml
mqtt:
  broker: localhost       # overridden by MQTT_BROKER env var
  port: 1883              # overridden by MQTT_PORT env var
  keepalive: 60

telemetry:
  interval_sec: 30             # not currently read by any service (see note below)
  low_power_interval_sec: 300  # not currently read by any service (see note below)

camera:
  resolution: [1920, 1080]     # JPEG capture resolution [width, height]

logging:
  level: INFO                  # not currently read — each service hardcodes INFO in its main.py
```

Create a `.env` file to override the environment-variable settings:

```ini
# MQTT broker (overrides config.yaml)
MQTT_BROKER=localhost
MQTT_PORT=1883

# Remote telemetry API (optional, disabled by default)
TELEMETRY_SEND_ENABLED=0
TELEMETRY_SEND_INTERVAL_SEC=30
TELEMETRY_API_URL=http://localhost:8080
TELEMETRY_API_KEY=your-api-key-here
```

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_BROKER` | `localhost` (from `config.yaml`) | Hostname or IP of the MQTT broker |
| `MQTT_PORT` | `1883` (from `config.yaml`) | MQTT broker port |
| `TELEMETRY_SEND_ENABLED` | `0` | Set to `1` to POST telemetry packets to a remote API (e.g. [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation)) |
| `TELEMETRY_SEND_INTERVAL_SEC` | `30` | Sleep interval of the telemetry aggregator's main loop — also governs how often it checks OBC state for SQLite writes and POSTs to the remote API, not just the remote send |
| `TELEMETRY_API_URL` | `http://localhost:8080` | Base URL of the remote telemetry server |
| `TELEMETRY_API_KEY` | _(none)_ | API key sent as the `X-API-Key` header; if unset, remote sending is skipped even when enabled |

> **Known gap:** `telemetry.interval_sec`, `telemetry.low_power_interval_sec`, and `logging.level` are defined in `config.yaml` and exposed as constants in `config.py`, but no service currently reads them — the telemetry loop cadence is actually controlled by `TELEMETRY_SEND_INTERVAL_SEC`, and log level is hardcoded to `INFO` per-service. Don't rely on editing those `config.yaml` keys to change behavior yet.

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
| [docs/code_smells.md](docs/code_smells.md) | Historical catalogue of bugs/tech debt found during the initial audit — most items (B1–B6, C1–C5, R1–R4) are already fixed; check [ROADMAP.md](ROADMAP.md#completed) for current status before acting on it |
| [docs/refactoring_plan.md](docs/refactoring_plan.md) | Prioritised refactoring plan with implementation examples (HAL and tests, H1–H7/T1–T7, are still pending) |
| [ROADMAP.md](ROADMAP.md) | Feature tracker: bugs, improvements, new features |

---

## Related Projects

| Project | Description |
|---|---|
| [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) | Cloud ground station (PHP/CodeIgniter 4 backend + React dashboard) that receives telemetry POSTed by this project's `TelemetryAggregator` (see [Telemetry Aggregator](#telemetry-aggregator) and [Configuration](#configuration)), stores it in MySQL, and visualizes it in real time |

---

## License

See `LICENSE` for details.
