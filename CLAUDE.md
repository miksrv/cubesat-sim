# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read This First

**The hardware is finished and validated. All of the software has now run on it, in two of the six
profiles, for minutes.** Those are different kinds of confidence, and conflating them is the main
way to go wrong here:

- Every component in the README's Hardware table is on the assembled satellite, bench-tested, and
  documented in `docs/hardware-*.md`. Those documents are the authority for anything electrical.
- All eight services exist, at 100 % line coverage with `ruff` and `mypy` clean.
- **First run on the Pi: 2026-08-31, in `HOSTED`.** `HOSTD`, `OBC`, `EPS` and `COMMS` executed
  against real hardware: five I2C devices answered, the Meshtastic node was reachable, a ground
  command round-tripped, and `SAFE` was entered and cleared for real.
- **First `DEMO`: 2026-09-01, 21:03.** `ADCS`, `PAYLOAD`, `DHS` and `DASHBOARD` ran for the first
  time — `DEPLOY` completed in 1.4 s into `NOMINAL`, the attitude widget and a photograph were
  watched live. So every service and every driver has now been exercised on the satellite.
- **`FLIGHT`, `EXPO` and `DIAG` have never been applied**, so what is untried is now a set of
  *profiles* rather than a set of services: the field radio-only case, the access point (V6), a
  moving GNSS fix (V2, V3), and the separate `diag.db`. `MAINTENANCE` came off that list on
  2026-09-02, applied to free `/dev/serial0` for the Heltec's modem preset and returned to `HOSTED`
  with nothing lost.

Neither run was free of surprises, and none of them were surprises a test could have had. `HOSTED`
cost three defects in the deployment rather than the logic, each now explained where it was fixed —
`config/tmpfiles.d/cubesat.conf`, `systemd/cubesat@.service`, `config/mosquitto/`. The second day
on the Pi cost a misidentified fuel gauge (a MAX17040/41 answering `0xFFFF` from the registers a
MAX17048 would have, which had made `charge_rate` a decoded constant for weeks), an undetectable
class of BNO055 bit error that reaches the recorder (V14), and one real defect in the logic —
**`restart_service` latched `SAFE`**, because the restarted service's goodbye read to OBC as a lost
subsystem; fixed with `health.expect_restart`, which waives the one departure OBC itself asked for.
Expect more of that shape from the four profiles still waiting: the mock HAL cannot fail the way a
shared directory, a file lock, a gauge or an access point does.

The practical consequence: a passing test proves the logic, never the register map. Where a driver
constant came from a datasheet rather than from our bench notes, the driver says so at the constant
— **preserve those markers**, they are the difference between verified and assumed. `ROADMAP.md`
carries the checks only the bench can settle, and every one of them is something that would produce
plausible wrong data rather than an error.

`README.md` and `docs/concept.md` now describe what the code actually does. If you find a place
where they disagree with `src/`, that is a bug in one of them — say which, do not quietly pick a
side.

| Document | Authority over |
|---|---|
| `docs/concept.md` | *Why* the design is shaped this way — profiles, states, control plane, traps |
| `README.md` | *What* the system is — services, MQTT contract, payloads, schema, config |
| `docs/hardware-*.md` | The real hardware, per component. The authority for every register and pin |
| `ROADMAP.md` | What is **left**, and the bench checks the code is waiting on. Finished work is deleted from it rather than ticked off — closed decisions live in `docs/concept.md`, settled hardware findings in `docs/hardware-*.md` and at the constants they justify |
| `PLAN.md` | Working document (Russian) for the LoRa migration; all stages closed |
| `docs/architecture.md`, `docs/code_smells.md` | Pre-rewrite record only — historical |

## Project Overview

CubeSat Sim is an educational platform for a real, physical CubeSat model. Independent Python
services each model one satellite subsystem and communicate exclusively over MQTT. Two orthogonal
state machines drive it:

- **Platform profile** — chosen by a human, decides what the Raspberry Pi is allowed to be
  (`HOSTED`, `DEMO`, `EXPO`, `FLIGHT`, `DIAG`, `MAINTENANCE`). Governs Wi-Fi mode, which systemd
  units run at all, the dashboard, the CPU governor, and whether persistence and each downlink
  channel are permitted.
- **Mission state** — chosen by the satellite from its own telemetry
  (`BOOT → STANDBY → DEPLOY → NOMINAL`, descending through `LOW_POWER → SAFE → CRITICAL`).
  Governs sensor cadence, camera permission, logging cadence, radio duty cycle. There was a
  `SCIENCE` above `NOMINAL`, entered by ground command; it was **removed on 2026-09-02** because
  every cadence, the beacon, the camera and the recording rule were identical to `NOMINAL` — a
  command that changed a label and nothing else. Do not reintroduce a state without its own
  numbers: cadence and photo interval are configuration, and the only thing that changes them at
  runtime is the power-driven descent, which the satellite decides for itself.

The profile is the **envelope of what is permitted**; the state is the **activity level inside it**.
A state change never alters the profile — except `CRITICAL`, the only state allowed to power the
host down. Do not merge these into one flat machine; that decision is argued out in
`docs/concept.md`.

