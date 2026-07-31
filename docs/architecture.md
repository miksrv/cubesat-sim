# Architecture Overview

## System Purpose

CubeSat Sim is a distributed simulation of a CubeSat satellite's onboard software. Each real-world CubeSat subsystem is represented by an independent Python process. The processes run on a Raspberry Pi (or cluster of boards) and communicate exclusively through MQTT, mirroring how physical subsystems communicate over a spacecraft bus.

---

## Runtime Architecture

```
┌───────────────────────────────────────────────────────────┐
│                      MQTT Broker (mosquitto)               │
│                                                           │
│  cubesat/command          cubesat/obc/status (retain)     │
│                           cubesat/eps/status (retain)     │
│                           cubesat/adcs/status             │
│                           cubesat/payload/status (retain) │
│                           cubesat/payload/data            │
│  cubesat/payload/photo    cubesat/comms/data              │
│                             (published on-demand only)    │
└───────┬──────────────────────────────────────────────────-┘
        │ publish/subscribe
  ┌─────┴──────────────────────────────────────────────┐
  │                                                    │
  ▼                                                    ▼
┌──────┐    ┌──────┐    ┌────────┐    ┌─────────┐    ┌───────────┐
│ OBC  │    │ EPS  │    │  ADCS  │    │ Payload │    │  COMMS    │
│      │    │      │    │        │    │         │    │           │
│State │    │Power │    │IMU/GPS │    │ Camera  │    │SQLite DB  │
│Mach. │    │Mon.  │    │ AHRS   │    │ Science │    │API + LoRa │
└──────┘    └──────┘    └────────┘    └─────────┘    └───────────┘
   │            │            │              │               │
   │         I2C/GPIO     I2C/UART        I2C / CSI        I2C
   │         MAX17048     QMI8658         LPS22HB       SC16IS752
   │         X728 UPS     AK09918         SHTC3          (LoRa,
   │                      A9G GPS         Picamera2      52Pi IoT
   └───────────────────────────────────────────────────  Node(A))
              Raspberry Pi Hardware
```

---

## Subsystems

### OBC — On-Board Computer (`src/obc/`)

The central authority of the simulation. Implements a finite state machine using the `transitions` library and is the only service that can change mission state.

**State machine:**
```
        ┌──────────────────────────────────────────┐
        │                                          │ enter_safe_mode (any state)
        ▼                                          │
      BOOT ──auto_deploy──► DEPLOY ──deployment_complete──► NOMINAL
                                                      │         ▲
                                          start_science│         │end_science
                                                      ▼         │
                                                   SCIENCE ──────┘
                                                      │
                                        enter_low_power│(battery <40%)
                                                      ▼
                                                 LOW_POWER
                                                      │
                                           enter_safe_mode (battery <20%)
                                                      ▼
                                                    SAFE
                                                      │
                                            recover ──┘ (external power restored)
```

**State transition rules (from `handlers.py`):**
- Battery < 40% → `LOW_POWER` (if not already `LOW_POWER` or `SAFE`)
- Battery < 20% → `SAFE` (from any state)
- External power restored while in `LOW_POWER`/`SAFE` → `NOMINAL`
- Ground commands on `cubesat/command`: `science_start`, `science_stop`, `safe_mode`, `recover`

**Files:**
| File | Responsibility |
|---|---|
| `main.py` | MQTT setup, main heartbeat loop (30s) |
| `state_machine.py` | `CubeSatStateMachine` — state definitions, transition callbacks, state publishing |
| `handlers.py` | `OBCMessageHandlers` — EPS status reactions, ground command parsing |

---

### EPS — Electrical Power System (`src/eps/`)

Reads hardware power state via I2C (MAX17048 fuel gauge at `0x36`) and GPIO (X728 UPS PLD pin). Publishes JSON status every 30 seconds.

**Published payload (`cubesat/eps/status`):**
```json
{
  "timestamp": 1700000000.0,
  "battery": 87.5,
  "voltage": 4.123,
  "external_power": true
}
```

**Files:**
| File | Responsibility |
|---|---|
| `main.py` | MQTT setup, publish loop |
| `power_monitor.py` | `EPSMonitor` — I2C reads, GPIO reads, status assembly |

---

### ADCS — Attitude Determination and Control (`src/adcs/`)

Reads the IMU sensor and runs a Mahony-style AHRS algorithm to fuse accelerometer, gyroscope, and magnetometer data into roll/pitch/yaw angles. Also reads the last known GPS/BDS fix from the A9G module (NMEA over UART) — orientation and position are both navigation state, so they're published together. Publishes at 2 Hz.

