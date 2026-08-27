# Refactoring Plan

> **⚠️ Pre-rewrite document.** Written against the five-service codebase. The HAL design below
> (`src/hal/` with interfaces, real drivers and mocks) carries forward and is now **phase P1** of
> the rewrite — see [`../ROADMAP.md`](../ROADMAP.md) — because nothing else in the rewrite can be
> tested without it. The rest is superseded by [`concept.md`](concept.md).
>
> Note that the concrete drivers named here (QMI8658, AK09918, LPS22HB, SHTC3, A9G) are all off the
> satellite. The interfaces they were meant to sit behind are still the right idea; the
> implementations behind them are BNO055, TEL0157, SEN0501 and Meshtastic.

## Proposed Clean Architecture

### Core Principle: Separate Hardware from Logic

The dominant problem is that hardware I/O is directly embedded in business logic classes. The fix is a **Hardware Abstraction Layer (HAL)** — a thin interface layer that lets business logic depend on an abstract sensor interface, not the concrete I2C/GPIO implementation.

```
┌────────────────────────────────────────────────────────────────┐
│                        Service Layer                           │
│   (MQTT wiring, main loop, service orchestration)              │
├────────────────────────────────────────────────────────────────┤
│                        Domain Layer                            │
│   (state machine, business rules, data models)                 │
│   Pure Python — no hardware, no MQTT, no SQLite                │
├────────────────────────────────────────────────────────────────┤
│                Hardware Abstraction Layer (HAL)                 │
│   (abstract interfaces + real RPi + mock implementations)      │
├───────────────┬───────────────────┬────────────────────────────┤
│  Hardware I/O │  Infrastructure   │  Config                    │
│  (smbus2,     │  (paho-mqtt,      │  (env, yaml, constants)    │
│  RPi.GPIO,    │  sqlite3,         │                            │
│  picamera2,   │  psutil)          │                            │
│  lgpio)       │                   │                            │
└───────────────┴───────────────────┴────────────────────────────┘
```

---

## Target Directory Structure

```
cubesat-sim/
├── src/
│   ├── common/
│   │   ├── __init__.py
│   │   ├── config.py              # constants + env loading
│   │   ├── logging_setup.py
│   │   ├── mqtt_client.py         # factory (cleaned up)
│   │   └── utils.py               # crc16, json helpers, timestamp
│   │
│   ├── hal/                       # NEW: Hardware Abstraction Layer
│   │   ├── __init__.py
│   │   ├── interfaces.py          # abstract base classes
│   │   ├── mock/                  # mock implementations for tests/dev
│   │   │   ├── __init__.py
│   │   │   ├── mock_imu.py
│   │   │   ├── mock_power.py
│   │   │   ├── mock_camera.py
│   │   │   └── mock_science.py
│   │   └── rpi/                   # real hardware implementations
│   │       ├── __init__.py
│   │       ├── imu_qmi8658_ak09918.py  # moved from common/
│   │       ├── power_monitor.py        # moved from eps/
│   │       ├── camera.py               # moved from payload/
│   │       └── science.py              # moved from payload/
│   │
│   ├── obc/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── state_machine.py       # no changes needed
│   │   └── handlers.py            # no changes needed
│   │
│   ├── eps/
│   │   ├── __init__.py
│   │   └── main.py                # uses hal.interfaces.IPowerMonitor
│   │
│   ├── adcs/
│   │   ├── __init__.py
│   │   └── main.py                # uses hal.interfaces.IIMU
│   │
│   ├── payload/
│   │   ├── __init__.py
│   │   └── main.py                # uses ICamera + IScienceCollector
│   │
│   └── comms/
│       ├── __init__.py
│       ├── main.py
│       ├── service.py             # split into smaller classes
│       ├── lora.py                # LoRa TX/RX (SC16IS752 I2C driver)
│       ├── packet_builder.py      # NEW: assembles the comms dict
│       └── storage.py             # NEW: SQLite write + retention cleanup logic
│
├── tests/
│   ├── __init__.py
│   ├── test_state_machine.py
│   ├── test_handlers.py
│   ├── test_comms_builder.py
│   ├── test_eps_logic.py
│   └── conftest.py                # pytest fixtures with mocks
│
├── docs/
│   ├── architecture.md
│   ├── code_smells.md
│   └── refactoring_plan.md
│
├── config/
│   ├── config.yaml                # runtime config (intervals, paths)
│   └── secrets.yaml.example       # template (never commit real secrets)
│
├── scripts/
│   ├── install.sh
│   ├── start.sh
│   └── stop.sh
│
├── systemd/
│   └── ...
│
├── requirements.txt
├── requirements-dev.txt           # NEW: pytest, pytest-mock, etc.
└── CLAUDE.md
```