## Services (target design)

| Service | Unit | Runs as | Always on | Role | Status |
|---|---|---|---|---|---|
| broker | `mosquitto` | own user | ✅ | The message bus | exists |
| HOSTD | `cubesat-hostd` | **root** | ✅ | Host executor: units, Wi-Fi mode, governor, poweroff, profile state file (five actions: `apply_profile`, `set_governor`, `restart_service`, `poweroff`, `clear_resume`) | **ran on hardware** 2026-08-31 |
| OBC | `cubesat@obc` | `cubesat` | ✅ | Both state machines, command parsing, subsystem health | **ran on hardware** 2026-08-31 |
| EPS | `cubesat@eps` | `cubesat` | ✅ | Battery + mains telemetry — drives `LOW_POWER`/`SAFE`/`CRITICAL` | **ran on hardware** 2026-08-31 |
| ADCS | `cubesat@adcs` | `cubesat` | by profile | BNO055 orientation + TEL0157 position | **ran on hardware** 2026-09-01 |
| PAYLOAD | `cubesat@payload` | `cubesat` | by profile | SEN0501 science + camera | **ran on hardware** 2026-09-01 |
| DHS | `cubesat@dhs` | `cubesat` | by profile | The flight recorder: owns the SQLite database | **ran on hardware** 2026-09-01 |
| COMMS | `cubesat@comms` | `cubesat` | all but `MAINTENANCE` | The link only: LoRa mesh, uplink re-publish | **ran on hardware** 2026-08-31 |
| DASHBOARD | `cubesat-dashboard` | `cubesat` | by profile | Static UI + read-only REST. **No WebSocket** — browsers subscribe to mosquitto's own listener | **ran on hardware** 2026-09-01 |

Three structural decisions that are easy to accidentally undo:

1. **`HOSTD` and `OBC` are split along the privilege boundary.** `HOSTD` is the hands: root, no
   decision logic, an explicit unit allowlist. `OBC` is the head: all logic, no privileges. Never
   give flight software root, and never move profile logic into `HOSTD`.
2. **`HOSTD` may never stop `cubesat@obc` or `mosquitto`; `OBC` may never stop `cubesat-hostd`.**
   Enforce in code, not by convention — each would be sawing off the branch it sits on.
3. **`DHS` owns persistence, not `COMMS`.** In `FLIGHT` and `SAFE` the radio goes off while the
   GNSS track must keep recording, so the recorder cannot live inside the link service. `COMMS`
   persists nothing.

Whether a telemetry row may be written is decided by the **profile**; how often, by the **state**.
The pre-rewrite gate ("write only while the OBC state is `SCIENCE`") is deliberately gone, and so
is that state.

**Only `FLIGHT` and `DIAG` write to the card** (Q7, decided 2026-09-01). A demonstration is not a
mission: the satellite stands on a desk, there is no track, and the card is the component that wears
out. `DEMO` and `EXPO` still run `DHS` — it assembles the wide row and publishes it on
`cubesat/dhs/telemetry`, which is the only carrier of the host's own CPU/RAM/disk anywhere, and
`DASHBOARD` keeps a bounded ring of those rows in memory for its charts (`dashboard/live.py`).
`/api/telemetry` therefore means *the current session*: the open mission from the database while one
is being recorded, the ring otherwise — never "the last N rows of the table", which drew two
different days as one line. `DIAG` was redefined in the same move: it is `FLIGHT` rehearsed on the
desk (network and dashboard kept, same cadences and radio, into `diag.db`), which also retires the
`cadence_scale: 0.2` that ran ADCS at 10 Hz.

**A mission photographs itself.** No `start_timelapse`/`stop_timelapse` and no interval on the wire:
while a mission is open and the state permits the camera, a frame is taken every
`photos.mission_interval_sec` (300 s) and filed under the mission. With no mission open a photograph
goes to `/run/cubesat/photo` — a tmpfs — is published on the retained `payload_photo`, and is
deleted; nothing reaches the card. That retired `photos/unfiled/`, which retention was forbidden to
clean and which therefore only grew. Note the consequence: at 300 s against a 60 s
`idle_close_sec`, **every** mission frame is a cold capture — the worry behind bench check V11,
measured on 2026-09-01 and settled there: a cold frame and a warm one came out indistinguishable
(luma 94.2 against 94.6), and what a cold capture costs is latency, 0.97 s against 0.21 s.

**A mission with no label is named after when it started** (`2026-09-01 07:12`). Supplying one is
`--mission` on the CLI or `mission_label` on the wire; without it an unlabelled mission used to list
as its profile, which is the same word for every trip ever taken.

**Missions.** Each continuous run of an active profile is a mission: a row in the `missions` table
that every telemetry row references via `mission_id`. `DHS` opens one when the state reaches
`NOMINAL` with persistence permitted, and closes it on a profile change, a shutdown, or `CRITICAL`.
A power loss closes nothing, so **`DHS` must run orphan recovery at every startup**: any mission
with a null `ended_at` is closed at the timestamp of its own last telemetry row with
`end_reason = interrupted`. Each row also records `start_reason` (`command` or `resume`), so a trip
a reset split in two is legible as one afterwards: same label, first half `interrupted`, second
half `resume`. The telemetry table is named `telemetry` (renamed from `comms_log` —
`COMMS` no longer owns it).

