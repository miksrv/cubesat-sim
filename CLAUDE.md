# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CubeSat Sim is an educational simulation platform for CubeSat satellite systems. It simulates a distributed satellite software architecture where each subsystem runs as an independent Python service communicating via MQTT.

## Setup and Running

**Install dependencies (Raspberry Pi / Linux):**
```bash
bash scripts/install.sh
```
This creates a virtualenv at `./venv`, installs requirements, copies systemd unit files, and starts core services.

**Run individual services manually (from repo root):**
```bash
source venv/bin/activate
PYTHONPATH=. python -m src.obc.main
PYTHONPATH=. python -m src.eps.main
PYTHONPATH=. python -m src.adcs.main
PYTHONPATH=. python -m src.payload.main
PYTHONPATH=. python -m src.telemetry.main
```

**Manage all services via systemd:**
```bash
bash scripts/start.sh    # start and enable all services
bash scripts/stop.sh     # stop and disable all services
bash scripts/restart.sh  # restart all services (e.g. after a system update)
```

**View logs:**
```bash
journalctl -u cubesat-obc.service -f
# Logs also written to /var/log/cubesat/<service>.log
```

**Dependencies:** `paho-mqtt`, `transitions`, `psutil`, `picamera2`, `smbus2`, `RPi.GPIO`, `lgpio`, `python-dotenv`, `pyyaml`, `requests`

## Architecture

Each subsystem is an independent Python process with its own `main.py` entry point. All inter-service communication happens exclusively over MQTT. Services must be run from the repo root so that `src` is importable as a package.

### Subsystems

| Module | Entry point | Role |
|---|---|---|
| `src/obc/` | `src.obc.main` | Central controller; runs the state machine |
| `src/eps/` | `src.eps.main` | Power monitoring; publishes battery/voltage status |
| `src/adcs/` | `src.adcs.main` | IMU-based orientation; reads QMI8658/AK09918 |
| `src/payload/` | `src.payload.main` | Camera photos + science data collection |
| `src/telemetry/` | `src.telemetry.main` | Aggregates all subsystem data; persists to SQLite |
| `src/common/` | — | Shared utilities used by all services |

### OBC State Machine

The OBC (`src/obc/state_machine.py`) drives the mission lifecycle using the `transitions` library:

```
BOOT → DEPLOY → NOMINAL ↔ SCIENCE
                   ↓         ↓
              LOW_POWER ← ← ←
                   ↓
                 SAFE
```

- State transitions are triggered by: EPS battery level (via `handlers.py`) or ground commands on `cubesat/command`
- Battery < 40% → `LOW_POWER`; battery < 20% → `SAFE`
- Telemetry is written to SQLite only when OBC state is `SCIENCE` (checked every `TELEMETRY_SEND_INTERVAL_SEC`, default 30s); publishing to `cubesat/telemetry/data` happens only on-demand, in response to a `get_telemetry` command — the aggregator does not publish periodically
- Photo capture and timelapse start only allowed when OBC state is `NOMINAL`; timelapse stop is permitted from any state

### MQTT Topic Map (defined in `src/common/config.py`)

All topic strings are centralized in `TOPICS` dict — always reference `TOPICS["key"]` rather than hardcoding strings.

| Key | Topic | Direction |
|---|---|---|
| `command` | `cubesat/command` | Ground → OBC, Payload, Telemetry |
| `obc_status` | `cubesat/obc/status` | OBC → All |
| `eps_status` | `cubesat/eps/status` | EPS → OBC, Telemetry |
| `adcs_status` | `cubesat/adcs/status` | ADCS → Telemetry |
| `payload_status` | `cubesat/payload/status` | Payload → All |
| `payload_data` | `cubesat/payload/data` | Payload → Telemetry |
| `payload_photo` | `cubesat/payload/photo` | Payload → (bot) |
| `telemetry_data` | `cubesat/telemetry/data` | Telemetry → Ground (on-demand only, see below) |

**`cubesat/command` payload format** — the `"command"` field routes the message to the correct handler:

| `"command"` value | Handler | Additional fields |
|---|---|---|
| `science_start` | OBC | — |
| `science_stop` | OBC | — |
| `safe_mode` | OBC | — |
| `recover` | OBC | — |
| `take_photo` | Payload | `"request_id"`, `"params": {"overlay": bool}` |
| `start_timelapse` | Payload | `"params": {"interval_sec": int}` |
| `stop_timelapse` | Payload | — |
| `get_telemetry` | Telemetry | `"request_id"` |

**`cubesat/obc/status` payload format:**
```json
{"timestamp": <unix_float>, "status": "<STATE>"}
```
Consumers read the `"status"` field (not `"state"`) to determine the current OBC state.

### Shared Common Module (`src/common/`)

- `config.py` — all constants: MQTT broker address, port, topic strings, file paths, intervals. Defaults are loaded from `config/config.yaml`; `MQTT_BROKER` and `MQTT_PORT` environment variables (or `.env`) override the YAML values. Remote-telemetry secrets (`TELEMETRY_API_KEY`, etc.) are env-var only and must never be committed to `config.yaml`.
- `mqtt_client.py` — `get_mqtt_client(client_id)` factory; creates MQTTv5 client with exponential backoff reconnect. Caller must set `on_connect`/`on_disconnect` after construction.
- `logging_setup.py` — `setup_logging()` must be called before any imports that use logging. Writes rotating logs to `/var/log/cubesat/<service>.log`.
- `system_metrics.py` — CPU/RAM/disk/temperature collection via `psutil`.
- `utils.py` — `crc16_ccitt()`, `json_dumps_pretty()`, `timestamp_iso()`, `ensure_dir()`.
- `imu_qmi8658_ak09918.py` — I2C driver for the IMU sensor used by ADCS.

### Remote Telemetry API (optional)

The telemetry aggregator can additionally POST each packet to a remote HTTP API (e.g. the [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) backend). Controlled entirely via environment variables — see `TELEMETRY_SEND_ENABLED`, `TELEMETRY_SEND_INTERVAL_SEC`, `TELEMETRY_API_URL`, `TELEMETRY_API_KEY` in `config.py`. Internet reachability is checked once at startup; if unreachable, remote sending stays disabled for the life of the process.

### Data Persistence

Telemetry is stored in SQLite at `data/telemetry.db` (path from `config.DB_PATH`). The `TelemetryAggregator` creates and writes the `telemetry_log` table directly via `sqlite3`.

### Hardware Notes

Services are designed for Raspberry Pi. `picamera2` (camera), `RPi.GPIO` (EPS), and `smbus2`/`lgpio` (I2C for IMU/EPS/Payload sensors) are hardware-specific. On non-Pi systems these will fail to import — mock or stub them for local development.

Physical build assets (not runtime data) live under `hardware/`: `hardware/models/` for 3D-printable/CAD files (frame, mounts), `hardware/photos/` for build photos embedded in `README.md`. This is distinct from `data/photos/`, which holds camera captures produced at runtime by the Payload service.

### Systemd Deployment

Unit files in `systemd/` expect the project at `/home/mik/cubesat-sim` and virtualenv at `/home/mik/cubesat-sim/venv`. Each service sets `PYTHONPATH` to the project root and runs with `python -m src.<module>.main`.

## Related Projects

[cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) — cloud ground station (PHP/CodeIgniter 4 + React) that receives telemetry packets POSTed by `TelemetryAggregator.send_to_remote_api()` and visualizes them on a web dashboard.
