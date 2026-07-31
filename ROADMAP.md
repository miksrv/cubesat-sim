# CubeSat Sim — Roadmap

This file tracks feature requests, bug fixes, and improvement tasks for the project. Items are grouped by theme and ordered by priority within each group.

---

## Status Legend

| Symbol | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress |
| `[x]` | Done |

---

## 🐛 Bug Fixes (Priority: High)

These are confirmed bugs that cause incorrect runtime behavior.

| # | Description | File | Status |
|---|-------------|------|--------|
| B1 | Signed 16-bit conversion loop rebinds local var — negative IMU values returned as large positive integers; garbage orientation data | `src/common/imu_qmi8658_ak09918.py` | `[x]` |
| B2 | AHRS quaternion state (`q0/q1/q2/q3`, `exInt/eyInt/ezInt`) declared as class variables — shared across instances | `src/common/imu_qmi8658_ak09918.py` | `[x]` |
| B3 | `get_uptime_seconds()` returns boot Unix epoch (~1.7 B), not elapsed seconds since boot | `src/common/system_metrics.py` | `[x]` |
| B4 | `ensure_dir()` calls `Path(path)` but `Path` is never imported — `NameError` at runtime | `src/common/utils.py` | `[x]` |
| B5 | OBC heartbeat uses raw f-string JSON construction instead of `json.dumps()` — inconsistent with rest of codebase | `src/obc/main.py` | `[x]` |
| B6 | `payload/main.py` error status value `"error"` inconsistent with `"SUCCESS"` casing | `src/payload/main.py` | `[x]` |

---

## 🔧 Configuration & Deployment Fixes (Priority: High)

| # | Description | File | Status |
|---|-------------|------|--------|
| C1 | `camera.py` hardcodes `PHOTO_DIR = "/home/mik/cubesat-sim/data/photos"` — ignores `config.PHOTOS_DIR` | `src/payload/camera.py` | `[x]` |
| C2 | `requests`, `lgpio`, `pyyaml` missing from `requirements.txt` (`python-dotenv` was already present) | `requirements.txt` | `[x]` |
| C3 | `install.sh` starts only 3 of 5 services — EPS and ADCS are copied to systemd but never enabled | `scripts/install.sh` | `[x]` |
| C4 | Add `config/config.yaml` for runtime config (MQTT broker/port, intervals, photo resolution) and update `config.py` to load from it | `config/config.yaml`, `src/common/config.py` | `[x]` |
| C5 | Add `scripts/restart.sh` to restart all services after a system update | `scripts/restart.sh` | `[x]` |

---

## 🏗️ Architecture: Hardware Abstraction Layer (Priority: Medium)

All subsystems hard-import hardware libraries (`RPi.GPIO`, `smbus2`, `lgpio`, `picamera2`) at module level. This prevents running the simulation on any non-Raspberry Pi machine and makes testing impossible.

**Goal:** introduce a HAL so services depend on abstract interfaces, not concrete hardware.

| # | Description | Status |
|---|-------------|--------|
| H1 | Create `src/hal/interfaces.py` — ABCs: `IPowerMonitor`, `IIMU`, `ICamera`, `IScienceCollector` | `[ ]` |
| H2 | Move `src/eps/power_monitor.py` → `src/hal/rpi/power_monitor.py`, implement `IPowerMonitor` | `[ ]` |
| H3 | Move `src/common/imu_qmi8658_ak09918.py` → `src/hal/rpi/imu_qmi8658_ak09918.py`, implement `IIMU` | `[ ]` |
| H4 | Move `src/payload/camera.py` → `src/hal/rpi/camera.py`, implement `ICamera` | `[ ]` |
| H5 | Move `src/payload/science.py` → `src/hal/rpi/science.py`, implement `IScienceCollector` | `[ ]` |
| H6 | Create mock implementations in `src/hal/mock/` — `MockPowerMonitor`, `MockIMU`, `MockCamera`, `MockScienceCollector` | `[ ]` |
| H7 | Update each service's `main.py` to use `CUBESAT_MOCK_HARDWARE` env var to select real vs. mock HAL | `[ ]` |

---

## 🧪 Testing (Priority: Medium)

Zero tests currently exist. The `tests/` directory is referenced in README but does not exist.