**Attitude has a table of its own.** `telemetry` holds one wide row per DHS tick — 30 s apart in
`NOMINAL` — while ADCS publishes orientation at 2 Hz, so every sixtieth sample survives and a
timeline replay is a slide show. `attitude(mission_id, t, quat_*, gyro_*)` is the same track at the
rate it was measured, decimated by `dhs.attitude_min_interval_sec` and buffered in memory until the
tick that was going to open a transaction anyway. It costs **SD-card writes, not I2C bus time** —
DHS holds no hardware, so what ADCS puts on the bus costs the same whether it is recorded or
discarded. Do not reach for that number hoping to unload the bus; the lever for that is the ADCS
cadence.

**Erasing a mission is DHS' job, and it deliberately disagrees with retention.** `delete_mission`
on `cubesat/command` removes the four tables' rows *and* the `missions` row, then that mission's
photo directory; retention keeps the row and stamps `purged_at`. That is not an
inconsistency to tidy up — the horizon is the satellite deciding it can no longer afford a record,
a person pressing delete is saying the trip should not be listed, and a delete leaving a ghost row
would read as a button that does nothing. Three fences hold it: it is refused in `EXPO` (an open
access point with an audience on it, and commands are still unauthenticated — a profile-dependent
rule, so it cannot live in the broker's ACL), it is refused for the mission currently being
recorded, and it has **no compact spelling**, so it is in neither the radio vocabulary nor the
console's mirror of it and cannot be met by somebody exploring what the satellite understands. Since
2026-09-03 that fence is **absolute over the air**: JSON stopped being a command on the radio, so
having no compact spelling now means having no radio path at all, where before it meant a
deliberately awkward one. It stays fully reachable over MQTT — the dashboard's archive dialog, the
CLI, any broker client — which is the right place for it, because that is where whoever presses
delete can see the mission they are deleting. Do not give it a compact spelling, and do not add an
HTTP `DELETE` to the dashboard: `archive.py` opens the
file `mode=ro` precisely so there can only ever be one writer.

Do not conflate the three uses of the word: a *mission state* is `NOMINAL`/`SAFE`/… on the OBC
axis; a *mission* is a recorded session; the field profile is called `FLIGHT`, not `MISSION`,
precisely so those stay distinguishable.

## Recurring Rules

These came out of building the services and each one exists because the alternative was tried,
argued or nearly shipped. They are not style preferences.

**Withhold rather than fabricate.** When a value cannot be justified, publish null and publish the
raw observation beside it — never a plausible substitute. `yaw` is null until the magnetometer is
fully calibrated, because the BNO055 reports a *constant* below that, not a poor estimate.
`uv_index` is null until the SEN0501 board revision is known, because two revisions read one
register with formulas that disagree by a factor of forty. A track segment shorter than the
receiver's noise floor does not count toward `distance_m`. A position on a photo carries the age of
the fix it came from. In every case the reasoning is the same: a wrong number that looks right is
worse than a gap, because nothing downstream can tell it from a measurement.

**A heartbeat proves a process, never its hardware.** Every service here logs a silent device and
stays up — deliberately, so OBC reacts to missing telemetry rather than to a vanished process. So
bring-up evidence is a subsystem's *status* message, which exists only because a device was read.
Heartbeat-only evidence would pass a `DEPLOY` with a cable knocked loose, which is the case
`DEPLOY` exists for.

**A departure OBC asked for is not a fault, and it is bounded.** Two things make a subsystem's
goodbye legitimate: a profile switch (`profile_machine.settling`, 30 s) and a relayed
`restart_service` (`health.expect_restart`, one loss grace). Both existed because the alternative
was met on the hardware — a healthy satellite latched into `SAFE` mid-switch on 2026-08-28, and a
`restart comms` doing the same on 2026-09-01. Both are **windows**, not flags: a switch HOSTD never
completes, or a service that never comes back, gets the health monitor its say back on schedule.
Never widen either into "ignore goodbyes from this service" — an unannounced departure is exactly
what `SAFE` is for.

**On mains there is no power emergency.** All three power-driven descents are suppressed while
external power is present *and* the pack is not still going down. Without the first half, a
satellite brought home flat and plugged in powers itself off and the X728 never restores it,
because mains never left. Without the second, one failed charger disables the protection forever.

**And "going down" is the voltage, full stop** (2026-09-04). The second half used to be
`charge_rate`, which on this gauge is a slope fitted to a *model*: the MAX17040/41 senses no current,
and its state of charge was watched falling at 8–10 %/h for an hour while the satellite sat on mains
with the charge LEDs lit and the terminal voltage flat to the millivolt — 0 mV/h against −197 mV/h
once unplugged. So a plugged-in satellite read as being on battery, and `SAFE` and `CRITICAL`
(neither of which asks what state it is in) were hours from powering off a unit the X728 could not
restart.

The first fix required both slopes to agree, and it was right for a day. Then the percentage itself
became derived from the voltage (see the rule below), which made its slope a restatement of
`voltage_rate` rather than a second opinion — so "both agree" turned into a condition that cannot be
false. **A fence that cannot fail is worse than no fence**, because the code then claims two checks
where reality has one, and somebody later reasons from the claim. `on_mains` now asks one slope, the
measured one, under −30 mV/h. Do not add a second condition on `charge_rate` to make it look
symmetrical again, and do not reach for the percentage as a deciding quantity anywhere: it is the
only number in this system that is not measured.

**The pack's level is a voltage, and every percentage is decoration** (2026-09-04). `LOW_POWER`,
`SAFE`, `CRITICAL` and the recovery compare volts — 3.64, 3.58, 3.45, 3.75 in
`obc/power_policy.py` — because `VCELL` is a direct ADC reading at 1.25 mV per LSB and the state of
charge beside it is a reconstruction. `common/battery.py` maps volts to a percentage for the
dashboard, the beacon's `b` field, `cubesat status` and the `battery` column, and **that curve is
inferred from a generic 18650 discharge curve, not measured on this pack** (ROADMAP V15). Two
consequences to preserve: nothing in the satellite's behaviour may depend on the curve, so that
being wrong by five points costs a chart and no decision; and the gauge's own figure is still
published as `gauge_percent` and recorded in its own column, because the pair is what will confirm
or replace the curve, and because a part that has already been wrong in one known way is worth
watching for a second.

**A threshold in volts needs a median, not a sample** (2026-09-04). A modelled percentage changed
slowly by construction — the part's own filter did the one favour it ever did us. A terminal voltage
drops the moment a load appears: 50 mV was measured at a single unplug, the camera pipeline starting
is the same order, and `SAFE` to `CRITICAL` is 130 mV apart. So EPS publishes `voltage_median` over
`eps.level_window_sec` (120 s) and the policy reads that, while the raw `voltage` is published and
recorded beside it — a chart must still be able to show the dip the policy is not allowed to act on.
A median rather than a mean, because a mean carries the transient into the answer. Do not stretch
that window to smooth the charts, and do not feed the policy the raw sample.

**Quiet is not deaf, and it is not mute either.** `beacon_enabled` rations the *schedule* only;
listening is the profile's call, and so is answering. A profile sets where transmission starts:
`downlink: {lora: true, beacon: false}` in `DEMO` and `EXPO` means the satellite listens and answers
but volunteers nothing until asked (`beacon on` over the radio, from `cubesat beacon on`, or from the
dashboard), because there it is a metre from its operator and the mesh channel is shared. Entering a
profile resets that flag to the profile's own default — otherwise "quiet in DEMO" would hold only
until the first time anybody turned the beacon on.

**An answer is not a beacon** (2026-09-03). A reply to an accepted command is gated on
`lora_listening` — the profile — exactly like the inbox and like the going-down beacon, so **every
profile that runs COMMS answers the commands it accepts**, `beacon off` or not. The three cases that
settled it were met inside five minutes on 2026-09-02 in `DEMO`: `!sys` answered from the caches and
the answer dropped, `!photo` taken and never mentioned, and `!beacon off` silenced by the flag it had
just set — which makes "the transmitter is off" and "the command never arrived" the same silence to
the one person who cannot tell them apart. The satellite was answering typos (a `!` line that fails
to parse has always been answered) and swallowing successes. The flag was called `lora_enabled` until
that change and the rename is part of it: a flag that no longer decides whether LoRa transmits at all
must not be named for it. The old spelling is accepted on the way in and mirrored on `comms_status`,
deprecated, because the dashboard deployed on 2026-09-02 sends and reads it.
`SAFE` wakes every 60 s to listen and beacons every 600 s. An earlier version silenced COMMS
entirely in `SAFE` — and `SAFE` is reachable from `FLIGHT`, where the radio is the only way in, so
the state that most needs a `recover` was the one state deaf to it. The same rule now covers
profiles: COMMS runs everywhere but `MAINTENANCE` (reflashing the Heltec needs `/dev/serial0`
free), so even `HOSTED` — every boot's landing profile, a field reboot included — hears an
uplinked `set_profile`; `STANDBY` has no beacon row, so it listens without transmitting. The
cloud API is gone: none was ever deployed, and the ground segment is being rebuilt as an interface
over the satellite's own dashboard rather than a service the satellite reports into. `downlink`
names one channel.

**A mission id names a directory only with its database beside it** (2026-09-03). `comms.db` and
`diag.db` number their missions independently, both from 1, so `DIAG` mission 3 and a `FLIGHT` trip
3 are two missions with one id — and one `photos/<mission_id>/` for both meant deleting the bench run
from the archive dialog took the trip's photographs with it. The second database files under a
**sibling** root, `photos-diag/<mission_id>/`, chosen over `photos/<db>/<mission_id>/` because the
leaf name stays a plain run of digits, which is the allowlist retention fences the most destructive
code here with, and because nothing already on the card has to move. `common/config.py` →
`photos_root_for` is the one mapping; PAYLOAD, DHS' delete and horizon, and the dashboard's two photo
routes all go through it. PAYLOAD takes the root from the same `dhs_status` as the id and never
separately — a mission whose database is missing reads as no mission, the same withholding a
non-integer id gets. Do not reintroduce a single root, and do not give the sibling a suffix on the
id instead: that would move the fence.

**A safety fence is an allowlist, and it is also an inventory.** HOSTD's permitted set names
everything root can `systemctl`, including levers no profile can reach, because a property that
takes two files to verify stops being verified. Photo directories are matched by a positive rule (a
run of digits, which is what a mission id is) rather than by forbidding `unfiled`: an allowlist
holds against inputs nobody thought of.

**Republish retained status on every connect.** A broker restart discards retained messages, and a
service that publishes only on change would then stay silent — so a healthy satellite would fail its
own bring-up because the *broker* bounced. `Service.on_connected()` is the hook. HOSTD is the
exception that proves it: it publishes nothing before its first profile is applied, because an empty
`host_status` reads to OBC as a fault.

**Mark verified apart from inferred.** Where a driver constant came from a datasheet or a vendor
library rather than from `docs/hardware-*.md`, the driver says so at the constant and names the
bench check that would confirm it. Do not tidy those markers away, and do not re-derive a constant
from a guess about how it was originally computed — that changes the number while looking like
cleanup.

**A test must not read a production config value.** It stops testing behaviour the moment that
value legitimately changes — and one did, when `comms: SAFE: 0` turned out to be a defect. Patch the
table the test needs; compute boundaries from the constant rather than repeating a literal.

## Hardware Facts That Constrain The Code

These are validated and non-negotiable:

- **I2C bus 1 is clamped to 10 kHz** (`dtparam=i2c_arm_baudrate=10000`). This is a correctness
  requirement, not tuning: at 100 kHz the BNO055 stretches the clock in a way the BCM2835
  mishandles and ~2/3 of byte reads come back with bit 7 forced to 1. Never raise it.
- **All I2C access goes through an advisory lock** (`flock` on `/run/cubesat/i2c.lock`) inside
  `hal/i2c.py`. Four processes share one 10 kHz bus where a single read costs tens of
  milliseconds; unsynchronised access will collide mid-transaction.
- **Addresses:** `0x20` TEL0157 GNSS (ADCS), `0x22` SEN0501 environment (PAYLOAD), `0x28` BNO055
  orientation (ADCS), `0x36` **MAX17040/41** fuel gauge (EPS — identified 2026-09-01; the driver
  file is still named `max17048.py` and reads only the registers both families share), `0x68`
  DS1307 RTC (**kernel-owned, shows as `UU`, never touch from user space**), `0x76` BMP280
  (undecided — duplicates SEN0501 pressure).
- **The BNO055 fuses on-chip.** It outputs quaternion/Euler directly, so there is no AHRS filter
  in this repo any more. Publish `calib_status` alongside — an uncalibrated magnetometer produces
  confident nonsense.
- **LoRa is a Heltec WiFi LoRa 32 V4 running stock Meshtastic**, on `/dev/serial0` at 115200 via
  the `meshtastic` library. Meshtastic does framing, CRC, retries and encryption — do not
  hand-roll any of that. `115200` is not optional: the Meshtastic Python library opens the port
  hard-coded at that rate.
- **A Meshtastic message carries at most 240 bytes**, while a full telemetry packet is several
  hundred. A compact beacon field set or chunking is a prerequisite for the radio-only profiles.
- **The DS1307 RTC is enabled**, so offline timestamps are trustworthy without NTP.

## MQTT Contract

All topic strings live in `src/cubesat/common/topics.py` as `TOPICS` — always reference
`TOPICS["key"]`, never a literal string. Profile and state names are enums in
`src/cubesat/common/states.py`, not bare strings.

| Key | Topic | Publisher | Retained |
|---|---|---|---|
| `command` | `cubesat/command` | any ground client, or COMMS relaying an uplink | no |
| `host_command` | `cubesat/host/command` | OBC | no |
| `host_status` | `cubesat/host/status` | HOSTD | **yes** |
| `obc_status` | `cubesat/obc/status` | OBC | **yes** |
| `eps_status` | `cubesat/eps/status` | EPS | **yes** |
| `adcs_status` | `cubesat/adcs/status` | ADCS | no |
| `payload_status` | `cubesat/payload/status` | PAYLOAD | **yes** |
| `payload_data` | `cubesat/payload/data` | PAYLOAD | no |
| `payload_photo` | `cubesat/payload/photo` | PAYLOAD | **yes** |
| `dhs_status` | `cubesat/dhs/status` | DHS | **yes** |
| `dhs_telemetry` | `cubesat/dhs/telemetry` | DHS | **yes** |
| `comms_status` | `cubesat/comms/status` | COMMS | **yes** |
| `comms_data` | `cubesat/comms/data` | COMMS | on-demand only |
| `comms_radio` | `cubesat/comms/radio` | COMMS | no |
| `heartbeat` | `cubesat/heartbeat` | **every service** | no |

`cubesat/obc/status` carries `timestamp`, `status` (the mission state — not `state`), `profile`,
`cadence_scale`, `persistence`, `mission_label` and `subsystems`. The middle three are there so a
subsystem needs no second channel: cadence for everyone, persistence and label for DHS to open a
mission from this one retained message. `subsystems` (`watched`/`lost`) is OBC's health verdict for
the ground segment — it is what lets a dashboard tell "off because the profile never started it"
from "expected and silent" without guessing.

**Browsers talk to the broker directly.** `config/mosquitto/` gives it two listeners — TCP on
localhost for the satellite's own services, WebSockets on 9001 for browsers — with an ACL on the
second alone (`per_listener_settings true`). There is deliberately no MQTT→WebSocket bridge to
write or keep in step with this table. The browser rule is `read cubesat/#`, `write cubesat/command`
and nothing else; the two denials that matter are `cubesat/host/command`, which is root's inbox, and
`cubesat/+/status`, because a forged `eps_status` at 4 % would walk the satellite into `CRITICAL` —
the one state permitted to power the host off. Do not widen that ACL without reading `acl.conf`,
and do not put the profile-dependent part of the fence there: it cannot see which profile is active.

Liveness runs on one shared topic, published by the `Service` base class on a fixed interval
independent of the poll cadence, plus an MQTT **last will** so an ungraceful death is announced
immediately. Do not move heartbeats onto per-service topics: OBC subscribes once.

`cubesat/host/status` reports `profile` (achieved) separately from `profile_requested` (intended).
That distinction is load-bearing: a profile can apply partially, and collapsing the two fields
turns a debuggable failure into a mystery. It also carries `previous` — `last-profile` as it read
*before* this boot overwrote it — and `boot`, and both are evidence rather than instruction: HOSTD
publishes them and still has no opinion about what they mean.

Ground commands share the one `cubesat/command` topic; the `command` field selects the handler.
**The vocabulary lives in `README.md` → The command vocabulary, and only there** — one table, every
command in all three spellings (radio, shell, JSON). Do not restate it elsewhere: a second copy is
a copy that disagrees after the first edit.

**Over the radio the vocabulary is the compact spelling and nothing else** (narrowed 2026-09-03).
`COMMS` canonicalises a compact line into JSON and publishes it onto `cubesat/command`, so nothing
downstream knows or cares which *link* a command arrived on — but a command with no compact spelling
now has no radio path at all. Hand-composed JSON over the air used to be relayed verbatim and is
not any more: one parser for the air instead of two, and nothing left that can disagree with
`compact.py` about what a command is.

**The `FLIGHT` recovery path is intact, and it is why this is written down.** `profile hosted`,
`safe`, `recover` and `restart <svc>` are all compact verbs, so a satellite with Wi-Fi down and no
SSH is still reachable from a phone — preserve *that*, not the JSON. Two things the radio can no
longer carry: `set_profile`'s `ttl_minutes` and `mission_label` (the profile's own TTL and a mission
named after its start time are the defaults, which is what the field scenario already relies on),
and `delete_mission`, which has no compact spelling on purpose.