---

## HAL Interface Design

```python
# src/hal/interfaces.py

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

class IPowerMonitor(ABC):
    @abstractmethod
    def get_battery_percent(self) -> Optional[float]: ...
    @abstractmethod
    def get_battery_voltage(self) -> Optional[float]: ...
    @abstractmethod
    def get_external_power(self) -> bool: ...
    def get_status(self) -> Dict:
        return {
            "battery": self.get_battery_percent(),
            "voltage": self.get_battery_voltage(),
            "external_power": self.get_external_power(),
        }

class IIMU(ABC):
    @abstractmethod
    def get_orientation_deg(self) -> Dict: ...
    @abstractmethod
    def read_imu_temp(self) -> float: ...

class ICamera(ABC):
    @abstractmethod
    def take_photo(self, overlay: bool = False) -> Optional[str]: ...
    @abstractmethod
    def cleanup(self) -> None: ...

class IScienceCollector(ABC):
    @abstractmethod
    def collect(self) -> Dict: ...
```

---

## Refactoring Steps

Steps are ordered to minimize risk. Each step is independently mergeable.

---

### Step 1 — Fix bugs (no architecture change)

**Priority: Do this first**

Fix all bugs identified in `code_smells.md` without restructuring anything:

1. **B1 — Signed 16-bit conversion** in `imu_qmi8658_ak09918.py:75-88`:
   ```python
   # Replace the broken loop with explicit conversions:
   def _to_signed16(v: int) -> int:
       return v - 65536 if v >= 32768 else v

   ax, ay, az, gx, gy, gz = (
       _to_signed16(x) for x in (ax, ay, az, gx_raw, gy_raw, gz_raw)
   )
   ```

2. **B2 — AHRS quaternion state**: move `q0`, `q1`, `q2`, `q3`, `exInt`, `eyInt`, `ezInt` from class variables to `__init__`.

3. **B3 — `get_uptime_seconds`**: fix to `return int(time.time() - psutil.boot_time())`.

4. **B4 — Missing `Path` import** in `utils.py`: add `from pathlib import Path`.

5. **D14 — Malformed f-string** in `payload/main.py`: replace with `json.dumps({...})`.

---

### Step 2 — Fix configuration

**Goal:** eliminate hardcoded paths and make the project deployable without editing source code.

1. Add `config/config.yaml` with keys for: MQTT broker/port, photo directory, COMMS loop interval, log level, photo resolution.
2. Update `src/common/config.py` to load `config.yaml` (with env var overrides).
3. Remove the hardcoded `PHOTO_DIR = "/home/mik/cubesat-sim/data/photos"` in `camera.py`; use `PHOTOS_DIR` from config.
4. Update `systemd/` unit files to use a configurable `WorkingDirectory`.

---

### Step 3 — Create the HAL

**Goal:** decouple hardware from logic, enable testing.

1. Create `src/hal/interfaces.py` with abstract base classes: `IPowerMonitor`, `IIMU`, `ICamera`, `IScienceCollector`.

2. Move hardware implementations to `src/hal/rpi/`:
   - `src/eps/power_monitor.py` → `src/hal/rpi/power_monitor.py` (implement `IPowerMonitor`)
   - `src/common/imu_qmi8658_ak09918.py` → `src/hal/rpi/imu_qmi8658_ak09918.py` (implement `IIMU`)
   - `src/payload/camera.py` → `src/hal/rpi/camera.py` (implement `ICamera`)
   - `src/payload/science.py` → `src/hal/rpi/science.py` (implement `IScienceCollector`)

3. Create mock implementations in `src/hal/mock/`:
   - `MockPowerMonitor`: returns configurable static or incrementally-changing values
   - `MockIMU`: returns sinusoidal roll/pitch/yaw to simulate movement
   - `MockCamera`: copies a test image file, returns its path
   - `MockScienceCollector`: returns static or randomised T/H/P values

4. Update each service's `main.py` to accept the HAL object via constructor injection:
   ```python
   # eps/main.py
   import os
   from src.hal.interfaces import IPowerMonitor

   def build_monitor() -> IPowerMonitor:
       if os.getenv("CUBESAT_MOCK_HARDWARE"):
           from src.hal.mock.mock_power import MockPowerMonitor
           return MockPowerMonitor()
       from src.hal.rpi.power_monitor import EPSMonitor
       return EPSMonitor()
   ```

