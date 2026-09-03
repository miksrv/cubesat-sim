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
| [W3](#w3-photo-directories-collide-across-the-two-databases) | `[ ]` not started | Photo directories collide across the two databases — a live hazard |
| [W4](#w4-adcs-mounting-offset--requested-2026-09-01-to-be-done-on-the-running-satellite) | `[ ]` needs the satellite | The ADCS mounting offset, captured as data |
| [W5](#w5-a-ships-log-not-mission-events--requested-2026-09-01) | `[ ]` needs a decision | A ship's log, not "Mission Events" |
| [W6](#w6-the-mesh-preset-both-nodes-are-moved--2026-09-02) | `[ ]` bench | Audit both mesh nodes against each other |
| [W8](#w8-an-answer-is-not-a-beacon-beacon-must-ration-the-schedule-only--decided-2026-09-02) | `[ ]` not started | An answer is not a beacon: `beacon` rations the schedule only |
| [W9](#w9-and-the-answer-has-to-say-what-happened--same-decision-2026-09-02) | `[ ]` not started | The ack must say what happened, and carry the `photo` fields |
| [W10](#w10-the-walk-to-work-what-to-check-on-the-first-real-trip) | `[ ]` the trip itself | The walk to work, end to end |
| [V2](#v2-tel0157-knots-to-ms-factor) | `[ ]` bench | TEL0157 knots to m/s factor |
| [V3](#v3-tel0157-altitude-triplet-high-byte) | `[ ]` bench | TEL0157 altitude triplet high byte |
| [V5](#v5-bno055-calibration-save-and-restore) | `[ ]` not implemented | BNO055 calibration save and restore |
| [V6](#v6-networkmanager-client-mode) | `[ ]` bench | NetworkManager client mode — `EXPO` depends on it |
| [V7](#v7-sen0501-board-revision) | `[ ]` bench | SEN0501 board revision — `uv_index` stays null until then |
| [V10](#v10-whether-the-private-channel-is-relayed-at-all) | `[ ]` bench | Whether the private channel is relayed at all |
| [V13](#v13-the-x728-charge-rate-on-mains) | `[ ]` bench | The X728 charge rate on mains |
| [V14](#v14-bno055-low-byte-bit-7-flips-reach-the-record) | `[ ]` bench | BNO055 low-byte bit-7 flips reach the record |
| [V15](#v15-the-channel-index-a-received-packet-reports) | `[ ]` bench | The channel index a received packet reports — the uplink filter rests on it |
| [Q1](#q1-keep-the-bmp280-at-0x76-and-for-what) | `[ ]` open | Keep the BMP280 at `0x76`, and for what? |
| [Q2](#q2-recovering-a-trip-after-an-unexpected-reset) | `[ ]` open | Recovering a trip after an unexpected reset |
| [Q4](#q4-may-a-safe-satellite-be-shown-in-expo) | `[ ]` open | May a `SAFE` satellite be shown in `EXPO`? |

---

## 🚀 The Rewrite (Current Work)

The hardware is finished and validated; the software is being rewritten against the operating
concept. Phases are ordered so each one is independently useful and testable. Full scope and
rationale per phase: [`docs/concept.md` → Implementation plan](docs/concept.md#implementation-plan).

Everything except `P6` is done and gone from this file. `P7` was retired on 2026-09-01 (the I2C
sweep and self-test it promised are what `DEPLOY` does on every ascent); `P2` closed the same day
with the `cubesat` CLI; `P8` closed with the test sweep that removed the last places a test asserted
a shipped configuration value instead of computing from it. The radio command contract is written
down to its last command — `restart_service` was that one — and what is outstanding is now the
**acks**: whether they are transmitted at all, and whether they say what happened. That includes the
`photo` field set the contract describes and has never had. Both live in [W8](#w8-an-answer-is-not-a-beacon-beacon-must-ration-the-schedule-only--decided-2026-09-02)
and [W9](#w9-and-the-answer-has-to-say-what-happened--same-decision-2026-09-02).

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
contract still unwritten — the chart line that should break on a gap, and an end-to-end test against
the replay build. The one defect the hardware found in the logic, `restart_service` latching `SAFE`,
is fixed: `health.expect_restart` waives the departure OBC itself asked for, for one loss grace.

### P6: The autonomous logging profile

**Status:** `[~]` — written; what is left is a walk.

The code landed piecemeal: `powersave` on entering `LOW_POWER` and in `FLIGHT`, the profile TTL
armed by HOSTD and reported as `ttl_expires_at`, the radio duty-cycled by the beacon table, and the
profile itself. What no test can settle is whether the GNSS track is right —
[V2](#v2-tel0157-knots-to-ms-factor) and [V3](#v3-tel0157-altitude-triplet-high-byte) are that walk. Mains-as-signal recovery is deliberately still open as
[Q2](#q2-recovering-a-trip-after-an-unexpected-reset).

### Mission archive: what the dialog still does not do — 2026-09-02

The dialog itself is **done**: `MISSION ARCHIVE` opens a modal listing every recorded session, and
each row replays or deletes. Deletion travels as `delete_mission` on `cubesat/command` and is
performed by DHS, which owns the file — the row goes with its detail and its photographs, refused in
`EXPO` and refused for the mission being recorded. Two verbs the original note wanted are still not
there, and both are still blocked on something structural rather than on effort.

#### W1: Mission export carries no photographs

**Export is in the dialog only as far as the endpoint goes.** `GET /api/missions/<id>/export`
returns `{mission, telemetry, attitude, radio}` as `mission-<id>.json`, and that body **carries no
photographs** — the frames live under `photos/<mission_id>/` and are listed separately. So a dialog
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

#### W3: Photo directories collide across the two databases

**A photo directory is named after a mission id, and there are two id spaces.** `comms.db` and
`diag.db` each number their missions from 1, while photographs are filed in one
`photos/<mission_id>/` for both — so `DIAG` mission 3 and `FLIGHT` mission 3 share a directory.
Retention has always had this (it removes the directory of any mission it ages out, in whichever
database it is running against), but a person deleting a `DIAG` bench run from the dialog can now
reach a `FLIGHT` trip's photographs in one click, which is a much easier way to meet it. The fix is
to file frames under the database as well as the mission — `photos/<db>/<mission_id>/` — which is a
migration of what is on the card, PAYLOAD's `path_for`, retention's fence and the dashboard's two
photo routes. **Until then, do not delete a `DIAG` mission whose id also exists in `comms.db`.**

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
   offset does not, so the mean of the two is the mount alone. Ten seconds of samples each, averaged.
   A `level` (or `calibrate mount`) command on the usual `cubesat/command` topic, so it works from the
   shell, the console and the radio alike, and prints the quaternion for the config file.
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

### W6: The mesh preset: both nodes are moved — 2026-09-02

The satellite's Heltec now runs `MEDIUM_FAST` on frequency slot 45, `hop_limit 6`,
`config_ok_to_mqtt true`, region `US` unchanged, channel 1 `CubeSat` and its PSK verified intact
after the write. Why that is the physical layer rather than a preference, what the automatic
frequency slot does when the preset name changes underneath it, and why the MQTT flag is what makes
the node visible at all are recorded in
[`docs/hardware-heltec-lora32-v4.md`](docs/hardware-heltec-lora32-v4.md) → Modem preset and the
local mesh, with the commands.

The personal node — the one node the satellite's radio has ever been tested against — was moved to
the same settings the same day, so there is a ground link again: nodes on different presets do not
demodulate each other at all.

**What is left.**

1. **Read both nodes back with `--info` and compare them to each other, not to a list.** Preset,
   `channel_num`, `hop_limit`, region, and channel 1 `CubeSat` with its PSK on both. The personal
   node has been configured by hand over several sessions and has never been audited against the
   satellite; a setting that drifted there is indistinguishable from a satellite fault.
2. ~~Watch `meshview.bayme.sh/nodelist` for `CSAT`.~~ **Done, same evening.** The node was there
   within a minute, 72 gateways published its first broadcast, a stranger traceroutes it, and the
   hop arithmetic behind `radio_log.hops` is measured rather than inferred — the numbers and what
   they do and do not prove are in
   [`docs/hardware-heltec-lora32-v4.md`](docs/hardware-heltec-lora32-v4.md) → Coverage. **The one
   finding worth acting on: exactly one node hears the satellite directly** (W6SRR Sunol Ridge, a
   bayme.sh router, −5.25 dB SNR / −94 dBm from indoors). The 72 are what the mesh does after that
   single link, so the reading to repeat is the *direct* receivers from where `FLIGHT` actually
   goes.

### W8: An answer is not a beacon: `beacon` must ration the schedule only — decided 2026-09-02

**The rule.** COMMS answers a command whenever it is in a position to hear one. `beacon` governs one
thing and one thing only: whether *state* goes out on a schedule, unasked. So `_maybe_ack` is gated
on `lora_listening` — the profile's envelope — and not on `lora_enabled`.

That is not a new principle, it is an existing one applied consistently. `_going_down_beacon` is
already written this way, and its docstring says why: *"Gated on ``lora_listening`` — the profile —
and **not** on ``lora_enabled``. Same reasoning as the inbox: a runtime flag somebody set an hour ago
should not be able to silence the one message that explains a disappearance. A profile that forbids
the radio still says nothing, because there the radio is not part of the mission at all."* Replace
"the one message that explains a disappearance" with "the answer to a question somebody just asked"
and the sentence still holds. The satellite currently has two rules for two transmissions that
deserve the same one.

**Three instances, all met inside five minutes on 2026-09-02**, with the satellite in `DEMO` (where
`beacon: false` is the profile's own default) and the operator on a phone one room away:

- `!sys` arrived, was answered from the caches, and the reply was dropped. The command worked; from
  the phone it was indistinguishable from a satellite that is not listening.
- `!beacon off` arrived and did its job — and its own confirmation was dropped *by the flag it had
  just set*, so "the transmitter is now off" and "the command never arrived" look identical. This is
  the worst of the three: the operator is left unable to tell whether the satellite is quiet or gone.
- `!photo` arrived and PAYLOAD took the photograph. A command with a **physical side effect**, and
  no way to learn from the radio that it happened.

The `!` contract in `compact.py` already refuses this outcome for a line that fails to parse —
answered *"because its sender meant to command and is standing in a field wondering why nothing
happened"* — while a line that parses gets nothing. A satellite that answers typos and swallows
successes has the priority backwards.

**The flag has to be renamed with the change**, or the same fault is simply moved along one step:
`beacon on|off` was itself renamed from `lora on|off` on 2026-09-01 *because the old name said the
wrong thing*, and a flag named `lora_enabled` that no longer governs whether LoRa transmits at all
is that same lie. It becomes `beacon_enabled`, in `set_comms_config` and in `comms_status`, with
`lora_enabled` accepted as an alias on the way in — the dashboard build deployed on 2026-09-02 sends
the old spelling, and the satellite must not stop answering its own console the day this lands.
Everything that names it: `comms/compact.py` (the two `_beacon` builders), `comms/service.py`
(`_set_config`, the `lora_enabled` property, `_publish_status`), `cli/main.py` and
`cli/commands/status.py`, `README.md` (the payload schema at the COMMS section, the
`lora_enabled`/`lora_listening` explanation, and the vocabulary table's `set_comms_config` row),
`CLAUDE.md` → Quiet is not deaf, the `DEMO` and `EXPO` comments in `profiles.yaml`, and in the
groundstation repo `QuickCommandsWidget`, `MissionConsoleWidget` and the `CommsStatus` type.

**The order this depended on is satisfied.** The uplink channel filter landed on 2026-09-03, so
only a holder of the `CubeSat` key can ask a question at all; until then "always answer a command"
would have meant any stranger in the community chat could make this satellite transmit. That filter
is what makes this rule safe rather than merely nicer, and it is now in place.

**Where "always" stops, stated so it is not read as absolute.** The gate is `lora_listening`, so a
profile with `downlink.lora: false` still says nothing — there the radio is not part of the mission
and the inbox is not even polled. Today that is `MAINTENANCE` alone, which runs no COMMS at all, so
in practice the rule reads: **every profile that runs COMMS answers commands.**

**What this deliberately gives up.** "Listen, but transmit nothing under any circumstances" stops
being reachable at runtime; the only route to it becomes a profile that also closes the way in. That
is accepted rather than overlooked: once only channel members can ask, silence is achieved by not
asking.

#### W9: And the answer has to say what happened — same decision, 2026-09-02

Transmitting the ack is half of it. **Every valid command uplinked over the radio is confirmed, and
the confirmation names the outcome rather than merely proving something was received.** The delay
exists for exactly this and its docstring already says so: *"by the time it goes out, ``st=`` and
``pr=`` show what the command actually did, which is more honest than an ``ok=1`` COMMS cannot vouch
for."* For a profile switch that is genuinely enough — `st=` and `pr=` *are* the outcome. For a
command whose result is not a state field, it is not: `!photo` was answered with the ordinary
telemetry line and nothing about the photograph (met on the hardware, 2026-09-02 — the frame was
visible in the dashboard while the radio said nothing about it).

This absorbs the `photo` field set that has been outstanding since the contract was written, and it
is cheaper than it looks, because **nothing needs measuring — it is all already on the bus.**
`payload_photo` carries `kind`, `file`, `size_bytes`, `mission_id` and `sequence`; the operator's
request is the size in KB, and the contract in [`docs/concept.md` → The radio command
contract](docs/concept.md#the-radio-command-contract) asks for the frame number and the free
megabytes. All three are available. What is missing is only that **COMMS is not subscribed to
`payload_photo`** — its `subscriptions` are `command`, `eps_status`, `adcs_status`, `payload_data`,
`dhs_status`, plus `obc_status` from the base class — so today it cannot know a photograph happened
even in principle.

**The trap in that subscription, which must not be walked into:** `payload_photo` also carries
`photo_base64` — the whole image — and COMMS keeps "the latest payload from each subsystem, kept
whole". Subscribing the ordinary way would park a base64 copy of every frame in the link service's
memory, for the sake of two integers. Keep the small fields, drop the image on arrival, and say so
at the handler.

Two smaller things belong in the same pass:

- **`re=` must name the verb the operator typed.** `beacon on` came back as `re=set_comms_config`
  (observed 2026-09-02). `compact.py` already states the intent for the queries — *"the short
  ``re=`` name a phone reader sees — the verb they typed, not the canonical command it became"* —
  and simply does not apply it to a relayed command, because `Compact` carries the canonical name
  and not the spelling. The spelling has to travel with the translation.
- **`ok=`/`err=` today appear "only where COMMS itself is the handler and actually knows"**, which
  is right and does not need weakening: the general shape is that the ack reads the *handler's own*
  message. `take_photo` → `payload_photo`; `set_profile` → the state fields it already carries;
  `safe`/`recover` → `st=`; `restart_service` → what HOSTD reports. Where a handler says nothing an
  ack can read, the honest answer stays the state line, and that is a gap to name per command rather
  than to paper over with an invented `ok=1`.

The 240-byte rule is unaffected: reply fields are already `protected` in `_fit`, so the routine
telemetry gives way to them rather than the line being truncated.

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

#### V13: The X728 charge rate on mains

**The X728 charges on mains, but at ~+3 %/h — a fraction of its rated 2.3–3.2 A.** Measured
2026-09-01 with `charge_rate` a real quantity (see `docs/hardware-x728-ups-hat.md`): CanaKit 3.5 A
on USB-C, LEDs one steady one blinking, SOC 50.39 → 50.78 % in fifteen minutes, voltage +17 mV. Next
check: a 5.1 V ≥ 4 A supply on the DC jack, the input Geekworm rates for full charging current; if
the rate does not change, the cells or the charger stage are the question. The original observation
follows for the record. Observed 2026-08-31 with the satellite plugged in all evening: 3.92 V, SOC
~66 %, `CRATE` one LSB from zero, and the board's own charge LEDs agreeing with roughly that level.
The datasheet figures in `docs/hardware-x728-ups-hat.md` say the recharge threshold is **4.1 V** and
cutoff 4.24 V — at 3.92 V the board is well past the point where it should have resumed, so "it
deliberately holds a partial charge" does not explain this reading. The **`CHG Ctrl` jumper was
checked on 2026-09-01 and is installed**, which per Geekworm means automatic charging whenever the
adapter is connected — so a floating GPIO16 is ruled out. Two candidates remain. First, the
**supply**: the X728 charges only from its own DC jack and wants 2.3–3.2 A for the pack on top of
the Pi's load; `PLD` proves the jack sees power, not enough of it. Second, tired 18650s: capacity
loss shows up as a cell that will not hold a charge, and the pack is unbranded.
`vcgencmd get_throttled` reads `0x0`, so the 5 V supply is not sagging and can be ruled out. **The
decisive check is now cheap, because `charge_rate` is a real measurement:** plug in and watch
`voltage` and `charge_rate` for ten minutes — a charger delivering current lifts the terminal
voltage within seconds and turns the rate positive after the five-minute window. (Opening the jumper
on purpose stays an opportunity: GPIO16 would let EPS hold a partial charge on the desk and top up
before a `FLIGHT`. A driver and a policy, later).

**Why it is not settled.** Nothing errors and nothing looks broken: the dashboard shows a plausible
66 %, the LEDs agree, and the satellite runs happily on mains. It is discovered at the worst
possible moment — leaving for a trip with a pack that was never full — which is precisely the
failure `FLIGHT` cannot afford. While charging is in doubt, the 2026-08-31 mains-day drift (SOC down
while voltage rose) cannot be interpreted either — that drift is now this item's question, since the
"70× disagreement" it was filed under turned out to be a comparison against a constant (see
`docs/hardware-x728-ups-hat.md`, 2026-09-01).

#### V14: BNO055 low-byte bit-7 flips reach the record

**The check.** Measured 2026-09-01 in `DEMO` at rest: about one published sample in twenty carries
an undetectable +128 LSB step — `gyro_x` exactly 8.0 °/s between neighbours of 0.06, `acc_x` 0.07 →
0.20 g and back — while the detectable high-byte flips ran at one read in eleven, all caught. The
plausibility check cannot see these by construction. The check the hardware doc has asked for since
2026-08-28: move the sensor to a bit-banged `i2c-gpio` bus, which honours clock stretching, and
measure both rates again. Until then a median-of-three in the driver would hide the isolated ones at
the cost of half a second of latency — a decision, not a fix.

**Why it is not settled.** They are physically plausible values and DHS writes them into `attitude`
as measured, so a replay shows an 8° twitch or a 0.13 g kick that never happened.

#### V15: The channel index a received packet reports

**The check.** Send one line to the satellite on the primary channel and one on channel 1
`CubeSat` — from the operator's node or the phone app — and log the packet dict each arrives as.
What is being read is the `channel` key: expected present as `1` on the private channel and
**absent**, not `0`, on the primary. A direct message is worth a third line, since it is refused by
the same rule and nobody has looked at what it carries either.

**Why it is not settled.** The absence is inferred from protobuf omitting a zero field, not from a
packet anyone has read — the marker is at `CHANNEL_KEY` in `hal/rpi/meshtastic_radio.py`, and the
uplink channel filter landed on 2026-09-03 resting its whole discrimination on it
(`comms/service.py` → `_refuse_uplink`). The two ways it can be wrong are not symmetric. If the key is present as
`0` on the primary, nothing changes: `0` is not the command channel either way. If it is absent on
*both*, every message reads as the primary and the satellite goes deaf to its own uplink — loud,
immediate, and recoverable, which is why the inference was written to fail in that direction rather
than towards a public command channel. What would be silent is the reverse assumption, so it is not
the one in the code.

---

### Decisions still open

Tracked in [`docs/concept.md` → Open questions](docs/concept.md#open-questions):

#### Q1: Keep the BMP280 at `0x76`, and for what?

`BMP280` at `0x76` duplicates the SEN0501 pressure reading — keep it, and for what? Log both and
compare over a few sessions; `DIAG` is the natural place, now that it rehearses a real mission.

**Blocks:** —

#### Q2: Recovering a trip after an unexpected reset

The profile is deliberately not persisted, so a brownout mid-trip silently ends the recording.
Fixing it without a stored profile means acting on a **boot reason** — mains absent at boot means
the satellite is demonstrably not on a desk. Deferred until `FLIGHT` has seen enough use to know
whether spurious resets happen at all.

**Blocks:** [P6](#p6-the-autonomous-logging-profile)

#### Q4: May a `SAFE` satellite be shown in `EXPO`?

May a human move a fault-latched (`SAFE`) satellite into `EXPO` to show it to an audience? Probably
yes, with the fault displayed — but it needs deciding rather than falling out of the implementation.

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