| # | Description | Status |
|---|-------------|--------|
| T1 | Create `tests/` directory, `conftest.py` with pytest fixtures using mock HAL | `[ ]` |
| T2 | `test_state_machine.py` — all transitions, edge cases (can't SCIENCE→SCIENCE), boot sequence | `[ ]` |
| T3 | `test_handlers.py` — EPS battery thresholds (39%→no change, 40%→LOW_POWER, 19%→no SAFE if already SAFE) | `[ ]` |
| T4 | `test_comms_builder.py` — `build_comms_packet()` with missing subsystem data, null values | `[ ]` |
| T5 | `test_eps_logic.py` — `get_battery_percent()`, `get_battery_voltage()` with known raw I2C register values | `[ ]` |
| T6 | `test_utils.py` — `crc16_ccitt()`, `ensure_dir()`, `timestamp_iso()` | `[ ]` |
| T7 | Add `requirements-dev.txt` with `pytest`, `pytest-mock`, `pytest-cov` | `[ ]` |

---

## 🔨 Minor Refactoring (Priority: High)

Small, focused changes to improve protocol consistency across all services.

| # | Description | Files | Status |
|---|-------------|-------|--------|
| RF1 | Standardize `cubesat/obc/status` message format: place `ts` (Unix float) first; rename `state` → `status`. Update all consumers. | `src/obc/state_machine.py`, `src/obc/main.py`, `src/payload/main.py`, `src/comms/service.py` (was `src/telemetry/aggregator.py`) | `[x]` |
| RF2 | Consolidate photo and telemetry commands into `cubesat/command`: remove dedicated `command_photo` and `command_telemetry` topics; add `take_photo` and `get_telemetry` commands to the unified command topic. | `src/common/config.py`, `src/payload/main.py`, `src/comms/service.py` (was `src/telemetry/aggregator.py`) | `[x]` |
| RF3 | Update documentation to reflect RF1 and RF2 changes: `README.md`, `CLAUDE.md`, `docs/architecture.md`. | `README.md`, `CLAUDE.md`, `docs/architecture.md` | `[x]` |

---

## ♻️ Refactoring (Priority: Medium)

Code quality improvements that reduce complexity and prevent future bugs.

| #  | Description | File | Status |
|----|-------------|------|--------|
| R1 | Fix MQTT `on_connect`/`on_disconnect` dead code in factory — module-level defaults are immediately overridden by every caller | `src/common/mqtt_client.py` | `[x]` |
| R2 | Fix OBC boot-time MQTT publish race: `BOOT→DEPLOY→NOMINAL` transitions fire synchronously in `__init__` before MQTT client connects | `src/obc/state_machine.py` | `[x]` |
| R3 | Wire up timelapse support (`start_timelapse` / `stop_timelapse` exist in `camera.py` but are never called) | `src/payload/` | `[x]` |
| R4 | Gate telemetry aggregation on OBC state SCIENCE (currently runs unconditionally every cycle) | src/comms/service.py (was src/telemetry/aggregator.py) | `[x]` |
---

## 🛰️ GPS + LoRa + COMMS Redesign (Priority: High)

The 52Pi IoT Node(A) module (GSM/GPS/LoRa) was on the physical build but unwired — see `docs/hardware-iot-node-a-52pi.md`. This wires up its GPS and LoRa halves and turns the old passive `telemetry` service into `COMMS`, the single point of contact with the ground.

| # | Description | Files | Status |
|---|-------------|-------|--------|
| G1 | Add `GPS` driver (A9G NMEA over UART) and publish `gps` sub-object in `adcs_status` | `src/common/gps_a9g.py`, `src/adcs/main.py` | `[x]` |
| G2 | Rename `src/telemetry/` → `src/comms/`; `TelemetryAggregator` → `CommsService` | `src/comms/` | `[x]` |
| G3 | Add `LoRaModule` driver (SC16IS752 I2C register access, CRC-16-CCITT framing via the existing `crc16_ccitt()`) | `src/comms/lora.py` | `[x]` |
| G4 | Add runtime-toggleable `api_enabled`/`lora_enabled`/`aggregation_enabled` flags + `set_comms_config` ground command (not persisted — reset to config defaults on restart) | `src/comms/service.py` | `[x]` |
| G5 | Re-check internet reachability every COMMS loop iteration instead of once at startup | `src/comms/service.py` | `[x]` |
| G6 | Add periodic SQLite retention cleanup (`COMMS_DB_RETENTION_DAYS`) | `src/comms/service.py` | `[x]` |
| G7 | Add remote-API pending-commands polling and LoRa RX, both re-publishing onto `cubesat/command` unchanged | `src/comms/service.py` | `[x]` |
| G8 | Rename MQTT topic/DB/config surface: `telemetry_data`→`comms_data`, `telemetry.db`→`comms.db`, `TELEMETRY_*`→`COMMS_*` env vars | `src/common/config.py`, `config/config.yaml` | `[x]` |
| G9 | Rename systemd unit + scripts: `cubesat-telemetry.service`→`cubesat-comms.service` | `systemd/`, `scripts/` | `[x]` |
| G10 | **Bench-test on real hardware before enabling** — none of this has been run against the physical 52Pi IoT Node(A) yet: (1) confirm the LoRa register protocol in `lora.py` (writes to `0x01`–`0x20` + trigger at `0x23`) actually produces a real transmission — verify with an SDR (e.g. RTL-SDR + `gqrx`) that there's carrier/chirp activity at 433MHz when `lora.send()` is called, and that `receive()` picks up a reply from a second module; adjust register offsets if they don't match the real vendor map. (2) Confirm `gps_a9g.py`'s NMEA parsing against a live A9G fix (GGA/RMC sentences, correct lat/lon/alt/speed decoding, sane `fix` behavior indoors vs. outdoors) | `src/comms/lora.py`, `src/common/gps_a9g.py` | `[ ]` |

> **`COMMS_LORA_ENABLED` defaults to `0`** specifically because of G10 — flip it to `1` only after the LoRa TX/RX protocol has been bench-verified.
> **Groundstation contract note:** `poll_remote_commands()` (G7) expects a `GET /api/cubesat/commands/pending` endpoint on `cubesat-groundstation` returning queued command envelopes — this is not yet implemented on that side, and the MQTT topic/API URL rename (G8) requires a matching update there too.
> **Deployment note:** on an already-deployed Pi, `.env` files using `TELEMETRY_*` variable names need updating to `COMMS_*`, and the old `cubesat-telemetry.service` unit should be removed manually (`sudo systemctl disable --now cubesat-telemetry.service && sudo rm /etc/systemd/system/cubesat-telemetry.service`).

---

## 📡 Deployment: Standalone Pi Mode-Orchestrator (Priority: Low — separate repo)

Not part of this codebase — a new repo (e.g. `rpi-mode-switcher`) that manages the whole Raspberry Pi, calling this project's `scripts/start.sh`/`stop.sh` as one piece.

| # | Description | Status |
|---|-------------|--------|
| O1 | On internet loss, attempt reconnect; if unsuccessful, stop internet-dependent services listed in its own config (telegram bot, star-map generator, etc.) and start cubesat-sim | `[ ]` |
| O2 | Read a physical GPIO toggle switch at the moment of transition to pick the mode: **simulation** (no Wi-Fi AP, non-essential services off, power-saving, possible CPU underclock) vs **simulation + demo** (also raises a Wi-Fi AP + serves a dashboard) | `[ ]` |

---

## Completed

_Items will be moved here when done._

| # | Description | Date |
|---|-------------|------|
| B1–B6 | All confirmed runtime bugs fixed (IMU sign conversion, AHRS class state, uptime calculation, missing Path import, OBC f-string JSON, payload status casing) | `[x]` |
| C1–C5 | All configuration & deployment fixes (hardcoded photo dir, missing requirements, incomplete install.sh, config.yaml, restart.sh) | `[x]` |
| RF1–RF3 | Minor refactoring: standardized obc/status format (`ts` + `status`), consolidated all commands onto `cubesat/command`, updated README/CLAUDE.md/architecture.md | `[x]` |
| R1–R4 | Refactoring (Medium): removed MQTT factory dead code, fixed OBC boot-time publish race, wired up timelapse commands, gated telemetry aggregation on SCIENCE state | `[x]` |
| G1–G9 | GPS wired into ADCS; `telemetry` renamed to `COMMS` with LoRa TX/RX, remote-API command polling, dynamic api/lora/aggregation flags, and SQLite retention cleanup | `[x]` |

---

## Notes

- **Refactoring steps are independent**: B-bugs, C-config, R1/R4/R5 can all be done in any order.
- **HAL (H1–H7) is a prerequisite for tests (T1–T7)** — mocks must exist before hardware-touching code can be tested.
- `docs/refactoring_plan.md` has detailed implementation guidance (code snippets) for all items listed here.