**On the radio side that is one mesh channel and no other.** An uplink counts only if it arrived on
`config.LORA_CHANNEL_INDEX` — the private `CubeSat` channel with its own key. Everything else the
node hears, the public primary channel and direct messages included, is dropped in
`comms/service.py` → `_collect_uplink` **before** the `cubesat/comms/radio` publish, so a stranger's
chat reaches neither the command parser, nor the dashboard's live Radio Link Log, nor `radio_log` on
the card and the mission export it travels inside. One log line — sender, channel, SNR, byte count,
never the text — and **nothing is transmitted back, not even an `err=` for a `!` line**: answering
spends airtime on a shared band and teaches a mesh of several hundred nodes that this node talks
back. The filter is on *acting*, never on hearing: `lora_listening` stays the profile's call and the
inbox is still polled in full. The credential is deliberately the channel's key and not the node id
— a key is portable, so a flat operator node is recovered by loading the channel URL onto another
one, whereas a node allowlist would be a locked door with the key on the far side. It is not even
available as a shortcut: relayed foreign chat arrives with `sender: null`, which is precisely the
traffic that has to be refused.

See `README.md` for full payload schemas and the SQLite table layout.

## Configuration

| Where | What | Committed |
|---|---|---|
| `config/config.yaml` | Runtime defaults: broker, intervals, per-state cadence, camera resolution | ✅ |
| `config/profiles.yaml` | Profile definitions and the external-unit registry | ✅ |
| `config/mosquitto/` | The broker's two listeners and the browser ACL, installed by `scripts/install.sh` | ✅ |
| `.env` / environment | Per-deployment values and **all secrets** | ❌ never |

