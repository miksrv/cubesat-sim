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
a shipped configuration value instead of computing from it. The radio command contract is complete
too — `restart_service` was its last line.

| Phase | Scope | Delivers | Status |
|---|---|---|---|
| **P6** | **Written; what is left is a walk.** The code landed piecemeal: `powersave` on entering `LOW_POWER` and in `FLIGHT`, the profile TTL armed by HOSTD and reported as `ttl_expires_at`, the radio duty-cycled by the beacon table, and the profile itself. What no test can settle is whether the GNSS track is right — V2 and V3 below are that walk. Mains-as-signal recovery is deliberately still open as Q2 | The autonomous logging profile | `[~]` |

**Defect found on the hardware, 2026-09-01: `restart_service` latches `SAFE`.** `cubesat restart comms`
relayed through HOSTD made the restarted service publish its goodbye, OBC read it as
`subsystem(s) lost: comms`, latched `SAFE`, and held there until a manual `recover` — the retained
`obc_status` kept `lost: [comms]` for some seconds after the service was back. `_check_health` only
waives a goodbye while `profile_machine.settling`, i.e. during a profile switch; a restart OBC itself
relayed opens no such window. That defeats the command's stated purpose (restart one service without
taking the dashboard from a room) and, in `FLIGHT`, would stop the camera and duty-cycle the radio.
The fix belongs in `OBC._restart_service`: mark the named service as expected to depart for a grace
window in `health.py`, the same idea as `settling` but for one service.

**Where the rewrite stands.** All eight services exist, at 100 % line coverage with `ruff` and
`mypy` clean. Four of them — `HOSTD`, `OBC`, `EPS`, `COMMS` — have run against real hardware;
`ADCS`, `PAYLOAD`, `DHS` and `DASHBOARD` have not, which `CLAUDE.md` states as the standing caveat
it is. **Almost everything left is a bench check or a decision.** What can still be written without
the satellite: the photographs missing from a mission export (which blocks the public demo), the
archive dialog below, the chart line that should break on a gap, and an end-to-end test against the
replay build.

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
- **Battery** — V12 and V13. An hour each way should be comfortable: on battery the SOC falls at a
  rate the load explains (2026-09-01), but whether it started full is exactly what V13 leaves open.
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
| V12 | **The gauge is a MAX17040/41, and `charge_rate` is a decoded `0xFFFF`.** Raw registers read under the bus lock on 2026-09-01: `VCELL`, `SOC`, `MODE`, `VERSION` (`0x0002`) and `CONFIG` (`0x9700`) all plausible, while `HIBRT` (`0x0A`), `CRATE` (`0x16`) and `STATUS` (`0x1A`) — the three registers that exist only on the MAX17048/49 — every one read `0xFFFF`, the value an unimplemented address returns. `0x9700` is the factory `RCOMP` with an empty low byte, which is the MAX17040/41 layout (the 17048 default is `0x971C`), and Geekworm's own X728 page links the `MAX17040-MAX17041` datasheet. So the `−0.208 %/h` seen at rest, on mains and now on battery under a real ~22 %/h discharge is `0xFFFF` as a signed word times the LSB — a constant, not a measurement — and the ~70× disagreement with `SOC` was a comparison against a constant. `SOC` itself looks honest on battery: 63.4 → 61.7 % in five minutes with the full DEMO load, which the pack size explains. **Still open:** the mains-day drift (SOC down while voltage rose, 2026-08-31), which is now V13's question alone. The code change this asks for is in `hal/rpi/max17048.py`: a `0xFFFF` `CRATE` must publish `charge_rate: null`, not a plausible number — withhold rather than fabricate. Rename the driver afterwards, or not; the register map it uses is the shared one | Nothing errors and the safety logic is quietly half-dead: `on_mains` treats a rate above −1.0 %/h as "charging or holding", and a constant −0.208 satisfies that for ever, so the second half of the mains check — the one meant to notice a failed charger — cannot trigger. With `null` it degenerates honestly to the pin alone, which is what it has in fact been all along |
| V13 | **The X728 does not appear to be charging on mains, and it should be.** Observed 2026-08-31 with the satellite plugged in all evening: 3.92 V, SOC ~66 %, `CRATE` one LSB from zero, and the board's own charge LEDs agreeing with roughly that level. The datasheet figures in `docs/hardware-x728-ups-hat.md` say the recharge threshold is **4.1 V** and cutoff 4.24 V — at 3.92 V the board is well past the point where it should have resumed, so "it deliberately holds a partial charge" does not explain this reading. Two candidates, cheap to separate. First, the **`CHG Ctrl` jumper**: on the V2.5 board, opening it hands charge control to **GPIO16**, and nothing in this repo drives that pin (the only GPIO the code touches is `PLD_PIN = 6`, and it only reads it) — so if the jumper is open, charging is simply disabled by a floating input. Check the jumper physically first. Second, tired 18650s: capacity loss shows up as a cell that will not hold a charge, and the pack is unbranded. `vcgencmd get_throttled` reads `0x0`, so the 5 V supply is not sagging and can be ruled out. **If the jumper turns out to be the answer, it is an opportunity rather than a defect** — GPIO16 would let EPS decide when to charge, which is exactly the wanted behaviour: hold a partial charge on the desk, where a Li-ion cell ages fastest sitting full, and top the pack up deliberately before a `FLIGHT` outing. That would be a driver and a policy, not a jumper left closed | Nothing errors and nothing looks broken: the dashboard shows a plausible 66 %, the LEDs agree, and the satellite runs happily on mains. It is discovered at the worst possible moment — leaving for a trip with a pack that was never full — which is precisely the failure `FLIGHT` cannot afford. Note this is upstream of V12: while charging is in doubt, the gauge's drift cannot be interpreted either |
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
