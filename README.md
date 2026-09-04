# CubeSat Sim

CubeSat Sim is a working flight software stack for a real, physical CubeSat — not just a simulation on paper. Eight independent Python services each model one satellite subsystem and talk to each other exclusively over MQTT, the same way modules communicate over a real spacecraft bus. Two state machines drive it: a **platform profile** chosen by the operator decides what the Raspberry Pi is allowed to be (desk host, live demo, standalone exhibit, autonomous field unit), and a **mission state** machine reacts on its own to battery telemetry and faults inside that envelope. A separate privileged service is the only thing in the project that may touch the host; everything else runs unprivileged.

Every driver sits behind a hardware abstraction layer, so the whole stack runs on a laptop against
mocked sensors — which is how it is developed, and how its 1400 tests reach 100 % line coverage
without a Raspberry Pi in the room.

![CubeSat Sim](hardware/photos/cover.jpg)

It runs on a Raspberry Pi inside a 3D-printed frame, wired to real sensors — a battery fuel gauge, a 9-axis absolute-orientation IMU, a GNSS receiver, an environmental sensor package, a camera, and a LoRa radio. Everything needed to build one yourself is in this repo: the code, the [3D models](hardware/models), and the [hardware list](#hardware).

If you're learning satellite software architecture, distributed systems, or embedded Python, feel free to dig in, fork it, and adapt it to your own build. If it's useful to you, a star helps other people find it.

---

## Project Status

**The hardware is finished.** Every component in the [Hardware](#hardware) table is on the assembled satellite and bench-validated — the full I2C bus was verified in a single scan with every module attached, and the LoRa link was confirmed in both directions over the air.

**The software is written, and it has now run on the satellite — in two of the six profiles, for
minutes.** All eight services exist, at 100 % line coverage with `ruff` and `mypy` clean in CI.
Every one of them has been exercised against the mock HAL for months; what is new is that they have
also been started on the Raspberry Pi:

- **2026-08-31, `HOSTED`** — `HOSTD`, `OBC`, `EPS` and `COMMS`. Five I2C devices answered, the
  Meshtastic node was reachable, a ground command round-tripped from a phone, and `SAFE` was
  entered and cleared for real.
- **2026-09-01, `DEMO`** — `ADCS`, `PAYLOAD`, `DHS` and `DASHBOARD` joined them. `DEPLOY` completed
  in 1.4 s into `NOMINAL`, and the attitude widget and a photograph were watched live in a browser.
- **2026-09-02, `MAINTENANCE`** — applied for the first time, to free `/dev/serial0` for the
  Heltec's modem preset. It did what it says: `COMMS` and the external units stopped, `cubesat.local`
  stayed up, and `HOSTED` brought everything back with no `SAFE` and no lost subsystem.
- **`FLIGHT`, `EXPO` and `DIAG` have never been applied.** So what is untried is now a set of
  profiles rather than a set of services: the field radio-only case, the access point, a GNSS fix
  taken while moving, and the separate `diag.db`.

Running for minutes on a desk and being validated are different kinds of confidence, and this README
does not blur them:

| Mark | Meaning |
|---|---|
| ✅ | Bench-validated on the assembled satellite |
| 🛰️ | Has run on the assembled satellite — the date and the profile are in the section that carries the mark |

There is no third mark for "never run on hardware": no service and no driver is in that state any
more. But every driver having been read from on the satellite is not the same as its numbers being
trusted — the register maps come from the bench notes in `docs/hardware-*.md`, and where a constant
had to come from a datasheet instead, the driver says so at the constant.
[`ROADMAP.md`](ROADMAP.md) carries the list of checks only the bench can settle — the ones that
would otherwise produce plausible wrong data rather than an error, which is the class of fault this
whole codebase is arranged around. What the hardware has found in the logic so far is short and all
of it is fixed: `restart_service` latched `SAFE` (2026-09-01), and then a single evening on the mesh
produced four — a command answered while the beacon was off went nowhere, `beacon off` swallowed
its own confirmation, `!photo` was answered with the weather, and the uplink had no channel filter
at all, which a stranger's chat appearing in the dashboard's Radio Link Log revealed
(2026-09-02, all fixed 2026-09-03). Everything else it found was in the deployment or in the
environment, which is the shape of surprise a mock HAL cannot produce: it can fail a sensor, but not
a shared directory, a file lock, a fuel gauge that answers as the wrong part, or a modem preset that
moves the satellite into a room with several hundred strangers.

Read [docs/concept.md](docs/concept.md) for *why* the design looks like this; this README is the
reference for *what* it is.

---

## Table of Contents

- [Project Status](#project-status)
- [Operating Concept](#operating-concept)
  - [Platform Profiles](#platform-profiles)
  - [Mission States](#mission-states)
- [Architecture Overview](#architecture-overview)
- [Services](#services)
  - [HOSTD](#hostd)
  - [OBC](#obc)
  - [EPS](#eps)
  - [ADCS](#adcs)
  - [PAYLOAD](#payload)
  - [DHS](#dhs)
  - [COMMS](#comms)
  - [DASHBOARD](#dashboard)
  - [Common Infrastructure](#common-infrastructure)
- [Hardware Ownership](#hardware-ownership)
- [Mission Sessions](#mission-sessions)
  - [Resuming an interrupted trip](#resuming-an-interrupted-trip)
- [MQTT Topic Reference](#mqtt-topic-reference)
- [Message Payloads](#message-payloads)
- [Data Flows](#data-flows)
- [Directory Structure](#directory-structure)
- [Hardware](#hardware)
  - [I2C Address Map](#i2c-address-map)
  - [New Components](#new-components)
  - [Mechanical / Fasteners](#mechanical--fasteners)
  - [3D Models](#3d-models)
  - [Build Photos](#build-photos)
- [Setup and Running](#setup-and-running)
- [Configuration](#configuration)
- [Logs](#logs)
- [Documentation](#documentation)
- [Related Projects](#related-projects)

---

## Operating Concept

The unit is used in four quite different situations, and each one wants a different Raspberry Pi — not just a different satellite state. So there are two orthogonal axes:

| Axis | What it governs | Who chooses it | How often it changes |
|---|---|---|---|
| **Platform profile** | Wi-Fi client / AP / off, which systemd units run at all, dashboard, CPU governor, whether persistence and each downlink channel are permitted | **a human**, deliberately | rarely |
| **Mission state** | Sensor cadence, camera permission, logging cadence, radio duty cycle | **the satellite**, from EPS telemetry and faults | any time, autonomously |

The profile defines the **envelope of what is permitted**; the state defines the **activity level inside that envelope**. A profile change is an external event to the mission state machine; a state change never alters the profile — with one exception, `CRITICAL`, which powers the host down.

### Platform Profiles

| Profile | Use case | Wi-Fi | External units | Mission services | Dashboard | Persistence | Downlink |
|---|---|---|---|---|---|---|---|
| `HOSTED` | Default. On the desk, mains powered, satellite idle | client | **running** | COMMS, listening | — | — | LoRa (RX only) |
| `DEMO` | Showing the satellite off at home | client | running | all | ✅ | — (in RAM) | LoRa, **beacon off** |
| `EXPO` | Science fair, school, library, office; usually on battery | **own AP** | stopped | all | ✅ | — (in RAM) | LoRa, **beacon off** |
| `FLIGHT` | On the move: a trip, the walk to work | **off** | stopped | all | — | ✅ + GNSS track + photos | LoRa only |
| `DIAG` | `FLIGHT` rehearsed on the desk: is the trip I am about to take going to record? | client | stopped | all | ✅ | separate DB | LoRa |
| `MAINTENANCE` | `apt upgrade`, `git pull`, reflashing the radio | client | stopped | — | — | — | — |

**Only `FLIGHT` and `DIAG` write to the SD card** (decided 2026-09-01). A demonstration is not a
mission: the satellite stands on a desk, so there is no track to record, and every showing used to
leave a row in the same table as real trips. What the dashboard needs is served without a card
write — the live widgets read the broker, and the charts' history is a bounded in-memory ring in
`DASHBOARD`, fed by `cubesat/dhs/telemetry`. `DHS` still runs in `DEMO` and `EXPO` as the assembler
of that row, which is also the only carrier of the host's own CPU, RAM and disk.

`DIAG` exists because of the awkward consequence: `FLIGHT` is the one recording profile with no
dashboard, so the profile whose recording matters most is the one whose recording cannot be watched.
`DIAG` is `FLIGHT` with the network and the dashboard kept — same cadences, same radio, same
automatic photography, the same code and migrations — writing to `diag.db` so a rehearsal never
lands in the archive of real trips. It used to mean "everything polled fast after a re-assembly";
the I2C sweep and the self-test that described are what `DEPLOY` already does on every ascent, in
every profile.

"External units" are the services this repository does not own — a Telegram bot, a star-map generator — named in a registry so `HOSTD` may start and stop them by an explicit allowlist.

Profiles are data, not code: they live in `config/profiles.yaml`.

**There is no remote ground-station API.** There was a design for one — POST each packet to a
cloud service, poll it for queued commands — and it is gone: no such deployment was ever made, and
the ground segment is being rebuilt as an interface over the satellite's own dashboard rather than
a service the satellite reports into. `downlink` names one channel now, and LoRa and the local
dashboard are the only ways off the satellite.

**The radio listens in every operational profile.** `COMMS` runs everywhere except `MAINTENANCE`
(reflashing the Heltec needs `/dev/serial0` free) and is deaf only there. `DIAG` was deaf too until
it became a rehearsal of `FLIGHT`, where the beacon and the uplink are exactly what cannot be
watched. Listening costs no airtime, and every boot lands in `HOSTED` —
including a reboot in the field, where an uplinked `set_profile` over LoRa is the only way back in
without SSH. Transmission is still rationed by the per-state beacon table, which never names
`STANDBY`: on the desk the radio hears everything and says nothing on its own.

**A profile is never persisted across a reboot — every boot starts in `HOSTED`.** A profile is a statement about the current situation, and a boot means the situation is no longer known. The decisive case is the ordinary one: the satellite is carried on a trip in `FLIGHT`, the battery hits `CRITICAL`, it shuts itself down, and hours later it is plugged in at a desk — resuming `FLIGHT` there would leave Wi-Fi down and no SSH on a unit sitting on mains next to its operator. This also makes a power cycle the simplest recovery path there is: whatever profile the satellite is stuck in, pulling power brings it back on the home network. `HOSTD` records the last applied profile to `/var/lib/cubesat/last-profile` and publishes it on `host_status`; it is *evidence*, never instruction — the file never decides whether to restore a profile, only which one, and only after a measurement has already said the satellite is not on a desk. That is the single exception, `FLIGHT` resuming itself after a reset, and it is set out under [Resuming an interrupted trip](#resuming-an-interrupted-trip).

### Mission States

```
 BOOT
   │  self-test complete
   ▼
 STANDBY        ◄── also where the satellite returns when an active profile is left
   │  an active profile is entered (DEMO / EXPO / FLIGHT / DIAG)
   ▼
 DEPLOY         ──► SAFE, if an expected address is silent or a subsystem never reports
   │  bring-up complete
   ▼
 NOMINAL        ──► the state an active profile stays in; the recorder runs here
   │
   │  pack < 3.64 V
   ▼
 LOW_POWER      ──► NOMINAL, on recover: pack ≥ 3.75 V, or mains restored
   │
   │  pack < 3.58 V, a subsystem lost, or the safe_mode command
   ▼
 SAFE
   │
   │  pack < 3.45 V
   ▼
 CRITICAL       ──► flush the recorder, then graceful poweroff
```

`safe_mode` reaches `SAFE` from any state; `CRITICAL` is reachable from any state too, and it is the only state permitted to change host power.

| State | Meaning |
|---|---|
| `BOOT` | Power-on self-test |
| `STANDBY` | Bus alive, mission not active — the state under `HOSTED` and `MAINTENANCE` |
| `DEPLOY` | Subsystem bring-up: an I2C presence sweep for the addresses this profile's services need, then a first **status** message from each of them. A heartbeat is not enough — a service whose sensor is dead still heartbeats. On failure → `SAFE`, not a pretended `NOMINAL` |
| `NOMINAL` | Healthy, subsystems polled at nominal cadence. Where an active profile lives: the mission records, the camera is permitted, the beacon runs |
| `LOW_POWER` | Pack under 3.64 V (≈ 30 %): cadence stretched, camera refused, radio duty-cycled, `powersave` governor |
| `SAFE` | Pack under 3.58 V (≈ 20 %), a fault, or a ground command |
| `CRITICAL` | Pack under 3.45 V (≈ 10 %) **and not on mains**: flush and `poweroff`. The X728 brings the Pi back when mains returns |

**The thresholds are volts, not percentages** (2026-09-04). They were percentages for as long as the satellite believed its fuel gauge; the gauge turned out to be a MAX17040/41 whose state of charge is reconstructed from an internal model with no current sense behind it, and that model was measured drifting down at 8–10 %/h on mains with the terminal voltage flat to the millivolt. So the descents compare `VCELL`, which is measured at 1.25 mV per LSB, and the percentages above are what `common/battery.py` maps those voltages to — an **inferred** curve, shown for orientation and in the path of no decision. One discharge log replaces the annotation without touching the behaviour ([ROADMAP](ROADMAP.md) V15). Percentages still exist everywhere a person reads one: the dashboard, the beacon's `b` field, `cubesat status`, the `battery` column.

Entering an active profile triggers `DEPLOY`; leaving one returns to `STANDBY`. Recovery from `LOW_POWER` happens at ≥ 3.75 V — a 110 mV band, so the state cannot flap around the threshold. The band is wider in volts than the ten points it replaced, and deliberately: ten points on the plateau is about 60 mV, while the load moving on or off the pack is worth 50 mV by itself (measured at an unplug, 2026-09-03). A modelled percentage absorbed that; a voltage does not, so hysteresis has to clear the load swing rather than the fitting noise. It is not wider still because a band a pack on a walk never climbs out of would be a one-way door, not hysteresis.

**The level compared is a median, not a sample.** EPS publishes both — `voltage` as read and `voltage_median` over `eps.level_window_sec` (120 s) — and the policy reads the median, because a threshold in volts is sensitive to a transient in a way a threshold in modelled percent was not: a camera capture is worth tens of millivolts and `SAFE` to `CRITICAL` is 130 mV apart, so one unlucky sample could descend a state on its own. A median rather than a mean, because a mean carries the transient into the answer.

**There was a `SCIENCE` state above `NOMINAL`, entered and left by ground command, and it was removed on 2026-09-02.** It had no content: every cadence row, the beacon interval, the camera permission and the recording rule were identical to `NOMINAL`, so `science start` changed a word on the wire and nothing a service could act on. What such a state would have been *for* — deciding whether telemetry is recorded — belongs to the profile, and how often to the state; the pre-rewrite build gated writes on it and that gate is what made `FLIGHT` record nothing unless somebody remembered a command. Cadence and photo interval are configuration, and the only thing that changes them at runtime is the power-driven descent, which is the satellite's decision rather than an operator's.

**Every power-driven descent is suppressed while mains is present**, because on mains there is no power emergency to react to. This is not a refinement, it is the difference between working and bricked: come back from a trip with a flat pack, plug the satellite in, and a `CRITICAL` keyed on battery level alone would power the host off — and the X728 would not bring it back, because mains never left. Plugging in a satellite at 5 % must take it to `NOMINAL`.

Mains alone is not trusted, though. A faulty charger or a stuck PLD pin would otherwise suppress the protection forever, so "on mains" means `external_power` **and** a pack that is not still going down. A pack draining while the satellite believes it is plugged in is not on mains as far as the power policy is concerned, and it still reaches `CRITICAL`.

**What "still going down" means is `voltage_rate`, in mV/h, and nothing else** (2026-09-04). It used to be `charge_rate` alone, until that was measured on the hardware: this gauge has no current sense, its state of charge is a *model*, and that model was watched drifting down at 8–10 %/h for an hour while the satellite sat on mains with its charge LEDs lit and its terminal voltage flat to the millivolt. The satellite therefore believed it was on battery at a desk, and `SAFE` and `CRITICAL` — which do not ask what state it is in — were hours from powering off a plugged-in unit that the X728 could not have restarted.

The first fix required both slopes to agree, the measured one and the modelled one, on the reasoning that a failed charger moves both while a settling model moves only one. That reasoning was sound and it lasted a day: making the percentage a function of the voltage made its slope a function of the voltage slope, so "both agree" became a condition that could not be false. A fence that cannot fail is worse than no fence, because the code then claims two checks where reality has one. So there is one, and it is the measured one — under −30 mV/h the pack is delivering current, whatever the pin says. A missing slope (EPS' first five minutes, and five minutes after the pin changes) falls back to trusting the pin, because a missing measurement must not power off a plugged-in satellite. Thresholds and the measurements behind them are at `obc/power_policy.py`.

Two things `DEPLOY` deliberately does **not** do. It does not require a GNSS fix: `DEMO` and `EXPO` run indoors, where a fix never arrives, and failing on that would send every indoor demonstration to `SAFE`. A fix is waited for best-effort and its absence is logged. And OBC does not read any device itself beyond the presence check — ADCS owns the IMU and the GNSS receiver, PAYLOAD owns the environmental sensor and the camera, COMMS owns the radio. Each subsystem's first status message *is* the proof its own hardware answered, and two processes reaching for one device over a 10 kHz bus is exactly the contention the [bus lock](#hardware-ownership) exists to prevent.

`LOW_POWER` is not just a label. Concretely:

| Knob | `NOMINAL` | `LOW_POWER` |
|---|---|---|
| ADCS poll | 2 Hz | 0.2 Hz |
| Payload science poll | 60 s | 300 s |
| Telemetry row cadence | 30 s | 300 s |
| Camera | permitted | refused |
| LoRa TX | every cycle | every Nth cycle |
| CPU governor | `ondemand` | `powersave` |

Notably, `LOW_POWER` and `SAFE` do **not** tear down the access point or the dashboard under `EXPO`: losing the display in front of an audience because the battery crossed a threshold would be the wrong behaviour, and the AP costs far less than the sensors it is throttling.

The trigger was 40 % until 2026-09-02, then 30 %, and since 2026-09-04 it is 3.64 V — the same place on the pack, expressed in the quantity that is actually measured. 40 % of an 18650 pair is a long way from an emergency, and throttling there cost the second half of a trip — the more interesting half — for no gain: what protects the card is `SAFE` and `CRITICAL`, and both are where they were.

---

## Architecture Overview

Each service is an independent Python process. No service calls another directly — all communication goes through a local MQTT broker. The design is split into three tiers by privilege:

```
╔═════════════════════════════════════════════════════════════════════════════╗
║ PRIVILEGED (root)                                                           ║
║   HOSTD ──► systemd units · NetworkManager / hostapd · cpufreq · poweroff   ║
╚═══════════▲══════════════════════════════════════════════╤══════════════════╝
       host/command                                   host/status (retained)
┌───────────┴──────────────────────────────────────────────▼──────────────────┐
│                        MQTT Broker (mosquitto)                              │
│                                                                             │
│  cubesat/command              cubesat/host/status    (retained)             │
│  cubesat/host/command         cubesat/obc/status     (retained)             │
│                               cubesat/eps/status     (retained)             │
│                               cubesat/adcs/status                           │
│                               cubesat/payload/status (retained)             │
│                               cubesat/payload/data                          │
│                               cubesat/payload/photo                         │
│                               cubesat/dhs/status                            │
│                               cubesat/comms/data     (on-demand)            │
└──┬──────────┬─────────┬──────────┬──────────┬──────────┬────────────────────┘
   │          │         │          │          │          │
   ▼          ▼         ▼          ▼          ▼          ▼
┌──────┐  ┌──────┐  ┌──────┐  ┌────────┐  ┌──────┐  ┌───────────┐
│ OBC  │  │ EPS  │  │ ADCS │  │PAYLOAD │  │ DHS  │  │   COMMS   │
│      │  │      │  │      │  │        │  │      │  │           │
│2 FSM │  │Fuel  │  │Orient│  │Science │  │SQLite│  │LoRa mesh  │
│Health│  │gauge │  │+ GNSS│  │+ Camera│  │Reten-│  │Beacon     │
│      │  │Mains │  │      │  │        │  │tion  │  │Uplink     │
└──────┘  └──┬───┘  └──┬───┘  └───┬────┘  └──┬───┘  └─────┬─────┘
             │         │          │          │            │
          I2C 0x36  I2C 0x28   I2C 0x22   data/*.db   /dev/serial0
          + GPIO    I2C 0x20   + CSI          │       (Meshtastic)
          (PLD)                               │
                                              ▼
                                        ┌───────────┐
                                        │ DASHBOARD │──► browser
                                        │   REST    │    (live data arrives
                                        └───────────┘     from mosquitto:9001)
                          Raspberry Pi 4 · I2C bus 1 @ 10 kHz
```

The key property is that **no service knows whether the others exist**. `COMMS` does not know whether anything is being recorded. `DHS` does not know whether there is a radio. `OBC` does not know whether the access point came up — it learns that from `cubesat/host/status`, where the *achieved* profile is reported separately from the *requested* one.

---

## Services

| # | Service | Unit | Runs as | Always on | Responsibility | Status |
|---|---|---|---|---|---|---|
| 0 | broker | `mosquitto` | own user | ✅ | The message bus. Every control path in the design runs through it | ✅ |
| 1 | [HOSTD](#hostd) | `cubesat-hostd` | **root** | ✅ | Host executor: units, Wi-Fi mode, CPU governor, power-off, profile state file | 🛰️ |
| 2 | [OBC](#obc) | `cubesat@obc` | `cubesat` | ✅ | Both state machines, command parsing, subsystem health monitoring | 🛰️ |
| 3 | [EPS](#eps) | `cubesat@eps` | `cubesat` + `i2c`,`gpio` | ✅ | Battery and mains telemetry — the input that drives `LOW_POWER`/`SAFE`/`CRITICAL` | 🛰️ |
| 4 | [ADCS](#adcs) | `cubesat@adcs` | `cubesat` + `i2c` | by profile | Absolute orientation (BNO055) and position (TEL0157) | 🛰️ |
| 5 | [PAYLOAD](#payload) | `cubesat@payload` | `cubesat` + `i2c`,`video` | by profile | Environmental science (SEN0501) and the camera | 🛰️ |
| 6 | [DHS](#dhs) | `cubesat@dhs` | `cubesat` | by profile | The flight recorder: owns the SQLite database, writes rows, enforces retention | 🛰️ |
| 7 | [COMMS](#comms) | `cubesat@comms` | `cubesat` + `dialout` | all but `MAINTENANCE` | The only link outward: LoRa mesh, uplink re-publish | 🛰️ |
| 8 | [DASHBOARD](#dashboard) | `cubesat-dashboard` | `cubesat` | by profile | The static UI and read-only REST over the database. No WebSocket — browsers talk to the broker | 🛰️ |
| — | external units | e.g. `telegram-bot` | their own | by profile | Not this repository's code — only named in `config/profiles.yaml` | — |

Startup order: `mosquitto` → `HOSTD` (applies the default profile, `HOSTED`) → `OBC` (picks the profile up from the retained `host/status`) → `EPS`. Nothing else runs until a profile asks for it. All units are `Restart=always`.

Two invariants are enforced in code, not by convention:

1. **`HOSTD` may never stop `cubesat@obc`, `mosquitto` or `NetworkManager`** — the first two are the branch it is sitting on, the third is the way back to a reachable profile. None of them is in the set of units any profile may touch.
2. **`OBC` may never stop `cubesat-hostd`** — the same reason in the other direction.

### HOSTD

**Path:** `src/cubesat/hostd/` | **Client ID:** `hostd` | **Runs as root** | 🛰️ `HOSTED` 2026-08-31

The hands. `HOSTD` has no decision logic at all: it consumes a fixed vocabulary of actions on `cubesat/host/command`, executes them, and reports what it actually achieved on `cubesat/host/status`. It is deliberately small enough to audit by reading it.

It is the only component in the project with privileges, and it acts only on an **explicit allowlist**, built once at startup: the mission services' `cubesat@*` instances, the dashboard, `avahi-daemon` (HOSTD's own lever for `cubesat.local`), and exactly what `config/profiles.yaml` names under `external_units`. A typo in a profile therefore cannot take down `sshd`.

The allowlist has a second job beyond preventing that: it is the **single readable inventory of everything root can `systemctl`** in this project. Anything HOSTD touches is listed there even when no profile can name it — a safety property that takes two files to verify is one people stop verifying. Four units are denied outright and re-checked independently of how the permitted set was built. `cubesat@obc`, `cubesat-hostd` and `mosquitto` are the branch HOSTD is sitting on. `NetworkManager` is that reason one step further out: every network mode is applied through `nmcli`, so a profile that stopped it would take away the way back to a reachable one — and the profile where that bites is `FLIGHT`, which has neither Wi-Fi nor SSH to fix it from. And the units a *profile* may start and stop are a strict subset: HOSTD's own levers are managed by the code that owns them, so "stop everything this profile did not ask for" cannot sweep up the mDNS daemon it is about to need.

| Action | Effect |
|---|---|
| `apply_profile` | Start/stop units, switch network mode, set the governor, note the profile in `/var/lib/cubesat/last-profile` |
| `set_governor` | CPU frequency governor only — used by `LOW_POWER` without a full profile change |
| `restart_service` | Restart one mission service by name, without re-applying a whole profile |
| `poweroff` | Graceful shutdown, requested by `OBC` on `CRITICAL` |
| `clear_resume` | Zero the consecutive-resume count in `last-profile`. Touches nothing else — see [Resuming an interrupted trip](#resuming-an-interrupted-trip) |

`HOSTD` also listens on a root-owned Unix socket at `/run/cubesat/hostd.sock`. That is the break-glass path for when the broker itself is dead; it funnels through the same validation as an MQTT command.

### OBC

**Path:** `src/cubesat/obc/` | **Client ID:** `obc` | 🛰️ `HOSTED` 2026-08-31

The head. `OBC` owns both state machines, decides what the platform should look like, and publishes that intent — it never touches the host itself and runs unprivileged, which keeps every decision inside the test suite.

Responsibilities:

- the **profile** machine: validate a requested profile, translate it into `apply_profile`, reconcile intent against what `HOSTD` reports back
- the **mission state** machine: `BOOT → STANDBY → DEPLOY → NOMINAL`, plus the power-driven descent through `LOW_POWER` → `SAFE` → `CRITICAL`
- **health monitoring**: every service publishes to `cubesat/heartbeat` on a fixed interval, independent of its poll cadence — a subsystem told to poll every 300 s in `LOW_POWER` must still prove it is alive more often than that. Three consecutive misses and the subsystem is declared lost, dropping the state to `SAFE`. A service that dies *ungracefully* is announced by its MQTT **last will** on the same topic, so OBC learns in milliseconds instead of waiting out the timeout.

  A heartbeat is **liveness only, and never bring-up evidence**. Every service in this project logs a silent device and stays up — that is deliberate, so that OBC reacts to missing telemetry rather than to a vanished process — which means a heartbeat proves the process started and says nothing at all about whether its sensor answered. `DEPLOY` therefore waits for a subsystem's *status* message, which only exists because its hardware was read
- **command parsing** for everything that is a mission decision (see [the command vocabulary](#the-command-vocabulary))

Because `HOSTD` holds the applied profile and `OBC` reads it from a retained message, `systemctl restart cubesat@obc` mid-demo does not disturb the access point or the dashboard.

### EPS

**Path:** `src/cubesat/eps/` | **Client ID:** `eps` | 🛰️ `HOSTED` 2026-08-31

Reads battery state from the X728's fuel gauge — a **MAX17040/41** (I2C `0x36`), identified on 2026-09-01; the driver file is still named `max17048.py` and reads only the registers both families share — and mains presence from the X728 UPS Power Loss Detection pin over GPIO. Publishes `cubesat/eps/status` retained.

What EPS trusts that gauge for is one number, `VCELL`. Its state of charge is published as `gauge_percent` and decided on by nothing (2026-09-04): it is a model with no current sense behind it, and it was measured drifting. The percentage, both rates and the two estimates of time remaining are all derived from the voltage through `common/battery.py`, and the thresholds they are compared against are volts.

`EPS` runs in **every** profile, including `HOSTED` where there is no mission at all: it is the only source of the telemetry that drives `CRITICAL`, and a satellite that cannot see its own battery cannot protect its filesystem. Mains appearing on the PLD pin is also the "I am back at a desk" signal that can request a return to `HOSTED`.

### ADCS

**Path:** `src/cubesat/adcs/` | **Client ID:** `adcs` | 🛰️ `DEMO` 2026-09-01

Attitude and position — *where and how the satellite is*, which is why both live in one subsystem.

- **BNO055** (I2C `0x28`) runs 9-axis sensor fusion on-chip and outputs quaternion/Euler directly, so there is no AHRS filter in this repo any more. This replaces the QMI8658 + AK09918 pair and the Mahony filter of the old Sense HAT (C) build. The driver performs a **full reset** on every start: a device left half-configured reports a fusion error while still claiming its magnetometer is calibrated, and returns all-zero magnetometer data — a stale-state artefact that cost real debugging time, recorded in [its hardware document](docs/hardware-bno055-bmp280-imu.md).
- **TEL0157** (I2C `0x20`) is a GPS/BeiDou/GLONASS receiver that parses NMEA on-module and exposes plain registers, so `pynmea2` is gone. It replaces the A9G GPS of the retired IoT Node(A), and its reads are the longest transactions on the bus. Two firmware quirks are handled in the driver and pinned by tests, because both yield *plausible* wrong positions rather than errors: the hemisphere bytes arrive **swapped**, so the hemisphere is taken from each byte's content rather than its position, and the registers carry an unsigned magnitude, so the sign for south and west has to be applied by us — the vendor library does neither, which is how the bug survived upstream.

Poll cadence comes from the mission state, not from a hardcoded constant: 2 Hz in `NOMINAL`, 0.2 Hz in `LOW_POWER`. `DIAG` used to multiply all of it by five; it no longer does — a rehearsal at five times the real rate rehearses something else, and 10 Hz on a bus clamped to 10 kHz was a standing question of its own.

This service remains **sensing-only** — actuator control (reaction wheels, magnetorquers) is not implemented and is not planned.

### PAYLOAD

**Path:** `src/cubesat/payload/` | **Client ID:** `payload` | 🛰️ `DEMO` 2026-09-01

Two responsibilities, one owner of the camera:

1. **Science** — the SEN0501 environmental package (I2C `0x22`): temperature, humidity, atmospheric pressure, ambient light and UV. Replaces the LPS22HB + SHTC3 pair of the Sense HAT (C).
2. **Camera** — a JPEG capture via Picamera2, either on a `take_photo` command or on the mission's
   own cadence. Capture is gated: permitted in `NOMINAL`, refused in `LOW_POWER` and
   below; *stopping* is always permitted.

**A mission photographs itself, and there is no timelapse command** (decided 2026-09-01). While DHS
reports an open mission and the state permits the camera, a frame is taken every
`photos.mission_interval_sec` (300 s) and filed under the mission; it stops when the mission closes,
resumes after a `LOW_POWER` dip recovers, and needs nobody to have remembered anything before
walking out of the house. `start_timelapse`/`stop_timelapse` are gone: the automatic series was the
only use they had.

**With no mission open, a photograph never reaches the card.** It is written to
`/run/cubesat/photo` — a tmpfs, so RAM — published as pixels on the retained `cubesat/payload/photo`,
and deleted. That is the whole of what `DEMO` and `EXPO` need: somebody asks, they see it, and a
page opened five minutes later still finds it in the broker's own memory. It replaced
`photos/unfiled/`, which retention was forbidden to touch and which therefore only grew — the one
directory guaranteed to accumulate held the photographs least likely to be wanted.

### DHS

**Path:** `src/cubesat/dhs/` | **Client ID:** `dhs` | 🛰️ `DEMO` 2026-09-01

The flight recorder — the Data Handling Subsystem (the *OBDH* / mass-memory role on a real spacecraft). It subscribes to every status topic, assembles a row from the cached values plus system health metrics, writes it to SQLite, and purges rows past the retention horizon.

**This is a deliberate split from COMMS**, where persistence used to live, and the reason is a use case: in `FLIGHT` and in `SAFE` the radio goes off, but the GNSS track must keep recording. With the database owned by the link service, turning off the link meant losing the recorder. It also keeps chart history out of a process that is busy doing radio I/O with timeouts.

Whether a row may be written at all is decided by the **profile**; how often is decided by the **mission state**. (The pre-rewrite code gated writes on a ground-commanded `SCIENCE` state, which would mean `FLIGHT` recorded nothing unless someone remembered a command before leaving the house. The gate went first; the state itself followed on 2026-09-02, once it was clear nothing else had ever depended on it.)

**DHS is also the only thing that may erase a mission.** `delete_mission` arrives on
`cubesat/command` like every other ground command and is answered in `dhs_status.last_delete`. It is
here rather than as an HTTP `DELETE` on the dashboard because this file has exactly one writer — the
dashboard opens it `mode=ro` so a stray write fails at SQLite rather than in review, and a second
writer would trade that property away for a button. The database it acts on is the one DHS has open,
falling back to `comms.db`, which is deliberately the same rule DASHBOARD follows for which archive
it serves: the listing an operator is looking at and the file the delete lands on cannot be two
different files. Outside `FLIGHT` and `DIAG` the recorder holds no database at all, so the command
opens one for itself and closes it again.

**SQLite schema** — `/var/lib/cubesat/comms.db`, table `telemetry` (renamed from `comms_log`: `COMMS` no longer owns it); `/var/lib/cubesat/diag.db` under `DIAG`:

| Column group | Fields |
|---|---|
| Timing | `id`, `timestamp` (ISO-8601 UTC string) |
| Context | `mission_id` → [`missions`](#mission-sessions), `profile`, `obc_state` |
| EPS | `battery` (derived from the voltage), `gauge_percent` (what the gauge itself said), `voltage`, `external_power`, `charge_rate`, `voltage_rate` |
| ADCS attitude | `roll`, `pitch`, `yaw`, `quat_w/x/y/z`, `imu_temp`, `accel_x/y/z`, `gyro_x/y/z`, `calib_status` |
| ADCS position | `lat`, `lon`, `alt`, `speed`, `fix`, `satellites` |
| Payload science | `temperature`, `humidity`, `pressure`, `light`, `uv_index` |
| System health | `cpu_percent`, `ram_percent`, `swap_percent`, `disk_percent`, `uptime_seconds`, `cpu_temperature` |
| Raw | `raw_json` (full packet as a JSON string) |

Position columns are new: the old schema kept GNSS data only inside `raw_json`, and `FLIGHT` exists to record a track — every chart and export would otherwise have to parse JSON per row.

**Table `attitude` — the same track at the rate it was measured.** `telemetry` holds one wide row per DHS tick, 30 s apart in `NOMINAL`, while `ADCS` publishes orientation at 2 Hz: every sixtieth sample survives, and replaying a hand-carried satellite from that is a slide show. So orientation gets a second, narrow table.

| Column group | Fields |
|---|---|
| Timing | `id`, `t` (epoch seconds, **from the ADCS payload** — when the IMU was read, not when the row was written) |
| Context | `mission_id` → [`missions`](#mission-sessions) |
| Orientation | `quat_w/x/y/z` |
| Rate | `gyro_x/y/z` |

Four decisions worth knowing before reading it:

- **The quaternion, not Euler angles.** It is what the BNO055 fuses and outputs, and it interpolates without gimbal trouble — which matters because a viewer replaying at 1 Hz *has* to interpolate.
- **`t` is a float where `telemetry.timestamp` is an ISO string to the second.** That column's resolution is a deliberate choice for rows 30 s apart; this one has to be finer.
- **Decimated by `dhs.attitude_min_interval_sec` (1.0 s).** One ceiling across every profile and state, so no profile can quietly turn the track into ten rows a second on the card. About 5 MB for a working day. This costs **SD-card writes and nothing else**: DHS holds no hardware and reads no bus, so what ADCS puts on the wire costs the same whether it is recorded or discarded.
- **Buffered in memory, written in one batch on the tick** that was going to open a transaction anyway — so recording at 1 Hz costs the same number of card writes as recording at 1/30 Hz did. The buffer is bounded (`dhs.attitude_buffer`): surviving a card that has stopped accepting writes is this service's job, and an unbounded buffer would turn that into an unbounded process. A batch that fails is held, not dropped.

A sample is written only when there is orientation to write. Nine nulls would look on a chart exactly like a satellite that was not moving, and `ADCS` publishes when *either* half answered — so a position-only message is normal and records nothing here.

Timestamps are trustworthy offline: the DS1307 RTC on the X728 is enabled, so a track recorded with no network is not stamped from the last boot epoch.

**Table `radio_log` — the radio's session log.** One row per radio transaction during a mission:
every message received **on the satellite's own channel** and every transmission attempted, observed
by `COMMS` on `cubesat/comms/radio` and recorded here — `COMMS` itself persists nothing, exactly as
with every sensor. Traffic on any other mesh channel never reaches this topic at all (see
[COMMS](#comms)), so what is on the record is this link's own conversation, gibberish included, and
not the community mesh's.

| Column group | Fields |
|---|---|
| Timing | `id`, `t` (epoch seconds, **from the COMMS event** — when the radio transacted) |
| Context | `mission_id` → [`missions`](#mission-sessions), `direction` (`rx`/`tx`) |
| The traffic | `text` (the line verbatim), `bytes` |
| Link quality (rx) | `sender`, `snr`, `rssi`, `hops` — each null where the node did not report it |
| Outcome (tx) | `kind` (`beacon`/`ack`/`down`), `sent` (0/1 — a failed transmit is recorded, not hidden) |

Unlike `attitude` it is **never decimated** — attitude is a continuous signal where any sample
stands for its neighbours, while radio traffic is discrete events, and the one packet dropped
would be the uplink somebody is trying to find. It shares the attitude buffer's other properties:
bounded (`dhs.radio_buffer`), flushed in one batch on the tick, held rather than dropped when a
write fails, and aged out by the same retention horizon. Traffic outside a mission — a `HOSTED`
desk listening — is deliberately not recorded: the table answers "what did the radio do on this
trip".

`DHS` also owns the `missions` table and the session lifecycle — see [Mission Sessions](#mission-sessions).

### COMMS

**Path:** `src/cubesat/comms/` | **Client ID:** `comms` | 🛰️ `HOSTED` 2026-08-31

The only point of contact with the ground, in both directions — and, after the split above, *only* that. It no longer persists anything.

- **LoRa** over a [Heltec WiFi LoRa 32 V4](docs/hardware-heltec-lora32-v4.md) running stock Meshtastic firmware, on `/dev/serial0` at 115200 via the `meshtastic` Python library. Meshtastic handles framing, CRC, retries and encryption, so the hand-rolled length+CRC framing of the old SC16IS752 driver is gone — and with it the register-level protocol that was never bench-verified.
- **Uplink, unified** — a command arriving over LoRa is canonicalised into JSON and published onto `cubesat/command`. Nothing downstream needs to know which link it came in on, so a command relayed off the radio is handled exactly as one a laptop published on the LAN. This is what makes `FLIGHT` recoverable: `profile hosted` typed into a phone arrives as `set_profile` for free.
- **Uplink, compact spellings only** — narrowed on 2026-09-03. What the radio accepts is the table in [`compact.py`](src/cubesat/comms/compact.py) and nothing else; hand-composed JSON used to be relayed byte for byte and is not any more. One parser for the air instead of two, so nothing is left that can disagree with `compact.py` about what a command is — and one fewer shape to keep the 240-byte budget honest for. What goes with it is narrow and worth naming: `set_profile` over the radio takes a profile and nothing else (no `ttl_minutes`, no `mission_label`, no `request_id`), and `delete_mission` — which has no compact spelling on purpose — is now unreachable over the air entirely. Every recovery verb is compact, so the `FLIGHT` path above is untouched.
- **Uplink, from one mesh channel** — and only from `config.LORA_CHANNEL_INDEX`, the private `CubeSat` channel. The node's primary channel is the stock public one, so since the mesh preset change of 2026-09-02 the community mesh's chat reaches this radio — and the command vocabulary is ordinary English (`ping`, `photo`, `safe`, `profile flight`). Anything on another channel, a direct message included, is dropped in `_collect_uplink` **before** the `cubesat/comms/radio` publish: it reaches neither the command parser nor the `radio_log` session log, so it is never shown in the dashboard's Radio Link Log, never written to the card in `FLIGHT`/`DIAG`, and never travels inside a mission export. The refusal is **silent** — one line in the service log carrying the sender, the channel, the SNR and a byte count but not the text, and nothing transmitted back, not even an `err=` for a `!` line. Listening is untouched: `lora_listening` remains the profile's call and the inbox is polled in full. The credential is the channel's key rather than a node id, deliberately — a key is portable, so an operator whose own node is flat loads the channel URL onto another one, where a node allowlist would be a locked door with the key on the far side of it.

**The radio carries a beacon, not the telemetry.** A Meshtastic message holds at most 240 bytes and a full telemetry packet is several hundred, so the choice was a compact field set or chunking. It is a compact beacon, for three reasons: one message is one complete observation, where a lost chunk voids a whole packet; LoRa airtime is slow and duty-cycle limited, so three messages per cycle costs three times as much of it; and the radio's job here is *alive, and where* — the full record is in DHS and gets collected when the satellite is back on a network.

The beacon is a single line of `key=value` pairs, deliberately **readable by a human in the Meshtastic phone app**:

```
CSAT t=1741863600 st=NOMINAL pr=FLIGHT b=78.2 v=3.94 ep=0 lat=55.7558 lon=37.6173 alt=156 sat=23 m=42
```

That readability is an operational property, not decoration: the satellite can be checked from a phone with no ground station at all. Unknown keys are ignorable, so the format can grow without breaking a reader.

One beacon is not routine. On entering `CRITICAL` the satellite sends a single **going-down**
message before the host powers off:

```
CSAT t=1741863600 st=CRITICAL down=1 pr=FLIGHT b=8.1 v=3.21 ep=0 lat=55.7578 lon=37.6173 alt=156 sat=22 m=42
```

Without it, a satellite that shut itself down at 8 % leaves a silence the ground cannot tell apart
from a flat battery, a dead radio, or someone walking out of range. One message turns that into a
recorded event: this is where I was, this is what was left, and I switched myself off deliberately.
It is sent once on the state change rather than on a schedule — there is no repeating in a state
that lasts ten seconds — and it goes out even when a `set_comms_config` has silenced transmission,
because a runtime flag set an hour ago must not suppress the one message that explains a
disappearance. If it fails, it is logged and stepped over: the recorder closing its mission cleanly
matters more than the radio being heard, and a pack at 8 % is exactly where a transmit-current
brownout is likeliest.

**Radio replies.** The agreed contract is written, `restart` included since 2026-09-01 and the
`photo` ack since 2026-09-03. What a person can type is in
[The command vocabulary](#the-command-vocabulary) and only there; what COMMS does with it is this: a
compact line is canonicalised into JSON *before* the relay, so every handler downstream sees one
format; every accepted command is answered by a single out-of-schedule beacon carrying `re=<verb>`
about ten seconds later; and the five query verbs are answered immediately from COMMS' own caches
rather than relayed, because the data is already here.

Four properties of that reply are worth stating here, because each one was a defect first:

- **It goes out whether or not the beacon is on.** An answer is not a beacon — the gate is the
  profile (`lora_listening`), so every profile that runs COMMS answers. See
  [`cubesat/comms/status`](#cubesatcommsstatus).
- **`re=` names the verb the operator typed**, not the canonical command it became. `beacon on` came
  back as `re=set_comms_config` until 2026-09-03, which asks a person on a phone to translate our
  vocabulary into theirs before they can believe the answer.
- **`photo` answers about the photograph**: `ok=1 kb=<size> seq=<frame>` read from PAYLOAD's own
  `payload_photo`, or `ok=0 err=state|nospace|camera` when the capture was refused — one word,
  because a beacon field may not contain a space. Nothing published within the window is
  `err=noreply` and deliberately no `ok=`: silence is an observation, and `ok=0` would be a verdict
  on a capture COMMS never saw. A mission's own frames are excluded by `kind`, so a `!photo` is
  never answered with a picture the mission took by itself.
- **Replies queue rather than overwrite each other**, one per wake, oldest first, eight deep. Only
  the state queries collapse into the latest — a fresh snapshot answers the older question too —
  while anything with an effect keeps its own reply. A single slot used to lose the first of any two
  commands sent inside ten seconds, silently.

**What each query answers with.** The spellings themselves stay in
[The command vocabulary](#the-command-vocabulary); this is only what comes *back*. All five ride
an ordinary beacon line, so `t=`, `st=`, `pr=` and the power fields are there too; these are the
fields the verb adds. `age=` is whole seconds since the source subsystem published, which is what
makes a stale answer honest — `env` still answers when PAYLOAD is stopped by the profile, with an
age that says exactly how stale.

| Verb | Adds | With nothing to say |
|---|---|---|
| `ping` | nothing — the transmission arriving *is* the answer | — |
| `pos` | `lat= lon= fix= age=`, plus `alt=` and `sat=` when the fix carries them | `ok=0 err=nodata` |
| `sys` | `cpu= ram= disk= up=`, plus `tc=` where the SoC reports one | never: local `psutil`, no cache |
| `env` | `age= tc= rh= hpa= lux=` | `ok=0 err=nodata` |
| `mission` | `m=` and `rows=` | `ok=0 err=nodata` — no mission is open |

`pos` reports a stale or fixless position where the scheduled beacon reports none: this is the
lost-satellite query, and `fix=` and `age=` are there to say precisely how much to trust it. The
schedule has no room for an age, so it stays live-fix-only.

The contract, with the reasoning, is
[`docs/concept.md` → The radio command contract](docs/concept.md#the-radio-command-contract).

Two rules hold it honest. **It is never truncated** — if the line will not fit, whole optional fields are dropped in a documented priority order, because the pre-rewrite driver silently cut the payload to 28 bytes and transmitted rubbish, which is the bug this work exists to remove. And **absent values are omitted rather than sent as zero**: a position that does not exist must not arrive as `lat=0 lon=0`, which is a real place in the Gulf of Guinea and a fault this project has already been bitten by once.

### DASHBOARD

**Path:** `src/cubesat/dashboard/` | **Client ID:** `dashboard` | 🛰️ `DEMO` 2026-09-01

The satellite carries **no UI code**. This service serves a static build and reads the recorder's database; the interface itself lives in [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) and is deployed onto the Pi as a built artifact, so one dashboard codebase serves the satellite, a recorded mission replayed from a static file, and a future USB receiver alike.

**It is deliberately not the live channel, and there is no `/ws`.** Browsers subscribe to [mosquitto's own WebSocket listener](#two-listeners-and-one-fence) and receive every retained message the moment they connect — so this project contains no MQTT-to-WebSocket bridge to write, to test, or to keep in step with the topic list. What is left for HTTP is what the broker cannot answer.

| Endpoint | Purpose |
|---|---|
| `/` | The static build, from `CUBESAT_DASHBOARD_ROOT` (default `/var/lib/cubesat/dashboard`) — nothing is committed to this repo. An unknown path falls back to `index.html`, so a reload on a deep link lands in the interface |
| `/api/telemetry?limit=N` | **The current session**, newest first — the open mission from the database while one is being recorded, DASHBOARD's in-memory ring otherwise; `source` says which answered. Also where the page reads CPU, RAM, disk and uptime, which exist in no status message |
| `/api/missions` | Every recorded session, newest first, from the stored summaries |
| `/api/missions/<id>` | One mission: the summary, its telemetry oldest-first, its `attitude` samples and its `radio` traffic. The radio rows are there so a replay shows the link of the trip rather than leaving that one widget reading the live satellite |
| `/api/missions/<id>/export` | The same body as a download — one endpoint backs both "keep a copy of this walk" and "produce the file the public demo replays". It carries no photographs; the two endpoints below are where those live |
| `/api/missions/<id>/photos`, `/api/photos/<id>/<name>` | The mission's photographs |

**There is no `/api/command`, and there is no `DELETE`.** A command from the dashboard goes onto `cubesat/command` over the browser's own broker connection — the same topic a laptop, the CLI and an uplink relayed off the radio all use. Nothing downstream knows it came from a browser, and this service needs no write path at all. That includes erasing a mission: the archive dialog publishes `delete_mission` and DHS, which owns the file, performs it — see [DHS](#dhs).

**It publishes no status of its own.** Every other service has one because `OBC`'s `DEPLOY` wants evidence that a device answered; this one owns no device. It sends a heartbeat like everything else, and adding a status topic would mean adding a subsystem that can fail a bring-up — for a service whose absence a profile is entitled to intend.

**The database it reads follows `DHS`,** from the retained `dhs_status`: in `DIAG` the recorder writes `diag.db`, and a dashboard still showing `comms.db` would be displaying last week's trip during a bench session. Read-only on its own connection (`mode=ro` in the URI, not a convention), so a stray write fails at SQLite rather than in review and the recorder is never blocked. A database that does not exist yet is an empty archive, not an error — a satellite in `HOSTED` has never opened one.

**A purged mission is not an empty one.** The summary always travels with the detail, because empty arrays alone cannot say *why* they are empty: aged out, recorded before the `attitude` table existed, or never recorded anything. `purged_at` and `rows` are what separate them.

Deploy the interface with `scripts/deploy-dashboard.sh` from a machine that has the groundstation checkout — it builds with `PUBLIC_SOURCE=live` and rsyncs `client/dist/` into place. The service reads from disk per request, so no restart is needed.

**Address:** `http://cubesat.local`. The bare `http://cubesat` is unreliable in client mode, where resolution goes through mDNS — `cubesat.local` is what actually works from a phone or a tablet, and is what belongs on a demo card. Under `EXPO` the access point owns DNS for its own network, so `HOSTD`'s `dnsmasq` can additionally answer the bare name.

### Common Infrastructure

**Path:** `src/cubesat/common/`, `src/cubesat/hal/`

| Module | Responsibility |
|---|---|
| `common/service.py` | `class Service` — the base every service inherits: broker connect, retained subscriptions, heartbeat, cadence from `obc/status`, clean SIGTERM shutdown. Eight hand-written copies of this would be eight different reconnect bugs |
| `common/config.py` | `config.yaml` + environment loading, resolved data paths |
| `common/profiles.py` | Loads and validates `config/profiles.yaml` |
| `common/states.py` | `enum Profile`, `enum MissionState`, `enum EndReason` — the shared vocabulary, so a profile name is never a bare string crossing a process boundary |
| `common/topics.py` | The `TOPICS` dict and payload helpers |
| `common/mqtt.py` | MQTTv5 client factory with exponential-backoff reconnect |
| `common/cadence.py` | Mission state → poll interval, in one table instead of six hardcoded constants |
| `common/log.py` | Rotating file + console logging to `/var/log/cubesat/<service>.log` |
| `common/metrics.py` | CPU / RAM / swap / disk / uptime / temperature via `psutil` |
| `hal/interfaces.py` | `typing.Protocol` definitions — `IMU`, `GNSS`, `Environment`, `FuelGauge`, `Camera`, `Radio`. Structural typing, so drivers and fakes inherit nothing and a test fake is any object with the right methods |
| `hal/registry.py` | The factory: `CUBESAT_MOCK_HARDWARE` decides whether `rpi` or `mock` implementations are handed out |
| `hal/i2c.py` | The shared bus and its advisory lock — see [Hardware Ownership](#hardware-ownership) |
| `hal/rpi/`, `hal/mock/` | Real drivers (BNO055, TEL0157, SEN0501, MAX17040/41 gauge, PLD GPIO, camera, Meshtastic) and one fake per interface |

The HAL is not optional. It is what makes `HOSTD`, both state machines and the cadence logic testable without a Raspberry Pi, which is why it was built first rather than last.

---

## Hardware Ownership

Each device has exactly one owning service. This matters more than it looks, because **every sensor shares I2C bus 1, and the bus is clamped to 10 kHz** — a correctness requirement of the BNO055, not tuning (see [I2C Address Map](#i2c-address-map)).

| Device | Address / port | Owner | Cadence |
|---|---|---|---|
| MAX17040/41 fuel gauge | I2C `0x36` | EPS | 30 s |
| X728 PLD pin | GPIO | EPS | 30 s |
| BNO055 orientation | I2C `0x28` | ADCS | 2 Hz |
| TEL0157 GNSS | I2C `0x20` | ADCS | 1 Hz, long transactions |
| SEN0501 environment | I2C `0x22` | PAYLOAD | 60 s |
| BMP280 | I2C `0x76` | undecided — duplicates SEN0501 pressure | — |
| Camera Module V2 | CSI | PAYLOAD | on demand |
| Heltec V4 / Meshtastic | `/dev/serial0` | COMMS | per cycle |
| DS1307 RTC | I2C `0x68` | the kernel, via `/dev/rtc0` | — |

At 10 kHz a single read costs tens of milliseconds, and a GNSS register block can cost far more. Four processes touching the bus independently will eventually collide mid-transaction, so **all I2C access goes through an advisory lock** (`flock` on `/run/cubesat/i2c.lock`) held around each transaction inside `hal/i2c.py`. It is cheap and it preserves the per-subsystem decomposition, which is the educational point of the project.

If the lock ever proves insufficient, the fallback is a single bus-owner process that others query — cleaner in theory, but it puts a process in the hot path and breaks the "a subsystem reads its own sensor" model.

---

## Mission Sessions

Recording is not one endless stream. Each continuous run of an active profile is a **mission** — a
row in the `missions` table that every telemetry row points at. That is what lets the dashboard
answer "show me the walk to work on Tuesday" rather than "show me all telemetry since March".

A mission is not a mission *state*. States (`NOMINAL`, `LOW_POWER`, `SAFE`) come and go **inside** a
mission and are recorded per row — which is what makes a timeline worth looking at: here is where
the battery started sagging, here is where the satellite went quiet.

| `missions` column | Purpose |
|---|---|
| `id` | Primary key, referenced by every telemetry row |
| `label` | Optional operator-supplied name (`cubesat profile flight --mission "walk to work"`) |
| `profile` | Which profile recorded it |
| `started_at`, `ended_at` | ISO-8601 UTC; `ended_at` is null while the mission is running |
| `end_reason` | `profile_change` · `shutdown` · `battery_critical` · `interrupted` |
| `start_reason` | `command` · `resume` — why the mission was opened. Null for missions recorded before 2026-09-03 |
| `rows` | Telemetry row count, filled in on close |
| `first_fix_at` | When GNSS first had a fix — null for an indoor session |
| `distance_m` | Track length, computed on close |
| `notes` | Free text, for whatever the operator wants to add afterwards |

`rows` and `distance_m` are derived values stored on the mission row on purpose: listing forty
missions must not mean scanning the telemetry table forty times.

### Lifecycle

| Event | Effect |
|---|---|
| Profile applied, persistence permitted, state reaches `NOMINAL` | `DHS` opens a mission |
| Profile changed, or `HOSTED`/`MAINTENANCE` entered | Closed, `end_reason = profile_change` |
| Graceful shutdown | Closed, `end_reason = shutdown` |
| `CRITICAL` | `DHS` flushes and closes, `end_reason = battery_critical` |
| Power loss, watchdog, kernel panic | Nothing closes it — recovered at next startup |

**Orphan recovery matters more than the happy path.** A satellite that dies on battery never closes
its mission, so at startup `DHS` finds every mission with a null `ended_at`, sets it to the
timestamp of that mission's last telemetry row, and marks `end_reason = interrupted`. Without this,
one hard power loss leaves an open-ended mission that every later query has to work around.

A trip interrupted by a reset becomes **two** missions rather than one resumed session, even when
`FLIGHT` [resumes itself](#resuming-an-interrupted-trip). That is the honest representation: there
is a real gap in the data, and stitching across it would draw a straight line on the map through
territory where the satellite was switched off. What ties the halves together is that they share a
label and the second carries `start_reason = resume`, while the first is closed as `interrupted`.

### Resuming an interrupted trip

**`FLIGHT` is the one profile that resumes itself after an unexpected reset**, and it resumes on a
measurement rather than on a stored command. The failure it closes is specific: a brownout, a
watchdog bite or the jolt of a landing brings the satellite up in `HOSTED`, where `DHS`, `ADCS` and
`PAYLOAD` never start, so the track, the telemetry and the photographs stop — in the one profile
with no dashboard, no Wi-Fi and nobody looking at it — and `STANDBY` has no beacon row, so nothing
is said about it either.

The rule lives in `src/cubesat/obc/resume.py`. Five conditions, all of which must hold:

| Condition | Why it is there |
|---|---|
| HOSTD still holds the profile it applied at boot (`host_status.boot`) | Tells a boot from a `systemctl restart cubesat@obc`. A human who has already asked for a profile has said what they want |
| The interrupted profile was `FLIGHT` | The only profile designed to run with nobody present. `EXPO` on battery without an operator is pointless, `DIAG` lives on a desk |
| **No mains** — `eps_status.external_power` false | The measurement the whole decision rests on. It is also what keeps the dangerous case safe: a satellite brought home flat and plugged in *has* mains, so it comes up in `HOSTED` with SSH exactly as before |
| The TTL from before the reset is still in the future | A trip whose strap ran out does not restart, and a resumed one serves out the **remainder** rather than a fresh full term |
| Fewer than `resume.max_consecutive` (3) resumes in a row | The boot-loop fence. A lifetime rather than a counter: the count clears once a resumed session has lived `resume.settle_sec` (300 s), so three means three *short* lives in a row |

With no `eps_status` at all inside `resume.evidence_timeout_sec` (60 s) nothing is resumed: a
missing measurement is not a measurement of no mains, and an EPS that failed to start is itself a
reason to stay somewhere an operator can reach.

**Everything about it is said out loud.** The resumed run inherits the mission label, records
`start_reason = resume`, publishes the verdict on `obc_status.boot`, and COMMS transmits one line
(`boot=FLIGHT rs=1`, or `rs=0 why=…` for a refusal) gated on the profile rather than on
`beacon_enabled` — the counterpart of the going-down beacon. A satellite that silently declines to
resume is indistinguishable from one that never woke up.

The decision is OBC's; `HOSTD` gains no judgement. It applies `default_profile` at boot exactly as
it always has and publishes what the file said (`previous`) beside whether anything has asked for a
profile since (`boot`). `last-profile` is JSON as of 2026-09-03 — profile, `written_at`, the
absolute `ttl_expires_at`, `mission_label` and `resume_count` — and the older one-line spelling
still parses, because a satellite upgraded in the field has that file on its card.

Labels are for grouping, not identity — two runs labelled the same are still two missions. Photos
are filed per mission (`/var/lib/cubesat/photos/<mission_id>/`), which PAYLOAD learns from the
retained `dhs_status` rather than by owning any part of a mission itself. A photo taken while no
mission is open goes to a tmpfs and is deleted after it is published, rather than being refused or
given an invented id — see [PAYLOAD](#payload). They group the way the
charts do. `DIAG` sessions are missions too, in `/var/lib/cubesat/diag.db` with the same schema, so one dashboard renders
a bench run and a trip with no special case.

**A mission id names a directory only together with its database.** The two databases number their
missions independently, both from 1, so `DIAG` mission 3 and a `FLIGHT` trip 3 are different
missions with the same id — and one `photos/<mission_id>/` for both meant they shared a directory,
where deleting the bench run took the trip's photographs with it. `diag.db` therefore files under
`/var/lib/cubesat/photos-diag/<mission_id>/`: a **sibling** root, not a level of nesting above every
id, so the directory name stays a plain run of digits — the allowlist retention fences its deletions
with — and nothing already on the card moves. `dhs_status.database` is what selects it, and PAYLOAD
takes it from the same message as the id, never separately: a mission id whose database is missing
reads as no mission at all, exactly as a non-integer id does. The mapping is
`common/config.py` → `photos_root_for`, and the dashboard's two photo routes follow the archive it
is serving through the same function.

### Retention: photos follow their mission

Telemetry rows age out after `retention.days` (30 by default), and `attitude` samples with them —
same horizon, same transaction. Both are a mission's detail and both belong to it; a rule that aged
one out and kept the other would leave a mission that can be replayed but not charted, which is a
state nobody would think to test and every consumer would have to handle. **Photos go with the
mission they belong to** — when a mission's last row passes the horizon, its photo directory
goes too, in the root belonging to the database being purged. "Photos live exactly as long as the detail of the mission they belong to" is a rule an
operator can hold in their head.

The `missions` row itself **survives**, stamped with `purged_at`: a trip that happened stays listed
after its detail has aged out, and `rows` keeps its honest historical meaning — what that mission
actually recorded. **Deleting a mission on purpose is the one thing that does remove the row**, and
that difference is deliberate: the horizon is the satellite deciding it can no longer afford a
record, while `delete_mission` is a person saying this trip should not be listed. A delete that left
a `purged_at` ghost behind would look, to whoever pressed it, exactly like a button that does
nothing. Everything else is identical — the same four tables in one transaction, then the same
fenced removal of that mission's photo directory. Without the stamp a dashboard joining the two tables would find a mission
claiming 1440 rows and holding none, which is a plausible wrong number of exactly the kind this
telemetry refuses everywhere else.

Without it the database stays bounded and the card does not, which matters here more than it
usually would: the camera is the only unbounded writer on this satellite, and the first write to
fail on a full card is the telemetry row the mission exists to record. That is also why PAYLOAD
enforces a free-space floor (`photos.min_free_mb`) — **the floor and this horizon are the same
headroom seen from two sides**, and setting them independently lets the card fill anyway.

Deleting photographs is the most destructive thing this codebase does, so it is fenced: only
directories named for a mission being purged in the same pass, never a sweep of a photo root, and
never a name that is not a run of digits — which is why the second database got a root of its own
rather than a suffix on the id or a level above it. `photos/unfiled/` used to be the case in point — retention
was forbidden to clean it, so it only ever grew — and it no longer exists; the fence stays as an
allowlist for whatever a future PAYLOAD might invent without telling retention. Every deletion is
logged with the mission, the file count and the bytes reclaimed. `retention.purge_photos: false` turns it off, with the consequence
above.

The **timeline UI** — scrubbing a mission, replaying a track, comparing two runs — belongs to the
dashboard and the groundstation client. The satellite's job ends at giving each mission an identity,
a start, an end, and a reason it ended.

---

## MQTT Topic Reference

All topic strings are defined in `src/cubesat/common/topics.py` (`TOPICS` dict). Always import from there — never hardcode topic strings.

| `TOPICS` key | Topic string | Publisher | On-satellite subscribers | Retained |
|---|---|---|---|---|
| `command` | `cubesat/command` | any ground client, or COMMS relaying an uplink | OBC, PAYLOAD, COMMS | no |
| `host_command` | `cubesat/host/command` | OBC | HOSTD | no |
| `host_status` | `cubesat/host/status` | HOSTD | OBC, DHS | **yes** |
| `obc_status` | `cubesat/obc/status` | OBC | EPS, ADCS, PAYLOAD, DHS, COMMS | **yes** |
| `eps_status` | `cubesat/eps/status` | EPS | OBC, DHS, COMMS | **yes** |
| `adcs_status` | `cubesat/adcs/status` | ADCS | OBC, PAYLOAD, DHS, COMMS | no |
| `payload_status` | `cubesat/payload/status` | PAYLOAD | OBC | **yes** |
| `payload_data` | `cubesat/payload/data` | PAYLOAD | DHS, COMMS | no |
| `payload_photo` | `cubesat/payload/photo` | PAYLOAD | — (browsers and ground clients only) | **yes** |
| `dhs_status` | `cubesat/dhs/status` | DHS | OBC, PAYLOAD, COMMS, DASHBOARD | **yes** |
| `dhs_telemetry` | `cubesat/dhs/telemetry` | DHS | DASHBOARD | **yes** |
| `comms_status` | `cubesat/comms/status` | COMMS | OBC | **yes** |
| `comms_data` | `cubesat/comms/data` | COMMS | — (ground clients only) | on-demand only |
| `comms_radio` | `cubesat/comms/radio` | COMMS | DHS | no |
| `heartbeat` | `cubesat/heartbeat` | **every service** | OBC | no |

The column lists the *code* subscribers — each service's `subscriptions` tuple plus the `obc_status`
subscription the base class adds for cadence tracking. The dashboard page in a browser reads nearly
every topic too, but through the broker's own WebSocket listener (`read cubesat/#`), not through the
DASHBOARD service — which itself subscribes to the recorder's two topics: `dhs_status` for the
database path and the open mission, `dhs_telemetry` for its in-memory chart history.

**`cubesat/dhs/telemetry`** carries the wide row exactly as it would be written to the database —
every column of `TELEMETRY_COLUMNS`, nested under `row`, minus `raw_json` — assembled by DHS on its
own tick whether or not it is being recorded. Two reasons it is on the bus. It is the sole carrier of
the host's own CPU, RAM, swap, disk, uptime and SoC temperature, which are collected inside DHS and
appear in no other message; and since only `FLIGHT` and `DIAG` record, it is where the charts' history
comes from in `DEMO` and `EXPO`. `mission_id` is null when no mission is open, which is the normal
case there.

**`cubesat/payload/photo` is retained**, and that is what "the dashboard shows the last photograph"
rests on where no history is kept: the frame is written to a tmpfs, published here and deleted, so
the broker's retained copy is the only place a page opened later can find it. mosquitto runs with
`persistence false`, so that copy is RAM and never the card. PAYLOAD clears it at start — HOSTD
starts the service as a profile is applied, so a start is where one session ends, and a visitor must
not meet the previous demonstration's photograph as though it were current.

Retained topics let a newly started service learn the current situation immediately instead of
waiting a cycle for the next publish — which is exactly how `OBC` recovers the active profile after
a restart.

### Two listeners, and one fence

`config/mosquitto/` installs the broker with two listeners, because there are two kinds of client and they are not trusted alike:

| Port | Protocol | Bound to | Clients | ACL |
|---|---|---|---|---|
| 1883 | TCP | `localhost` | the satellite's own services | none — they *are* the satellite |
| 9001 | WebSockets | all interfaces | browsers | `config/mosquitto/acl.conf` |

**There is no MQTT-to-WebSocket bridge in this project.** `DASHBOARD` serves the page; the page talks to the broker itself. Nothing to write, nothing to test, and nothing to fall out of step with `topics.py` — and a browser subscribing gets every retained message on connect, which is exactly what a freshly opened dashboard needs.

The split is per-listener (`per_listener_settings true`), and it has to be: a single ACL governing both would also stop `OBC` from reaching `HOSTD`.

What a browser may do:

- **read `cubesat/#`** — telemetry is not secret; the satellite beacons it over an open mesh.
- **write `cubesat/command`** and nothing else — the same single entry point a laptop, the CLI and an uplink relayed by `COMMS` all use.

Two denials carry the weight. `cubesat/host/command` is `HOSTD`'s inbox and `HOSTD` runs as root; `OBC` reaches it over the localhost listener, which the ACL does not govern. And `cubesat/+/status` is the telemetry `OBC` makes decisions from — a forged `eps_status` claiming 4 % battery would walk the satellite down through `LOW_POWER` and `SAFE` into `CRITICAL`, the one state permitted to power the host off. Withholding a reading is a gap; inventing one is a shutdown.

A browser can still issue everything the radio can, `set_profile` included. That is the [one-vocabulary rule](#the-command-vocabulary) and not an oversight, and its sharp edge is `EXPO`: the satellite is its own access point in a public room, so a visitor sending `set_profile HOSTED` ends the demonstration by taking the access point down. The ground vocabulary has no `poweroff`, so the worst case is a nuisance — and if it becomes one in a real room, the fix is a per-profile command allowlist in `DASHBOARD`, not a change to the ACL, which cannot see which profile is active.

---

## Message Payloads

### `cubesat/host/status`

Published retained after every profile application. `profile` versus `profile_requested` is not redundancy: a profile can be *partially* applied — an AP that failed to come up, a unit that refused to start — and reporting the achieved state separately from the intended one is what makes that debuggable instead of mysterious.

```json
{
  "timestamp": 1741863600.0,
  "profile": "EXPO",
  "profile_requested": "EXPO",
  "network": {"mode": "ap", "ssid": "cubesat", "clients": 2},
  "units": {"cubesat@adcs.service": "active", "telegram-bot.service": "inactive"},
  "governor": "ondemand",
  "ttl_expires_at": 1741899600.0,
  "boot": false,
  "previous": {
    "profile": "FLIGHT",
    "written_at": 1741860000.0,
    "ttl_expires_at": 1741896000.0,
    "mission_label": "walk to work",
    "resume_count": 0
  },
  "errors": []
}
```

`network` carries `mode`, `ssid` and `clients` only; anything that went wrong applying the network
mode is reported in the top-level `errors` list along with every other failure, so there is one
place to look.

`previous` is `/var/lib/cubesat/last-profile` as it read **before this boot overwrote it**, and
`boot` is true while the active profile is still the one HOSTD applied at start — false the moment
anything asks for a profile, a resume included. The pair is evidence, not instruction: HOSTD still
applies `default_profile` on every boot and has no opinion about what either field means. What
weighs them is OBC, against a measurement — see [Resuming an interrupted trip](#resuming-an-interrupted-trip).
`previous` is null on a satellite that has never recorded a profile.

### `cubesat/obc/status`

Consumers read `status` for the mission state and `profile` for the platform profile.

```json
{
  "timestamp": 1741863600.0,
  "status": "NOMINAL",
  "profile": "EXPO",
  "cadence_scale": 1.0,
  "persistence": "mission_db",
  "mission_label": "walk to work",
  "mission_start_reason": "resume",
  "boot": {
    "at": 1741863000.0,
    "previous": "FLIGHT",
    "resumed": true,
    "reason": null
  },
  "subsystems": {
    "watched": ["adcs", "comms", "dhs", "eps", "payload"],
    "lost": []
  }
}
```

The four scalar fields after `cadence_scale` exist so that a subsystem needs no second channel to do
its job from this one retained message: `cadence_scale` lets every service derive its own poll
interval, and `persistence`, `mission_label` and `mission_start_reason` are everything DHS needs to
open a mission and record why it exists. `mission_label` is null unless the operator supplied one
with the profile; `mission_start_reason` is `command` or `resume`.

`boot` is OBC's verdict on how this run began — null until HOSTD's first status has been weighed,
which is the honest answer rather than "an ordinary boot". `previous` names the interrupted profile
when there was one worth resuming and is null otherwise; `resumed` says whether it was; `reason`
carries `mains`, `ttl`, `loop`, `noeps` or `profile` on a refusal. COMMS transmits it once
(`boot=FLIGHT rs=0 why=mains`) and only when `previous` is non-null, because a desk reboot is not
news. See [Resuming an interrupted trip](#resuming-an-interrupted-trip).

`subsystems` is OBC's own health verdict, for the ground segment. `watched` is the set of services
the active profile expects to be running (the health monitor's watch list — always including
`eps`), and `lost` names the watched services whose heartbeats have stopped or that announced a
goodbye. A dashboard needs this to tell an intentional silence from a fault: a service absent from
`watched` is off because the profile says so, while one in `lost` is the failure OBC latched `SAFE`
over. `lost` is published empty while a profile switch is settling, matching OBC's own refusal to
read mid-switch goodbyes as faults.

### `cubesat/eps/status`

```json
{
  "timestamp": 1741863600.0,
  "voltage": 3.759,
  "voltage_median": 3.757,
  "external_power": false,
  "battery_percent": 48.4,
  "gauge_percent": 47.71,
  "voltage_rate": -197.0,
  "charge_rate": -24.62,
  "time_to_empty_sec": 7077.2,
  "time_to_full_sec": null
}
```

Those are real numbers: the pack as it read on 2026-09-03 a minute after the plug was pulled, with
every derived field computed from the two measured ones rather than made up for the example. Note
what the pair says about the gauge — 47.71 against 48.4 is close here, and it was the *slope* of the
gauge's figure that was wrong by an order of magnitude on mains, not its level on battery.

**`voltage` is the only measurement in this payload; everything else is arithmetic on it.** That is
the shape of the 2026-09-04 change: the X728's gauge is a MAX17040/41, it has no shunt and no
coulomb counter, and its state of charge is reconstructed from an internal model that was measured
drifting down at 8–10 %/h on mains with the terminal voltage flat to the millivolt. `VCELL` is a
direct ADC reading at 1.25 mV per LSB, so it is what the power policy compares and what every
derived field below is computed from.

`voltage_median` is the median of the last `eps.level_window_sec` (120 s) of samples, and **the level
the power policy actually compares**. `voltage` is published beside it unfiltered, because the
recorder stores the raw series and a median is recoverable from it while the reverse is not. A median
rather than a mean: a camera capture pulls the terminal voltage down for one sample, and a mean would
carry that into the answer.

`battery_percent` is `voltage_median` through the pack curve in `common/battery.py` — **an inferred
curve**, and the reason nothing in the satellite's behaviour depends on it. If it is wrong by five
points, a chart is wrong by five points and no decision changes. `gauge_percent` is what the fuel
gauge itself reports, published beside it and believed by nothing: the pair over a few missions is
what will confirm or replace the curve, and a record that kept only the conclusion could not audit
it.

`voltage_rate` is a least-squares slope over the last `eps.charge_rate_window_sec` (600 s) of
readings, in **millivolts per hour**, and the one slope the power policy consults. It is `null` until
the window holds `eps.charge_rate_min_span_sec` (300 s) of history, and again for that long after
`external_power` changes, because a slope measured on battery says nothing about the pack once it is
plugged in — the policy reads `null` as "trust the pin". `charge_rate` is that same slope converted
through the curve's local gradient into percent per hour, for whoever is reading a screen; it is a
restatement rather than a second opinion, which is why the mains test asks only one of them.

`time_to_empty_sec` and `time_to_full_sec` are estimates against the pack's own floor and ceiling
(3.0 V and 4.2 V), computed in the percentage domain rather than by dividing a voltage gap by a
voltage slope: a satellite drawing constant power loses constant *charge* per hour, not constant
volts, and extrapolating millivolts straight down over-states the time remaining worst exactly at
the knee. At most one of the two is ever a number, and both are `null` whenever the slope is missing,
flat or pointing the wrong way. The floor is deliberately the pack's rather than `CRITICAL`'s: EPS
sets no thresholds and does not know where `CRITICAL` is — the satellite will have powered itself off
long before this number runs out. `time_to_full_sec` inherits one extra caveat, stated at the
function: the constant-voltage tail of a charge is not modelled, so it is optimistic about the last
few points.

### `cubesat/adcs/status`

The BNO055 reports fused orientation directly, so both Euler angles and the raw quaternion are published, along with the sensor's own calibration status — a value worth surfacing, because an uncalibrated magnetometer produces confident nonsense.

```json
{
  "timestamp": 1741863600.0,
  "roll": 1.23,
  "pitch": -0.45,
  "yaw": 178.9,
  "quaternion": {"w": 0.999, "x": 0.01, "y": 0.02, "z": 0.03},
  "calib_status": {"sys": 3, "gyro": 3, "accel": 3, "mag": 2},
  "imu_temp": 34.5,
  "accel_g": {"x": 0.01, "y": 0.02, "z": 0.99},
  "gyro_dps": {"x": 0.1, "y": -0.2, "z": 0.05},
  "gnss": {"lat": 55.7558, "lon": 37.6173, "alt": 156.2, "speed": 0.4, "fix": true, "satellites": 23}
}
```

Units, because a number without one is an invitation to a silent factor-of-3.6 error: `roll`,
`pitch` and `yaw` in degrees; `gyro_dps` in degrees per second; `accel_g` in g; `imu_temp` in °C;
`lat`/`lon` in signed decimal degrees, negative for south and west; `alt` in metres; **`speed` in
metres per second** — the receiver's register holds knots and the driver converts, so nothing
downstream ever sees them.

The `gnss` sub-object always reflects the **last known fix** and never blocks the poll loop: with no signal it carries stale values and `"fix": false`. Beware that this receiver reports no fix as tidy zeros rather than as an error, and `0.000000, 0.000000` is a real place in the Gulf of Guinea — so a fix is only claimed when satellites used is above zero *and* both hemisphere characters are present.

**`yaw` is null until the magnetometer is calibrated.** Below `calib_status.mag = 3` the BNO055's heading is not a poor estimate, it is a constant — typically `0.00` — and a constant masquerading as a heading is worse than an absent one, because nothing downstream can tell the difference. Roll and pitch do not depend on the magnetometer and stay valid throughout. This is why `calib_status` is published at all: an uncalibrated magnetometer produces confident nonsense, and the only defence is saying so in the telemetry.

### `cubesat/payload/status`

Retained, and published as soon as the broker connection is up — not on the first tick, because
PAYLOAD's `NOMINAL` cadence is 60 s and `DEPLOY`'s window is 20 s. This is the message OBC's
`DEPLOY` waits for, so it carries `present` per device: the flags are the result of an actual
transaction with each one, which is what distinguishes **the sensor answered** from **the process
started**. A heartbeat proves only the second, and every service here is written to log a silent
device and stay up — so heartbeat-only evidence would pass a bring-up with a cable knocked loose,
which is the case `DEPLOY` exists for.

```json
{
  "timestamp": 1741863600.0,
  "sensor": {"device": "SEN0501", "present": true, "readings": 148, "last_read": 1741863595.0},
  "camera": {"device": "Camera Module V2", "present": true},
  "storage": {"free_mb": 21493.7, "min_free_mb": 512.0, "blocked": false},
  "mission_photos": {"active": false, "interval_sec": null, "frames": 0, "reason": null},
  "mission_id": 42,
  "photo_dir": "/var/lib/cubesat/photos/42"
}
```

`photo_dir` is the directory the *open* mission files under, so it names the root that belongs to
DHS' current database — `photos-diag/42` during a `DIAG` session. With no mission open it is the
scratch directory on the tmpfs.

Published on connect and on any change worth reporting: a device appearing or going silent, a
mission's photography starting or ending, a capture refused for want of space, a new `mission_id`
from `dhs_status`. It is **not** republished on the science cadence, so `last_read` and `free_mb` are
snapshots from the last such change rather than a live feed — liveness is the heartbeat's job, and
disk usage is also in the system-health block DHS records every row.

One dead device degrades the payload; it does not silence the subsystem. A camera that will not
open still leaves `sensor.present` true and the science flowing, and vice versa. Only when
**neither** answered is nothing published at all — an empty status would tell `DEPLOY` that
hardware answered when none did, and the bus sweep at `0x22` is what fails that bring-up.

**`storage` is why a satellite may have stopped taking photos.** The camera is the only unbounded
writer here, so it is the one that watches the card: below `photos.min_free_mb` (`blocked: true`) a
`take_photo` is refused with the room left in the reason, and a running series stops itself.
PAYLOAD deletes nothing — retention is DHS's, and the two numbers are the same headroom seen from
either side. The science tick is deliberately **not** gated on free space: that reading is small,
bounded, and the thing most likely to explain what went wrong.

`mission_photos` describes the mission's own photography — there is no command for it, so this is
the only place its state appears. `frames` counts frames on disk, not frames attempted. `reason` is
null while a series is running **and** when none was ever started; afterwards it says why it ended —
the mission closed, a mission state that no longer permits the camera, the camera failing several
frames in a row, or the free-space floor. Without it, a satellite that quietly stopped taking
pictures looks exactly like one whose mission never opened.

`mission_id` is null, and `photo_dir` is `/run/cubesat/photo`, whenever DHS has no mission open —
the tmpfs a photograph goes to when nothing is being recorded.

**The camera is opened on demand and given back when idle.** An open Picamera2 runs its ISP loops —
metering, white balance, the preview stream — continuously, which is SoC heat on a satellite that
may not take a photo for hours. After `camera.idle_close_sec` (default 60 s) without a capture the
sensor is closed; the next capture re-opens it, which costs about a second. A series faster than
that window keeps the camera warm; a slower one lets it cool between frames — and
`photos.mission_interval_sec` (300 s) is deliberately slower, so **every frame of a mission is a cold
capture**. Whether Picamera2's auto-exposure has converged by then was the question behind bench
check V11 — settled on the satellite on 2026-09-01: a cold frame and a warm one came out
indistinguishable (luma 94.2 against 94.6), and what a cold capture actually costs is latency, 0.97 s
against 0.21 s. Recorded at the setting in `config/config.yaml`.

### `cubesat/payload/data`

```json
{
  "timestamp": 1741863600.0,
  "temperature": 23.4,
  "humidity": 45.2,
  "pressure": 1013.0,
  "light": 412.0,
  "uv_index": null,
  "uv_raw": 14
}
```

**`uv_index` is null until the sensor's board revision is known.** The SEN0501 exposes one raw UV
register that two revisions read with different formulas, and they do not disagree slightly: at raw
14 the V1.0 element gives `0.00` and the V3.0 one `84.35`. Which board is on this satellite has not
been established, so the index is withheld and `uv_raw` — the register as read — is published
instead. An unresolved reading is still a recorded observation; an invented one is not a reading at
all. Set `science.sen0501_revision` in `config/config.yaml` — or `CUBESAT_SEN0501_REVISION`, which
overrides it — to `v1` or `v3` once the board is identified, and the index appears with no code
change.

There is deliberately **no altitude** here. The vendor library derives one from pressure against a
hard-coded sea-level reference, which makes it meaningless as an absolute, and the register's 1 hPa
resolution is about eight metres per bit. Altitude comes from the GNSS receiver, which measures it.

### `cubesat/payload/photo`

**Retained.** An on-demand `take_photo` carries the image itself, base64-encoded — that is how the
dashboard receives it, and the retain flag is what lets a page opened five minutes later still show
it. A **mission frame carries metadata only**: path, size, sequence number and mission, with no
`photo_base64`. A hundred frames at a few hundred kilobytes each would be tens of megabytes through
a broker whose actual job is carrying the telemetry the satellite exists to collect. Those are on
the card, filed under their mission, for whoever wants them.

Both variants carry `kind`, which is what a consumer should branch on — never the presence or
absence of a base64 blob.

With no mission open the `path` is under `/run/cubesat/photo` and the file is **deleted immediately
after this message is published**: the pixels in it are the whole delivery, and nothing reaches the
SD card. PAYLOAD clears the retained frame when it starts, so a session never shows the previous
one's photograph.

`take_photo` response:

```json
{
  "timestamp": 1741863600.0,
  "request_id": "req_001",
  "status": "SUCCESS",
  "kind": "photo",
  "file": "photo_20260824_120000.jpg",
  "path": "/var/lib/cubesat/photos/42/photo_20260824_120000.jpg",
  "size_bytes": 1874233,
  "mission_id": 42,
  "sequence": null,
  "overlay": null,
  "photo_base64": "<base64-encoded JPEG>"
}
```

One mission frame — the same fields, `sequence` filled in, and **no `photo_base64`**:

```json
{
  "timestamp": 1741863660.0,
  "status": "SUCCESS",
  "kind": "mission_frame",
  "file": "frame_20260824_120100_0007.jpg",
  "path": "/var/lib/cubesat/photos/42/frame_20260824_120100_0007.jpg",
  "size_bytes": 1863004,
  "mission_id": 42,
  "sequence": 7,
  "overlay": null
}
```

A refusal, on the same topic, so a ground station is never left waiting for a photo that was never
coming:

```json
{
  "timestamp": 1741863600.0,
  "request_id": "req_001",
  "status": "ERROR",
  "reason": "Photo capture not allowed: mission state is 'LOW_POWER'",
  "reason_code": "state"
}
```

**Two spellings of the same no, and both are needed.** `reason` is the sentence a person reads, with
the numbers in it — which state refused, how many megabytes are left. `reason_code` is one word
(`state`, `nospace`, `camera`, defined in `payload/camera.py`) and it exists because the sentence
cannot cross the radio: a beacon field may not contain a space, so `!photo`'s ack carries the code
as `err=nospace` while the dashboard carries the sentence. Added 2026-09-03 with that ack.

`mission_id` is null and the path falls under `/run/cubesat/photo` when DHS has no mission open — a
photo is taken and delivered, never refused over a bookkeeping detail, and the frame is deleted once
this message carries its pixels.

**`overlay` is a sidecar, not ink on the pixels.** `params: {"overlay": true}` writes a JSON file
beside the photo and echoes the same object here; nothing is drawn into the image. There is no
imaging library in this project, and a science image with a caption burned across it has been
defaced — the text cannot be removed and the pixels under it are gone.

```json
{
  "captured_at": "2026-08-24T12:00:00Z",
  "timestamp": 1741863600.0,
  "file": "photo_20260824_120000.jpg",
  "mission_id": 42,
  "mission_state": "NOMINAL",
  "position": {"lat": 55.7558, "lon": 37.6173, "alt": 156.2, "speed": 0.4,
               "fix": true, "satellites": 23, "at": 1741863580.0},
  "width": 1920,
  "height": 1080
}
```

The position is the last **fixed** reading PAYLOAD saw on `adcs_status`, carrying `at` — the
timestamp of the message it came from. A last known fix can be minutes old, and a coordinate on a
photo with no age attached is exactly the plausible wrong number this telemetry keeps trying to
avoid. It is null when no fix has been seen: `adcs_status` is not retained, so "if one is known" is
literal.

### `cubesat/comms/status`

Published retained on connect and whenever a channel is toggled. It exists so
that OBC's `DEPLOY` has evidence the radio answered — COMMS was the one mission
service with no status topic, and its absence is what once pushed bring-up onto
heartbeats, which prove a process started and nothing more.

```json
{
  "timestamp": 1741863600.0,
  "radio": {"present": true, "node": "!698204b0", "region": "US"},
  "beacon_enabled": true,
  "lora_enabled": true,
  "lora_listening": true,
  "command_channel": 1,
  "last_uplink": 1741863400.0
}
```

**The beacon starts off in `DEMO` and `EXPO`** (`downlink: {lora: true, beacon: false}`, decided
2026-09-01). Those are the profiles where the satellite is a metre from its operator with the
dashboard open, and beaconing at them over a shared mesh channel is noise. The radio still listens —
which is what makes an uplinked `set_profile` the way a trip begins — and `beacon on` from the radio,
from `cubesat beacon on` over SSH, or from the dashboard turns transmission back on inside the same
profile. Entering a profile resets transmission to that profile's own starting state, so "quiet in
DEMO" stays true rather than being true only until the first time anybody turned the beacon on.

**`beacon_enabled` and `lora_listening` are different things, and the difference is the way home.**
`beacon_enabled` governs the *scheduled* beacon; listening is governed by the profile alone. So a
satellite told to stop talking is still reachable over the same radio that told it to — otherwise
`set_comms_config {"beacon_enabled": false}` sent over LoRa would be a one-way door, on the profile
where the radio is the only door there is.

**And it is still reachable in the other direction: an answer is not a beacon** (2026-09-03). A
reply to an accepted command is gated on `lora_listening`, not on this flag, so **every profile that
runs COMMS answers the commands it accepts** — `beacon off` rations what the satellite says
*unasked*, and nothing else. Three commands were obeyed and left unacknowledged in `DEMO` on
2026-09-02 before that changed, including `beacon off` itself, whose confirmation was dropped by the
flag it had just set: "the transmitter is off now" and "the command never arrived" are the same
silence to the operator holding the phone.

**`lora_enabled` is the name `beacon_enabled` had until 2026-09-03**, and it is still published here
with the same value — deprecated, and only for the dashboard build deployed on 2026-09-02, which
reads it. It goes when a groundstation build that reads `beacon_enabled` is deployed; nothing on the
satellite consumes either key. It is also still accepted as a parameter of `set_comms_config`, for
the same client and until the same day. The rename is not cosmetic: `beacon on|off` was itself
renamed from `lora on|off` on 2026-09-01 because the old word said the wrong thing, and a flag that
no longer decides whether LoRa transmits at all is that same lie one level down.

**`command_channel` is the third member of that set**, and it is there because hearing a message and
acting on one became different things when the uplink filter landed. It reports the mesh channel
index an uplink must arrive on — `1`, the private `CubeSat` channel, unless `LORA_CHANNEL_INDEX`
says otherwise. A ground station one index out from the satellite transmits perfectly, receives
perfectly and never holds a conversation, which is the hardest radio fault there is to diagnose from
the outside; this field is what makes it a five-second check instead. See
[Setting up the command channel](#setting-up-the-command-channel).

The same reasoning shapes what `SAFE` does. An earlier version silenced COMMS entirely there, which
silenced *receiving* too — and `SAFE` is reachable from `FLIGHT` through a subsystem fault, so the
state that most needs a `recover` command would have been the one state deaf to it. COMMS now wakes
every 60 s in `SAFE` to listen and beacons every 600 s: ten chances to hear a recovery for every
transmission. Listening is a memory read; transmitting is airtime and a current spike, and the
asymmetry reflects that.

It still beacons in `SAFE` rather than going quiet, because the beacon *is* how the ground learns
the satellite is in `SAFE`. A satellite that falls silent exactly when something is wrong is one
nobody can help; safe mode exists to stay contactable, not to hide.

### `cubesat/comms/radio`

One event per radio transaction, published as it happens — a received message, or a transmission
attempt with whether it left. Not retained: this is a log line, not a state. DHS records these
into `radio_log` while a mission is open; the dashboard renders them live.

An `rx` event only ever describes a message on the satellite's own mesh channel. Traffic on any
other channel is refused ahead of this publish and appears nowhere here — see
[COMMS](#comms) for why the order matters.

```json
{
  "timestamp": 1741863600.0,
  "direction": "rx",
  "text": "!pos",
  "bytes": 4,
  "sender": "!e2f1a4c8",
  "snr": 6.25,
  "rssi": -96,
  "hops": 0
}
```

```json
{
  "timestamp": 1741863610.0,
  "direction": "tx",
  "kind": "ack",
  "text": "CSAT t=1741863610 st=NOMINAL re=pos lat=55.7558 lon=37.6173 fix=1 age=2 alt=156 sat=23 pr=FLIGHT b=78.2 v=3.94 ep=0 m=42",
  "bytes": 120,
  "sent": true
}
```

Link-quality fields are null where the node did not report them — `rssi` is absent on some
packets, and `hops` (mesh hops to arrival, 0 = heard directly) is derived from packet fields not
yet exercised on the bench. A failed transmission is published with `sent: false` rather than
suppressed: it spent no airtime, but it says something about the link that a log without it would
paper over.

### `cubesat/dhs/status`

Retained. Recording:

```json
{
  "timestamp": 1741863600.0,
  "recording": true,
  "database": "/var/lib/cubesat/comms.db",
  "mission": {"id": 42, "label": "walk to work", "started_at": "2026-08-24T07:12:03Z", "rows": 318},
  "rows": 148213,
  "db_size_bytes": 24117248,
  "last_write": 1741863595.0,
  "retention_days": 30,
  "attitude": {"written": 2283, "buffered": 0, "min_interval_sec": 1.0},
  "radio": {"written": 57, "buffered": 0},
  "last_delete": null,
  "photos": {"free_mb": 21493.7, "min_free_mb": 512}
}
```

`attitude.buffered` and `radio.buffered` growing is the one signal that the card has stopped
accepting writes while the recorder is still, correctly, alive.

Not recording — published on connect, before the retained `obc_status` has said which profile is
active, and again whenever a mission closes or a database is refused:

```json
{
  "timestamp": 1741863600.0,
  "recording": false,
  "database": null,
  "mission": null,
  "rows": null,
  "db_size_bytes": null,
  "last_write": null,
  "retention_days": 30,
  "attitude": {"written": 0, "buffered": 0, "min_interval_sec": 1.0},
  "radio": {"written": 0, "buffered": 0},
  "last_delete": null,
  "photos": {"free_mb": 21493.7, "min_free_mb": 512}
}
```

`database`, `rows` and `db_size_bytes` are null when no database is open — and `rows` is **also**
null when the table could not be counted. Unknown is reported as unknown rather than as zero, which
on this topic would read as an empty recorder rather than as a recorder that could not look.

`mission.id` **and `database` together** are what PAYLOAD files photographs under, which is why this
message is retained and published promptly on any mission change. Not the id alone: both databases
issue mission ids from 1, so the pair is what names a directory (see
[Mission Sessions](#mission-sessions)). PAYLOAD takes them from this one message so they cannot
get out of step, and treats a mission arriving without a database as no mission — the same
withholding a non-integer id gets. `recording: false` is what OBC's `CRITICAL` path waits for
before asking HOSTD to power the host off, with a bounded grace — a late publish there costs the
flush it was waiting for.

**`last_delete` is how a `delete_mission` is answered**, and it is the only response channel on this
topic. Null until one has been asked for; afterwards, what the last one did:

```json
{
  "at": 1741863612.4,
  "request_id": "req_042",
  "mission_id": 41,
  "ok": true,
  "error": null,
  "rows": 318, "attitude": 2283, "radio": 57,
  "photos": 12, "bytes_reclaimed": 5242880
}
```

A refusal carries `ok: false` and the reason in `error` — the profile is `EXPO`, the mission is the
one currently being recorded, there is no such mission, or the database would not open. It rides on
the status rather than on a topic of its own because a delete changes exactly what this message
already reports. The status is retained, so a page opening later sees the last result: a client acts
on one whose `request_id` matches a command it sent, and a stale result matches nothing.

`photos.unfiled_bytes` was reported here until 2026-09-01 and is deliberately not replaced: a
photograph taken with no mission open never reaches the card now, so there is no longer a pile of
files no policy covers. `free_mb` and `min_free_mb` are the recorder's horizon and PAYLOAD's floor — the same headroom from two sides,
put in one message so they can be compared without an ssh session.

### The command vocabulary

**One table, and it is the only one.** Every way of reaching the satellite ends at the same JSON on
`cubesat/command`: the radio (COMMS canonicalises the compact spelling *before* relaying, so nothing
downstream knows which *link* a command arrived on — the mesh channel it must have arrived on is a
separate rule, and COMMS applies it before any of this), the dashboard's Mission Console, its Quick
Commands buttons, the `cubesat` CLI, and anything else that can publish to a broker. If a command is
not here, it does not exist.

| Action | Radio & dashboard console | Shell | On the bus | Handler |
|---|---|---|---|---|
| Switch profile | `profile flight` — the profile and nothing else | `cubesat profile flight [--ttl 8h] [--mission "walk to work"]` | `set_profile` `{profile, ttl_minutes?, mission_label?}` | OBC |
| Latch `SAFE` / clear it | `safe` / `recover` | — | `safe_mode` / `recover` | OBC |
| Take one photograph | `photo` | — | `take_photo` `{overlay?}` | PAYLOAD |
| Start / stop the scheduled beacon | `beacon on` / `beacon off` | `cubesat beacon on\|off` | `set_comms_config` `{beacon_enabled}` | COMMS |
| Am I heard? | `ping` | — | `ping` | COMMS |
| Where is it | `pos` | — | `get_position` | COMMS |
| Host CPU, RAM, disk, uptime | `sys` | — | `get_system` | COMMS |
| Temperature, humidity, pressure, light | `env` | — | `get_environment` | COMMS |
| What is being recorded | `mission` | — | `get_mission` | COMMS |
| The whole telemetry bundle | `telemetry` — **console only**, not over the radio: the answer is far past 240 bytes | — | `get_telemetry` | COMMS |
| Restart one service | `restart adcs` | `cubesat restart adcs` | `restart_service` `{service}` | OBC relays it; HOSTD executes it against the allowlist |
| Erase one recorded mission | — | — | `delete_mission` `{mission_id}` | DHS |

Three read-only commands exist in the shell alone. They publish nothing: they read the retained
statuses the broker already holds, or the recorder's database.

| Question | Shell | Reads |
|---|---|---|
| What is it doing? | `cubesat profile` | `host_status`, `obc_status`, `dhs_status` |
| Is it well? | `cubesat status` | the above plus `eps`, `comms`, `payload`, and `dhs_telemetry` for the host metrics |
| What trips are on the card? | `cubesat mission list [--all]` | `comms.db`, read-only, no broker involved |

**The compact spelling.** `!` is optional and buys exactly one thing: declared intent. A `!` line
that does not parse is answered `re=? ok=0 err=unknown`, because the sender is a person standing in
a field wondering why nothing happened; a bare line that does not parse is ordinary mesh chat and is
left alone. The flip side, accepted knowingly: chat that is exactly a command line (`ping` on its
own) is a command. `lora on|off` is still accepted as the name `beacon` had until 2026-09-01 —
undocumented, and kept only so a command that worked last week does not answer "unknown".

**The compact spelling is the whole of what the radio takes** (2026-09-03). The middle column above
is the radio vocabulary exactly: an uplink is a compact line or it is not a command. JSON composed
by hand used to be a second, verbatim path off the air, and removing it leaves one parser for the
air rather than two. Concretely, over the radio `set_profile` carries a profile and nothing else —
the TTL comes from the profile's own definition and an unlabelled mission is named after the minute
it started, which is exactly what a walk-to-work `profile flight` relies on — and `delete_mission`,
alone in having no short line, is now unreachable over the air at all. Every command still works
identically over MQTT, where the dashboard, the CLI and any other broker client publish the full
JSON in the fourth column. An uplink that is neither a compact line nor answerable gets one line in
the service log and no airtime; it is behind the private-channel filter, so the likeliest sender is
an operator reaching for last week's spelling, and that log line is the trace they will look for.

```json
{"command": "set_profile", "params": {"profile": "EXPO"}, "request_id": "req_010"}
{"command": "take_photo", "request_id": "req_001", "params": {"overlay": false}}
```

`request_id` is optional and echoed back on whatever topic answers, so a client can tell its own
reply from somebody else's. It is deliberately **not** echoed over the radio: the airtime costs more
than the disambiguation is worth when there is one operator with one phone.

**`restart_service` names a subsystem, never a systemd unit.** The translation into
`cubesat@adcs.service` happens once, inside HOSTD, next to the allowlist that bounds it — a ground
client able to name a unit would be reaching past this vocabulary into systemd, so a unit name is
refused. `cubesat@obc`, `cubesat-hostd`, `mosquitto` and `NetworkManager` are outside reach whatever
is asked, and re-applying a profile is still the way to restart *everything* a profile names: this
exists so that restarting one service does not take the dashboard away from a room full of people.

**A restart OBC asked for is not a lost subsystem.** The restarted service says goodbye on its way
out (`alive: false`), and OBC's health monitor is built to act on that immediately — so the first
version of this command latched `SAFE` until a ground `recover`, which is precisely what it was
built to avoid (found on the hardware, 2026-09-01). OBC now declares the departure before relaying
the restart, and `health.expect_restart` waives that one goodbye for one loss window
(`heartbeat.interval_sec × heartbeat.miss_threshold`, 30 s). A service that does not come back
inside it is declared lost exactly as it would have been: the protection is postponed, not switched
off. A restart nobody announced — `systemctl restart` by hand — is still a fault, deliberately.

**`delete_mission` is deliberately the one command with no compact spelling.** Every other command is
either recoverable — a profile can be re-applied, a beacon re-enabled — or harmless to repeat; this
one erases a recorded flight, and commands are still unauthenticated. Having no short line means it
is not in the radio vocabulary and not in the Mission Console's, whose table mirrors it, so it cannot
be reached by somebody exploring what the satellite understands. Since 2026-09-03 that is **absolute
over the air**: JSON stopped being a command on the radio, so having no compact spelling now means
having no radio path at all, where until then it meant a deliberately awkward one. The fence got
stronger, not narrower — this verb erases a recorded flight and commands are still unauthenticated,
and there is no version of "typed it carefully" that makes a 240-byte one-way link the right place
to do it from. It stays fully reachable over MQTT: the dashboard's mission archive dialog, and any
other broker client. That is the right home for it, because it is the one surface where whoever
presses delete is looking at the mission being deleted.

The fence that does refuse it is the profile: `EXPO`, where the satellite is its own open access
point with an audience on it. That lives in DHS rather than in the broker's ACL, because `acl.conf`
cannot see which profile is applied. DHS also refuses to delete the mission it is recording.

**There is no `poweroff` in this vocabulary, and that is deliberate.** `CRITICAL` is the only thing
permitted to power the host down, and it decides that from the battery rather than from a button —
see [`docs/concept.md`](docs/concept.md#the-radio-command-contract) for the reply contract and the
reasoning behind each of these.

---

## Data Flows

### Cold boot

```
1. systemd starts mosquitto, then HOSTD.

2. HOSTD applies the DEFAULT profile, HOSTED — it never restores what was active
   before. It reads /var/lib/cubesat/last-profile first, publishes what it said as
   `previous`, then overwrites it with this run.
   Publishes  →  cubesat/host/status  (retained, boot: true)

3. OBC starts, reads the retained host/status, and settles in STANDBY.
   EPS starts and begins publishing battery telemetry.

3a. If `previous` was an unfinished FLIGHT, OBC waits for the first eps_status and
    reads one thing: whether there is mains. With mains it stays here — the desk
    case. Without it, the trip resumes, on the remainder of its own TTL and under
    its own label. See "Resuming an interrupted trip".

4. Nothing else runs. No sensors are polled, no rows are written, no dashboard,
   no access point — until a profile asks for it.

5. Whenever DHS is next started, its first act is orphan recovery: any mission row
   with a null ended_at is closed at the timestamp of its own last telemetry row,
   with end_reason = interrupted. This is what a battery-death mid-trip leaves behind.
```

So a satellite that shut itself down at 9 % battery on a walk and gets plugged in at a desk
comes up idle and reachable, with the interrupted mission properly closed in the database.

### Profile switch

```
1. Operator runs:  cubesat profile expo
   (or the Telegram bot / dashboard / a LoRa uplink sends the same command)
   {"command": "set_profile", "params": {"profile": "EXPO"}}  →  cubesat/command

2. OBC validates the profile against config/profiles.yaml, then publishes
   {"action": "apply_profile", "profile": "EXPO"}  →  cubesat/host/command

3. HOSTD (root):
   - stops the external units named in the registry
   - switches the network from client to AP, brings up dnsmasq + mDNS
   - starts cubesat@adcs, cubesat@payload, cubesat@dhs, cubesat@comms, cubesat-dashboard
   - sets the CPU governor
   - notes the profile, its deadline and the mission label in
     /var/lib/cubesat/last-profile (evidence for the next boot, never instruction)
   Publishes the achieved state  →  cubesat/host/status  (retained)

4. OBC reconciles achieved against requested.
   Match → mission state STANDBY → DEPLOY.
   Mismatch → the error stays visible in host/status and OBC does not pretend otherwise.

5. DEPLOY: sweep the bus for the addresses this profile's services need, then wait
   (with a timeout) for a first status message from each of them. A GNSS fix is
   awaited best-effort and only logged — see below.
   Success → NOMINAL.  Failure → SAFE.

6. On reaching NOMINAL with persistence permitted, DHS opens a mission: a new row in
   `missions` with started_at, the profile, and the optional label from the command.
   Every telemetry row from here on carries its mission_id.
   Any previously open mission was closed in step 2 with end_reason = profile_change.
```

### Telemetry recording

```
1. EPS      every 30 s   → cubesat/eps/status
   ADCS     every 500 ms → cubesat/adcs/status
   PAYLOAD  every 60 s   → cubesat/payload/data
   (each interval derived from the current mission state)

2. DHS caches the latest value from each topic. Every cadence tick, if the profile
   permits persistence, it assembles a row — subsystem values + mission_id + profile
   + state + system health metrics — and writes it to SQLite. Rows past the retention
   horizon are purged periodically.

3. COMMS, independently and from its own cache: transmits a beacon over LoRa if
   the profile permits and the beacon table says this state talks, and polls the
   radio inbox on every wake the profile permits.
```

Steps 2 and 3 are independent by design: the radio can be off while recording continues, and recording can be off while the radio still beacons.

### Uplink command over LoRa

```
1. Ground sends a message from another Meshtastic node

2. COMMS receives it through the meshtastic library callback and translates the
   compact line into canonical JSON (a line the table does not know is not a
   command: it is logged and dropped, never answered unless it began with `!`)

3. COMMS publishes the canonical JSON  →  cubesat/command

4. OBC / PAYLOAD / COMMS handle it exactly as a locally published command
```

This is the recovery path for `FLIGHT`, where Wi-Fi is down and there is no SSH.

### Photo request

```
1. {"command": "take_photo", "request_id": "req_001"}  →  cubesat/command

2. PAYLOAD checks the mission state from the retained obc/status:
   NOMINAL      → capture via Picamera2, save under photos/<mission_id>/, base64-encode
   anything else → publish an error with the reason

3. Publishes the response  →  cubesat/payload/photo
```

### Battery descent

```
1. EPS publishes voltage 3.628 V, voltage_median 3.630 V, battery_percent 29.4
   →  cubesat/eps/status

2. OBC: 3.630 V < 3.64 V, and not on mains  → LOW_POWER
   ADCS drops to 0.2 Hz, PAYLOAD to 300 s, DHS to 300 s, camera refused,
   OBC asks HOSTD for the powersave governor.
   Under EXPO the AP and the dashboard stay up.

3. 3.57 V → SAFE: sensors only, no camera; the radio keeps listening and
   beacons every 10 min instead of every minute.

4. 3.44 V → CRITICAL: OBC tells DHS to flush and close the mission
   (end_reason = battery_critical), then asks HOSTD to power off.
   The X728 brings the Pi back when mains returns — into HOSTED, not into
   whatever profile was running. See Cold boot above.

   From here to the X728's own 3.0 V cutoff is over two hours at the idle
   discharge measured on the hardware, which is what the margin is for: the
   flush and the poweroff spend charge, and they must not run out of it.

5. Plug the satellite in at any point and the descent stops: mains recovers it to
   NOMINAL from LOW_POWER or SAFE at any level, and suppresses the descent in the
   first place. On battery, recovery needs ≥ 3.75 V.
   Either way the mission continues uninterrupted — a state change happens inside
   a mission, it does not end one.

   "On mains" means the PLD pin AND a terminal voltage that is not still falling
   (voltage_rate above -30 mV/h). A charger that has stopped charging does not
   count, and the satellite still reaches CRITICAL — otherwise one failed jack
   would disable the protection permanently. A single dip does not count either:
   the level compared is the 120 s median, so a camera capture cannot descend a
   state on its own.
```

### Live dashboard

```
1. Browser opens http://cubesat.local, gets the static React build from DASHBOARD

2. Browser connects to mosquitto's own WebSocket listener on 9001 and subscribes
   to cubesat/# — every retained status is delivered on connect. DASHBOARD is not
   in that path at all: there is no /ws and no bridge to keep in step.

3. Charts request /api/telemetry, which answers the current session: the open
   mission from the recorder's database while one is being recorded, DASHBOARD's
   in-memory ring otherwise. `source` says which answered.

4. A button in the UI publishes to cubesat/command over that same broker
   connection — the same path the CLI and a radio uplink use. There is no
   /api/command; this service has no write path.
```

---

## Directory Structure

A real `src` layout: `cubesat` is the package, installed with `pip install -e .`, so imports read
`from cubesat.common.topics import TOPICS` and services launch as `python -m cubesat.adcs` with no
`PYTHONPATH` juggling.

```
cubesat-sim/
├── pyproject.toml                  # deps, [project.scripts], pytest/ruff/mypy config
├── src/
│   └── cubesat/
│       ├── common/                 # shared by every process
│       │   ├── service.py          #   class Service — MQTT, heartbeat, cadence, SIGTERM
│       │   ├── config.py           #   config.yaml + env, resolved paths
│       │   ├── profiles.py         #   profiles.yaml loading and validation
│       │   ├── states.py           #   Profile, MissionState, EndReason, Persistence
│       │   ├── topics.py           #   the TOPICS dict and the payload envelope
│       │   ├── mqtt.py             #   MQTTv5 factory, backoff, last will
│       │   ├── cadence.py          #   mission state → poll interval
│       │   ├── last_profile.py     #   the last-profile file: what the run before this one did
│       │   ├── log.py              #   rotating file + console
│       │   └── metrics.py          #   CPU / RAM / disk / temperature
│       │
│       ├── hal/                    # the laptop/Pi seam
│       │   ├── interfaces.py       #   Protocols and reading types; MAX_RADIO_MESSAGE_BYTES
│       │   ├── i2c.py              #   shared bus, reentrant flock on /run/cubesat/i2c.lock
│       │   ├── registry.py         #   CUBESAT_MOCK_HARDWARE picks rpi or mock
│       │   ├── rpi/                #   bno055, tel0157, sen0501, max17048,
│       │   │                       #     camera (Picamera2), meshtastic_radio
│       │   └── mock/               #   one fake per interface, + _signal.py
│       │
│       ├── hostd/                  # root. The only privileged process
│       │   ├── service.py          #   MQTT listener + Unix control socket
│       │   ├── executor.py         #   Executor protocol, subprocess and recording
│       │   ├── network.py          #   client / AP / off, and the mDNS daemon
│       │   ├── allowlist.py        #   the whole inventory of what root may systemctl
│       │   └── control_socket.py   #   break-glass channel for when the broker is down
│       │
│       ├── obc/                    # the head, with no hands
│       │   ├── service.py          #   wiring; the decisions live next door
│       │   ├── profile_machine.py  #   request a profile, reconcile what HOSTD achieved
│       │   ├── mission_machine.py  #   the legal moves between mission states
│       │   ├── power_policy.py     #   what a pack voltage means, in one place
│       │   ├── resume.py           #   may a reset put an interrupted FLIGHT back?
│       │   ├── deploy.py           #   the bring-up self-test
│       │   ├── health.py           #   who is still alive
│       │   └── commands.py         #   parsing what the ground sent
│       │
│       ├── eps/                    # read the gauge and the mains pin
│       │   ├── service.py
│       │   └── slopes.py          #   %/h and mV/h as least-squares slopes over history
│       ├── adcs/                   # service.py — orientation and position
│       │
│       ├── payload/
│       │   ├── service.py
│       │   ├── camera.py           #   capture policy, mission cadence, filing, disk floor
│       │   └── science.py          #   SEN0501 readings, normalised
│       │
│       ├── dhs/                    # the flight recorder
│       │   ├── service.py
│       │   ├── schema.py           #   versioned, forward-only migrations
│       │   ├── missions.py         #   session lifecycle, orphan recovery
│       │   ├── recorder.py         #   row assembly and write
│       │   └── retention.py        #   row purge, and photos with their mission
│       │
│       ├── comms/                  # the link, and nothing else
│       │   ├── service.py
│       │   ├── beacon.py           #   the 240-byte line: build, prioritise, never truncate
│       │   ├── compact.py          #   the bare-verb uplink, canonicalised into JSON on entry
│       │   ├── mesh.py             #   Meshtastic TX/RX
│       │
│       ├── dashboard/              # static files + read-only REST (no WebSocket)
│       │   ├── archive.py          #   the recorder's database, read-only, mode=ro
│       │   ├── http.py             #   routing, the SPA fallback, the photo allowlist
│       │   ├── live.py             #   the bounded ring of dhs_telemetry rows
│       │   └── service.py          #   lifecycle; follows DHS onto diag.db
│       │
│       └── cli/                    # the `cubesat` console script
│           ├── main.py             #   argument parsing and dispatch
│           ├── session.py          #   one short-lived broker connection, no state of its own
│           └── commands/           #   profile, status, mission
│
├── tests/
│   ├── conftest.py                 # temp data dir, repo config, mock HAL — set before any import
│   ├── fakes/                      # the fake MQTT client every service test uses
│   ├── unit/                       # mirrors src/cubesat/
│   └── integration/                # whole services driven through their real run() loop
│
├── config/
│   ├── config.yaml                 # broker, cadence, beacon intervals, retention, camera
│   └── profiles.yaml               # the six profiles + the external unit registry
│
├── systemd/                        # cubesat@.service template, hostd and dashboard units
├── scripts/install.sh
├── docs/                           # concept.md, architecture.md, hardware-*.md
└── hardware/                       # models/ and photos/
```


Runtime data lives **outside the checkout**, created by systemd rather than by hand:

| Path | Contents | Created by |
|---|---|---|
| `/var/lib/cubesat/` | `comms.db`, `diag.db`, `photos/<mission_id>/` and `photos-diag/<mission_id>/` (one root per database — both number their missions from 1), `last-profile`, the deployed dashboard build | `config/tmpfiles.d/cubesat.conf` |
| `/run/cubesat/` | `i2c.lock`, `hostd.sock` | `config/tmpfiles.d/cubesat.conf` |
| `/var/log/cubesat/` | `<service>.log`, rotating | `config/tmpfiles.d/cubesat.conf` |

All three are owned by the `cubesat` service account, mode `2775`, and are created by
`systemd-tmpfiles` at boot rather than by the units' `StateDirectory`/`RuntimeDirectory`/
`LogsDirectory`. The reason is that those directives re-apply the *starting unit's own* user to the
directory tree every time it starts, and `cubesat-hostd` runs as root over the same three paths, so
ownership ended up belonging to whichever unit restarted last. The setgid bit is what lets an
operator in the `cubesat` group deploy the dashboard build without `sudo`.

Keeping the database inside a git checkout means `git pull` sits next to live mission history and
file ownership depends on who cloned the repo. Set `CUBESAT_DATA_DIR=./data` for development on a
laptop.

### Why the files fall this way

| Directory | The cut |
|---|---|
| `obc/` | The most files, and it earns them: two state machines, a power policy, a self-test and a health monitor are four independently testable things. Merged into one `service.py` they become a file nobody reads end to end |
| `eps/`, `adcs/` | Two files each. **No helper files were created for symmetry** — "read a sensor and publish it" does not decompose. When logic appears, a file appears |
| `dhs/` | `schema.py` is separate because SQLite migrations are the one place where a mistake is irreversible: accumulated missions cannot be regenerated |
| `comms/` | `beacon.py` is separate because it blocks the `EXPO` and `FLIGHT` profiles and is the most likely thing to be redesigned |
| `hostd/` | `network.py` is separate because bringing up an AP with `dnsmasq` and mDNS is the fiddliest part of the project, and it needs testing apart from unit management |
| `dashboard/` | No React build in the package. The artifact from `cubesat-groundstation` is deployed to `/var/lib/cubesat/dashboard/`, path configurable. A committed frontend bundle is a thing nobody can review |
| `hal/` | Drivers live here, not in the subsystems. A driver is hardware, not subsystem logic — and the open question about `BMP280` moving from ADCS to PAYLOAD then costs one line in a service instead of a file move |

Two conventions worth stating because they are easy to erode:

- **`__init__.py` files stay empty.** No re-exports. `hostd` runs as root, and it must not pull
  `paho`, `yaml`, `smbus2` and the whole HAL into that process through a chain of package
  initialisers. The verbose explicit import is the point.
- **The `cubesat@.service` template** covers the six identical units. Eight near-identical unit
  files drift; one template cannot. `hostd` and `dashboard` stay separate — different privileges,
  different dependencies.

---

## Hardware

The satellite targets Raspberry Pi. Every component below is on the assembled unit and bench-validated; each links to its own document with specs, Pi setup steps and the gotchas found while bringing it up.

**Components** — Raspberry Pi, Pi Camera, 2× 18650 batteries, the UPS HAT, the IO Expansion HAT and the four Gravity modules. The photo below is from an earlier stage of the build and still shows the Sense HAT (C), which is no longer in the design.

![CubeSat hardware components](hardware/photos/02-hardware-components.jpg)

| Component | Purpose | Interface / Library | Product link | Documentation |
|---|---|---|---|---|
| Raspberry Pi 4 Model B | Main compute — hosts and runs every service | — | [Raspberry Pi](https://www.raspberrypi.com/products/raspberry-pi-4-model-b/) | [Raspberry Pi Docs](https://www.raspberrypi.com/documentation/) |
| [X728 V2.5 UPS HAT](docs/hardware-x728-ups-hat.md) | Battery power management: LiPo fuel gauge (MAX17040/41) + AC-loss detection (PLD pin) — used by EPS | I2C (`0x36`) + GPIO · `smbus2`, `RPi.GPIO` | [AliExpress](https://www.aliexpress.us/item/3256804825472151.html) | [Geekworm Wiki](https://wiki.geekworm.com/X728) |
| ~~[Sense HAT (C)](docs/hardware-sense-hat-c.md)~~ | ~~Environmental/orientation sensor HAT: QMI8658 (accel + gyro) + AK09918 (magnetometer) drive ADCS orientation; LPS22HB (pressure) + SHTC3 (humidity) feed Payload science data~~ | ~~I2C · `smbus2`, `lgpio`~~ | ~~[AliExpress](https://www.aliexpress.us/item/3256811354242582.html)~~ | ~~[Waveshare Wiki](<https://www.waveshare.com/wiki/Sense_HAT_(C)>)~~ |
| [Raspberry Pi Camera Module V2 (8MP, 1080p)](docs/hardware-camera-module-v2.md) | Photo capture — used by Payload | CSI · `picamera2` | [Amazon](https://a.co/d/02oyeWg8) | [Raspberry Pi Docs](https://www.raspberrypi.com/documentation/accessories/camera.html#camera-module-2) |
| ~~[IoT Node(A) — 52Pi Docker Pi Series (GSM/GPS/LoRa)](docs/hardware-iot-node-a-52pi.md)~~ | ~~Onboard GSM/GPS/LoRa module (A9G): GPS/BDS position feeds ADCS, LoRa (via the SC16IS752 I2C↔UART bridge) is COMMS' radio ground link alongside its HTTP API~~ | ~~UART (GPS) + I2C (LoRa) · `pyserial`, `pynmea2`, `smbus2`~~ | ~~[AliExpress](https://www.aliexpress.us/item/2251832864586218.html)~~ | ~~[52Pi Wiki](https://wiki.52pi.com/index.php?title=EP-0105)~~ |
| [Heltec WiFi LoRa 32 V4 (Meshtastic)](docs/hardware-heltec-lora32-v4.md) | LoRa ground link for COMMS — runs stock Meshtastic firmware, which handles framing, CRC, retries and encryption; replaces the LoRa half of the IoT Node(A) | UART `/dev/serial0` @ 115200 · `meshtastic` | [Heltec](https://heltec.org/project/wifi-lora-32-v4/) | [Heltec Wiki](https://wiki.heltec.org/docs/devices/open-source-hardware/esp32-series/lora-32/wifi-lora-32-v4/) |
| [Gravity SEN0501 — Multifunctional Environmental Sensor](docs/hardware-sen0501-environmental-sensor.md) | Environmental science data for Payload: temperature, humidity, atmospheric pressure, ambient light and UV — replaces the LPS22HB/SHTC3 pair of the Sense HAT (C) | I2C (`0x22`) · `smbus2` | [DFRobot](https://www.dfrobot.com/product-2528.html) | [DFRobot Wiki](https://wiki.dfrobot.com/SKU_SEN0501_Gravity_Multifunctional_Environmental_Sensor) |
| [Gravity 10 DOF IMU AHRS — BNO055 + BMP280](docs/hardware-bno055-bmp280-imu.md) | Absolute orientation for ADCS: BNO055 runs sensor fusion on-chip and outputs quaternion/Euler directly; onboard BMP280 adds pressure and temperature. Replaces the QMI8658 + AK09918 pair of the Sense HAT (C) | I2C (`0x28` + `0x76`) · `smbus2` — **requires a 10 kHz bus clock** | [DFRobot](https://www.dfrobot.com/product-1793.html) | [DFRobot Wiki](https://wiki.dfrobot.com/SEN0253) |
| [Gravity GNSS Receiver TEL0157](docs/hardware-tel0157-gnss.md) | Satellite positioning for ADCS — GPS/BeiDou/GLONASS with on-module NMEA parsing, exposed as plain registers. Replaces the A9G GPS of the IoT Node(A) | I2C (`0x20`) · `smbus2` | [DFRobot](https://www.dfrobot.com/product-2651.html) | [DFRobot Wiki](https://wiki.dfrobot.com/TEL0157) |

> Also requires a `mosquitto` MQTT broker running on the Pi (software, not hardware) — see [Prerequisites](#prerequisites).

> **Non-Pi development:** every driver sits behind the [HAL](#common-infrastructure), and `CUBESAT_MOCK_HARDWARE=1` runs the whole stack on a laptop against fakes — nothing imports `smbus2`, `RPi.GPIO` or `picamera2` unless a real driver is selected, so the suite needs no Pi and no bus.

### I2C Address Map

Every I2C peripheral shares bus 1 (`/dev/i2c-1`, `dtparam=i2c_arm=on`), so addresses must not collide. Scan the bus with `i2cdetect` (part of `i2c-tools`, which installs into `/usr/sbin` and is therefore not on the `PATH` of a non-interactive SSH shell):

```bash
sudo apt install -y i2c-tools
/usr/sbin/i2cdetect -y 1
```

`/dev/i2c-20` and `/dev/i2c-21` also exist on a Pi 4 — those are the HDMI DDC buses, not usable for sensors.

**The bus clock must be held at 10 kHz**, project-wide:

```
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=10000
```

This is not tuning, it is a correctness requirement: at the default 100 kHz the BNO055 stretches the clock in a way the Pi's BCM2835 controller mishandles, and roughly two thirds of byte reads come back with bit 7 silently forced to 1. Every other peripheral on the bus is low-rate and unaffected by the slower clock. See [the BNO055 documentation](docs/hardware-bno055-bmp280-imu.md#the-clock-stretching-problem) for the measurements and the reasoning.

**Verified on the bus** — all four Gravity modules were bench-tested on 2026-08-23, but **one at a time**: a single Gravity cable was moved from one to the next, so no scan ever showed all of them together. Addresses do not collide. Confirmed on the fully assembled satellite the same day: a single scan showed all seven addresses at once, and a bus-integrity check passed with 0 corrupted reads out of 200 with every module attached.

| Address | Device | Used by | Status |
|---|---|---|---|
| `0x10` | IO Expansion HAT (DFR0566) co-processor (ADC/PWM) — *identification not yet verified* | — | permanently on the HAT |
| `0x20` | [TEL0157](docs/hardware-tel0157-gnss.md) GNSS receiver (GPS + BeiDou + GLONASS) | ADCS | verified with a 3D fix, 23 satellites |
| `0x22` | [Gravity SEN0501](docs/hardware-sen0501-environmental-sensor.md) environmental sensor (temperature, humidity, pressure, ambient light, UV) | PAYLOAD | verified: all five measurements read correctly |
| `0x28` | [BNO055](docs/hardware-bno055-bmp280-imu.md) 9-axis absolute orientation sensor (on-chip fusion) | ADCS | verified: fusion running, all vectors self-consistent |
| `0x36` | MAX17040/41 LiPo fuel gauge on the [X728 V2.5 UPS HAT](docs/hardware-x728-ups-hat.md) | EPS | permanently on the UPS HAT |
| `0x68` | DS1307 real-time clock on the [X728 V2.5 UPS HAT](docs/hardware-x728-ups-hat.md#the-ds1307-at-0x68) | the kernel, via `/dev/rtc0` | **shows as `UU`, not `68`** — claimed by the `i2c-rtc,ds1307` overlay; do not access it from user space |
| `0x76` | [BMP280](docs/hardware-bno055-bmp280-imu.md) pressure + temperature, on the same board as the BNO055 | undecided — duplicates the SEN0501 pressure reading | verified: temperature and pressure compensated correctly |

**Known but not currently on the bus** (the Sense HAT (C) is out of the design):

| Address | Device | Used by | Status |
|---|---|---|---|
| `0x6B` | QMI8658 accelerometer + gyroscope, ~~[Sense HAT (C)](docs/hardware-sense-hat-c.md)~~ | ADCS (`src/common/imu_qmi8658_ak09918.py`) | removed |
| `0x0C` | AK09918 magnetometer, ~~[Sense HAT (C)](docs/hardware-sense-hat-c.md)~~ | ADCS (`src/common/imu_qmi8658_ak09918.py`) | removed |
| `0x16` | SC16IS752 I2C↔UART bridge, ~~[IoT Node(A)](docs/hardware-iot-node-a-52pi.md)~~ | COMMS (`src/comms/lora.py`), ADCS (`src/common/gps_a9g.py`) | removed — replaced by [Heltec V4](docs/hardware-heltec-lora32-v4.md) on UART |

> **`0x68` is the DS1307 RTC on the X728 UPS HAT, and it is now enabled** — `dtoverlay=i2c-rtc,ds1307`, so the satellite keeps time across reboots and network outages without NTP. Because the kernel owns the address it appears as `UU` in `i2cdetect` rather than `68`; a scan showing `UU` there is correct. See [the X728 documentation](docs/hardware-x728-ups-hat.md#the-ds1307-at-0x68).

### New Components

| Component | Power | Interface | Cost | Order Link |
|---|---|---|---|---|
| IO Expansion HAT for Raspberry Pi 5 / 4B / 3B+ (DFR0566) | 5V (3.3V/5V sensor rails, 6–12V for PWM) | Gravity — I2C, UART, SPI, IIS, digital, analog, PWM | $9.90 | [DFRobot](https://www.dfrobot.com/product-1930.html) |
| Gravity 4Pin I2C/UART Sensor Cable (10PCS) | — | Gravity PH2.0-4P ↔ XH2.54 DuPont | $6.00 | [DFRobot](https://www.dfrobot.com/product-1581.html) |
| Gravity: 10 DOF IMU AHRS BNO055 + BMP280 | 3.3–5V, 5mA | Gravity-I2C | $25.90 | [DFRobot](https://www.dfrobot.com/product-1793.html) |
| Gravity SEN0501 — High Accuracy Temperature, Humidity, Pressure, Ambient Light and UV Sensor | 3.3–5V, ~4mA | Gravity — I2C/UART | $29.90 | [DFRobot](https://www.dfrobot.com/product-2528.html) |
| Gravity: GNSS GPS BeiDou Receiver Module (TEL0157) | 3.3–5.5V, 40mA | Gravity — I2C/UART, IPEX1 antenna | $17.90 | [DFRobot](https://www.dfrobot.com/product-2651.html) |

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
| `CUBESAT.blend` | Full 3D assembly of the satellite in Blender, organized into layers (frame, mounts, electronics stack, etc.) |
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

> **Note:** all of this has been installed on a Raspberry Pi — the project lives at
> `/opt/cubesat-sim`, the units are in place, and the always-on tier comes up at boot. Two things
> the first installs taught, both still true:
>
> - **`cubesat` is not on the operator's `PATH`.** The console script is installed into the
>   project's virtualenv, so it is `/opt/cubesat-sim/venv/bin/cubesat` until a symlink or a shell
>   profile line says otherwise.
> - **The dashboard is built elsewhere.** There is no `node` on the satellite and no groundstation
>   checkout: `scripts/deploy-dashboard.sh` builds on a development machine and rsyncs
>   `client/dist/` into `/var/lib/cubesat/dashboard`.

### Prerequisites

- Raspberry Pi running Raspberry Pi OS (validated on a Pi 4, Bookworm, kernel 6.12)
- `mosquitto` MQTT broker (`sudo apt install mosquitto`) — `scripts/install.sh` configures its two listeners, see [Two listeners, and one fence](#two-listeners-and-one-fence)
- Python 3.10+
- I2C enabled **at 10 kHz** and the DS1307 RTC overlay, in `/boot/firmware/config.txt`:
  ```
  dtparam=i2c_arm=on
  dtparam=i2c_arm_baudrate=10000
  dtoverlay=i2c-rtc,ds1307
  ```
- The PL011 UART freed for the LoRa radio: `enable_uart=1`, `dtoverlay=disable-bt`, and
  `console=serial0,115200` removed from `/boot/firmware/cmdline.txt` — see
  [docs/hardware-heltec-lora32-v4.md](docs/hardware-heltec-lora32-v4.md)

### Install

```bash
sudo git clone <repo-url> /opt/cubesat-sim
cd /opt/cubesat-sim
bash scripts/install.sh
```

On a development machine, without any of the systemd side:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -e ".[test]"
CUBESAT_MOCK_HARDWARE=1 CUBESAT_DATA_DIR=./data python -m cubesat.obc
pytest --cov --cov-report=term-missing
```

`install.sh` creates the `cubesat` system account the services run as, adds you to its group,
installs `config/tmpfiles.d/cubesat.conf` and creates the three runtime directories from it, hands
the checkout to `<you>:cubesat`, creates a virtualenv at `./venv`, runs `pip install -e .`, copies
the unit files to `/etc/systemd/system/`, and enables the always-on tier (`cubesat-hostd`,
`cubesat@obc`, `cubesat@eps`). Everything else is started and stopped by `HOSTD` as a profile is
applied — not by hand. Nothing in the code ever calls `mkdir` on a system path.

Log out and back in after installing: the group membership that lets you deploy the dashboard build
does not apply to a session that already exists.

### Setting up the command channel

The satellite's radio traffic — the beacon out, every command in — travels on **one Meshtastic
channel, and it is not the public primary one.** Getting a second node onto that channel is the last
install step, and it is the one that decides whether your ground station can talk to the satellite at
all.

The setting is split in two on purpose, because the two halves live in different places:

| Half | Where it lives | Default |
|---|---|---|
| **Which channel** — the index | This repository, `LORA_CHANNEL_INDEX` | `1` |
| **What that channel is** — its name and its key | The Heltec node's own firmware, written with the `meshtastic` CLI | name `CubeSat`, key generated on your node |

**The software addresses the channel by index and never by name.** `COMMS` transmits on
`LORA_CHANNEL_INDEX` and accepts a command only from it; nothing in this repository reads or sets
the channel's name or its key, which is why neither is a configuration value here. The name is a
convention so that two people reading these documents mean the same channel — call yours something
else and only this table is wrong.

**Channel 0 is left stock.** It is the public primary with Meshtastic's well-known default key
`AQ==`, shared with every node in range; "encrypted" there means nothing, and since the mesh preset
change of 2026-09-02 this node sits on it alongside several hundred strangers. That is exactly why
commands are taken from channel 1 and nowhere else — see [COMMS](#comms).

#### Create it on the satellite's node

`COMMS` holds `/dev/serial0` whenever it is running, so stop it first. Expect `OBC` to enter `SAFE`
while COMMS is gone — it is a watched subsystem and its last will fires — and clear it with
`recover` afterwards, because a fault-latched `SAFE` does not lift on its own.

```bash
sudo systemctl stop cubesat@comms

meshtastic --port /dev/serial0 --ch-add CubeSat     # creates channel 1 with a random PSK
meshtastic --port /dev/serial0 --info               # verify: index, name and psk

sudo systemctl start cubesat@comms

# `recover` has no shell spelling — publish it, or use the dashboard's Mission Console
mosquitto_pub -t cubesat/command -m '{"command": "recover"}'
```

Then check the satellite agrees, without an SSH session — the retained
[`cubesat/comms/status`](#cubesatcommsstatus) carries `command_channel`, and if it does not read `1`
the two halves of the table above have drifted apart.

#### Add it to your own mesh

```bash
meshtastic --port /dev/serial0 --qr-all             # "Complete URL (includes all channels)"
```

Open that URL on the phone or node you want to command from. It contains **both** channels, so a
phone that already has the stock primary reports `Channel already exists` if you choose *Add*;
choosing *Replace* is safe as long as the primary in the URL is still the stock one with the default
key (`psk: "AQ=="`, `name: ""` in `--info`) — the result is that same primary plus the new secondary.

**There is no key in this repository, and there must never be one.** That URL embeds the channel's
pre-shared key, and since the uplink filter the key *is* the command credential: whoever holds it can
read the telemetry and also send `safe`, `photo` or `profile flight` — every compact verb in the
vocabulary. A published default key would hand that to everyone who cloned the repo, which is
why yours is generated on your own node by `--ch-add` and belongs in a password manager or a personal
note — never in `config.yaml`, never in a committed file, never in an issue. If it is ever exposed,
regenerate and re-import on every node:

```bash
meshtastic --port /dev/serial0 --ch-set psk random --ch-index 1
```

Two nodes must also agree on **region, modem preset and frequency slot** before a shared key means
anything — nodes on different presets do not demodulate each other at all, and this satellite is
deliberately not on the flasher's defaults. That, the node identity, and what still goes out in the
clear on the primary channel are in
[`docs/hardware-heltec-lora32-v4.md`](docs/hardware-heltec-lora32-v4.md).

### Operating it

What the CLI can say is in [The command vocabulary](#the-command-vocabulary) with everything else;
this section is only what is worth knowing about using it. A morning looks like:

```bash
cubesat profile flight --ttl 8h --mission "walk to work"   # start the trip
cubesat status                                             # over SSH, from anywhere
cubesat profile demo                                       # arrive; the mission closes
```

**`--ttl` is an override, not a requirement.** Without it the profile's own `ttl_minutes` applies —
600 for `FLIGHT`, so a trip that nobody remembered to end falls back to `HOSTED` after ten hours. It
takes `8h`, `45m` or a bare number of minutes.

**`--mission` is optional too.** With no label a mission is named after when it started
(`2026-09-01 07:12`), which is what a listing needs: before that an unlabelled mission showed as its
profile — the same word for every trip ever taken.

`cubesat status` also reports the profile the satellite was in before its last boot, read from
`/var/lib/cubesat/last-profile`. That answers "what was it doing when it died?" without the
satellite ever acting on the file. Its host metrics — CPU, RAM, disk, uptime — come off the retained
`cubesat/dhs/telemetry`, so the command answers the same way in the profiles that record nothing.

The CLI is a thin MQTT client: it publishes the same ground commands the dashboard and a LoRa uplink
publish, onto the same topic, and reads the same retained statuses. It needs no privileges, only a
reachable broker. `cubesat mission list` is the exception that needs no broker at all — the archive
is a file on the same disk, opened read-only, which matters because the dashboard does not run in
`FLIGHT` and mosquitto may be the thing that fell over.

Exit codes, because this ends up in scripts: `0` done or answered, `1` the satellite did not answer
or answered badly (a profile that applied only in part is `1`, and says what applied), `2` the
command line was wrong.

Individual units should not normally be started by hand — a profile is the unit of operation. For
debugging, they run directly from the project root so that `src` is importable as a package:

```bash
source venv/bin/activate
python -m cubesat.obc
python -m cubesat.dhs           # and so on, per service
```

No `PYTHONPATH` is needed: `pip install -e .` puts the `cubesat` package on the path, and each
service's `__main__.py` is its entry point.

Set `CUBESAT_MOCK_HARDWARE=1` to run any service against the mock HAL — no Raspberry Pi, no
sensors, no broker on the Pi. This is how the stack is developed on a laptop.

---

## Configuration

Configuration is split three ways by how often it changes and how secret it is:

| Where | What | Committed |
|---|---|---|
| [`config/config.yaml`](config/config.yaml) | Runtime defaults: broker, intervals, cadence per mission state, camera resolution, the resume fences | ✅ |
| `config/profiles.yaml` | Platform profile definitions and the external-unit registry | ✅ |
| `.env` / environment | Per-deployment values and **all secrets** | ❌ never |

Environment variables override `config.yaml`. Secrets are environment-only and must never be
committed — there is deliberately no YAML key for one.

### `config/profiles.yaml`

Profiles are data, so adding one does not mean editing `OBC`:

```yaml
default_profile: HOSTED     # applied on every boot — the profile is never restored

profiles:
  EXPO:
    mission:            active          # active | standby | none
    network:
      mode:             ap              # client | ap | off
      ssid:             cubesat
      advertise_mdns:   true            # answer cubesat.local
    external_units:     stop            # start | stop | [unit.service, ...]
    services:           [adcs, payload, dhs, comms, dashboard]
    persistence:        mission_db      # none | mission_db | diag_db
    downlink:           {lora: true, beacon: false}   # beacon: start transmitting? (default true)
    power:              {governor: ondemand}
    ttl_minutes:        null            # null = never expires

external_units:                          # the only units HOSTD may touch besides cubesat-*
  - unit: telegram-bot.service
    requires_internet: true
  - unit: starmap.service
    requires_internet: true
```

A profile's `external_units` is `start` (every registered unit), `stop` (none of them), or a list
naming exactly the ones it wants — needed as soon as the registry holds more than one thing, since
one unrelated service may belong on the desk and another only during a demonstration. A name that
is not in the registry fails at load rather than being ignored. Whatever a profile does not ask for
is stopped, so the field states the whole intent and never a change to it; the shorthands are
resolved when the file is read, and HOSTD receives a plain list of units.

A profile may carry a `ttl_minutes`. On expiry the satellite falls back to `HOSTED`, which brings
Wi-Fi and SSH back — one of three ways out of a mistyped `FLIGHT`, in order of preference:

1. **Power cycle.** Boots into `HOSTED` on the home network, because the profile is not persisted.
   No tooling, no waiting — this is the whole reason not to persist it. The one exception proves
   the rule rather than weakening it: an unfinished `FLIGHT` resumes itself only when the mains pin
   says the satellite is *not* on a desk, so a power cycle at a desk still lands in `HOSTED` — see
   [Resuming an interrupted trip](#resuming-an-interrupted-trip).
2. **`set_profile` over the LoRa uplink**, which `COMMS` re-publishes like any other command.
3. **TTL expiry**, as above. The split follows the rule that runs through this whole design:
   OBC decides what expiry *means*, while HOSTD — which holds the applied profile — turns the
   requested duration into an absolute deadline and publishes it as `ttl_expires_at`. OBC reads it
   back from the retained message, so `systemctl restart cubesat@obc` mid-flight recovers the
   deadline instead of silently discarding the safety net the profile was relying on.

**Plugging into mains is deliberately not one of them.** It reads as a fourth way out — the
satellite is home, so let it come back — and it was described as one here until 2026-09-03, when
implementing the resume made the asymmetry explicit. *No* mains is reliable evidence: a desk is
always in a socket, so a satellite without it is demonstrably not on one. *Mains present* is not the
converse. The X728 sees power on its own DC jack, and a power bank on a long trip looks exactly like
a wall socket — so an automatic return would close the mission halfway through a walk, bring Wi-Fi
up hunting a network that is not there, and lose the rest of the track. That is the silent loss the
[resume](#resuming-an-interrupted-trip) exists to prevent, re-introduced by the act of feeding the
satellite.

Ending a trip is a person's decision, and the three ways above are all a person exercising it.
`EPS` still reports `external_power`, and the power policy uses it — together with the voltage and
charge slopes, never the pin alone — to suppress the power-driven descents; it changes no profile
anywhere.

### Environment variables

```ini
MQTT_BROKER=localhost
MQTT_PORT=1883

# LoRa radio (Heltec V4 / Meshtastic over the PL011 UART)
LORA_PORT=/dev/serial0
LORA_BAUDRATE=115200
LORA_CHANNEL_INDEX=1

# Development
CUBESAT_MOCK_HARDWARE=0
```

| Variable | Default | Description |
|---|---|---|
| `MQTT_BROKER` | `localhost` | Hostname or IP of the broker |
| `MQTT_PORT` | `1883` | Broker port |
| `LORA_PORT` | `/dev/serial0` | Serial device the Meshtastic node is on |
| `LORA_BAUDRATE` | `115200` | Not optional — the Meshtastic Python library opens the port hard-coded at this rate |
| `LORA_CHANNEL_INDEX` | `1` | The mesh channel telemetry goes out on **and the only one a command is accepted from**. See [Setting up the command channel](#setting-up-the-command-channel) |
| `DASHBOARD_PORT` | `8080` | Port the local dashboard listens on |
| `DHS_RETENTION_DAYS` | `30` (from `retention.days`) | Telemetry rows and attitude samples older than this are purged, and their missions' photos with them |
| `DHS_ATTITUDE_MIN_INTERVAL_SEC` | `1.0` (from `dhs.attitude_min_interval_sec`) | Floor on how often one attitude sample is recorded. One ceiling across every profile. Costs card writes, never bus time |
| `PHOTO_MISSION_INTERVAL_SEC` | `300` (from `photos.mission_interval_sec`) | How often an open mission photographs by itself. There is no command and no interval on the wire; frames stop when the mission closes |
| `DASHBOARD_LIVE_ROWS` | `720` (from `dashboard.live_history_rows`) | How many published telemetry rows DASHBOARD keeps in memory. The charts' whole history in the profiles that record nothing — about six hours at the 30 s `NOMINAL` cadence |
| `CUBESAT_MOCK_HARDWARE` | `0` | `1` selects the mock HAL — sensors, camera and radio are fakes |
| `CUBESAT_SEN0501_REVISION` | unset | `v1` or `v3`. Unset means the UV index is withheld rather than guessed — see [`payload/data`](#cubesatpayloaddata) |
| `CUBESAT_MOCK_HOST` | `0` | `1` selects HOSTD's no-op executor — nothing is started, stopped or reconfigured. A separate axis from the HAL: running the whole stack on a laptop needs both |
| `CUBESAT_DATA_DIR` | `/var/lib/cubesat` | Database, photos and the `last-profile` marker. Point it at `./data` for development |
| `CUBESAT_DASHBOARD_ROOT` | `/var/lib/cubesat/dashboard` | Where the deployed React build is served from |
| `CUBESAT_CONFIG_DIR` | `/etc/cubesat`, else the repo's `config/` | Where `config.yaml` and `profiles.yaml` are read from |
| `CUBESAT_RUN_DIR` | `/run/cubesat` | Bus lock and the HOSTD socket |
| `CUBESAT_LOG_DIR` | `/var/log/cubesat` | Rotating log files |

The mock HAL takes a few knobs of its own, which is how the power-driven states
are reached without waiting for a real battery to drain:

| Variable | Effect |
|---|---|
| `CUBESAT_MOCK_BATTERY` | Pin the level as a percentage, e.g. `15` to sit in `SAFE`. The mock converts it to the voltage a pack at that level would show, because that is what the policy compares |
| `CUBESAT_MOCK_DISCHARGE_SEC` | Full to empty in this many seconds (default 3600) |
| `CUBESAT_MOCK_EXTERNAL_POWER` | `1` for mains present, so nothing discharges |
| `CUBESAT_MOCK_FIX_DELAY_SEC` | How long the mock GNSS reports no fix (default 60) |
| `CUBESAT_MOCK_LAT`, `CUBESAT_MOCK_LON` | Where the mock track starts walking from |

`GPS_PORT`, `LORA_I2C_ADDRESS`, `COMMS_*_ENABLED` and `COMMS_LOOP_INTERVAL_SEC` were read by the
pre-rewrite code and are gone: all four belonged to hardware or a design that is no longer on the
satellite. Nothing reads them now, so leaving one in a `.env` has no effect.

---

## Logs

Each service writes rotating logs to `/var/log/cubesat/<service>.log` (10 MB per file, 5 files
retained). As systemd units they are also in the journal:

```bash
journalctl -u cubesat-hostd.service -f      # profile changes and what they actually did
journalctl -u cubesat@obc.service -f        # state and profile transitions, health
journalctl -u cubesat@eps.service -f
journalctl -u cubesat@adcs.service -f
journalctl -u cubesat@payload.service -f
journalctl -u cubesat@dhs.service -f
journalctl -u cubesat@comms.service -f
journalctl -u cubesat-dashboard.service -f
```

`cubesat-hostd` is the one to read first when a profile did not do what you expected: it logs
every action it took and every one it refused because the unit was not on the allowlist.

Under `FLIGHT` the journal should be volatile (`Storage=volatile`) — continuous SQLite writes plus
journald on an SD card, unattended and on battery, is the wear scenario worth avoiding.

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/concept.md](docs/concept.md) | The operating concept: why there are two axes, what each profile is for, the control plane, known traps, and the phased implementation plan |
| [docs/architecture.md](docs/architecture.md) | Detailed runtime architecture and subsystem internals — **pre-rewrite**, describes the five-service version |
| [docs/hardware-*.md](docs) | One file per component in the [Hardware](#hardware) table: specs, Pi setup, bench-validation notes, gotchas |
| [PLAN.md](PLAN.md) | Working document (in Russian): the LoRa migration from the 52Pi IoT Node(A) to Heltec V4 + Meshtastic. Closed — all stages done, and the link has run on the satellite |
| [docs/code_smells.md](docs/code_smells.md) | Historical audit of the pre-rewrite code |
| [ROADMAP.md](ROADMAP.md) | What is **left**: the bench checks the code is waiting on, the decisions still open, and the work that can be written without the satellite. Finished work is deleted from it rather than ticked off |

---

## Related Projects

| Project | Description |
|---|---|
| [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) | The ground segment. Being rebuilt from a PHP/CodeIgniter backend with a React dashboard into **one React interface over several data sources** — the satellite's own [DASHBOARD](#dashboard) service, a mission exported to a static file, and later a USB Meshtastic receiver. The PHP and MySQL half is removed: no cloud service is deployed, and none is planned. See [`docs/dashboard-architecture.md`](https://github.com/miksrv/cubesat-groundstation/blob/main/docs/dashboard-architecture.md) |

---

## License

See `LICENSE` for details.