Environment variables override YAML. Secrets are environment-only — never write one into
`config.yaml`, and there is deliberately no YAML key for one.

Profiles are **data, not code**: adding a profile must not require editing `OBC`. `HOSTD` acts only
on an explicit allowlist — the `cubesat@*` mission instances, the dashboard, `avahi-daemon`, and
what `external_units` names — so a typo in a profile cannot take down `sshd`. That allowlist is also
the single readable inventory of everything root can `systemctl`: **anything HOSTD touches belongs
in it**, including levers no profile can name, because a safety property that takes two files to
verify stops being verified. The units a profile may start and stop are a strict subset of it.

**The profile is deliberately NOT persisted.** Every boot applies `default_profile` (`HOSTED`).
Do not "fix" this by restoring the last profile at startup — it is a decision, not an oversight.
The case that settles it: a satellite that hits `CRITICAL` on a trip, shuts down, and is plugged in
at a desk hours later must not come back up with Wi-Fi off and no SSH. It also makes a power cycle
the simplest recovery path from any profile.

**The one exception is `FLIGHT` resuming itself, and it turns on a measurement rather than on the
file** (2026-09-03, ROADMAP W11 → `obc/resume.py`). A reset mid-trip otherwise ends the recording
silently, in the profile with no dashboard, no Wi-Fi and nobody watching — the case that settled it
is a satellite under a balloon, reset by the jolt of a parachute. So the satellite asks "is there
mains?", not "what was I told an hour ago": absent mains at boot it is demonstrably not on a desk,
and the dangerous case above stays safe by construction, because a satellite brought home flat and
plugged in *has* mains. `/var/lib/cubesat/last-profile` is now JSON — profile, `written_at`,
absolute `ttl_expires_at`, `mission_label`, `resume_count` — published by HOSTD on `host_status` as
`previous`, beside `boot` (true while the profile applied at start is still the active one). It
answers *what*, never *whether*: do not let it decide anything on its own, and do not extend the
resume to a second profile — `EXPO` on battery with no operator is pointless and `DIAG` lives on a
desk. Four fences hold it: no mains, an unexpired TTL carried across the reset, three consecutive
short sessions max (cleared by `clear_resume` once a resumed run lives `resume.settle_sec`), and no
`eps_status` inside `resume.evidence_timeout_sec` means no resume at all — a missing measurement is
not a measurement of no mains. Every outcome is said out loud: `obc_status.boot`, one radio line
(`boot=FLIGHT rs=1`, or `rs=0 why=…`) gated on the profile like `_going_down_beacon`, and
`missions.start_reason`.