**Published payload (`cubesat/adcs/status`):**
```json
{
  "timestamp": 1700000000.0,
  "roll": 1.23,
  "pitch": -0.45,
  "yaw": 178.9,
  "imu_temp": 34.5,
  "accel_g": {"x": 0.01, "y": 0.02, "z": 0.99},
  "gyro_dps": {"x": 0.1, "y": -0.2, "z": 0.05},
  "gps": {"lat": 55.7558, "lon": 37.6173, "alt": 156.2, "speed": 0.4, "fix": true}
}
```

**Files:**
| File | Responsibility |
|---|---|
| `main.py` | MQTT setup, publish loop (0.5 s) |
| `common/imu_qmi8658_ak09918.py` | `IMU` — QMI8658 (accel+gyro) and AK09918 (mag) I2C drivers, Mahony AHRS |
| `common/gps_a9g.py` | `GPS` — A9G NMEA (GGA/RMC) reader over UART, non-blocking |

---

### Payload (`src/payload/`)

Two responsibilities combined in one service:
1. **Camera**: Takes single photos on demand (via MQTT command), encodes as Base64, publishes on `cubesat/payload/photo`
2. **Science**: Polls LPS22HB (pressure/temperature) and SHTC3 (humidity/temperature) sensors every 60 seconds, publishes on `cubesat/payload/data`

Photo capture and timelapse start are gated: only allowed when OBC is in `NOMINAL` state (tracked by subscribing to `cubesat/obc/status`). Timelapse stop is permitted from any state.

**Files:**
| File | Responsibility |
|---|---|
| `main.py` | MQTT wiring, OBC state tracking, command routing, science poll loop |
| `camera.py` | `PayloadCamera` — Picamera2 integration, timelapse threading |
| `science.py` | `ScienceCollector` — LPS22HB + SHTC3 I2C reads, data averaging |

---

### COMMS (`src/comms/`)

The single point of contact with the ground, in both directions. Subscribes to all subsystem status topics and maintains a cache of the latest values from each. Every `COMMS_LOOP_INTERVAL_SEC` (default 30s):

- If the cached OBC state is `SCIENCE` **and** `aggregation_enabled` is on, assembles a packet and writes it to SQLite; rows older than `COMMS_DB_RETENTION_DAYS` are purged periodically.
- If `api_enabled` is on and internet reachability (re-checked every iteration, not just at startup) succeeds, POSTs the packet to the remote API and polls it for queued ground commands.
- If `lora_enabled` is on, transmits the packet over LoRa and polls for an inbound LoRa packet.

It does **not** publish to `cubesat/comms/data` on that periodic loop. That topic is published only on-demand, in response to a `get_telemetry` command on `cubesat/command`.

**Dynamic flags:** `api_enabled`, `lora_enabled`, `aggregation_enabled` default from `COMMS_API_ENABLED`/`COMMS_LORA_ENABLED`/`COMMS_AGGREGATION_ENABLED` at startup, and can be changed at runtime via a `set_comms_config` command — not persisted, reset on restart.

**Unified command ingress:** commands arriving over LoRa (CRC-16-CCITT checked) or polled from the remote API's pending-commands queue are re-published verbatim onto `cubesat/command`, so OBC/Payload/COMMS route them exactly like a command sent directly over MQTT — no channel-specific handling anywhere else in the system.

**SQLite schema** (`data/comms.db`, table `comms_log`):
- `timestamp` (ISO-8601 UTC string — not a Unix float, unlike the MQTT payloads above), EPS fields (battery, voltage, external_power)
- ADCS fields (roll, pitch, yaw, imu_temp, accel x/y/z, gyro x/y/z)
- Payload fields (temperature, humidity, pressure)
- System fields (cpu_percent, ram_percent, swap_percent, disk_percent, uptime_seconds, cpu_temperature)
- OBC state, raw JSON blob

**Files:**
| File | Responsibility |
|---|---|
| `main.py` | Entry point, logging setup |
| `service.py` | `CommsService` — MQTT subscriptions, data cache, packet builder, SQLite writer + cleanup, remote API send/poll, LoRa TX/RX, main loop |
| `lora.py` | `LoRaModule` — SC16IS752 I2C register driver, CRC-16-CCITT framing |

---

### Common (`src/common/`)

Shared infrastructure used by all services.

