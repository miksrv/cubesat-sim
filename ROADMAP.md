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

## Everything outstanding, at a glance

Every item in this file has a number and a heading of its own; the numbers below link to them.
`P` is a rewrite phase, `W` work that can be written, `V` a bench check the code is waiting on,
`Q` a decision nobody has made yet. Numbers are stable identities, not a sequence — a gap means a
finished item left the file, and a new item takes the next free number rather than reusing one.

**Status.** `[ ]` not started · `[~]` in progress. There is no "done" — finished work leaves this
file. The word beside the box names what the item is waiting on, which for most of what is left is
not effort: *bench* wants the satellite or the radio, *needs a decision* wants an answer before any
code is worth writing.

| # | Status | What it is |
|---|---|---|
| [P6](#p6-the-autonomous-logging-profile) | `[~]` in progress | The autonomous logging profile — written, waiting on a walk |
| [W1](#w1-mission-export-carries-no-photographs) | `[ ]` needs a decision | A mission export carries no photographs |
| [W2](#w2-import-belongs-in-the-client-not-in-the-satellite) | `[ ]` not started | Import belongs in the client, not in the satellite |
| [W4](#w4-adcs-mounting-offset--requested-2026-09-01-to-be-done-on-the-running-satellite) | `[ ]` needs the satellite | The ADCS mounting offset, captured as data |
| [W5](#w5-a-ships-log-not-mission-events--requested-2026-09-01) | `[ ]` needs a decision | A ship's log, not "Mission Events" |
| [W10](#w10-the-walk-to-work-what-to-check-on-the-first-real-trip) | `[ ]` the trip itself | The walk to work, end to end |
| [V2](#v2-tel0157-knots-to-ms-factor) | `[ ]` bench | TEL0157 knots to m/s factor |
| [V3](#v3-tel0157-altitude-triplet-high-byte) | `[ ]` bench | TEL0157 altitude triplet high byte |
| [V5](#v5-bno055-calibration-save-and-restore) | `[ ]` not implemented | BNO055 calibration save and restore |
| [V6](#v6-networkmanager-client-mode) | `[ ]` bench | NetworkManager client mode — `EXPO` depends on it |
| [V7](#v7-sen0501-board-revision) | `[ ]` bench | SEN0501 board revision — `uv_index` stays null until then |
| [V10](#v10-whether-the-private-channel-is-relayed-at-all) | `[ ]` bench | Whether the private channel is relayed at all |
| [V13](#v13-does-the-x728-actually-charge-the-pack-and-from-which-input) | `[ ]` bench | Does the X728 actually charge the pack, and from which input |
| [V14](#v14-bno055-low-byte-bit-7-flips-reach-the-record) | `[ ]` bench | BNO055 low-byte bit-7 flips reach the record |
| [Q1](#q1-keep-the-bmp280-at-0x76-and-for-what) | `[ ]` open | Keep the BMP280 at `0x76`, and for what? |
| [Q4](#q4-may-a-safe-satellite-be-shown-in-expo) | `[ ]` open | May a `SAFE` satellite be shown in `EXPO`? |
| [Q5](#q5-a-watchdog-under-the-satellite-and-what-it-must-not-undo) | `[ ]` open | A watchdog under the satellite, and what it must not undo |

---

## 🚀 The Rewrite (Current Work)

The hardware is finished and validated; the software is being rewritten against the operating
concept. Phases are ordered so each one is independently useful and testable. Full scope and
rationale per phase: [`docs/concept.md` → Implementation plan](docs/concept.md#implementation-plan).

Everything except `P6` is done and gone from this file. `P7` was retired on 2026-09-01 (the I2C
sweep and self-test it promised are what `DEPLOY` does on every ascent); `P2` closed the same day
with the `cubesat` CLI; `P8` closed with the test sweep that removed the last places a test asserted
a shipped configuration value instead of computing from it. The radio command contract is written
down to its last command — `restart_service` was that one — and the **acks** closed on 2026-09-03:
a reply is gated on the profile rather than on the beacon flag, every command with an effect keeps
its own answer instead of being erased by the next one, and `!photo` reports the frame it took. The
`photo` field set the contract had described and never had is part of that, and on 2026-09-03 the
last open question about it was measured away in `DIAG`: the frame was ready 29 seconds before the
reply went out, so the ack window is bounded by the radio cadence rather than by the camera
(`comms/service.py` -> `_photo_fields`).

**Where the rewrite stands.** All eight services exist, at 100 % line coverage with `ruff` and
`mypy` clean, and all eight have now run on the satellite: `HOSTD`, `OBC`, `EPS` and `COMMS` in
`HOSTED` on 2026-08-31, and `ADCS`, `PAYLOAD`, `DHS` and `DASHBOARD` in `DEMO` on 2026-09-01 —
`DEPLOY` in 1.4 s, `NOMINAL`, the attitude widget and a photograph watched live. The standing caveat
has therefore moved from services to **profiles**: `FLIGHT`, `EXPO` and `DIAG` have never been
applied, and with them the access point (V6), a moving GNSS fix (V2, V3) and `diag.db` are untried.
`MAINTENANCE` was applied for the first time on 2026-09-02 — to free `/dev/serial0` for the modem
preset change — and did what it says: COMMS and the `external_units` stopped, `cubesat.local` stayed
up, and `HOSTED` brought all of it back with no `SAFE` and no lost subsystem. **Almost everything
left is a bench check or a decision.** What can still be written without the satellite: the
photographs missing from a mission export (which blocks the public demo), the export and import
verbs the archive dialog still lacks, the `photo` ack's frame fields — the last line of the radio
contract still unwritten — the chart line that should break on a gap, an end-to-end test against
the replay build. The one defect the hardware found in the logic, `restart_service` latching `SAFE`,
is fixed: `health.expect_restart` waives the departure OBC itself asked for, for one loss grace.

### P6: The autonomous logging profile

**Status:** `[~]` — written; what is left is a walk.

The code landed piecemeal: `powersave` on entering `LOW_POWER` and in `FLIGHT`, the profile TTL
armed by HOSTD and reported as `ttl_expires_at`, the radio duty-cycled by the beacon table, and the
profile itself. What no test can settle is whether the GNSS track is right —
[V2](#v2-tel0157-knots-to-ms-factor) and [V3](#v3-tel0157-altitude-triplet-high-byte) are that walk. Mains-as-signal recovery was the last piece of it and
was written on 2026-09-03 — `obc/resume.py`, and `docs/concept.md` for why — so what `P6` is
waiting on is entirely the walk, including the reset that has never happened on one.

### Mission archive: what the dialog still does not do — 2026-09-02

The dialog itself is **done**: `MISSION ARCHIVE` opens a modal listing every recorded session, and
each row replays or deletes. Deletion travels as `delete_mission` on `cubesat/command` and is
performed by DHS, which owns the file — the row goes with its detail and its photographs, refused in
`EXPO` and refused for the mission being recorded. Two verbs the original note wanted are still not
there, and both are still blocked on something structural rather than on effort.

#### W1: Mission export carries no photographs

**Export is in the dialog only as far as the endpoint goes.** `GET /api/missions/<id>/export`
returns `{mission, telemetry, attitude, radio}` as `mission-<id>.json`, and that body **carries no
photographs** — the frames live in the mission's own photo directory (`photos/<mission_id>/`, or
`photos-diag/<mission_id>/` for a `DIAG` run) and are listed separately. So a dialog
that offers "export" and hands back a file with an empty camera panel will surprise someone, and the
backend-less demo build replays a trip the same way. Closing that means either embedding the frames
(a mission's worth of JPEGs inside one JSON) or exporting a container, and that is the decision
nobody has made yet.

#### W2: Import belongs in the client, not in the satellite

**Import should probably not touch the satellite at all.** Writing a foreign mission into `comms.db`
invents history the satellite never recorded and collides with its own mission ids. The cheaper and
truer answer already exists in the client: the `replay` source, built for the backend-less public
demo, already renders a recorded mission through the same widgets as the live view. Import then
means "open this exported file as a replay source" — no write path, no ownership question, no id
collision, and it works in a browser that is not talking to a satellite at all. If import into the
archive is genuinely wanted later, it is a DHS command like delete, not an upload endpoint.

### W4: ADCS mounting offset — requested 2026-09-01, to be done on the running satellite

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
   offset does not, so the mean of the two is the mount alone.
   A `level` (or `calibrate mount`) command on the usual `cubesat/command` topic, so it works from the
   shell, the console and the radio alike, and prints the quaternion for the config file.

   Two corrections to this step, both measured on 2026-09-03 during a capture attempt that ran out
   of battery before the second position was taken.

   **Calibrate the accelerometer first — it is step zero, not a nicety.** The satellite reported
   `calib.accel = 0` at the start, and the procedure above cannot remove an accelerometer bias: the
   bias sits in the sensor frame exactly like the mount offset does, so it would be captured as part
   of it and then drift away on its own the first time the unit is carried anywhere. Bringing it to
   3/3 changed the reading from `roll 4.375° / pitch −2.0°` to `roll 0.0° / pitch −2.31°` and `|g|`
   from 0.969 to 0.991 — **an error of about 4.4°, larger than the offset being measured**. Getting
   there took two rounds of standing the cube on all six faces for 10–15 s each; the face that gets
   skipped is the ordinary upright one, because the operator starts by turning it over.

   **Median, not mean, over at least a minute per position** — [V14](#v14-bno055-low-byte-bit-7-flips-reach-the-record),
   confirmed in the same session at 4 outliers per 82 samples. Ten seconds is 20 samples at 2 Hz,
   which is one corrupted sample on average, and its 8° step is comparable to the whole quantity
   being measured. Note also that the poll rate is state-dependent: in `LOW_POWER` ADCS drops to
   0.2 Hz, so a minute buys 12 samples rather than 120.
4. **Say so on the wire.** `adcs_status` carries the offset that was applied (or that none was), so a
   consumer can tell corrected attitude from raw. Recorded missions before this change hold sensor
   frame attitude, later ones body frame — worth a line in the schema notes, not worth a migration.
5. The BNO055's own `AXIS_MAP` remap is not the tool: it swaps axes in 90° steps only.

After any reassembly the capture is repeated and the new value recorded in
`docs/hardware-bno055-bmp280-imu.md` beside the axis convention already written there.

### W5: A ship's log, not "Mission Events" — requested 2026-09-01

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

### W10: The walk to work: what to check on the first real trip

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

**And one thing to cause on purpose, once the trip is otherwise going well: pull the power.** The
resume written on 2026-09-03 (`obc/resume.py`) has never met a real reset. Hold the trip somewhere
convenient, cut power to the Pi, and watch what comes back — expected is one radio line
`boot=FLIGHT rs=1`, `FLIGHT` re-applied within seconds of EPS' first reading, and a second mission
in the archive carrying `start_reason = resume` under the same label as the first, which is closed
as `interrupted`. Three things are worth timing rather than assuming: how long EPS actually takes to
publish its first `eps_status` after a boot (the rule waits `resume.evidence_timeout_sec`, 60 s, and
withholds rather than guessing if nothing arrives), whether the `clear_resume` lands 300 s later,
and whether the mains pin reads false the instant the pack takes over — the whole decision rests on
that one bit, and a PLD pin that lags by a few seconds during the switchover would read as a desk.
Then repeat it at a desk on mains: it must stay in `HOSTED`, and say `rs=0 why=mains`.

### Bench checks the code is waiting on

Written from the drivers, which mark verified constants apart from inferred ones. Each of these
produces plausible data rather than an error, which is why none of them can be settled by reading.

Settled checks are removed from this file, and their findings live where the code that depends on
them is: the BNO055 Euler swap and the accelerometer scale in `hal/rpi/bno055.py`, the residual
bit-7 flips at 10 kHz in [`docs/hardware-bno055-bmp280-imu.md`](docs/hardware-bno055-bmp280-imu.md),
and the mosquitto ACL that must not sit in `conf.d/` in `config/mosquitto/`.

#### V2: TEL0157 knots to m/s factor

**The check.** One moving fix — a walk with the antenna out.

**Why it is not settled.** The bench reading was 0.00 knots at rest, and zero converts to zero, so
no measurement pins the factor.

#### V3: TEL0157 altitude triplet high byte

**The check.** The same walk, somewhere above 255 m.

**Why it is not settled.** The bench altitude of 116.59 m fits in one byte, so the big-endian high
byte has never been exercised.

#### V5: BNO055 calibration save and restore

**The check.** Deliberately not implemented: the profile register block is not
in the verified docs, and writing unverified registers into the fusion engine on every boot is what
produced the `SYS_ERR = 9` session already recorded there.

**Why it is not settled.** Without it the magnetometer must be re-calibrated after every reset, so
`yaw` is withheld for a while after each restart.

**What that costs is now measured, 2026-09-03.** Standing the cube on its six faces for a couple of
minutes took the sensor to a full `sys 3 / gyro 3 / accel 3 / mag 3` — the first time this project
has seen it — and `yaw` immediately started publishing (548 of 599 samples, around 181°), which is
the withholding rule working exactly as designed. It was then lost, as it always will be, at the
next stop of ADCS: a profile change to `HOSTED` is enough. So the cost of not having V5 is not only
a wait after a reset — it is that **every calibration is hand-made and unrepeatable**, and any
procedure needing a calibrated sensor (notably [W4](#w4-adcs-mounting-offset--requested-2026-09-01-to-be-done-on-the-running-satellite))
has to redo those two minutes each time, in the field included.

#### V6: NetworkManager client mode

**The check.** `nmcli connection down Hotspot` with a pinned `wlan0`.

**Why it is not settled.** Written against the documentation, never run on the Pi. `EXPO` depends on
it.

#### V7: SEN0501 board revision

**The check.** Read the silkscreen, or compare the pair of candidate values the driver
logs against a known UV source.

**Why it is not settled.** One raw register, two formulas: at raw 14 they give 0.00 and 84.35.
`uv_index` stays null until this is settled.

#### V10: Whether the private channel is relayed at all

**The check.** *The hop arithmetic itself is settled.* `hops = hopStart − hopLimit` was measured on
2026-09-02: one NodeInfo broadcast reported by 72 bayme.sh gateways, `hopStart` fixed at 6 and
`hopLimit` arriving as everything from 6 to 0 (see
[`docs/hardware-heltec-lora32-v4.md`](docs/hardware-heltec-lora32-v4.md) → Coverage). The check that
remains is on **channel 1**, and it is *not* "get far away and look for `hops ≥ 1`". **The
operator's own node holds the `CubeSat` key, so it relays that channel like any other — and it was
observed doing exactly that on the primary channel on 2026-09-02** (a chat message arriving with
`hops: 3` at −23 dBm, the last relay being the personal node one room away). A hop counted through
your own house proves nothing about strangers, and a passing hop count cannot tell the two apart. So
the measurement is a **traceroute on channel 1**: the reply carries the route as a list of node ids
— the same shape `Troy` produced when it trace-routed this satellite — and the question is whether a
node that is *not* the operator's appears in it. It is answered by the firmware itself, so it needs
no COMMS and works in any profile where the node is powered.

**Why it is not settled.** All of that measurement is the *primary* channel, and telemetry and
commands do not travel there. A foreign node rebroadcasts a packet it cannot decrypt only while its
`rebroadcast_mode` is the default `ALL`; one set to `LOCAL_ONLY` drops `CubeSat` without a trace. So
the satellite can sit in a public node list, relayed six hops, having proved nothing about the
channel `FLIGHT` depends on.

#### V13: Does the X728 actually charge the pack, and from which input

**The gauge drift is settled and is no longer part of this item.** Measured 2026-09-03 across a
deliberate unplug: on mains the terminal voltage held flat at 0 mV/h while the gauge's modelled SOC
fell at 8–10 %/h; unplugged, the voltage dropped 50 mV at once and then fell at −197 mV/h. This part
has no current sense, so its state of charge is a reconstruction, and a rate fitted to it describes
the model settling. That closed the 2026-08-31 "SOC down while voltage rose" mystery, and it found a
defect on the way: `on_mains` rested on that rate alone, so a plugged-in satellite read as being on
battery and was hours from a `CRITICAL` poweroff on a desk. The fix — `voltage_rate` beside
`charge_rate`, the measured slope deciding and the modelled one confirming — is in
`obc/power_policy.py`, with the full series in `docs/hardware-x728-ups-hat.md`.

**What is left is the original question: the pack charges slowly, and nobody knows why.** Measured
2026-09-01: CanaKit 5.1 V / 3.5 A on the X728's USB-C input, LEDs one steady one blinking, SOC
50.39 → 50.78 % in fifteen minutes, voltage +17 mV — about +3 %/h, on the order of 150 mA against
the 2.3–3.2 A the board advertises. At that rate 50 → 100 % takes about seventeen hours.

**The check.** A 5.1 V, ≥ 4 A supply on the **DC 5.5 × 2.1 jack** — the input Geekworm rates for the
full charging current, and the one input never yet tried. Watch `voltage` and `voltage_rate` for ten
minutes: a charger delivering real current lifts the terminal voltage within seconds, and that is
now a quantity worth watching, unlike the percentage. If the rate does not change, the remaining
candidates are the charger stage and the cells themselves — unbranded 18650s that no longer hold a
charge look exactly like this. Ruled out already: the `CHG Ctrl` jumper is installed (2026-09-01),
so charging is not gated by a floating GPIO16, and `vcgencmd get_throttled` reads `0x0`, so the 5 V
output is not sagging. (Opening that jumper on purpose stays an opportunity rather than a fix:
GPIO16 would let EPS hold a partial charge on the desk and top up before a `FLIGHT`.)

**Why it is not settled.** Nothing errors and nothing looks broken: the LEDs agree, the dashboard
shows a plausible level, and the satellite runs happily on mains. It is discovered at the worst
possible moment — leaving for a trip with a pack that was never full — which is precisely the
failure `FLIGHT` cannot afford.

#### V14: BNO055 low-byte bit-7 flips reach the record

**The check.** Measured 2026-09-01 in `DEMO` at rest: about one published sample in twenty carries
an undetectable +128 LSB step — `gyro_x` exactly 8.0 °/s between neighbours of 0.06, `acc_x` 0.07 →
0.20 g and back — while the detectable high-byte flips ran at one read in eleven, all caught. The
plausibility check cannot see these by construction. The check the hardware doc has asked for since
2026-08-28: move the sensor to a bit-banged `i2c-gpio` bus, which honours clock stretching, and
measure both rates again. Until then a median-of-three in the driver would hide the isolated ones at
the cost of half a second of latency — a decision, not a fix.

**Confirmed again 2026-09-03, and this time in the Euler angles.** With the satellite standing
still on a desk and the magnetometer freshly calibrated, `yaw` alternated between **181.125° and
189.125°** — a difference of exactly 8.000°, which is 128 LSB at the Euler scale of 1/16° per LSB.
The same reading in the same session put the rate at **4 outliers in 82 published `accel_g` samples**
(deviations above 50 mg from the median), i.e. one in twenty, matching 2026-09-01 exactly. So the
defect is stable, it is not confined to one register block, and it reaches every consumer of
attitude — the heading included, which is the field an operator is most likely to believe.

**What it forces on anything that averages this sensor: take the median, never the mean.** Measured
in the same series — over 82 samples the median and the mean of `accel_g` differed by 1–2 mg, small
only because the outliers were few; a single 8° step inside a 20-sample window is comparable to the
whole quantity [W4](#w4-adcs-mounting-offset--requested-2026-09-01-to-be-done-on-the-running-satellite)
is trying to measure. Any capture procedure built on this sensor therefore averages by median over a
window of at least a minute, and W4's own text says so at its capture step.

**Why it is not settled.** They are physically plausible values and DHS writes them into `attitude`
as measured, so a replay shows an 8° twitch, a 0.13 g kick, or an 8° turn of heading that never
happened.

---

### Decisions still open

Tracked in [`docs/concept.md` → Open questions](docs/concept.md#open-questions):

#### Q1: Keep the BMP280 at `0x76`, and for what?

`BMP280` at `0x76` duplicates the SEN0501 pressure reading — keep it, and for what? Log both and
compare over a few sessions; `DIAG` is the natural place, now that it rehearses a real mission.

**Blocks:** —

#### Q4: May a `SAFE` satellite be shown in `EXPO`?

May a human move a fault-latched (`SAFE`) satellite into `EXPO` to show it to an audience? Probably
yes, with the fault displayed — but it needs deciding rather than falling out of the implementation.

**Blocks:** —

#### Q5: A watchdog under the satellite, and what it must not undo

Raised 2026-09-03, from reading how CubeSat flight computers survive on commercial silicon: the
processor is an ordinary Compute Module, and what makes it flyable is the obvious things around it —
a latch-up-aware power switch and a watchdog that does not run on the processor it is watching. Two
of the three levers below already exist on this Pi and cost one line each; none of them is used.

The BCM SoC carries a hardware watchdog in its power-management block (`bcm2835_wdt`,
`/dev/watchdog0`, enabled by default on current firmware, **maximum ~15 s** — the counter is in the
SoC and does not tick longer). Three separable decisions, not one:

1. **The SoC timer under PID 1.** `RuntimeWatchdogSec=14` in `/etc/systemd/system.conf`, and
   systemd pets it at half the interval. It catches a locked kernel or a wedged PID 1 and nothing
   else: a hung *service* leaves a healthy systemd petting happily.
2. **A per-unit watchdog.** `WatchdogSec=` with `Type=notify` and a `WATCHDOG=1` datagram on
   `NOTIFY_SOCKET` — a few lines of `socket`, no new dependency, and **one place to put them**,
   because every service already publishes a heartbeat on a fixed interval in `common/service.py`.
   Two things it does not buy, both of which are existing rules rather than new caveats. It proves a
   process, never its hardware, so it sits *under* OBC's health verdict and not in place of it. And a
   restart OBC did not ask for is not covered by `health.expect_restart` — it reads as a lost
   subsystem and latches `SAFE`, which is the correct reading for a service wedged badly enough to be
   killed from outside, and must not be "fixed" by widening that grace.
3. **An external watchdog.** A $2 MCU on the reset line, independent of the SoC. The only one of the
   three that survives a processor which has stopped answering at all — which is also the only
   failure the other two cannot see. Out of scope until the first two are decided, but it is the one
   that would matter.

**What has to be settled before any of it lands, and it is a bench check rather than a reading:
what a watchdog does to `CRITICAL`.** `CRITICAL` is the one state permitted to power the host down,
and a `poweroff` that stalls past the timeout would be *undone* by a reset — the satellite comes back
up on a flat pack, in exactly the situation the shutdown exists to prevent. systemd has
`RebootWatchdogSec=` for the shutdown path and the driver reports magic-close support, but which of
those actually governs a `poweroff` on this Pi is not something to infer from documentation. Arm the
watchdog, take the satellite into `CRITICAL`, and watch whether it stays down.

**Blocks:** —

---

## Notes

- The phase order in **The Rewrite** is a dependency order, not a preference: `P6` is what is left
  of it.
- **This file holds only what is outstanding.** The pre-rewrite bug, configuration and refactoring
  logs that used to live here are gone: the code they describe no longer exists, and
  `docs/code_smells.md` and `docs/architecture.md` keep the detailed write-ups as the historical
  record they are. Closed decisions are in
  [`docs/concept.md`](docs/concept.md); settled hardware findings are in the relevant
  `docs/hardware-*.md` and at the constants they justify.