## Testing

The suite runs on any machine — never real Pi hardware, never a real broker. Every driver sits
behind `src/cubesat/hal/`, and `CUBESAT_MOCK_HARDWARE=1` selects the mocks. `tests/` mirrors
`src/cubesat/`: `tests/unit/<service>/` per module, `tests/integration/` for several services
against an embedded broker, `tests/fakes/` for fake peripherals.

```bash
python3 -m venv venv && source venv/bin/activate
pip install -e ".[test]"
pytest --cov --cov-report=term-missing
```

Dependencies and tool config live in `pyproject.toml`; the test extra adds `pytest`, `pytest-cov`
only. Coverage is enforced at 95 % (`fail_under`). GitHub Actions runs the suite on
every push to `main` and every PR, on Python 3.10–3.12.

`tests/conftest.py` sets the environment **before** anything from `cubesat` is imported, because
`cubesat.common.config` resolves its paths at import time: a temporary data, run and log directory,
the repository's own `config/`, and `CUBESAT_MOCK_HARDWARE=1`. The pre-rewrite trick of replacing
`RPi.GPIO`, `lgpio`, `smbus2` and `picamera2` in `sys.modules` is gone — that is what the HAL
removed the need for, and it should not come back.

The HAL was the first phase of the rewrite precisely because `HOSTD`, both state machines and the
cadence logic cannot be tested at all without mocked sensors.

