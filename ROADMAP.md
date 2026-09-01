# CubeSat Sim — Roadmap

Feature requests, bug fixes and improvement tasks. The top section is the live plan; everything
below it is historical record of the pre-rewrite codebase.

**Where the design lives:** [`docs/concept.md`](docs/concept.md) is the operating concept (profiles,
mission states, control plane) and owns the phase plan. [`README.md`](README.md) is the reference
for the target system. This file tracks the work.

---

## Status Legend

| Symbol | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress |
| `[x]` | Done |

---

## 🚀 The Rewrite (Current Work)

The hardware is finished and validated; the software is being rewritten against the operating
concept. Phases are ordered so each one is independently useful and testable. Full scope and
rationale per phase: [`docs/concept.md` → Implementation plan](docs/concept.md#implementation-plan).

| Phase | Scope | Delivers | Status |
|---|---|---|---|
| **P0** ✅ | LoRa on Meshtastic: `src/comms/lora.py` → `mesh.py` over the `meshtastic` library, compact beacon field set or chunking, config keys, tests. This is stage 6 of [`PLAN.md`](PLAN.md) | A radio link the radio-only profiles can be built on | `[x]` |
| **P1** ✅ | **Skeleton.** `pyproject.toml` and the `src/cubesat/` package layout (no more `src.*` imports or `PYTHONPATH=.`); `common/service.py` base class, `states.py` enums, `topics.py`, `cadence.py`; `src/cubesat/hal/` with `typing.Protocol` interfaces, real drivers for the four Gravity modules, mocks behind `CUBESAT_MOCK_HARDWARE`, and the shared I2C advisory lock; runtime data moved to `/var/lib/cubesat` via systemd `StateDirectory`; `tests/` mirroring the package | The whole stack runnable and testable on a laptop — the prerequisite for every phase below | `[x]` |
| **P2** `[~]` | `config/profiles.yaml`; `cubesat-hostd` (root, unit allowlist, `systemctl`, state file); `set_profile` in OBC; `cubesat/host/*` topics; `cubesat` CLI; profiles `HOSTED`, `MAINTENANCE`, `DEMO` (no AP yet) | Switching between "desk" and "demo" without touching systemd by hand | `[ ]` |
| **P3** `[~]` | `cubesat-dhs` split out of COMMS: `comms_log` → `telemetry` with position, `profile` and `mission_id` columns; the `missions` table, its lifecycle and orphan recovery at startup; writes gated on profile; retention. COMMS loses persistence | A recorder that keeps working when the radio is off, and history divided into missions | `[ ]` |
| **P4** ✅ | `STANDBY` and `CRITICAL` states; real `DEPLOY` self-test; every service derives its cadence from `obc/status`; `LOW_POWER` knobs; recovery hysteresis; graceful `poweroff` | The mission state machine finally does something measurable | `[x]` |
| **P5** ✅ | AP mode in `hostd` (NetworkManager + mDNS); the browser's live channel — mosquitto's own WebSocket listener plus an ACL; the groundstation client reworked into one UI over several data sources; `cubesat-dashboard` (static files + read-only REST); profile `EXPO`. **All written, none of it run on the Pi** — see V8/V9 | A satellite that can be shown to a room with no internet | `[x]` |
| **P6** | Power saving; profile TTL; mains-as-signal recovery; GNSS track verified end to end; profile `FLIGHT` | The autonomous logging profile | `[ ]` |
| ~~**P7**~~ | ~~Profile `DIAG`: I2C sweep, full-rate polling, self-test report, separate persistence~~ **Retired 2026-09-01.** The sweep and the self-test report are what `DEPLOY` already does on every ascent, in every profile; the full-rate polling was `cadence_scale: 0.2` — ADCS at 10 Hz on a bus clamped to 10 kHz — and was removed rather than built on. `DIAG` keeps its separate database and is now `FLIGHT` rehearsed on the desk | — | `[x]` |
| **P8** | Docs and tests brought in line; test coverage for `hostd` and both state machines | The repo describing what it actually does | `[ ]` |

### Next: DASHBOARD

The last service, and the only one that is two projects at once. What is decided and what is not:

**Decided already, in `README.md` → Services → DASHBOARD.** The satellite carries no UI code. This
service is a transport — `/ws` pushing MQTT status through to browsers, `/api/history` reading
`comms.db` read-only, `/api/command` publishing onto `cubesat/command` like any other ground
client, and the static React build served from `CUBESAT_DASHBOARD_ROOT`. The interface itself lives
in [cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) and is deployed onto the
Pi as a built artifact, so one dashboard codebase serves both the cloud and the bench and no PHP or
MySQL runs on a Pi on battery.

**Agreed 2026-08-25: the boundary between the two repositories.** The full record, with the four
use cases it has to serve, is
[`cubesat-groundstation/docs/dashboard-architecture.md`](https://github.com/miksrv/cubesat-groundstation/blob/main/docs/dashboard-architecture.md).
What it settles on this side:

- **The service stays here**, as `cubesat.dashboard`. It needs `TOPICS`, the state enums, the table
  layout owned by `dhs/schema.py`, `common.service.Service` and `common/config.py`; a copy of any of
  those in another repository is a copy that drifts on the first edit. The slot, the unit file and
  HOSTD's `DASHBOARD_UNIT` already exist.
- **Not Node.** A second runtime and package manager on a Pi running seven Python services on
  battery, to re-implement broker plumbing that `Service` already provides and tests already cover.
- **The UI arrives as `client/dist`**, never as a checkout — building React on a Pi 4 costs minutes
  and a `node_modules` tree on the card to produce a few megabytes of static files.
- **HOSTD starts it, never OBC.** `dashboard: true` in a profile is the whole mechanism, and it
  works today.
- **The contract is written before the service.** The other repository defines the data-source
  interface first, because the public demo has no backend at all: a contract shaped by whatever was
  convenient to hand out of SQLite and MQTT would leave the static build imitating the quirks of a
  backend it does not have.

**Settled 2026-08-25.** The order of work is
[`cubesat-groundstation/docs/dashboard-plan.md`](https://github.com/miksrv/cubesat-groundstation/blob/main/docs/dashboard-plan.md).

| # | Question | Answer |
|---|---|---|
| D1 | Which HTTP/WebSocket library? | **Neither — no WebSocket is written.** Shipped: `config/mosquitto/` plus `install.sh`, with V8 and V9 below as the bench checks. Live data reaches the browser over a mosquitto WebSocket listener; the service serves static files and read-only JSON, which `http.server` covers. Nothing from `aiohttp`/`fastapi` arrives on a Pi running on battery |
| D2 | Does it read `comms.db` while DHS writes it? | Yes, and that is a requirement rather than a question: read-only, its own connection, WAL, never a long transaction and never a write lock against the recorder |
| D3 | What does it show when a mission is `purged_at`? | The mission row, and an explicit "detail removed by the retention policy" where the charts would be. An empty chart would be a lie. Attitude rows purge with the mission |
| D4 | Does `/api/command` need authentication on an open AP? | **Deferred deliberately: one command vocabulary, whatever the channel.** The dashboard is a ground client publishing to `cubesat/command` exactly as the radio does. The ground vocabulary has no `poweroff` by design, so the worst a visitor in `EXPO` can do is `set_profile HOSTED` — which takes the AP down and disconnects them too. A per-profile allowlist stays available if that turns out to be a nuisance in a real room |
| D5 | Live free space | DHS's system-health columns and the retained `dhs_status`; not by polling `payload_status`, which is not republished on a cadence |

Two decisions that belong to the satellite rather than to the dashboard came out of the same
discussion:

- **An `attitude` table.** DHS writes one telemetry row per tick — 30 s in `NOMINAL` — while ADCS
  publishes at 2 Hz, so the archive holds every sixtieth attitude sample and a timeline replay would
  be a slide show. A narrow `attitude(mission_id, t, quat, gyro)` table, filled from a bounded buffer
  flushed on the existing tick, fixes it for about 5 MB a day. It is decimated by
  `dhs.attitude_min_interval_sec` (default 1.0) — which costs **SD-card writes, not I2C bus time**:
  DHS holds no hardware, and the bus load is ADCS's cadence whether the samples are recorded or
  discarded.
- **The cloud leg is removed**, closing Q6 below: no ground-station API in any shape. `comms/api.py`,
  the cloud half of `comms/service.py` and the `downlink.api` flag itself all go.

### Dashboard progress

Tracked in full in
[`cubesat-groundstation/docs/dashboard-plan.md`](https://github.com/miksrv/cubesat-groundstation/blob/main/docs/dashboard-plan.md);
repeated here because half the stages land in this repository.

| Stage | What | Repository | Status |
|---|---|---|---|
| 0 | Remove the cloud leg from COMMS — `comms/api.py`, `_cloud_cycle`, the `downlink.api` flag | here | `[x]` |
| 1 | The `attitude` table: schema, bounded buffer, batch flush, decimation, retention | here | `[x]` |
| 2 | mosquitto's WebSocket listener and the browser ACL (`config/mosquitto/`, `install.sh`) | here | `[x]` |
| 3 | The contract and the data-source layer | groundstation | `[x]` |
| 4 | Delete PHP and MySQL | groundstation | `[x]` |
| 5 | `cubesat.dashboard`: static files from `CUBESAT_DASHBOARD_ROOT`, read-only REST | here | `[x]` |
| 6 | The mission timeline: play, scrub, one clock for map and attitude | groundstation | `[ ]` |
| 7 | Export a real mission; replace the placeholder recording | both | `[ ]` |
| 8 | The USB Meshtastic receiver | groundstation | `[ ]` |

**Stage 5 landed 2026-08-25.** `cubesat.dashboard` is three modules — `archive.py` (the recorder's
database, `mode=ro` on its own connection), `http.py` (routing, the SPA fallback, the photo
allowlist) and `service.py` (lifecycle, and following DHS onto `diag.db`). No WebSocket code at all,
per D1: `http.server` covers static files and five read-only endpoints, and nothing from `aiohttp`
or `fastapi` arrives on a Pi on battery.

Deployment of the interface is `scripts/deploy-dashboard.sh` — built on a laptop with
`PUBLIC_SOURCE=live`, rsynced into `/var/lib/cubesat/dashboard`, which the unit's `StateDirectory`
creates. The service reads from disk per request, so a redeploy needs no restart.

**Everything that remains is bench work or the other repository.** The next thing this repository
can usefully do is be installed on the Pi — see the verification table below, and V8/V9 in
particular, which are what confirm the decision this whole service is shaped around.

### Widgets pass and the radio session log — 2026-08-29

A ground-segment session that turned out to be half satellite work, because twice the dashboard
needed data the bus did not carry. **Written and tested (100 % coverage, ruff/mypy clean), not yet
run on the Pi and not yet deployed** — the client changes land with the next
`deploy-dashboard.sh`, the service changes with the next install/restart, and `comms.db` picks up
schema migration 4 automatically on first open.

- **`obc_status` now carries `subsystems: {watched, lost}`** — OBC's own health verdict, published
  so the ground can tell "off because the profile never started it" from "expected and silent".
  `lost` is published empty while a profile switch settles, matching `_check_health`'s own
  suppression. The dashboard's Subsystem Status widget renders it as OK/WARN/**FAIL**/**OFF**
  (FAIL only on OBC's verdict, OFF for profile-stopped services), and the MQTT Bus Monitor greys
  out and stops animating OFF nodes.
- **`cubesat/comms/radio`** (not retained): one event per radio transaction — every message heard
  (text verbatim, bytes, sender, SNR/RSSI/hops) and every transmission attempted (`kind` =
  beacon/ack/down, `sent` — a failed transmit is published, not suppressed). COMMS observes,
  publishes, and still persists nothing.
- **DHS records the traffic into `radio_log`** (schema migration 4): mission-scoped like attitude,
  buffered and batch-flushed like attitude, but **never decimated** — radio traffic is discrete
  events, and the one packet dropped would be the uplink somebody is looking for. Same retention
  horizon; `dhs_status` gains `radio: {written, buffered}`. Traffic outside a mission is
  deliberately not recorded.
- **`RadioMessage` gained `rssi` and `hops`** in the HAL. `hops = hopStart − hopLimit` is inferred
  from the library, not bench-verified — see V10 below.
- On the other side: the **Radio Link Log** widget (its own grid row), a `subscribeRadio` channel
  and a `radio` capability on the data-source interface, and the demo recording format extended
  with a `radio` array — the placeholder generator now writes plausible traffic (beacon per minute,
  two queries with their 10 s acks, one failed transmit) and `ReplaySource` replays it in step with
  the playhead. **Stage 7's export must include the mission's `radio_log` rows**; the client-side
  loader already accepts them.

### Agreed: the radio reply contract

Designed and agreed 2026-08-24; the full contract, with the reasoning, is
[`docs/concept.md` → The radio command contract](docs/concept.md#the-radio-command-contract).
Decisions taken there: one universal ack rule (an out-of-schedule beacon carrying `re=`),
replies broadcast into the shared channel, a `!` compact syntax canonicalised once at entry,
an event-transmission budget of one per 10 s with `down=1` exempt, replies obey `lora_enabled`,
no `request_id` echo over the radio, and deliberately no `poweroff` command.

| # | Work | Status |
|---|---|---|
| R1 | Compact `!` syntax in COMMS: translate to canonical JSON *before* the relay; unknown `!` lines answered `re=? ok=0 err=unknown` instead of silence. **Done 2026-08-28** (`comms/compact.py`); deliberately narrower than the agreed table — `!restart` stays `err=unknown` until R5 writes its handler, because a silent relay into an empty bus is the silence R1 exists to end. **Amended 2026-08-31**: the bare spelling (`ping`, `profile FLIGHT` — the dashboard console's own lines) is accepted too, `!` now optional and meaning declared intent: a `!` typo is answered, a bare non-command is chat and stays unanswered | `[x]` |
| R2 | The ack rule: every accepted uplink schedules one out-of-schedule beacon ~10 s later carrying `re=<command>`; `re`/`ok`/`err` join the never-dropped core in `beacon.py`. **Done 2026-08-28**, plus `!ping` answered immediately (the trivial half of R3); one pending slot collapses bursts, acks obey `lora_enabled` and need no beacon-table row — a satellite listening in HOSTED/STANDBY may answer a ping | `[x]` |
| R3 | Query commands in COMMS: `ping` (an immediate beacon), `get_position` (with `age=`, may honestly report a stale fix), `get_mission` (from the DHS cache); `take_photo` ack fields (frame number, free MB) | `[ ]` |
| R4 | The event-transmission budget: one per 10 s, extras collapse into the latest; `down=1` exempt; all of it gated on `lora_enabled` | `[ ]` |
| R5 | `restart_service` through OBC → HOSTD, validated against the known services — the allowlist and the denied set already bound what it can reach | `[ ]` |
| R6 | README: document the radio command table as shipped behaviour once R1–R5 land | `[ ]` |

**Where the rewrite actually stands.** All eight services exist — `HOSTD`, `OBC`, `EPS`, `ADCS`,
`PAYLOAD`, `DHS`, `COMMS`, `DASHBOARD` — on the skeleton and the HAL, at 100 % line coverage with
`ruff` and `mypy` clean. `P0` is closed: the radio is a compact beacon over Meshtastic, and the split that put
persistence in `DHS` and left `COMMS` as the link alone is complete on both sides.

What is left: the `cubesat` CLI that `P2` still wants, and what `P6`–`P8` deliver.

### First hardware run — 2026-08-28

The stack ran on the satellite for the first time: install, always-on tier, profile switches over
MQTT (`HOSTED`, `DEMO`, `DIAG`, `MAINTENANCE`), a full DEPLOY→NOMINAL bring-up, missions recorded
into both databases with orphan recovery observed working, a photo taken by MQTT command, V8/V9
passed from an external WebSocket client. Every service read its real hardware on the first try.
What 100 % mock coverage did *not* catch — each found live, fixed, and pinned by a test:

| Found | Fix |
|---|---|
| mosquitto parses **every** file in `conf.d/` as broker config, so the ACL file there kills the broker at startup — the exact V8 prediction | ACL moved to `/etc/mosquitto/cubesat-acl.conf` (`install.sh`, `cubesat.conf`) |
| `HOSTED`/`MAINTENANCE` had no `advertise_mdns`, so applying the default profile stopped avahi and took `cubesat.local` — the desk's only name for the box — off the air | `advertise_mdns: true` in both (`profiles.yaml`) |
| The unit registry named `telegram-bot.service`, which does not exist on the Pi; one failed start marks the whole profile partially applied | registry now mirrors `systemctl list-unit-files` (three real bots) |
| A service that **survives** the profile switch (COMMS by design; all four in DEMO→EXPO) publishes status only on change, so DEPLOY saw no fresh evidence and latched SAFE | `Service.report_in()` hook, fired on entering DEPLOY; COMMS and PAYLOAD override it |
| PAYLOAD's first camera probe imports picamera2 — 35 s cold on a Pi 4, longer than the whole DEPLOY window | staged start: the sensor reports in first, camera honestly `null` until probed |
| The one-shot bus sweep hit the BNO055 inside the ~650 ms soft-reset window ADCS opens with, failing DEPLOY in 70 ms | missing addresses are re-probed once a second and fail only when the window closes |
| HOSTD stops the old profile's units on OBC's own request, and their last wills beat `host_status` by ~4 s — read as "subsystem lost", latching SAFE mid-switch | `ProfileMachine.settling`: subsystem-loss is suppressed while a request is in flight, bounded at 30 s |
| `pip install lgpio` needs swig and serves nothing — no code imports it; pip's `picamera2`/`RPi.GPIO` cannot work on Trixie (libcamera is apt-only, classic RPi.GPIO is dead on kernel ≥ 6.6) | venv with `--system-site-packages` over apt `python3-picamera2` + `python3-rpi-lgpio`; the `rpi` extra trimmed to what PyPI can actually provide |
| hostd tests read the shipped `profiles.yaml` — the rule from CLAUDE.md broken again, caught by the registry edit above | hostd fixtures pin their own registry; the same sweep over the obc/common tests still to do |

**V1 closed the same evening, by hand.** Two controlled tilts (camera-up 45°, right-side-up 45°)
against the registers and the quaternion settled it: Bosch's Euler names mirror the aerospace
convention, so the driver now publishes `pitch = −EUL_ROLL`, `roll = +EUL_PITCH` — the swap the
check existed to catch. Sensor axes on the frame: +X away from the camera, +Y right, +Z up.

**And the tilt caught something V-table never listed: the 10 kHz bus still flips bit 7.** The
clock-stretch corruption that forced the bus down from 100 kHz is not gone at 10 kHz — up to ~20 %
of reads in a bad stretch, hitting the Euler, accel and quaternion blocks alike, reproduced with a
bare `i2cget` (0x167F → 0x16FF). The driver now validates every block against physical plausibility
and re-reads (5 attempts); a flipped *low* byte (±8° / 0.13 g / 0.008 quat) still slips through.
Candidate real fix, needs a bench pass: `dtoverlay=i2c-gpio` — the software bus has no clock-stretch
bug at all; measuring both corruption rates belongs in `DIAG` (P7).

Still open on the bench: V2–V3 need a walk (carry the antenna), V5 stays by design, V6's AP path
and V7's UV reading were out of scope for a session that had to keep Wi-Fi up. The camera also
needs physically re-aiming: the test frame is mostly two of the satellite's own frame struts.

### Found in the dashboard, in front of a live satellite — 2026-08-31

Both came out of the second hardware run, with `DEMO` applied and the UI open. Both are the
ground segment's half of the work, but the first has a satellite-side half as well, which is why
they are recorded here.

- **Charts splice together days that have nothing to do with each other.** `Telemetry Graphs`,
  `Electrical Power System`, `Temperatures` and `Orbit Ground Track` all render `shownHistory`,
  which `Dashboard.tsx` fills once at page open with `recentTelemetry(200)`. That resolves to
  `GET /api/telemetry?limit=200`, and `archive.telemetry()` is
  `SELECT * FROM telemetry ORDER BY timestamp DESC LIMIT ?` — no mission filter, no time window.
  Measured on the satellite: `?limit=60` returned 33 rows from mission 11 (today) and 27 from
  mission 9 (two days earlier), a series spanning `2026-08-29T17:38` → `2026-09-01T05:53` drawn as
  one continuous line. On the ground track it is worse than a wrong chart: two days' positions
  joined into a single path. The satellite-side options are a `since=` window, a `mission_id=`
  filter, or defaulting to the open mission; the client-side one is breaking the line on a gap,
  which is worth doing regardless, because gaps happen *within* a mission too — the recorder
  stops in `HOSTED` and resumes on the next active profile. **Satellite half done 2026-09-01** — see the section below: the endpoint now means "the current
  session" and cannot return another one's rows, and the live host metrics moved onto
  `cubesat/dhs/telemetry` rather than being polled out of the archive. The client-side half — break
  the line on a gap — is still open, and is worth doing regardless, because gaps happen *within* a
  mission too.

- **Two log widgets, two directions.** `Live Telemetry Stream` appends
  (`[...prev, ...buildLines(latest)].slice(-MAX_LINES)`, newest at the bottom) while
  `Mission Events` (`[...fresh.reverse(), ...current]`) and `Radio Link Log`
  (`useRadioLog`: `[event, ...current]`) both prepend, newest at the top. So the odd one out is
  the telemetry stream, and majority alone suggests moving it to newest-first. Worth a moment's
  thought rather than a blind edit: bottom-append is the console idiom, and it is the only one of
  the three that reads as a running feed — if it flips, whatever auto-scrolls it has to flip too,
  and `MAX_LINES` trimming moves from the tail to the head.

- **`Quick Commands` should say nothing; the command belongs in `Mission Console`.** Today the
  button handler ends in `setLastMessage(\`${command.command} published to cubesat/command\`)`
  and prints it under the grid. Wanted instead: pressing a button puts the command straight into
  the console as sent, and the panel itself stays silent. The obstacle is that neither widget
  shares anything — `MissionConsoleWidget` keeps its transcript in its own `useState<string[]>`,
  `QuickCommandsWidget` its own `lastMessage` — so the log has to move up into something both
  reach, the way `useRadioLog` already does for radio events.

  **Do not simply delete the line:** that same `lastMessage` is where a *failed* publish surfaces
  (`catch` → `error.message`), and a command that never reached the broker must stay visible. It
  moves to the console with the rest.

  There is a choice worth making deliberately. The cheap version has both widgets append to a
  shared client-side log — a record of what this page *intended* to send. The better one
  subscribes to `cubesat/command` itself, which `SUBSCRIBED` deliberately excludes today, and
  builds the transcript from what actually crossed the broker; the browser ACL already permits
  `read cubesat/#`. That version also shows commands from other clients — a phone, the `cubesat`
  CLI, and an uplink relayed onto the topic by COMMS — which is the visible form of "every command
  works identically over MQTT and over LoRa". Its costs: the echo of one's own command arrives
  over the network, so "immediately" still needs an optimistic line written on click and
  reconciled when the echo lands, plus a rule for how another client's command is labelled.

### Recording narrowed to `FLIGHT`, and what it moved — 2026-09-01

Q7 answered: only `FLIGHT` and `DIAG` write to the SD card. The decision is the use case — `FLIGHT`
is entered *because* the satellite is being taken somewhere, a satellite on a desk has no track, and
the card is the component that wears out. Three of the first eleven missions came from
demonstrations. **Written and tested (100 % coverage, ruff/mypy clean); nothing of it has run on the
Pi**, and two items in the list below are the ground segment's half.

Kept whole rather than narrowed with it, because `FLIGHT` is the one recording profile with no
dashboard and `DEMO`/`EXPO` the only ones with one:

- **`cubesat/dhs/telemetry`** (retained): the wide row as it would be written, nested under `row`,
  minus `raw_json`, published every DHS tick whether or not it is recorded. Worth doing on its own
  account — the host's own CPU, RAM, swap, disk, uptime and SoC temperature are read by `psutil`
  *inside* DHS and appear in no other message, so a non-recording profile previously had no way to
  report the state of the machine it runs on, and the dashboard was polling the archive for live
  numbers.
- **A bounded ring in DASHBOARD** (`dashboard/live.py`, `dashboard.live_history_rows` = 720, about
  six hours): the charts' history where nothing is written. On the satellite rather than in the
  browser deliberately — `EXPO` is visitors arriving one at a time, and a per-page buffer gives each
  of them a chart starting from zero points and loses it on every reload.
- **`/api/telemetry` now means "the current session"** — the open mission from the database while one
  is being recorded, the ring otherwise. That closes the first of the 2026-08-31 findings above: the
  endpoint used to mean "the last N rows of the table", which returned 33 rows of one day's mission
  and 27 of another two days earlier and drew them as one line. The response carries `source`
  (`mission`/`live`) so the interface can say *why* a chart is short. **The client-side half — break
  the line on a gap, since gaps happen within a mission too — is still open.**

`DIAG` redefined in the same move: `FLIGHT` with the network and the dashboard kept, same cadences,
same radio, same automatic photography, into `diag.db`. Its `cadence_scale: 0.2` — ADCS at 10 Hz on a
10 kHz bus, a standing question of its own — is gone, and `downlink.lora` is now true, because the
beacon and the uplink are exactly what `FLIGHT` cannot show you. **P7 is retired**: the I2C sweep and
self-test report it promised are what `DEPLOY` already does on every ascent.

Photographs, same argument:

- **A mission photographs itself** every `photos.mission_interval_sec` (300 s) while one is open and
  the state permits the camera; it stops when the mission closes and resumes after a `LOW_POWER` dip
  recovers. `start_timelapse`/`stop_timelapse` are gone from PAYLOAD and from the compact radio
  vocabulary — the automatic series was the only use they had, and one fewer verb is one fewer thing
  to garble over a radio link. The `_Timelapse` engine survives as `_PhotoRun`: the CMA-leak guard,
  the three-failures-in-a-row stop and the free-space stop were all worth keeping.
- **With no mission open, a photograph never reaches the card.** It goes to `/run/cubesat/photo` (a
  tmpfs), is published on the now-**retained** `cubesat/payload/photo`, and is deleted. The retain
  flag is what makes "show the last photograph" work with no history: mosquitto runs
  `persistence false`, so that copy is RAM. PAYLOAD clears it at start, so a session never shows the
  previous one's picture.
- **`photos/unfiled/` is gone**, with `retention.unfiled_bytes` and `dhs_status.photos.unfiled_bytes`.
  Retention was forbidden to touch that directory, which made the one place guaranteed to grow
  without limit the one holding the photographs least likely to be wanted.
- **V11 got more important.** At 300 s against a 60 s `camera.idle_close_sec`, *every* frame of a
  mission is a cold capture — the check that was about an occasional photograph now covers a whole
  trip's photographs.

**Still to do from this, and it is the ground segment:** `decodePhoto` must accept
`kind: "mission_frame"` (it silently dropped every frame once already, on exactly this field);
`missionId` becomes nullable in `TelemetryRecord`, because a published row has no mission in
`DEMO`/`EXPO`; the Flight Recorder card loses unfiled bytes; the Payload widget loses the timelapse
interval and Quick Commands the two timelapse verbs; and `payload_status.timelapse` is now
`mission_photos`.

### Mission archive as a real dialog — requested 2026-08-31

`MISSION ARCHIVE` today opens an inline list inside `MissionTimelineBar` (`phase === 'picking'`)
whose only action is "replay this one". Wanted: a large modal that lists the missions and lets an
operator pick one, delete it, export it, or import one. Export already exists —
`GET /api/missions/<id>/export` returns `{mission, telemetry, attitude}` as
`mission-<id>.json` — so the work is the dialog plus the three verbs around it. Each of the
others runs into something structural, which is why this is a note and not a ticket yet.

**Delete is not the dashboard's to do.** The service is read-only by construction: `http.py` is
"static files, read-only JSON, and nothing else", and `archive.py` opens the file with `mode=ro`
— deliberately a mode rather than a promise, so it *cannot* write even by mistake. DHS owns the
database. So deletion should travel as a ground command on `cubesat/command`, executed by DHS,
exactly like every other thing the ground asks of the satellite; adding an HTTP `DELETE` to the
dashboard would hand a second writer to a file with one owner.

**Delete reopens D4.** Command authentication was deferred on the argument that the worst a
visitor on an open `EXPO` access point can do is `set_profile HOSTED`, which disconnects them
too. Erasing a recorded flight is not in that class. Note also where the gate can live: not in
the broker ACL, which cannot see which profile is active, so a profile-dependent restriction has
to be enforced by DHS or OBC.

**Delete has to agree with retention.** `retention.py` deliberately never deletes a mission row —
"a trip that happened stays listed even after its detail is gone" — it drops the detail and stamps
`purged_at`, so the archive can tell a purged mission from an empty one. Manual deletion either
follows that same semantics (detail goes, the row and its `purged_at` stay) or breaks it on
purpose, and that is a decision to make rather than to discover. Whatever it does, it also has to
remove `photos/<mission_id>/`.

**Import needs the most thought, and probably should not touch the satellite at all.** Writing a
foreign mission into `comms.db` invents history the satellite never recorded and collides with
its own mission ids. The cheaper and truer answer already exists in the client: the `replay`
source, built for the backend-less public demo, already renders a recorded mission through the
same widgets as the live view. Import then means "open this exported file as a replay source" —
no write path, no ownership question, no id collision, and it works in a browser that is not
talking to a satellite at all. If import into the archive is genuinely wanted later, it is a DHS
command like delete, not an upload endpoint.

**One gap to close either way:** the export carries no photos. `{mission, telemetry, attitude}`
is the whole body, while frames live under `photos/<mission_id>/` and are listed separately. An
archive dialog that exports a mission and gets no pictures back will surprise someone.

### Bench checks the code is waiting on

Written from the drivers, which mark verified constants apart from inferred ones. Each of these
produces plausible data rather than an error, which is why none of them can be settled by reading.

| # | Check | Why it is not settled |
|---|---|---|
| ~~V1~~ | ~~**BNO055 Euler field order**~~ **Done 2026-08-28, two controlled tilts:** the predicted swap was real — Bosch's roll/pitch mirror the aerospace names (their ranges give it away: roll ±90 is the asin-shaped one). Driver now remaps `pitch = −EUL_ROLL`, `roll = +EUL_PITCH`; the quaternion needed no remap | fixed + verified |
| V2 | **TEL0157 knots → m/s factor.** One moving fix — a walk with the antenna out | The bench reading was 0.00 knots at rest, and zero converts to zero, so no measurement pins the factor |
| V3 | **TEL0157 altitude triplet high byte.** The same walk, somewhere above 255 m | The bench altitude of 116.59 m fits in one byte, so the big-endian high byte has never been exercised |
| ~~V4~~ | ~~**BNO055 accelerometer scale.** `\|a\|` ≈ 9.8 m/s² at rest~~ **Done 2026-08-28, first hardware run:** at rest `accel_g` = (0.035, −0.073, 0.968), \|a\| ≈ 0.97 g with the accelerometer still uncalibrated (`calib.accel = 0`) — a wrong scale factor would be off by 2× or 100×, not 3 % | measured |
| V5 | **BNO055 calibration save/restore.** Deliberately not implemented: the profile register block is not in the verified docs, and writing unverified registers into the fusion engine on every boot is what produced the `SYS_ERR = 9` session already recorded there | Without it the magnetometer must be re-calibrated after every reset, so `yaw` is withheld for a while after each restart |
| V7 | **SEN0501 board revision.** Read the silkscreen, or compare the pair of candidate values the driver logs against a known UV source | One raw register, two formulas: at raw 14 they give 0.00 and 84.35. `uv_index` stays null until this is settled |
| V6 | **NetworkManager client mode.** `nmcli connection down Hotspot` with a pinned `wlan0` | Written against the documentation, never run on the Pi. `EXPO` depends on it |
| ~~V8~~ | ~~**The mosquitto WebSocket listener.**~~ **Done 2026-08-28.** The predicted failure happened: the ACL file in `conf.d/` is parsed as broker config and kills mosquitto at startup ("Invalid bridge configuration" on the first `topic` line). Moved to `/etc/mosquitto/cubesat-acl.conf`; after that a WebSocket client on :9001 receives all six retained statuses on subscribe, and :1883 is unreachable from off the satellite | fixed + verified |
| V10 | **Meshtastic hop count.** `hops = hopStart − hopLimit` is read from the library's documentation, not from a bench run. The check: a packet relayed through a third node should arrive with `hops = 1`; everything heard so far has been direct | Every direct packet reads 0 whichever interpretation is right, so no traffic to date can falsify it. Wrong arithmetic would put a plausible small integer in `radio_log.hops` |
| ~~V9~~ | ~~**The browser ACL actually denies.**~~ **Done 2026-08-28**, from an external WebSocket client: publishes to `cubesat/host/command` and a forged `cubesat/eps/status` were silently dropped (confirmed by a subscribed witness), `cubesat/command` passed — and COMMS answered the `get_telemetry` it carried on `comms_data`, so the whole ground loop works from a browser's vantage point | verified |
| V12 | **MAX17048 state of charge disagrees with its own charge rate — by ~70×.** First hardware run, on mains: `battery_percent` fell 72.4 → 70.3 in eight minutes (≈ −15 %/h) while `voltage` sat unmoved at 3.905–3.906 V and `CRATE` read −0.208 %/h — which is **exactly one LSB** (the register's resolution is 0.208 %/h), the same value the bench recorded at rest on 2026-08-23, so the gauge is saying "about zero", not measuring a discharge. A pack actually losing 15 %/h moves its terminal voltage, so it is the SOC register that is wrong, not the battery — but which way is not decidable from eight minutes. **Confirmed over 85 minutes the same evening: voltage went *up* 3.905 → 3.920 V while SOC went *down* 72.41 → 66.14 %.** The two move in opposite directions, which no discharging cell does, so the reading is not merely imprecise — it is describing something that is not happening. Whether the cell is also being charged is the separate question in V13. Candidates, in order: the gauge is still relaxing after power-on and has no quick-start; `SOC_LSB_PER_PERCENT` is wrong (it would have to be wrong in a way that still lands near a plausible 70 %, so this is the weakest); the pack really is discharging on mains because the X728 is not charging it. The check: leave the satellite on mains for several hours logging `voltage`, `battery_percent` and `charge_rate` together, then repeat on battery. **Do not touch the constant until the log says which** | Nothing errors: `on_mains` currently holds, because −0.208 is above `DRAINING_PERCENT_PER_HOUR` (−1.0), so every power-driven descent stays suppressed and the false SOC is inert. It stops being inert the moment `CRATE` crosses −1.0 — then the pin-plus-rate agreement that protects a plugged-in satellite breaks, and a SOC that reaches 10 % takes the host down for real |
| V13 | **The X728 does not appear to be charging on mains, and it should be.** Observed 2026-08-31 with the satellite plugged in all evening: 3.92 V, SOC ~66 %, `CRATE` one LSB from zero, and the board's own charge LEDs agreeing with roughly that level. The datasheet figures in `docs/hardware-x728-ups-hat.md` say the recharge threshold is **4.1 V** and cutoff 4.24 V — at 3.92 V the board is well past the point where it should have resumed, so "it deliberately holds a partial charge" does not explain this reading. Two candidates, cheap to separate. First, the **`CHG Ctrl` jumper**: on the V2.5 board, opening it hands charge control to **GPIO16**, and nothing in this repo drives that pin (the only GPIO the code touches is `PLD_PIN = 6`, and it only reads it) — so if the jumper is open, charging is simply disabled by a floating input. Check the jumper physically first. Second, tired 18650s: capacity loss shows up as a cell that will not hold a charge, and the pack is unbranded. `vcgencmd get_throttled` reads `0x0`, so the 5 V supply is not sagging and can be ruled out. **If the jumper turns out to be the answer, it is an opportunity rather than a defect** — GPIO16 would let EPS decide when to charge, which is exactly the wanted behaviour: hold a partial charge on the desk, where a Li-ion cell ages fastest sitting full, and top the pack up deliberately before a `FLIGHT` outing. That would be a driver and a policy, not a jumper left closed | Nothing errors and nothing looks broken: the dashboard shows a plausible 66 %, the LEDs agree, and the satellite runs happily on mains. It is discovered at the worst possible moment — leaving for a trip with a pack that was never full — which is precisely the failure `FLIGHT` cannot afford. Note this is upstream of V12: while charging is in doubt, the gauge's drift cannot be interpreted either |
| V11 | **Exposure of the first capture after an idle close.** The camera now gives the sensor back after `camera.idle_close_sec` of no captures (the ISP pipeline is SoC heat while open), so a cold `take_photo` re-opens and shoots within about a second. Whether Picamera2's AE/AWB have converged by then is read from nothing — the lores stream exists precisely to run those loops, but how long they need after `start()` is not in our docs. The check: one photo warm, one photo cold, same scene | A dark or colour-cast first frame is plausible wrong data, not an error; if the bench shows it, the fix is a short settle delay after a cold open, and its length is a measurement, not a guess |

---

### Decisions still open

Tracked in [`docs/concept.md` → Open questions](docs/concept.md#open-questions):

| # | Question | Blocks |
|---|---|---|
| Q1 | `BMP280` at `0x76` duplicates the SEN0501 pressure reading — keep it, and for what? Settle it in `DIAG` by logging both | P7 |
| Q2 | Recovering a trip after an unexpected reset. The profile is deliberately not persisted, so a brownout mid-trip silently ends the recording. Fixing it without a stored profile means acting on a **boot reason** — mains absent at boot means the satellite is demonstrably not on a desk. Deferred until `FLIGHT` has seen enough use to know whether spurious resets happen at all | P6 |
| Q3 | Timelapse in `FLIGHT` — worth the battery and the card space? Currently an opt-in profile parameter | P6 |
| Q4 | May a human move a fault-latched (`SAFE`) satellite into `EXPO` to show it to an audience? | P5 |
| ~~Q6~~ | ~~**The remote ground-station API is marked absent.**~~ **Answered 2026-08-25: nothing beyond LoRa and the local dashboard.** The PHP groundstation is being rewritten into a UI with several data sources and keeps no backend of its own. The answer turned out not to be a data change after all: `comms/api.py`, the cloud half of `comms/service.py` and the `downlink.api` flag are removed outright — see Stage 0 of the dashboard plan | done |
| Q5 | The Heltec cannot actually be powered down — it is fed from the Pi's 5 V pin, so "radio off" can only mean "stop talking to it". Real power-off needs a MOSFET on that rail, driven from a spare HAT pin | P6 |
| ~~Q7~~ | ~~**Is a recorded mission the privilege of `FLIGHT`?**~~ **Answered 2026-09-01: yes** — `FLIGHT`, plus `DIAG` rehearsing it into `diag.db`. Full record in [`docs/concept.md` → Open questions](docs/concept.md#open-questions); what it took is the section below | done |

---

## 🧯 Retired Items

| # | Was | Why it is gone |
|---|---|---|
| O1, O2 | A separate `rpi-mode-switcher` repository that would stop internet-dependent services on connectivity loss and read a GPIO switch to pick a mode | Superseded by the profile design in `docs/concept.md`. The orchestration lives in this repo instead, split as `HOSTD` (root, executes) + `OBC` (decides), so docs and tests cannot drift across two repositories. A GPIO switch is noted as an optional future recovery path, not a mode selector |
| G10 | Bench-verify the SC16IS752 register protocol in `lora.py` with an SDR, and the A9G NMEA parsing against a live fix | Both modules are off the satellite. The 52Pi IoT Node(A) was replaced by a Heltec V4 on Meshtastic (verified over the air in both directions, 2026-08-23) and the A9G GPS by a TEL0157 on I2C (verified with a 3D fix, 23 satellites). The register-level protocol no longer needs verifying — it needs deleting, in P0 |
| H1–H7 | Hardware abstraction layer as a standalone medium-priority item | Folded into **P1** and promoted to the front of the queue: nothing in the rewrite is testable without it |
| T1–T7 | Individual test files against the pre-rewrite modules | The suite exists and is enforced at 95 % coverage. New tests follow each rewrite phase rather than being tracked separately |

---

## 🐛 Pre-Rewrite Bug Fixes (Historical)

Confirmed bugs in the five-service codebase, all fixed. Kept because several are instructive.

| # | Description | File | Status |
|---|-------------|------|--------|
| B1 | Signed 16-bit conversion loop rebinds local var — negative IMU values returned as large positive integers; garbage orientation data | `src/common/imu_qmi8658_ak09918.py` | `[x]` |
| B2 | AHRS quaternion state (`q0/q1/q2/q3`, `exInt/eyInt/ezInt`) declared as class variables — shared across instances | `src/common/imu_qmi8658_ak09918.py` | `[x]` |
| B3 | `get_uptime_seconds()` returns boot Unix epoch (~1.7 B), not elapsed seconds since boot | `src/common/system_metrics.py` | `[x]` |
| B4 | `ensure_dir()` calls `Path(path)` but `Path` is never imported — `NameError` at runtime | `src/common/utils.py` | `[x]` |
| B5 | OBC heartbeat uses raw f-string JSON construction instead of `json.dumps()` | `src/obc/main.py` | `[x]` |
| B6 | `payload/main.py` error status value `"error"` inconsistent with `"SUCCESS"` casing | `src/payload/main.py` | `[x]` |

> B1 and B2 are why the BNO055 is a genuine simplification and not just a hardware swap: both bugs
> were in a hand-rolled AHRS filter that no longer exists, because the new sensor fuses on-chip.

---

## 🔧 Pre-Rewrite Configuration Fixes (Historical)

| # | Description | File | Status |
|---|-------------|------|--------|
| C1 | `camera.py` hardcoded `PHOTO_DIR` — ignored `config.PHOTOS_DIR` | `src/payload/camera.py` | `[x]` |
| C2 | `requests`, `lgpio`, `pyyaml` missing from `requirements.txt` | `requirements.txt` | `[x]` |
| C3 | `install.sh` started only 3 of 5 services — EPS and ADCS never enabled | `scripts/install.sh` | `[x]` |
| C4 | Add `config/config.yaml` for runtime config and load it in `config.py` | `config/`, `src/common/config.py` | `[x]` |
| C5 | Add `scripts/restart.sh` | `scripts/restart.sh` | `[x]` |

---

## ♻️ Pre-Rewrite Refactoring (Historical)

| # | Description | Status |
|----|-------------|--------|
| RF1 | Standardise `cubesat/obc/status`: timestamp first, `state` → `status`; update all consumers | `[x]` |
| RF2 | Consolidate photo and telemetry commands onto the single `cubesat/command` topic | `[x]` |
| RF3 | Update `README.md`, `CLAUDE.md`, `docs/architecture.md` for RF1 and RF2 | `[x]` |
| R1 | Remove MQTT `on_connect`/`on_disconnect` dead code in the client factory | `[x]` |
| R2 | Fix OBC boot-time publish race — transitions fired in `__init__` before MQTT connected | `[x]` |
| R3 | Wire up timelapse commands (existed in `camera.py`, never called) | `[x]` |
| R4 | Gate telemetry aggregation on OBC state `SCIENCE` | `[x]` |

> R4 is being **undone** in P3, on purpose. Gating persistence on `SCIENCE` means `FLIGHT` records
> nothing unless someone remembers to send `science_start` before leaving the house. The profile
> decides whether a row may be written; the state decides how often.

---

## 🛰️ GPS + LoRa + COMMS Redesign (Historical)

Wired up the 52Pi IoT Node(A) and turned the passive `telemetry` service into `COMMS`. The module
is now off the satellite, but the COMMS restructuring it brought survives into the rewrite.

| # | Description | Status |
|---|-------------|--------|
| G1 | `GPS` driver (A9G NMEA over UART); `gps` sub-object in `adcs_status` | `[x]` |
| G2 | Rename `src/telemetry/` → `src/comms/`; `TelemetryAggregator` → `CommsService` | `[x]` |
| G3 | `LoRaModule` driver (SC16IS752 I2C registers, CRC-16-CCITT framing) | `[x]` |
| G4 | Runtime-toggleable `api_enabled`/`lora_enabled`/`aggregation_enabled` + `set_comms_config` | `[x]` |
| G5 | Re-check internet reachability every loop iteration instead of once at startup | `[x]` |
| G6 | Periodic SQLite retention cleanup | `[x]` |
| G7 | Remote-API command polling and LoRa RX, both re-publishing onto `cubesat/command` | `[x]` |
| G8 | Rename the topic/DB/config surface `TELEMETRY_*` → `COMMS_*` | `[x]` |
| G9 | Rename the systemd unit and scripts | `[x]` |
| G10 | Bench-verify the SC16IS752 register protocol and A9G NMEA parsing | retired — see [Retired Items](#-retired-items) |

> **G7 is the piece worth keeping in mind.** Unifying the uplink — every inbound command
> re-published verbatim onto `cubesat/command`, whatever channel it arrived on — is what makes
> `FLIGHT` recoverable over the radio when Wi-Fi is down. Preserve that property through the rewrite.

> **Deployment note for an already-deployed Pi:** `.env` files using `TELEMETRY_*` names need
> updating to `COMMS_*`, the old `cubesat-telemetry.service` unit should be removed manually, and
> `dtoverlay=sc16is752-i2c` should come out of `/boot/firmware/config.txt` — the hardware it served
> is gone, and it conflicts with nothing only because no `ttySC*` devices are created any more.

---

## Notes

- The phase order in **The Rewrite** is a dependency order, not a preference: P1 gates every later
  phase's tests, and P0 gates the radio-only profiles (`EXPO`, `FLIGHT`).
- Historical sections are kept deliberately. `docs/code_smells.md` and `docs/refactoring_plan.md`
  hold the detailed write-ups behind them.