---

### Step 4 — Split CommsService

**Goal:** apply Single Responsibility.

Extract from `CommsService` into:

- `CommsCache` — dict of latest subsystem data, updated by MQTT callbacks
- `CommsPacketBuilder` — pure function `build_comms_packet(cache, system_metrics) -> dict`
- `CommsStorage` — wraps SQLite: `save(packet)`, `_create_table()`, retention cleanup
- `CommsService` — thin orchestrator: wires MQTT, calls builder/storage, drives the LoRa (`lora.py`) and remote-API channels

This makes `CommsPacketBuilder` trivially unit-testable with no MQTT or SQLite.

---

### Step 5 — Write tests

**Goal:** establish a baseline test suite.

Create `tests/` with `pytest` and `pytest-mock`:

1. `test_state_machine.py`: test all state transitions, including edge cases (can't go SCIENCE → SCIENCE).
2. `test_handlers.py`: test EPS status thresholds (39% → no transition, 40% → LOW_POWER, 19% → no SAFE if already SAFE).
3. `test_comms_builder.py`: test `build_comms_packet()` with various cache states (missing subsystem data, null values).
4. `test_eps_logic.py`: test `get_battery_percent()`, `get_battery_voltage()` with known raw register values.
5. `test_utils.py`: test `crc16_ccitt()`, `ensure_dir()`, `timestamp_iso()`.

Add `requirements-dev.txt`:
```
pytest
pytest-mock
pytest-cov
```

Add to `Makefile` or document in `CLAUDE.md`:
```bash
PYTHONPATH=. pytest tests/ -v
PYTHONPATH=. pytest tests/ --cov=src --cov-report=term-missing
```

---

### Step 6 — Fix `mqtt_client.py` dead code

**Goal:** make the factory actually useful.

Option A (minimal): Remove the module-level `on_connect`/`on_disconnect` defaults from the factory since every caller overrides them anyway. Each service defines its own handlers completely.

Option B (better): Move common reconnection logging into a base class or mixin:
```python
class MQTTService:
    def _on_connect_base(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logger.error(f"MQTT connection failed: {reason_code}")
            return
        logger.info(f"MQTT connected: {client._client_id.decode()}")
        self._on_connected(client)  # subclass hook

    def _on_connected(self, client):
        """Override to subscribe topics after connect."""
        pass
```

---

### Step 7 — Fix MQTT payload consistency

**Goal:** eliminate string-format JSON and standardise on `reason_code`.

1. Replace all `f'{{...}}'` JSON construction with `json.dumps({...})`.
2. Update all `on_connect` callbacks to use `reason_code` consistently (not `rc`).
3. Add a `payload_status` message on `PayloadService.__init__` that uses `json.dumps`.

---

### Step 8 — Camera lifecycle fix

**Goal:** reduce photo latency and hardware wear.

Keep the camera initialised as long as the payload service is running, rather than re-initialising per photo:

```python
class PayloadCamera:
    def __init__(self, ...):
        self._picam2 = None

    def start(self):
        self._picam2 = self._init_camera()

    def take_photo(self, overlay=False):
        if self._picam2 is None:
            raise RuntimeError("Camera not started")
        ...  # use self._picam2 directly

    def cleanup(self):
        if self._picam2:
            self._picam2.stop()
            self._picam2.close()
```

---

## Effort Estimate by Step

| Step | Description | Effort | Risk |
|---|---|---|---|
| 1 | Fix bugs | Small | Low |
| 2 | Fix configuration | Small | Low |
| 3 | Create HAL | Medium | Medium |
| 4 | Split Aggregator | Small | Low |
| 5 | Write tests | Medium | Low |
| 6 | Fix mqtt_client | Small | Low |
| 7 | Fix MQTT payloads | Small | Low |
| 8 | Camera lifecycle | Small | Low |

Steps 1, 2, 4, 6, 7, 8 can be done in any order. Step 3 (HAL) should come before Step 5 (tests), as mocks are prerequisites for testing hardware-touching code.

---

## What Not To Change

- The state machine logic in `obc/state_machine.py` is clean and correct (modulo the class variable bug).
- The MQTT topic naming in `config.py` is well-organised; centralisation is good.
- The logging setup pattern is functional; only the module-level side-effect is problematic.
- The `SystemMetricsCollector` design (static methods + one `collect()`) is clean.