## Layout and Running

The package is `cubesat` under a real `src` layout, installed editable. There is **no**
`PYTHONPATH=.` and no `src.*` imports:

```bash
pip install -e ".[test]"
python -m cubesat.obc            # each service's __main__.py is its entry point
```

Structural rules that are easy to erode, all with a reason:

- **`__init__.py` files stay empty.** No re-exports. `hostd` runs as root and must not pull `paho`,
  `yaml`, `smbus2` and the HAL into that process through a chain of package initialisers.
- **`__main__.py` is thin** — argument parsing, logging setup, construct, run, handle SIGTERM. The
  logic lives in `service.py` so it is testable without spawning a process.
- **Every service inherits `common.service.Service`** for broker connect, retained subscriptions,
  heartbeat, cadence and shutdown. Do not hand-roll that per service.
- **Drivers live in `hal/`, never in a subsystem directory.** A driver is hardware, not subsystem
  logic; the undecided BMP280 may move between ADCS and PAYLOAD, and that must cost one line.
- **HAL interfaces are `typing.Protocol`.** Drivers and fakes inherit nothing; a test fake is any
  object with the right methods. mypy catches mismatches.
- **Do not add helper files for symmetry.** `eps/` and `adcs/` are two files each because "read a
  sensor and publish it" does not decompose. Add a file when logic appears, not before.

Runtime data lives outside the checkout, created by systemd, never by `mkdir` in code:

