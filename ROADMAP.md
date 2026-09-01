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

`P0`–`P5` are done and gone from this table; `P7` was retired on 2026-09-01 (the I2C sweep and
self-test it promised are what `DEPLOY` does on every ascent — see `docs/concept.md`). `P2` closed
the same day with the `cubesat` CLI: `profile`, `status`, `mission list` and `beacon on|off`, written
and tested but **not yet run on the Pi**.

| Phase | Scope | Delivers | Status |
|---|---|---|---|
| **P6** | Power saving; profile TTL; mains-as-signal recovery; GNSS track verified end to end; profile `FLIGHT` | The autonomous logging profile | `[ ]` |
| **P8** | Docs and tests kept in line as each change lands, rather than as a phase of its own. What is genuinely left is the sweep for tests that assert a *production* config value instead of computing from it: `hostd` pins its own registry, and the `obc`/`common` cases were done on 2026-09-01. `comms` has at least one left — `config.BEACON_INTERVALS["SAFE"] == 600` — and each such line needs deciding rather than rewriting: some are a claim about the shipped table (SAFE must have a beacon at all), which is worth keeping, and some only repeat a number | Tests that do not break when a legitimate setting changes | `[~]` |

### The radio command contract: what is left of it

The contract itself is [`docs/concept.md` → The radio command
contract](docs/concept.md#the-radio-command-contract). Shipped and gone from this table: the compact
`!` syntax canonicalised before the relay (`comms/compact.py`, with the bare spelling accepted since
2026-08-31), the one-ack rule, the four query verbs COMMS answers itself, and the airtime budget —
which is one pending ack slot rather than a queue, so a burst of commands costs one transmission and
`down=1` leaves immediately on its own thread.

| # | Work | Status |
|---|---|---|
| R5 | `restart_service` through OBC → HOSTD, validated against the known services — the allowlist and the denied set already bound what it can reach | `[ ]` |
| R6 | README: document the radio command table as shipped behaviour, once `R5` lands and the vocabulary stops changing | `[ ]` |

**Where the rewrite stands.** All eight services exist, at 100 % line coverage with `ruff` and
`mypy` clean. Four of them — `HOSTD`, `OBC`, `EPS`, `COMMS` — have run against real hardware;
`ADCS`, `PAYLOAD`, `DHS` and `DASHBOARD` have not, which `CLAUDE.md` states as the standing caveat
it is. What is left is the `cubesat` CLI, `P6`, `P8`, and the bench work below.

### Mission archive as a real dialog — requested 2026-08-31

`MISSION ARCHIVE` today opens an inline list inside `MissionTimelineBar` (`phase === 'picking'`)
whose only action is "replay this one". Wanted: a large modal that lists the missions and lets an
operator pick one, delete it, export it, or import one. Export already exists —
`GET /api/missions/<id>/export` returns `{mission, telemetry, attitude, radio}` as
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

**One gap to close either way:** the export carries no photographs. The body is
`{mission, telemetry, attitude, radio}`, while frames live under `photos/<mission_id>/` and are
listed separately — so an archive dialog that exports a mission and gets no pictures back will
surprise someone, and the backend-less demo build replays a trip with an empty camera panel.

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
- **Every photograph of the trip is a cold capture** (V11): 300 s between frames against a 60 s
  `camera.idle_close_sec`.
- **Battery** — V12 and V13. An hour each way should be comfortable, but the gauge's SOC is not
  currently trustworthy and it is not established that the pack charges on mains at all.
- **The beacon reset on arrival.** Switching to `DEMO` is what silences it; if it keeps beaconing at
  a desk, the reset in `_reconcile_downlink` did not happen.
- **`cubesat` itself has never run on the Pi.** It is a console script from `pip install -e .`, so
  the first thing to find out is whether the entry point is even on the operator's `PATH` after the
  install.

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
| V11 | **Exposure of the first capture after an idle close.** The camera now gives the sensor back after `camera.idle_close_sec` of no captures (the ISP pipeline is SoC heat while open), so a cold `take_photo` re-opens and shoots within about a second. Whether Picamera2's AE/AWB have converged by then is read from nothing — the lores stream exists precisely to run those loops, but how long they need after `start()` is not in our docs. The check: one photo warm, one photo cold, same scene | A dark or colour-cast first frame is plausible wrong data, not an error; if the bench shows it, the fix is a short settle delay after a cold open, and its length is a measurement, not a guess |
| V12 | **MAX17048 state of charge disagrees with its own charge rate — by ~70×.** First hardware run, on mains: `battery_percent` fell 72.4 → 70.3 in eight minutes (≈ −15 %/h) while `voltage` sat unmoved at 3.905–3.906 V and `CRATE` read −0.208 %/h — which is **exactly one LSB** (the register's resolution is 0.208 %/h), the same value the bench recorded at rest on 2026-08-23, so the gauge is saying "about zero", not measuring a discharge. A pack actually losing 15 %/h moves its terminal voltage, so it is the SOC register that is wrong, not the battery — but which way is not decidable from eight minutes. **Confirmed over 85 minutes the same evening: voltage went *up* 3.905 → 3.920 V while SOC went *down* 72.41 → 66.14 %.** The two move in opposite directions, which no discharging cell does, so the reading is not merely imprecise — it is describing something that is not happening. Whether the cell is also being charged is the separate question in V13. Candidates, in order: the gauge is still relaxing after power-on and has no quick-start; `SOC_LSB_PER_PERCENT` is wrong (it would have to be wrong in a way that still lands near a plausible 70 %, so this is the weakest); the pack really is discharging on mains because the X728 is not charging it. The check: leave the satellite on mains for several hours logging `voltage`, `battery_percent` and `charge_rate` together, then repeat on battery. **Do not touch the constant until the log says which** | Nothing errors: `on_mains` currently holds, because −0.208 is above `DRAINING_PERCENT_PER_HOUR` (−1.0), so every power-driven descent stays suppressed and the false SOC is inert. It stops being inert the moment `CRATE` crosses −1.0 — then the pin-plus-rate agreement that protects a plugged-in satellite breaks, and a SOC that reaches 10 % takes the host down for real |
| V13 | **The X728 does not appear to be charging on mains, and it should be.** Observed 2026-08-31 with the satellite plugged in all evening: 3.92 V, SOC ~66 %, `CRATE` one LSB from zero, and the board's own charge LEDs agreeing with roughly that level. The datasheet figures in `docs/hardware-x728-ups-hat.md` say the recharge threshold is **4.1 V** and cutoff 4.24 V — at 3.92 V the board is well past the point where it should have resumed, so "it deliberately holds a partial charge" does not explain this reading. Two candidates, cheap to separate. First, the **`CHG Ctrl` jumper**: on the V2.5 board, opening it hands charge control to **GPIO16**, and nothing in this repo drives that pin (the only GPIO the code touches is `PLD_PIN = 6`, and it only reads it) — so if the jumper is open, charging is simply disabled by a floating input. Check the jumper physically first. Second, tired 18650s: capacity loss shows up as a cell that will not hold a charge, and the pack is unbranded. `vcgencmd get_throttled` reads `0x0`, so the 5 V supply is not sagging and can be ruled out. **If the jumper turns out to be the answer, it is an opportunity rather than a defect** — GPIO16 would let EPS decide when to charge, which is exactly the wanted behaviour: hold a partial charge on the desk, where a Li-ion cell ages fastest sitting full, and top the pack up deliberately before a `FLIGHT` outing. That would be a driver and a policy, not a jumper left closed | Nothing errors and nothing looks broken: the dashboard shows a plausible 66 %, the LEDs agree, and the satellite runs happily on mains. It is discovered at the worst possible moment — leaving for a trip with a pack that was never full — which is precisely the failure `FLIGHT` cannot afford. Note this is upstream of V12: while charging is in doubt, the gauge's drift cannot be interpreted either |

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

Not code — the box in the corner. Each of these is a leftover of the hardware that came off it.

- **`dtoverlay=sc16is752-i2c` should come out of `/boot/firmware/config.txt`.** It served the 52Pi
  IoT Node(A)'s UART bridge, which is gone; it conflicts with nothing only because no `ttySC*`
  devices are created any more.
- **The old `cubesat-telemetry.service` unit should be removed** if it is still installed, along with
  any `.env` still using `TELEMETRY_*` names — the service is `COMMS` now.
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
