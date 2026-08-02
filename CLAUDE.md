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
PYTHONPATH=. python -m src.comms.main
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

**Dependencies:** `paho-mqtt`, `transitions`, `psutil`, `picamera2`, `smbus2`, `RPi.GPIO`, `lgpio`, `pyserial`, `pynmea2`, `python-dotenv`, `pyyaml`, `requests`

## Testing

The test suite (`tests/`) runs on any machine — it never needs real Raspberry Pi hardware or a real MQTT broker. `tests/conftest.py` replaces `RPi.GPIO`, `lgpio`, `smbus2`, `picamera2`/`libcamera` with mocks in `sys.modules` before any `src.*` module is imported, and individual tests inject fake I2C/serial peripherals (see `tests/fakes.py`) or mock the MQTT client after construction.

**Run tests:**
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-test.txt
pytest --cov --cov-report=term-missing
```
`requirements-test.txt` is separate from `requirements.txt`: it skips the four hardware-only packages (mocked instead, see above) and adds `pytest`, `pytest-cov`, `requests-mock`. Coverage config (`.coveragerc`) enforces a 95% minimum (`fail_under`).

GitHub Actions (`.github/workflows/tests.yml`) runs the suite on every push to `main` and on every pull request, across Python 3.10–3.12.

## Architecture

Each subsystem is an independent Python process with its own `main.py` entry point. All inter-service communication happens exclusively over MQTT. Services must be run from the repo root so that `src` is importable as a package.

### Subsystems

| Module | Entry point | Role |
|---|---|---|
| `src/obc/` | `src.obc.main` | Central controller; runs the state machine |
| `src/eps/` | `src.eps.main` | Power monitoring; publishes battery/voltage status |
| `src/adcs/` | `src.adcs.main` | IMU + GPS-based navigation; reads QMI8658/AK09918/A9G GPS |
| `src/payload/` | `src.payload.main` | Camera photos + science data collection |
| `src/comms/` | `src.comms.main` | Ground link: aggregates all subsystem data, persists to SQLite (with retention cleanup), relays over remote HTTP API and LoRa |
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
- COMMS writes to SQLite only when OBC state is `SCIENCE` **and** its `aggregation_enabled` flag is on (checked every `COMMS_LOOP_INTERVAL_SEC`, default 30s); publishing to `cubesat/comms/data` happens only on-demand, in response to a `get_telemetry` command — COMMS does not publish that topic periodically
- COMMS' `api_enabled`/`lora_enabled`/`aggregation_enabled` flags can be flipped at runtime by a `set_comms_config` ground command, sent directly to COMMS (not routed through OBC); they are not persisted and reset to config defaults on restart
- Photo capture and timelapse start only allowed when OBC state is `NOMINAL`; timelapse stop is permitted from any state

### MQTT Topic Map (defined in `src/common/config.py`)

All topic strings are centralized in `TOPICS` dict — always reference `TOPICS["key"]` rather than hardcoding strings.

| Key | Topic | Direction |
|---|---|---|
| `command` | `cubesat/command` | Ground → OBC, Payload, COMMS (COMMS also republishes here any command it receives over LoRa or polls from the remote API) |
| `obc_status` | `cubesat/obc/status` | OBC → All |
| `eps_status` | `cubesat/eps/status` | EPS → OBC, COMMS |
| `adcs_status` | `cubesat/adcs/status` | ADCS → COMMS |
| `payload_status` | `cubesat/payload/status` | Payload → All |
| `payload_data` | `cubesat/payload/data` | Payload → COMMS |
| `payload_photo` | `cubesat/payload/photo` | Payload → (bot) |
| `comms_data` | `cubesat/comms/data` | COMMS → Ground (on-demand only, see below) |

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
| `get_telemetry` | COMMS | `"request_id"` |
| `set_comms_config` | COMMS | `"params": {"api_enabled": bool, "lora_enabled": bool, "aggregation_enabled": bool}` (any subset; omitted keys unchanged) |

**`cubesat/obc/status` payload format:**
```json
{"timestamp": <unix_float>, "status": "<STATE>"}
```
Consumers read the `"status"` field (not `"state"`) to determine the current OBC state.

### Shared Common Module (`src/common/`)

- `config.py` — all constants: MQTT broker address, port, topic strings, file paths, intervals. Defaults are loaded from `config/config.yaml`; `MQTT_BROKER` and `MQTT_PORT` environment variables (or `.env`) override the YAML values. Remote COMMS secrets (`COMMS_API_KEY`, etc.) are env-var only and must never be committed to `config.yaml`.
- `mqtt_client.py` — `get_mqtt_client(client_id)` factory; creates MQTTv5 client with exponential backoff reconnect. Caller must set `on_connect`/`on_disconnect` after construction.
- `logging_setup.py` — `setup_logging()` must be called before any imports that use logging. Writes rotating logs to `/var/log/cubesat/<service>.log`.
- `system_metrics.py` — CPU/RAM/disk/temperature collection via `psutil`.
- `utils.py` — `crc16_ccitt()`, `json_dumps_pretty()`, `timestamp_iso()`, `ensure_dir()`. `crc16_ccitt()` is used by COMMS' LoRa driver (`src/comms/lora.py`) to frame/verify packets.
- `imu_qmi8658_ak09918.py` — I2C driver for the IMU sensor used by ADCS.
- `gps_a9g.py` — NMEA/UART driver for the A9G GPS module used by ADCS; non-blocking, returns the last known fix (with `fix: false`) rather than stalling the ADCS loop when there's no signal.

### Remote COMMS API (optional)

COMMS can additionally POST each packet to a remote HTTP API (e.g. the [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) backend) and poll that same API for ground commands queued while the link was down. Controlled via `COMMS_API_ENABLED` (startup default for the runtime-toggleable `api_enabled` flag), `COMMS_LOOP_INTERVAL_SEC`, `COMMS_API_URL`, `COMMS_API_KEY` in `config.py`. Unlike the previous one-shot startup check, internet reachability is now re-checked every loop iteration — this channel is meant for ground/debug use, where connectivity comes and goes; it's expected to be permanently unreachable (and thus a no-op) on a real in-flight satellite. The pending-commands polling endpoint (`GET .../api/cubesat/commands/pending`) is a contract this repo expects from `cubesat-groundstation` — it is not yet implemented on that side.

### LoRa (52Pi IoT Node(A))

`src/comms/lora.py` drives LoRa TX/RX over the module's SC16IS752 I2C↔UART bridge (`smbus2`, see `docs/hardware-iot-node-a-52pi.md`). Controlled by `COMMS_LORA_ENABLED` (startup default for the runtime-toggleable `lora_enabled` flag) — **defaults to `0`**, since the register-level protocol is a best-effort implementation against a thin vendor doc and has not been bench-verified to actually transmit/receive (see `ROADMAP.md`). Packets are length-prefixed and CRC-16-CCITT checked; commands received over LoRa are republished onto `cubesat/command` exactly like ones polled from the remote API, so OBC/Payload/COMMS don't need to know which channel a command arrived on.

### Data Persistence

COMMS packets are stored in SQLite at `data/comms.db` (path from `config.DB_PATH`). `CommsService` (`src/comms/service.py`) creates and writes the `comms_log` table directly via `sqlite3`, and periodically purges rows older than `COMMS_DB_RETENTION_DAYS` (default 30).

### Hardware Notes

Services are designed for Raspberry Pi. `picamera2` (camera), `RPi.GPIO` (EPS), `smbus2`/`lgpio` (I2C for IMU/EPS/Payload/LoRa), and `pyserial` (GPS UART) are hardware-specific. On non-Pi systems these will fail to import — mock or stub them for local development.

Physical build assets (not runtime data) live under `hardware/`: `hardware/models/` for 3D-printable/CAD files (frame, mounts), `hardware/photos/` for build photos embedded in `README.md`. This is distinct from `data/photos/`, which holds camera captures produced at runtime by the Payload service.

Per-component hardware documentation (specs, Raspberry Pi setup requirements, Python/Bash usage examples, links to vendor docs) lives in `docs/hardware-*.md` — one file per component in the `README.md` Hardware table, which links to them. See `docs/hardware-x728-ups-hat.md`, `docs/hardware-sense-hat-c.md`, `docs/hardware-camera-module-v2.md`, `docs/hardware-iot-node-a-52pi.md`.

### Systemd Deployment

Unit files in `systemd/` expect the project at `/home/mik/cubesat-sim` and virtualenv at `/home/mik/cubesat-sim/venv`. Each service sets `PYTHONPATH` to the project root and runs with `python -m src.<module>.main`.

## Related Projects

[cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) — cloud ground station (PHP/CodeIgniter 4 + React) that receives COMMS packets POSTed by `CommsService.send_to_remote_api()`, visualizes them on a web dashboard, and is expected (but not yet implemented) to expose a pending-commands queue for `CommsService.poll_remote_commands()`.
