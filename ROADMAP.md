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
| T4 | `test_telemetry_builder.py` — `build_packet()` with missing subsystem data, null values | `[ ]` |
| T5 | `test_eps_logic.py` — `get_battery_percent()`, `get_battery_voltage()` with known raw I2C register values | `[ ]` |
| T6 | `test_utils.py` — `crc16_ccitt()`, `ensure_dir()`, `timestamp_iso()` | `[ ]` |
| T7 | Add `requirements-dev.txt` with `pytest`, `pytest-mock`, `pytest-cov` | `[ ]` |

---

## 🔨 Minor Refactoring (Priority: High)

Small, focused changes to improve protocol consistency across all services.

| # | Description | Files | Status |
|---|-------------|-------|--------|
| RF1 | Standardize `cubesat/obc/status` message format: place `ts` (Unix float) first; rename `state` → `status`. Update all consumers. | `src/obc/state_machine.py`, `src/obc/main.py`, `src/payload/main.py`, `src/telemetry/aggregator.py` | `[x]` |
| RF2 | Consolidate photo and telemetry commands into `cubesat/command`: remove dedicated `command_photo` and `command_telemetry` topics; add `take_photo` and `get_telemetry` commands to the unified command topic. | `src/common/config.py`, `src/payload/main.py`, `src/telemetry/aggregator.py` | `[x]` |
| RF3 | Update documentation to reflect RF1 and RF2 changes: `README.md`, `CLAUDE.md`, `docs/architecture.md`. | `README.md`, `CLAUDE.md`, `docs/architecture.md` | `[x]` |

---

## ♻️ Refactoring (Priority: Medium)

Code quality improvements that reduce complexity and prevent future bugs.

| # | Description | File | Status |
|---|-------------|------|--------|
| R1 | Split `TelemetryAggregator` god-object into `TelemetryCache`, `TelemetryPacketBuilder`, `TelemetryStorage`, and thin orchestrator | `src/telemetry/aggregator.py` | `[ ]` |
| R2 | Fix MQTT `on_connect`/`on_disconnect` dead code in factory — module-level defaults are immediately overridden by every caller | `src/common/mqtt_client.py` | `[ ]` |
| R3 | Fix OBC boot-time MQTT publish race: `BOOT→DEPLOY→NOMINAL` transitions fire synchronously in `__init__` before MQTT client connects | `src/obc/state_machine.py` | `[ ]` |
| R4 | Gate telemetry aggregation on OBC state `SCIENCE` (currently runs unconditionally every cycle) | `src/telemetry/aggregator.py` | `[ ]` |
| R5 | Fix camera lifecycle — reinitialise `picamera2` per-photo causes latency and hardware wear; keep camera open for service lifetime | `src/payload/camera.py` | `[ ]` |
| R6 | Wire up timelapse support (`start_timelapse` / `stop_timelapse` exist in `camera.py` but are never called) | `src/payload/` | `[ ]` |
| R7 | Move OBC commented-out subsystem control commands (`wifi`, `payload_power`) out of `on_enter_*` callbacks and into proper command handlers | `src/obc/state_machine.py` | `[ ]` |

---

## 🚀 New Features (Priority: Low)

New capabilities not yet implemented.

| # | Description | Status |
|---|-------------|--------|
| F1 | **`comm/` subsystem** — LoRa/WiFi communication module; `crc16_ccitt()` in `utils.py` and architecture docs reference this, but the module does not exist | `[ ]` |
| F2 | **Ground station CLI** — command-line tool to send MQTT commands (`science_start`, `safe_mode`, `photo`, etc.) to the running simulation for manual testing | `[ ]` |
| F3 | **Web dashboard** — browser UI to visualise live telemetry from the SQLite database and MQTT stream | `[ ]` |
| F4 | **Mission replay** — load a `telemetry.db` and replay the mission timeline for post-flight analysis | `[ ]` |
| F5 | **Orbit simulation** — integrate a simple SGP4/TLE propagator to add position data (lat/lon/alt) to telemetry packets | `[ ]` |
| F6 | **Photo annotation overlay** — `camera.py` already has `overlay=False` parameter; implement ADCS attitude + timestamp overlay on captured images | `[ ]` |
| F7 | **Anomaly detection in OBC** — add watchdog that detects subsystem silence (no MQTT heartbeat within N seconds) and triggers `safe_mode` | `[ ]` |
| F8 | **Multi-node simulation** — run multiple CubeSat instances on the same MQTT broker with distinct client IDs to simulate a constellation | `[ ]` |

---

## 📋 Documentation Gaps

| # | Description | Status |
|---|-------------|--------|
| D1 | README references `tests/` directory and test files that do not exist — update or remove | `[ ]` |
| D2 | Document `CUBESAT_MOCK_HARDWARE` env var and local development workflow once HAL is implemented | `[ ]` |
| D3 | Add `CONTRIBUTING.md` with branch/PR conventions and how to run tests locally | `[ ]` |

---

## Completed

_Items will be moved here when done._

| # | Description | Date |
|---|-------------|------|
| B1–B6 | All confirmed runtime bugs fixed (IMU sign conversion, AHRS class state, uptime calculation, missing Path import, OBC f-string JSON, payload status casing) | 2026-03-13 |
| C1–C5 | All configuration & deployment fixes (hardcoded photo dir, missing requirements, incomplete install.sh, config.yaml, restart.sh) | 2026-03-13 |
| RF1–RF3 | Minor refactoring: standardized obc/status format (`ts` + `status`), consolidated all commands onto `cubesat/command`, updated README/CLAUDE.md/architecture.md | 2026-03-13 |

---

## Notes

- **Refactoring steps are independent**: B-bugs, C-config, R1/R4/R5 can all be done in any order.
- **HAL (H1–H7) is a prerequisite for tests (T1–T7)** — mocks must exist before hardware-touching code can be tested.
- `docs/refactoring_plan.md` has detailed implementation guidance (code snippets) for all items listed here.