| File | Responsibility |
|---|---|
| `config.py` | All constants: MQTT broker, port, keepalive, all topic strings (`TOPICS` dict), data paths, intervals. Defaults load from `config/config.yaml`; `MQTT_BROKER`/`MQTT_PORT` env vars (or `.env`) override it. Remote COMMS secrets are env-var only |
| `mqtt_client.py` | `get_mqtt_client()` factory — creates MQTTv5 client with exponential backoff reconnect |
| `logging_setup.py` | `setup_logging()` — rotating file handler (10 MB × 5) + optional console, writes to `/var/log/cubesat/` |
| `system_metrics.py` | `SystemMetricsCollector` — CPU/RAM/swap/disk/uptime/temperature via `psutil` and sysfs |
| `utils.py` | `crc16_ccitt()`, `json_dumps_pretty()`, `timestamp_iso()`, `ensure_dir()` |
| `imu_qmi8658_ak09918.py` | `IMU` — hardware driver (see ADCS) |
| `gps_a9g.py` | `GPS` — hardware driver (see ADCS) |

---

## Data Flow: Typical SCIENCE Mode Cycle

```
1. Ground sends:  {"command": "science_start"} → cubesat/command

2. OBC receives command, transitions NOMINAL → SCIENCE
   Publishes: {"timestamp": <unix_float>, "status": "SCIENCE"} → cubesat/obc/status (retain)

3. Payload reads obc_state = "SCIENCE" (no action — science poll is always running)
   Every 60s: collects T/H/P → cubesat/payload/data

4. ADCS: every 500ms: reads IMU + last known GPS fix → cubesat/adcs/status

5. EPS: every 30s: reads battery/voltage → cubesat/eps/status

6. COMMS sees obc_state == "SCIENCE" and aggregation_enabled:
   Every COMMS_LOOP_INTERVAL_SEC (default 30s): builds packet from cached data + system metrics → writes to SQLite
   (does NOT publish to cubesat/comms/data on this loop — see "Data Flow: On-Demand Telemetry" below)
   If api_enabled + internet reachable: also POSTs the packet and polls for queued commands
   If lora_enabled: also transmits the packet over LoRa and polls for an inbound LoRa packet

7. Ground sends:  {"command": "science_stop"} → cubesat/command
   OBC: SCIENCE → NOMINAL
```

## Data Flow: On-Demand Telemetry

```
1. Ground sends: {"command": "get_telemetry", "request_id": "req_002"} → cubesat/command

2. COMMS builds a packet from its in-memory cache (independent of OBC state and flags)
   Attaches request_id to the packet

3. Publishes → cubesat/comms/data (retained)
```

## Data Flow: Command via LoRa or Remote API

```
1. Ground sends a command over LoRa, or queues it on the remote API's pending-commands endpoint

2. COMMS: next loop iteration —
   LoRa: polls the LoRa RX register, verifies CRC-16-CCITT, decodes the JSON payload
   API:  polls .../api/cubesat/commands/pending (only while api_enabled + internet reachable)

3. COMMS re-publishes the command verbatim → cubesat/command
```

## Data Flow: Photo Request

```
1. Ground sends: {"command": "take_photo", "request_id": "req_001", "params": {"overlay": false}}
   → cubesat/command

2. Payload checks obc_state:
   - If not NOMINAL → publishes error to cubesat/payload/photo
   - If NOMINAL → captures JPEG via Picamera2

3. Photo encoded as Base64:
   Full response (with photo_base64) → cubesat/payload/photo
```

## Data Flow: Timelapse

```
1. Ground sends: {"command": "start_timelapse", "params": {"interval_sec": 60}}
   → cubesat/command
   Payload: OBC must be NOMINAL; starts background thread capturing every interval_sec seconds.

2. Ground sends: {"command": "stop_timelapse"}
   → cubesat/command
   Payload: stops timelapse thread (allowed from any OBC state).
```

---

## Deployment

Services run as systemd units. Unit files are in `systemd/` and are installed by `scripts/install.sh`. Use `scripts/start.sh` / `scripts/stop.sh` / `scripts/restart.sh` to manage all services at once (e.g. `restart.sh` after a system update).

Each unit:
- Sets `PYTHONPATH` to project root (enables `import src.xxx`)
- Runs `python -m src.<module>.main`
- Restarts automatically on failure (`Restart=always`, `RestartSec=10s`)
- Requires `mosquitto.service` to be up first

**Service startup order:** mosquitto → all CubeSat services (parallel, no defined order between them; they reconnect if broker isn't ready)