| Path | Contents | Created by |
|---|---|---|
| `/var/lib/cubesat/` | `comms.db`, `diag.db`, `photos/<mission_id>/`, `photos-diag/<mission_id>/`, `last-profile`, dashboard build | `config/tmpfiles.d/cubesat.conf` |
| `/run/cubesat/` | `i2c.lock`, `hostd.sock`, `photo/` (a photograph with no mission, in RAM) | `config/tmpfiles.d/cubesat.conf` |
| `/var/log/cubesat/` | `<service>.log` | `config/tmpfiles.d/cubesat.conf` |

**`systemd-tmpfiles` creates these, not `StateDirectory`/`RuntimeDirectory`/`LogsDirectory`, and
that is deliberate.** Those directives re-apply the *starting unit's own* `User=` to the whole tree
on every start, and two users share these three paths: `cubesat-hostd` is root, everything else is
`cubesat`. So ownership followed whichever unit restarted last — a HOSTD restart handed all three
directories and their contents to `root:root`, after which the unprivileged services could no
longer write. That is not a logging inconvenience: DHS stops recording, PAYLOAD cannot create a
mission's photo directory, and `i2c.lock` becomes unopenable, at which point `hal/i2c.py` falls
back to a process-local lock and four processes stop serialising the bus — silently (found on the
first hardware run, 2026-08-31). The units name the paths in `ReadWritePaths=` instead, which is
what re-opens them under `ProtectSystem=strict`. Do not restore the directives.

**Never open `comms.db` or `diag.db` as anybody but `cubesat`, read-only included** (found
2026-09-03). Both run in WAL mode, and SQLite creates `-shm`/`-wal` beside the file even for a
reader — so a `sqlite3 'file:…?mode=ro'` run from an operator's shell leaves those two files owned
by that operator, and DHS, which is `cubesat`, then fails its next write with `attempt to write a
readonly database` while the database file itself still looks perfectly writable. It cost the first
`DIAG` run its recording: migrations 4→7 aborted and the service logged `nothing will be recorded`
and stayed up, exactly as it should. Diagnose through `sudo -u cubesat sqlite3 …`, or on a copy.
Fixing an instance of it is `chown cubesat:cubesat` on the two sidecar files — never delete a `-wal`
that is not zero-length, it holds committed transactions.

Set `CUBESAT_DATA_DIR=./data` for development. Units are a systemd template — `cubesat@obc`,
`cubesat@adcs`, `cubesat@dhs` and so on — with `cubesat-hostd` and `cubesat-dashboard` separate,
having different privileges and dependencies.

Individual units are not started by hand — **a profile is the unit of operation**, applied via the
`cubesat` CLI: `cubesat profile flight --ttl 8h --mission "walk to work"`, plus `cubesat status`,
`cubesat mission list` and `cubesat beacon on|off`. It is a thin MQTT client publishing the same ground
commands the dashboard and a LoRa uplink publish, and it holds no state of its own — `mission list`
is the one command that touches the disk instead of the broker, read-only, because the dashboard
does not run in `FLIGHT` and mosquitto may be what fell over. Only the always-on tier is enabled at
install time; `HOSTD` starts and stops everything else as a profile is applied.

Logs: rotating files at `/var/log/cubesat/<service>.log`, plus `journalctl -u cubesat@<svc> -f`.
When a profile did not do what you expected, read `cubesat-hostd` first — it logs every action it
took and every one it refused.

## Repo Conventions

- `hardware/` holds physical build assets: `hardware/models/` for 3D/CAD files, `hardware/photos/`
  for photos embedded in `README.md`. Distinct from `data/photos/`, which is camera output at
  runtime.
- Per-component hardware docs live in `docs/hardware-*.md`, one per row of the README's Hardware
  table, and each is linked from it. When bench-validating hardware, that file is where the
  findings go — including the failures and the gotchas, which are the most valuable part.
- systemd units expect the project at `/opt/cubesat-sim` with a virtualenv at
  `/opt/cubesat-sim/venv`, owned `<operator>:cubesat` so the operator can `git pull` and the
  service account can read it. It is deliberately **not** in anyone's home directory: the services
  run as the `cubesat` system account under `ProtectHome=yes`, and a checkout under `/home/<user>`
  would have meant opening that home directory up to them.
- `README.md` and `docs/*.md` are in English. `PLAN.md` and `docs/troubleshooting-*.md` are working
  documents in Russian. Match the file you are editing.
- The phased plan for the rewrite is the table at the end of `docs/concept.md` (P0–P8). `ROADMAP.md`
  tracks it.

## Related Projects

[cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) — the ground segment. It
used to be PHP/CodeIgniter 4 + React receiving packets POSTed by `COMMS`; **that HTTP downlink is
gone from this repo** — `COMMS` names one channel, the radio, and posts to nothing. Its React
client is being reworked into **one interface over several data sources**: the satellite's
own `DASHBOARD` service, a mission exported to a static file for a public demo with no backend at
all, and later a USB Meshtastic receiver. The PHP and MySQL half is removed. The satellite carries
no UI code of its own — the build arrives as an artifact. The agreed boundary between the two
repositories is `cubesat-groundstation/docs/dashboard-architecture.md`.
