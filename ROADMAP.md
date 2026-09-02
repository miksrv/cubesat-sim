# CubeSat Sim — Roadmap

What is left to do, and the bench checks the code is waiting on. **Finished work is removed from
this file rather than ticked off** — the reasoning behind a closed decision lives where the decision
is now implemented, and a roadmap that keeps its own history stops being a list of what is left. Git
holds what this file used to say.

**Where the design lives:** [`docs/concept.md`](docs/concept.md) is the operating concept (profiles,
mission states, control plane) and owns the phase plan, including the record of every settled
question. [`README.md`](README.md) is the reference for the target system, and `docs/hardware-*.md`
for anything electrical. This file tracks only the work outstanding.

---

## Status Legend

`[ ]` not started · `[~]` in progress. There is no "done" — finished work leaves this file.

---

## 🚀 The Rewrite (Current Work)

The hardware is finished and validated; the software is being rewritten against the operating
concept. Phases are ordered so each one is independently useful and testable. Full scope and
rationale per phase: [`docs/concept.md` → Implementation plan](docs/concept.md#implementation-plan).

Everything except `P6` is done and gone from this table. `P7` was retired on 2026-09-01 (the I2C
sweep and self-test it promised are what `DEPLOY` does on every ascent); `P2` closed the same day
with the `cubesat` CLI; `P8` closed with the test sweep that removed the last places a test asserted
a shipped configuration value instead of computing from it. The radio command contract is written
down to its last command — `restart_service` was that one — with a single field set outstanding:
`photo`'s ack answers with the ordinary state fields instead of the frame number and the free
megabytes the contract describes.

| Phase | Scope | Delivers | Status |
|---|---|---|---|
| **P6** | **Written; what is left is a walk.** The code landed piecemeal: `powersave` on entering `LOW_POWER` and in `FLIGHT`, the profile TTL armed by HOSTD and reported as `ttl_expires_at`, the radio duty-cycled by the beacon table, and the profile itself. What no test can settle is whether the GNSS track is right — V2 and V3 below are that walk. Mains-as-signal recovery is deliberately still open as Q2 | The autonomous logging profile | `[~]` |

**Where the rewrite stands.** All eight services exist, at 100 % line coverage with `ruff` and
`mypy` clean, and all eight have now run on the satellite: `HOSTD`, `OBC`, `EPS` and `COMMS` in
`HOSTED` on 2026-08-31, and `ADCS`, `PAYLOAD`, `DHS` and `DASHBOARD` in `DEMO` on 2026-09-01 —
`DEPLOY` in 1.4 s, `NOMINAL`, the attitude widget and a photograph watched live. The standing caveat
has therefore moved from services to **profiles**: `FLIGHT`, `EXPO`, `DIAG` and `MAINTENANCE` have
never been applied, and with them the access point (V6), a moving GNSS fix (V2, V3) and `diag.db`
are untried. **Almost everything left is a bench check or a decision.** What can still be written
without the satellite: the photographs missing from a mission export (which blocks the public demo),
the export and import verbs the archive dialog still lacks, the `photo` ack's frame fields — the last
line of the radio contract still unwritten — the chart line that should break on a gap, and an
end-to-end test against the replay build. The one defect the hardware found in the logic, `restart_service` latching `SAFE`, is fixed:
`health.expect_restart` waives the departure OBC itself asked for, for one loss grace.

### The dashboard still offers `science start` / `science stop` — 2026-09-02

The `SCIENCE` mission state was removed from the satellite on 2026-09-02 (the reasoning is in
[`docs/concept.md` → Mission states](docs/concept.md#mission-states): every cadence, the beacon,
the camera permission and the recording rule were identical to `NOMINAL`, so the two commands
changed a label and nothing a service could act on). The commands are gone from the vocabulary,
from OBC, from the compact radio syntax and from `MissionState`.

**The Quick Commands buttons live in the groundstation repo**, which this repository does not
contain — the dashboard arrives as a built artifact. So until that client is edited and
redeployed, those two buttons publish commands the satellite now ignores. Nothing breaks: an
unrecognised command on `cubesat/command` belongs to another service by construction and is
dropped without complaint (there is a test for exactly that). But a button that does nothing is
worth removing, along with any `SCIENCE` case in the state colouring and in `observed.ts`, and any
mission recorded before this date can still hold `SCIENCE` in `telemetry.obc_state` — a replay
must not fall over on a state the enum no longer has.

### Mission archive: what the dialog still does not do — 2026-09-02

The dialog itself is **done**: `MISSION ARCHIVE` opens a modal listing every recorded session, and
each row replays or deletes. Deletion travels as `delete_mission` on `cubesat/command` and is
performed by DHS, which owns the file — the row goes with its detail and its photographs, refused in
`EXPO` and refused for the mission being recorded. Two verbs the original note wanted are still not
there, and both are still blocked on something structural rather than on effort.

**Export is in the dialog only as far as the endpoint goes.** `GET /api/missions/<id>/export`
returns `{mission, telemetry, attitude, radio}` as `mission-<id>.json`, and that body **carries no
photographs** — the frames live under `photos/<mission_id>/` and are listed separately. So a dialog
that offers "export" and hands back a file with an empty camera panel will surprise someone, and the
backend-less demo build replays a trip the same way. Closing that means either embedding the frames
(a mission's worth of JPEGs inside one JSON) or exporting a container, and that is the decision
nobody has made yet.

**Import should probably not touch the satellite at all.** Writing a foreign mission into `comms.db`
invents history the satellite never recorded and collides with its own mission ids. The cheaper and
truer answer already exists in the client: the `replay` source, built for the backend-less public
demo, already renders a recorded mission through the same widgets as the live view. Import then
means "open this exported file as a replay source" — no write path, no ownership question, no id
collision, and it works in a browser that is not talking to a satellite at all. If import into the
archive is genuinely wanted later, it is a DHS command like delete, not an upload endpoint.

**A photo directory is named after a mission id, and there are two id spaces.** `comms.db` and
`diag.db` each number their missions from 1, while photographs are filed in one
`photos/<mission_id>/` for both — so `DIAG` mission 3 and `FLIGHT` mission 3 share a directory.
Retention has always had this (it removes the directory of any mission it ages out, in whichever
database it is running against), but a person deleting a `DIAG` bench run from the dialog can now
reach a `FLIGHT` trip's photographs in one click, which is a much easier way to meet it. The fix is
to file frames under the database as well as the mission — `photos/<db>/<mission_id>/` — which is a
migration of what is on the card, PAYLOAD's `path_for`, retention's fence and the dashboard's two
photo routes. **Until then, do not delete a `DIAG` mission whose id also exists in `comms.db`.**

### ADCS mounting offset — requested 2026-09-01, to be done on the running satellite

With the satellite level on a desk, `Roll (X)` reads 2.6° and `Pitch (Y)` −4.1°: the BNO055 is
mounted a few degrees off the frame. Aligning it mechanically is the wrong tool — by eye it cannot
be brought within a degree, and every reassembly moves it again — so the offset becomes **data**,
the way every real vehicle treats IMU mounting.

What to build:

1. **A mounting rotation in configuration**, as a quaternion (sensor frame → body frame), with
   the date it was captured. Data, not code: it is a property of this assembly and changes whenever
   the frame is opened. Zero rotation is the default, so nothing changes until it is captured.
2. **Applied in ADCS, not in the driver.** The driver reports what the sensor measures; ADCS knows
   how the sensor is bolted to the frame. Body attitude is `q_mount⁻¹ · q_sensor`, and `roll`,
   `pitch`, `yaw` are then derived from the corrected quaternion — **not** by subtracting two Euler
   angles, which stops being right the moment the satellite turns about its vertical axis. The same
   rotation goes onto `accel_g` and `gyro_dps`, or the dashboard's own frame check ("measured g up
   at rest") reports the 5° the attitude no longer has.
3. **A capture procedure that cancels the desk.** A desk is not level either, and a naive "level
   here" bakes its tilt into the satellite. Capture with the satellite on the desk, turn it 180°
   about vertical, capture again: the desk's tilt changes sign relative to the frame while the mount
   offset does not, so the mean of the two is the mount alone. Ten seconds of samples each, averaged.
   A `level` (or `calibrate mount`) command on the usual `cubesat/command` topic, so it works from the
   shell, the console and the radio alike, and prints the quaternion for the config file.
4. **Say so on the wire.** `adcs_status` carries the offset that was applied (or that none was), so a
   consumer can tell corrected attitude from raw. Recorded missions before this change hold sensor
   frame attitude, later ones body frame — worth a line in the schema notes, not worth a migration.
5. The BNO055's own `AXIS_MAP` remap is not the tool: it swaps axes in 90° steps only.

After any reassembly the capture is repeated and the new value recorded in
`docs/hardware-bno055-bmp280-imu.md` beside the axis convention already written there.

### A ship's log, not "Mission Events" — requested 2026-09-01

The dashboard's `Mission Events` widget is built in the browser from transitions the page itself
witnessed (`features/events/observed.ts` in the groundstation repo): mission state, profile applied
or partly applied, a mission opening or closing, a subsystem going quiet, a photo refused, a
transmit failing. Wanted — and every one of these is already on the bus, so the first step is
client-side and cheap:

- **Power source changed** — `eps_status.external_power` flipping either way ("mains lost", "mains
  restored"), the event that today's charging investigation kept wanting a timestamp for.
- **Photograph taken** — from the retained `payload_photo`: an on-demand `photo` and a mission
  `mission_frame` are different lines, with the file name and, for a frame, its sequence number.
- **GNSS fix acquired / lost** — `adcs_status.gnss.fix` flipping, with the satellite count.
- **Beacon on / off** — `comms_status.lora_enabled` flipping, and whether it was the profile's own
  default on entry or a command.
- **Rename it.** The widget is a ship's log — *бортовой журнал* — and should be titled as one.

The structural question underneath, to decide rather than to drift into: the browser log **starts
when the page does** and is empty after a reload, which the widget honestly says. A ship's log that
means the term keeps its entries on the satellite — a small `events(mission_id, t, kind, detail)`
table owned by DHS, fed from the same transitions DHS already sees on the topics it subscribes to,
exported with the mission and replayed with it. That is a fifth table beside `radio_log`, which is
the precedent: discrete events with their own timestamps, kept because a 30 s telemetry row cannot
stand for them. Until that decision is made, the four events above go into `observed.ts`; when the
table exists, the widget reads it for history and keeps deriving live entries the same way.

### The walk to work: what to check on the first real trip

The use case `FLIGHT` exists for, written down as a sequence because every piece of it is now built
and none of it has been done end to end. Everything below is code that exists and has never met a
morning.

1. **Boot lands in `HOSTED`.** Nothing to do — that is the default profile, deliberately not
   persisted across a reboot.
2. **Start the trip from the phone**, over the Heltec: `profile flight` in the Meshtastic channel.
   The satellite is listening in `HOSTED` (the profile permits the radio; `STANDBY` has no beacon
   row, so it hears without transmitting). With no `--mission` the mission is labelled with its own
   start time; with no `--ttl` the profile's 600 minutes apply, which is the strap for a trip
   somebody forgets to end.
3. **`FLIGHT` should then**: stop the units the profile does not name, take Wi-Fi down, set the
   `powersave` governor, open a mission, record telemetry and the GNSS track, photograph itself every
   `photos.mission_interval_sec` (300 s), and beacon (`downlink.beacon` is true there).
4. **End it on arrival**, again from the phone: `profile demo` or `profile expo`. DHS closes the
   mission on the profile change; the beacon goes quiet, because those profiles start transmission
   off; the dashboard comes up. Then `ssh` and `sudo poweroff` — or `cubesat status` first, which is
   the one command that answers without a browser.
5. **Look at it at home** in `DEMO`: the mission in the archive, its track on the map, its charts,
   its photographs changing as the replay plays.

What to watch for, in order of how likely it is to bite:

- **The GNSS track** — V2 and V3 below are exactly this walk, and both produce plausible wrong
  numbers rather than errors.
- **Battery** — V13. An hour each way should be comfortable: on battery the SOC falls at a rate the
  load explains (2026-09-01), but whether it started full is exactly what V13 leaves open.
- **The beacon reset on arrival.** Switching to `DEMO` is what silences it; if it keeps beaconing at
  a desk, the reset in `_reconcile_downlink` did not happen.
- **`cubesat` is not on the operator's `PATH`.** It ran on the Pi on 2026-09-01 — `cubesat status`
  answers in about 2.5 s — but only as `/opt/cubesat-sim/venv/bin/cubesat`, because the console
  script lives in the virtualenv the services use and nothing puts it on a login shell's path. On a
  trip that is a long command typed by hand; worth a symlink or a profile line before relying on it
  in a field.

### Bench checks the code is waiting on

Written from the drivers, which mark verified constants apart from inferred ones. Each of these
produces plausible data rather than an error, which is why none of them can be settled by reading.

Settled checks are removed from this table, and their findings live where the code that depends on
them is: the BNO055 Euler swap and the accelerometer scale in `hal/rpi/bno055.py`, the residual
bit-7 flips at 10 kHz in [`docs/hardware-bno055-bmp280-imu.md`](docs/hardware-bno055-bmp280-imu.md),
and the mosquitto ACL that must not sit in `conf.d/` in `config/mosquitto/`.

| # | Check | Why it is not settled |
|---|---|---|
| V2 | **TEL0157 knots → m/s factor.** One moving fix — a walk with the antenna out | The bench reading was 0.00 knots at rest, and zero converts to zero, so no measurement pins the factor |
| V3 | **TEL0157 altitude triplet high byte.** The same walk, somewhere above 255 m | The bench altitude of 116.59 m fits in one byte, so the big-endian high byte has never been exercised |
| V5 | **BNO055 calibration save/restore.** Deliberately not implemented: the profile register block is not in the verified docs, and writing unverified registers into the fusion engine on every boot is what produced the `SYS_ERR = 9` session already recorded there | Without it the magnetometer must be re-calibrated after every reset, so `yaw` is withheld for a while after each restart |
| V6 | **NetworkManager client mode.** `nmcli connection down Hotspot` with a pinned `wlan0` | Written against the documentation, never run on the Pi. `EXPO` depends on it |
| V7 | **SEN0501 board revision.** Read the silkscreen, or compare the pair of candidate values the driver logs against a known UV source | One raw register, two formulas: at raw 14 they give 0.00 and 84.35. `uv_index` stays null until this is settled |
| V10 | **Meshtastic hop count.** `hops = hopStart − hopLimit` is read from the library's documentation, not from a bench run. The check: a packet relayed through a third node should arrive with `hops = 1`; everything heard so far has been direct | Every direct packet reads 0 whichever interpretation is right, so no traffic to date can falsify it. Wrong arithmetic would put a plausible small integer in `radio_log.hops` |
| V13 | **The X728 charges on mains, but at ~+3 %/h — a fraction of its rated 2.3–3.2 A.** Measured 2026-09-01 with `charge_rate` a real quantity (see `docs/hardware-x728-ups-hat.md`): CanaKit 3.5 A on USB-C, LEDs one steady one blinking, SOC 50.39 → 50.78 % in fifteen minutes, voltage +17 mV. Next check: a 5.1 V ≥ 4 A supply on the DC jack, the input Geekworm rates for full charging current; if the rate does not change, the cells or the charger stage are the question. The original observation follows for the record. Observed 2026-08-31 with the satellite plugged in all evening: 3.92 V, SOC ~66 %, `CRATE` one LSB from zero, and the board's own charge LEDs agreeing with roughly that level. The datasheet figures in `docs/hardware-x728-ups-hat.md` say the recharge threshold is **4.1 V** and cutoff 4.24 V — at 3.92 V the board is well past the point where it should have resumed, so "it deliberately holds a partial charge" does not explain this reading. The **`CHG Ctrl` jumper was checked on 2026-09-01 and is installed**, which per Geekworm means automatic charging whenever the adapter is connected — so a floating GPIO16 is ruled out. Two candidates remain. First, the **supply**: the X728 charges only from its own DC jack and wants 2.3–3.2 A for the pack on top of the Pi's load; `PLD` proves the jack sees power, not enough of it. Second, tired 18650s: capacity loss shows up as a cell that will not hold a charge, and the pack is unbranded. `vcgencmd get_throttled` reads `0x0`, so the 5 V supply is not sagging and can be ruled out. **The decisive check is now cheap, because `charge_rate` is a real measurement:** plug in and watch `voltage` and `charge_rate` for ten minutes — a charger delivering current lifts the terminal voltage within seconds and turns the rate positive after the five-minute window. (Opening the jumper on purpose stays an opportunity: GPIO16 would let EPS hold a partial charge on the desk and top up before a `FLIGHT`. A driver and a policy, later) | Nothing errors and nothing looks broken: the dashboard shows a plausible 66 %, the LEDs agree, and the satellite runs happily on mains. It is discovered at the worst possible moment — leaving for a trip with a pack that was never full — which is precisely the failure `FLIGHT` cannot afford. While charging is in doubt, the 2026-08-31 mains-day drift (SOC down while voltage rose) cannot be interpreted either — that drift is now this item's question, since the "70× disagreement" it was filed under turned out to be a comparison against a constant (see `docs/hardware-x728-ups-hat.md`, 2026-09-01) |
| V14 | **BNO055 low-byte bit-7 flips reach the record.** Measured 2026-09-01 in `DEMO` at rest: about one published sample in twenty carries an undetectable +128 LSB step — `gyro_x` exactly 8.0 °/s between neighbours of 0.06, `acc_x` 0.07 → 0.20 g and back — while the detectable high-byte flips ran at one read in eleven, all caught. The plausibility check cannot see these by construction. The check the hardware doc has asked for since 2026-08-28: move the sensor to a bit-banged `i2c-gpio` bus, which honours clock stretching, and measure both rates again. Until then a median-of-three in the driver would hide the isolated ones at the cost of half a second of latency — a decision, not a fix | They are physically plausible values and DHS writes them into `attitude` as measured, so a replay shows an 8° twitch or a 0.13 g kick that never happened |

---

### Decisions still open

Tracked in [`docs/concept.md` → Open questions](docs/concept.md#open-questions):

| # | Question | Blocks |
|---|---|---|
| Q1 | `BMP280` at `0x76` duplicates the SEN0501 pressure reading — keep it, and for what? Log both and compare over a few sessions; `DIAG` is the natural place, now that it rehearses a real mission | — |
| Q2 | Recovering a trip after an unexpected reset. The profile is deliberately not persisted, so a brownout mid-trip silently ends the recording. Fixing it without a stored profile means acting on a **boot reason** — mains absent at boot means the satellite is demonstrably not on a desk. Deferred until `FLIGHT` has seen enough use to know whether spurious resets happen at all | P6 |
| Q4 | May a human move a fault-latched (`SAFE`) satellite into `EXPO` to show it to an audience? Probably yes, with the fault displayed — but it needs deciding rather than falling out of the implementation | — |
| Q5 | The Heltec cannot actually be powered down — it is fed from the Pi's 5 V pin, so "radio off" can only mean "stop talking to it". Real power-off needs a MOSFET on that rail, driven from a spare HAT pin | P6 |

---

## Still to do on the satellite itself

Not code — the box in the corner.

- **Re-aim the camera.** The first test frame was mostly two of the satellite's own frame struts.

---

## Notes

- The phase order in **The Rewrite** is a dependency order, not a preference: `P2` and `P6` are what
  is left of it.
- **This file holds only what is outstanding.** The pre-rewrite bug, configuration and refactoring
  logs that used to live here are gone: the code they describe no longer exists, and
  `docs/code_smells.md`, `docs/refactoring_plan.md` and `docs/architecture.md` keep the detailed
  write-ups as the historical record they are. Closed decisions are in
  [`docs/concept.md`](docs/concept.md); settled hardware findings are in the relevant
  `docs/hardware-*.md` and at the constants they justify.
