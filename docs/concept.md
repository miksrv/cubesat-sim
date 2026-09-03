# Operating Concept — Platform Profiles and Mission States

**Status:** design document, drafted 2026-08-24. Nothing here is implemented yet.
**Supersedes:** `ROADMAP.md` items O1/O2 (the "standalone Pi mode-orchestrator in a separate repo" sketch).

This document defines *how the satellite is operated* — as opposed to `docs/architecture.md`,
which defines how the individual services are built. It answers: what modes does the unit
have, who decides which one is active, and who is allowed to touch the host to make it so.

---

## Table of Contents

- [The problem](#the-problem)
- [Two orthogonal axes](#two-orthogonal-axes)
- [Platform profiles](#platform-profiles)
- [Mission states](#mission-states)
- [The profile × state matrix](#the-profile--state-matrix)
- [Control plane](#control-plane)
  - [Who decides, who executes](#who-decides-who-executes)
  - [MQTT contract](#mqtt-contract)
  - [Ways to switch](#ways-to-switch)
  - [The radio command contract](#the-radio-command-contract)
  - [Profile definitions](#profile-definitions)
  - [The profile is deliberately NOT persisted](#the-profile-is-deliberately-not-persisted)
- [Service inventory](#service-inventory)
- [Data and downlink per profile](#data-and-downlink-per-profile)
- [Mission sessions](#mission-sessions)
- [Local dashboard](#local-dashboard)
- [Safety and recovery](#safety-and-recovery)
- [Known traps](#known-traps)
- [Open questions](#open-questions)
- [Implementation plan](#implementation-plan)
- [Impact on what exists today](#impact-on-what-exists-today)

---

## The problem

The hardware is finished and validated (see the [I2C Address Map](../README.md#i2c-address-map)
and the `docs/hardware-*.md` files). What is missing is an operating concept: the unit is used
in four quite different situations, and each one wants a *different Raspberry Pi*, not just a
different satellite state.

| Situation | What the Pi should be doing |
|---|---|
| On the desk at home, mains powered | Hosting unrelated services (Telegram bots, a star-map generator). The CubeSat stack is dead weight. |
| Demonstrating at home | Full CubeSat stack **plus** the unrelated services, home Wi-Fi up, a live dashboard reachable from a phone on the same LAN. |
| Demonstrating at a school, a library, the office | Full CubeSat stack, **its own access point**, dashboard on a tablet, no internet, usually on battery. |
| Walking to work, or on a trip | Fully autonomous: sensors logged, GNSS track recorded, radios and Wi-Fi minimised, maximum battery life. |

None of this was expressible in the OBC state machine as it stood, and it should not have been:
those are host configurations, not satellite states. The pre-rewrite `src/obc/state_machine.py`
carried commented-out stubs of exactly this idea (`publish_control("telegram/stop")`, `wifi/off`,
`adcs/reduce_frequency`) — this document is what those stubs were reaching for, and `HOSTD` plus
`config/profiles.yaml` are what they became.

---

## Two orthogonal axes

| Axis | What it governs | Who chooses it | How often it changes |
|---|---|---|---|
| **Platform profile** | Wi-Fi client / AP / off, which systemd units run at all, dashboard, CPU governor, whether persistence and each downlink channel are permitted | **a human**, deliberately, before the situation changes | rarely |
| **Mission state** | The existing OBC state machine: sensor cadence, camera permission, logging cadence, radio duty cycle | **the satellite itself**, from EPS telemetry and errors | any time, autonomously |

The profile defines the **envelope of what is permitted**; the state defines the **activity
level inside that envelope**. This is why they must not be merged into one flat machine:
`LOW_POWER` inside `EXPO` means "stretch the poll intervals, throttle the radio, but keep the
AP and the dashboard alive because people are looking at it"; `LOW_POWER` inside `FLIGHT` means
the same throttling with no AP to keep, because there never was one.

A profile change is an *external* event to the state machine. A state change never alters the
profile — with exactly one exception, spelled out in [Safety and recovery](#safety-and-recovery):
`CRITICAL` overrides everything and powers the host down.

---

## Platform profiles

| Profile | Use case | Wi-Fi | External units | CubeSat services | Dashboard | Persistence | Downlink |
|---|---|---|---|---|---|---|---|
| `HOSTED` | Default. On the desk, mains powered, satellite idle | client (home) | **running** | OBC + EPS + COMMS (listening) | — | — | LoRa (RX only) |
| `DEMO` | Showing the satellite off at home | client (home) | running | all | ✅ `cubesat.local` | — (RAM only) | LoRa, beacon off |
| `EXPO` | Science fair, school, library, office; usually on battery | **own AP** | stopped | all | ✅ `cubesat.local` | — (RAM only) | LoRa, beacon off |
| `FLIGHT` | On the move: a trip, the walk to work | **off** | stopped | all | — | ✅ + GNSS track + photos | LoRa only |
| `DIAG` | `FLIGHT` rehearsed on the desk | client (home) | stopped | all | ✅ | separate DB file | LoRa |
| `MAINTENANCE` | `apt upgrade`, `git pull`, reflashing the Heltec | client (home) | stopped | **none** | — | — | none |

`DIAG` and `MAINTENANCE` are additions to the four original use cases, and both close a real gap.
`MAINTENANCE` is the profile in which it is safe to update the OS without a handful of services
writing to the SD card underneath you.

`DIAG` was originally "every sensor at full rate, verbose logs, an I2C self-test report, none of it
polluting the mission database" — the hardware bring-up profile. **Redefined 2026-09-01**, because
that job turned out to belong elsewhere and a different one had no home. The sweep and the self-test
are what `DEPLOY` already does on every ascent, in every profile, and the full-rate polling was
`cadence_scale: 0.2` — ADCS at 10 Hz on a bus clamped to 10 kHz, which was a standing question
rather than a feature. What had no home is the question *"will the trip I am about to take actually
be recorded?"*: `FLIGHT` takes Wi-Fi down, so the profile whose recording matters most is the one
whose recording cannot be watched. `DIAG` is now `FLIGHT` with the network and the dashboard kept —
same cadences, same radio, same automatic photography, the same code through the same migrations —
writing to `diag.db` so a rehearsal never lands in the archive of real trips. That last point is why
a separate database file was the right mechanism all along, and the only field of the profile that
did not change.

### A note on naming

The fourth use case was described as "simulation mode". It is worth *not* calling it that: it
is the most flight-like profile the unit has — autonomous, battery powered, no ground link
except the radio. `FLIGHT` describes it honestly, and it keeps the word "simulation" free for
what it should mean in this repo: running the stack on a laptop against mocked hardware — the HAL,
`src/cubesat/hal/`. Two different things called "simulation" in one project's docs is a guaranteed
source of confusion later.

---

## Mission states

The existing six states stay. Two are added, one gets actual content — and one, `SCIENCE`, was
later taken away again.

| State | Meaning | Change |
|---|---|---|
| `BOOT` | Power-on self-test | unchanged |
| `STANDBY` | **new** — bus alive, mission not active. The state OBC sits in under `HOSTED` and `MAINTENANCE` | added |
| `DEPLOY` | Bring-up of the subsystems | **given real work** — see below |
| `NOMINAL` | Healthy, subsystems polled at nominal cadence | unchanged; where an active profile lives |
| ~~`SCIENCE`~~ | ~~Payload actively collecting~~ | **removed 2026-09-02** — see below |
| `LOW_POWER` | Battery < 30 % (40 % until 2026-09-02) | **given real content** — see below |
| `SAFE` | Battery < 20 %, or a fault, or a ground command | unchanged |
| `CRITICAL` | **new** — battery < 10 %: graceful `poweroff` | added |

New lifecycle: `BOOT → STANDBY`, and `STANDBY → DEPLOY → NOMINAL` on entering an active
profile (`DEMO`/`EXPO`/`FLIGHT`/`DIAG`). Leaving an active profile returns to `STANDBY`.
This makes the profile change the trigger for `DEPLOY`, which finally gives that state
something to do:

- sweep the I2C bus for the addresses this profile's services actually need — only those, so a
  profile that runs no payload is not failed for a sensor it never asked for
- wait, with a timeout, for a first status message from each mission service the profile started.
  That message is the proof the subsystem's own hardware answered, which is better evidence than
  OBC probing devices it does not own — ADCS owns the IMU and GNSS, PAYLOAD the environmental
  sensor and camera, COMMS the radio, and two readers on one 10 kHz bus is the contention the
  bus lock exists to prevent
- wait for a GNSS fix **best-effort only**: `DEMO` and `EXPO` run indoors where no fix will ever
  arrive, so failing on it would send every indoor demonstration to `SAFE`. Log it and move on
- publish the result; on a real failure go to `SAFE` rather than pretending `NOMINAL`

`LOW_POWER` currently only changes a label. It should mean, concretely:

| Knob | `NOMINAL` | `LOW_POWER` |
|---|---|---|
| ADCS poll | 2 Hz | 0.2 Hz |
| Payload science poll | 60 s | 300 s |
| COMMS loop | 30 s | 300 s |
| Camera | permitted | refused |
| LoRa TX | every loop | every Nth loop |
| CPU governor | `ondemand` | `powersave` |

**`SCIENCE` was removed on 2026-09-02, and the reasoning is worth keeping.** It came through the
rewrite untouched, entered and left by ground command — and once persistence stopped being gated on
it (below), it had nothing left to change: every cadence row, the beacon interval, the camera
permission and the recording rule were identical to `NOMINAL`, so `science start` moved a word on
the wire and no service could act on it. A button that does nothing observable is worse than no
button, and the two axes already answer what it was reaching for: **the profile decides whether
telemetry is recorded, the state decides how often, and both cadence and photo interval are
configuration.** The only thing that changes them at runtime is the power-driven descent — which is
the satellite's own decision from its own telemetry, not an operator's verb. If an "intensive
observation" mode is ever genuinely wanted, it needs to arrive with its own numbers rather than as
a state that shares `NOMINAL`'s.

`CRITICAL` is not a nicety: in `EXPO` and `FLIGHT` the unit runs off the X728 UPS, and a Pi
that browns out mid-write risks the SD card. At < 10 % OBC must stop the services, flush the
database and call `poweroff`; the X728 brings the Pi back up by itself when mains returns.

But every power-driven descent — `LOW_POWER`, `SAFE` and `CRITICAL` alike — is suppressed while
mains is present, because on mains there is no power emergency. Skipping that rule is the
difference between working and bricked: arriving home with a flat pack and plugging in would
otherwise power the host off, and the X728 would not restore it, because mains never left.

Mains alone is not trusted either. A faulty charger or a stuck PLD pin would suppress the
protection indefinitely, so "on mains" means `external_power` **and** a charge rate that is not
still falling — a pack draining while the satellite believes it is plugged in still reaches
`CRITICAL`. The rate was meant to come from the gauge's `CRATE` register; the X728's gauge turned
out to be a MAX17040/41 with no such register (2026-09-01), so EPS derives it from the
state-of-charge history over a ten-minute window and publishes `null` until it has one. `null`
means "trust the pin" — the honest fallback, and the only one that lets a flat pack just plugged in
climb out of `SAFE` rather than power itself off.

Recovery also needs hysteresis, which the pre-rewrite handler lacked: `handlers.py` dropped to
`LOW_POWER` below its threshold and only ever left it when external power appeared. Returning to
`NOMINAL` at ≥ 40 % on battery — ten points above the 30 % trigger, the band moving with it —
prevents flapping around the threshold and
makes recovery possible at all in `FLIGHT`. Mains recovers from any level, but "mains" has to
mean the pin *and* a charge rate that is not still falling — see the paragraph on suppressing
the descents below.

---

## The profile × state matrix

What the profile *permits*, versus what the state *asks for*. `—` means the profile forbids
it outright, no matter what the state wants.

| | `STANDBY` | `NOMINAL` | `LOW_POWER` | `SAFE` | `CRITICAL` |
|---|---|---|---|---|---|
| `HOSTED` | idle, EPS watch | — | — | log + alert | poweroff |
| `DEMO` | — | poll + stream + camera | throttled, dashboard kept | sensors only | poweroff |
| `EXPO` | — | poll + stream + camera | throttled, AP + dashboard kept | sensors only, AP kept | poweroff |
| `FLIGHT` | — | poll + log + track, a frame every 5 min | throttled, radio duty-cycled | log only, still listening, beacon every 10 min | poweroff |
| `DIAG` | — | as `FLIGHT`, separate DB | *not applicable* (mains) | report and stop | poweroff |
| `MAINTENANCE` | services down | — | — | — | poweroff |

The important reading: `LOW_POWER` and `SAFE` do **not** tear down the AP or the dashboard in
`EXPO`. Losing the display in front of an audience because the battery hit 39 % would be the
wrong behaviour, and the AP costs far less than the sensors it is throttling.

**Nor does `SAFE` silence the radio** — the descent slows transmission down and never turns
receiving off. COMMS keeps waking every 60 s to poll the inbox and beacons every 600 s
(`config/config.yaml`, the `comms` cadence table and the `beacon` table beside it), because `SAFE`
is reachable from `FLIGHT` through a subsystem fault: the state that most needs a `recover`
would otherwise be the one state deaf to it, on the one profile where the radio is the only way
in. Listening costs no airtime.

---

## Control plane

### Who decides, who executes

Switching a profile means `systemctl`, `nmcli`, `hostapd`/`dnsmasq` and the CPU governor —
all of which need root. Flight software must not have root. So the two halves are split:

```
        set_profile command                    apply_profile
ground ─────────────────────► OBC ──────────────────────────► cubesat-hostd
 (CLI / bot / dashboard)       │                                  │ root
                              │ owns the profile state machine    │ systemctl / nmcli
                              │ owns the mission state machine    │ hostapd / dnsmasq
                              │ validates every transition        │ cpufreq / poweroff
                              │                                   │
                              ◄─── cubesat/host/status (retained) ┘
```

- **OBC** owns both state machines. It decides *what* the platform should look like and
  publishes that intent. It runs unprivileged, and all of the decision logic stays inside the
  test suite, like the rest of the repo.
- **`cubesat-hostd`** is a new, deliberately small privileged service in this repository. It
  does exactly what it is told, from a fixed vocabulary of actions, and reports back what it
  actually achieved. Nothing else in the project gets root.

Two invariants that must be enforced in code, not by convention:

1. `hostd` must never stop `cubesat@obc` or `mosquitto` — that is the branch it is sitting on —
   nor `NetworkManager`, which is how every network mode is applied and therefore the way back.
   Both are outside the set of units any profile may touch.
2. OBC must never stop `hostd`, for the same reason in the other direction.

### MQTT contract

Two new topics, one new command. Everything else reuses the existing surface.

| `TOPICS` key | Topic | Direction | Retained |
|---|---|---|---|
| `host_command` | `cubesat/host/command` | OBC → hostd | no |
| `host_status` | `cubesat/host/status` | hostd → all | yes |

New ground command on the existing `cubesat/command`, handled by OBC:

```json
{"command": "set_profile", "params": {"profile": "EXPO"}, "request_id": "req_010"}
```

Because COMMS canonicalises what it receives over LoRa onto `cubesat/command`, `set_profile`
arrives over the radio for free — `profile expo` is what a person types — and that is what makes
`FLIGHT` recoverable at all (see [Safety and recovery](#safety-and-recovery)). Over the radio it
carries the profile alone: see the amendment of 2026-09-03 under
[The radio command contract](#the-radio-command-contract) for what the compact spelling cannot say.

OBC → hostd:

```json
{"action": "apply_profile", "profile": "EXPO", "request_id": "req_010"}
{"action": "set_governor", "params": {"governor": "powersave"}}
{"action": "poweroff", "params": {"reason": "battery_critical"}}
```

hostd → everyone, retained, so a late subscriber knows the truth immediately:

```json
{
  "timestamp": 1741863600.0,
  "profile": "EXPO",
  "profile_requested": "EXPO",
  "network": {"mode": "ap", "ssid": "cubesat", "clients": 2},
  "units": {"cubesat@adcs.service": "active", "telegram-bot.service": "inactive"},
  "governor": "ondemand",
  "errors": []
}
```

`profile` versus `profile_requested` is not redundancy: a profile can be *partially* applied
(the AP failed to come up, a unit refused to start). Reporting the achieved state separately
from the intended one is what makes that debuggable instead of mysterious.

`cubesat/obc/status` gains a `profile` field alongside the existing `status`. Consumers that
read only `status` keep working unchanged.

### Ways to switch

Both entry points end at the same MQTT command, so there is exactly one code path:

- **`cubesat profile expo`** — a thin CLI in this repo that publishes `set_profile` and waits
  for the matching `cubesat/host/status`. No root needed; it only talks to the broker. It also
  reads: `cubesat status` (state, power, radio, recorder, host metrics) and `cubesat mission list`,
  which is the one command that reads the card rather than the bus — the dashboard is absent in
  `FLIGHT`, and a fallen-over broker is exactly when the last trip is the thing being investigated.
- **`set_profile` over MQTT** — which is how the Telegram bot, the dashboard, the ground
  station and the LoRa uplink all do it.

A physical GPIO switch is deliberately *not* in the design — it was sketched in an earlier roadmap
and is not tracked as work. It stays noted as a future option, mainly as a recovery path — see
[Known traps](#known-traps).

### The radio command contract

*Agreed 2026-08-24; written since, the compact spelling and the reply half included. The `photo`
ack was the last outstanding piece and landed on 2026-09-03, with the amendment below saying which
of its promised fields it actually carries and why.*

The uplink already works: a command over LoRa lands on `cubesat/command` exactly as one over MQTT
does, and 55 bytes of `{"command":"set_profile","params":{"profile":"HOSTED"}}` fit inside a
240-byte Meshtastic message with room to spare. What is designed here is everything around it: how a
person in a field, with a phone and no ground station, addresses the satellite comfortably and gets
an answer. *(That JSON is no longer what goes over the air — see the amendment below. It is quoted
because it is what the compact line becomes on the bus.)*

**A compact spelling, translated once at entry.** Typing quoted JSON on a phone keyboard in a
field is where commands go to be mistyped, so COMMS additionally accepts a compact form and
canonicalises it into JSON *before* the relay. One translation point, on the way in — and at the
time this was written the JSON path stayed verbatim alongside it, so there was no re-encoding step
that could disagree with anyone.

*Amended 2026-08-31:* the compact spelling is a **bare verb** — `ping`, `profile FLIGHT` — the
same lines the dashboard's Mission Console takes, so one command language covers every way of
reaching the satellite. The `!` prefix remains accepted and buys exactly one thing: declared
intent. A `!` line that does not parse is answered with `err=unknown`; a bare line that does not
parse is ordinary mesh chat and is left alone — answering stray sentences on a shared channel
would spend the transmission budget on other people's conversations. The flip side, accepted
knowingly: chat that is exactly a command line (`ping` alone) is a command.

*Amended 2026-09-03:* **the compact spelling is now the whole of it.** Hand-composed JSON off the
air stopped being a command: an uplink is a compact line or it is nothing. What that buys is a
parser count of one on the radio side — two parsers eventually disagree about what a command is, and
then a command works over one spelling and not the other — and the removal of a shape check that
existed only to guard a path nobody was asked to use. Every command still works identically over
MQTT, which is where the dashboard, the CLI and any other broker client publish the JSON above; what
narrowed is the air.

The reason this was written down in the first place survives it whole: `profile hosted`, `safe`,
`recover` and `restart <svc>` are all compact verbs, so a satellite in `FLIGHT` with Wi-Fi down and
no SSH is still reachable from a phone. Two things do go. `set_profile` over the radio carries the
profile and nothing else — `ttl_minutes` and `mission_label` have no room in a one-argument verb, so
the TTL is the profile's own and an unlabelled mission is named after the minute it started, which
is what a walk-to-work `profile flight` was always going to do anyway. And `delete_mission`, the one
command deliberately without a compact spelling, is now unreachable over the air entirely rather
than awkwardly reachable; it stays available over MQTT, which is the surface where whoever presses
delete can see the mission they are deleting.

A line that is neither a compact command nor a `!` gets one line in the service log and no airtime.
Logged rather than dropped in silence, because this is behind the channel filter below: the sender
holds the key, so the likeliest author of an unrecognised line is our own operator reaching for last
week's spelling, and the log line is the trace that person goes looking for. Not answered, because a
reply is exactly what `!` buys.

*Amended 2026-09-03:* that flip side is now bounded by the channel. The mesh preset change of
2026-09-02 put this node on a public primary channel shared with several hundred strangers whose
ordinary English is the command vocabulary, so an uplink counts only if it arrived on
`config.LORA_CHANNEL_INDEX` — the private `CubeSat` channel, whose key is the credential. Anything
else the node hears, a direct message included, is dropped in `comms/service.py` → `_collect_uplink`
*before* the `cubesat/comms/radio` publish, so it reaches neither the parser nor the dashboard's
Radio Link Log nor `radio_log` on the card, and is refused in silence — not even an `err=` for a `!`
line, because the `!` contract exists so *the operator* is never left wondering, and answering a
stranger teaches a mesh of several hundred nodes that this one talks back. "The channel is the trust
boundary" below is therefore enforced rather than merely intended. The filter is on *acting*, never
on hearing: `lora_listening` stays the profile's call and the inbox is polled in full. The
credential is the channel's key and not a node id deliberately — a key is portable, so a flat
operator node is recovered by loading the channel URL onto another one, where a node allowlist would
be a locked door with the key on the far side.

**The vocabulary itself is in [`README.md`](../README.md) → The command vocabulary** — one table,
listing every command in all three spellings (radio, shell, and the JSON on the bus). It is not
repeated here: two tables of the same thing disagree the first time one of them is edited, and the
one that gets edited is the one somebody is looking at.

An unrecognised `!` line is answered with `re=? ok=0 err=unknown` rather than dropped in
silence: the sender is a person standing in a field wondering why nothing happened. A bare line
that is not exactly a command is still just chat — see the amendments above for where that
boundary runs and why.

**One reply rule, not one per command.** Every accepted command schedules a single
out-of-schedule beacon about ten seconds later — long enough for the effect to land — extended
with `re=<command>`::

    CSAT t=1741863600 re=set_profile st=DEPLOY pr=FLIGHT b=78.2 v=3.94 …

The state fields *are* the verdict. COMMS relays a command without interpreting it and cannot
honestly vouch for what OBC did with one, so the ack carries no fabricated `ok=1` for relayed commands — the
reader sees `pr=` and `st=` and judges, the same withhold-rather-than-fabricate rule the rest
of the telemetry follows. `ok=` and `err=` appear only where COMMS itself is the handler and
actually knows: its own commands, and the compact-syntax errors above. `re=`, `ok=` and `err=`
join `t=` and `down=` in the never-dropped core of the beacon format.

*Amended 2026-09-03, twice.* First, `re=` names **the verb the operator typed**, not the canonical
command it became: `beacon on` came back as `re=set_comms_config` (observed on the hardware), which
asks a person on a phone to translate our vocabulary into theirs before they can believe the answer.
The spelling now travels with the translation out of `compact.py`, and since the air takes nothing
but compact lines there is no second case left to reconcile. Second, the general shape of a
reply is that it reads the **handler's own** message where there is one to read — `take_photo` →
`payload_photo`, `set_profile` → the `st=`/`pr=` it already carries, `safe`/`recover` → `st=`. Where
a handler says nothing an ack can read, the honest answer stays the state line and nothing is
invented; `restart_service` is that case today, because HOSTD reports on `host_status` and COMMS
does not subscribe to it.

Queries are the same mechanism with more fields and no ten-second wait. `!ping` answers with an
ordinary beacon immediately — proof of life on demand. `!pos` adds `age=<seconds>` and may
therefore report a **stale** fix honestly; the scheduled beacon never does, because it has no
room for an age and a coordinate without one is indistinguishable from a current one. That is
the lost-satellite query. `!mission` answers `re=mission m= rows= dist=` from the DHS cache,
and `!photo`'s ack carries the frame number and the free megabytes.

*Amended 2026-09-03:* `!photo` now answers about the photograph, and the fields are not quite the
ones promised above. It carries `ok=1 kb=<size> seq=<frame>` read from PAYLOAD's own `payload_photo`
message — the size in kilobytes because that is what an operator asked for, the sequence because
that is the frame number, and the mission id not at all because every beacon already carries `m=`.
The **free megabytes are not there**: they live on `payload_status`, which COMMS does not subscribe
to, and the case they were wanted for arrives instead as `err=nospace` from the refusal itself. A
refusal is the other half of the answer and is named: `err=state` (the mission state forbids the
camera), `err=nospace`, `err=camera`, one word each from `payload/camera.py`, because a beacon field
may not contain a space and PAYLOAD's sentence cannot travel. Nothing published in the ten seconds
is `err=noreply` with **no `ok=`** — silence is COMMS' own observation and worth reporting, while
`ok=0` would be a verdict on a capture it never saw. A mission's own frames are excluded by `kind`:
a mission photographs itself every 300 s against a ten-second window, so without that filter roughly
one `!photo` in thirty would be answered with somebody else's picture.

**Replies cost airtime, so they are budgeted.** *Amended 2026-09-03, because the old budget was
made out of losing things:* it was one pending slot, so a second command inside ten seconds erased
the first one's confirmation without a word — and for `!photo`, a command with a physical side
effect, that is a photograph taken and never mentioned. The budget is now **at most one reply per
wake** (a 30 s cadence in `NOMINAL`, 60 s in `SAFE`), drained from a short queue in arrival order,
first *due* entry first — so a query asked while a photo ack is still ripening is answered now
rather than made to wait out somebody else's ten seconds. What collapses is **only the state
queries** — `ping`, `sys`, `env`, `pos`, `mission` — because a fresh snapshot answers the older
question too and five near-identical telemetry lines are airtime spent saying one thing. Everything
with an effect in the world keeps its own reply. The queue holds eight; beyond that the oldest is
dropped and logged, which is the same judgement the bound is made of — at one reply per wake, an
answer eight deep is four minutes late and describes a satellite that has moved on.

Replies are broadcast into the shared PSK-encrypted channel — the channel is the trust boundary,
commands arrive on it, and everyone on it benefits from seeing the answer. *Amended 2026-09-03:*
replies no longer obey the runtime beacon flag. **An answer is not a beacon**: `beacon_enabled`
(named `lora_enabled` until that day, for the same reason the command was renamed on 2026-09-01)
rations the schedule, and a flag somebody set an hour ago must not swallow the answer to a question
somebody just asked — least of all the confirmation of `beacon off` itself, whose success and whose
total failure are the same silence from a phone. Replies are gated on `lora_listening` — the profile
— exactly like the inbox and the going-down beacon, so **every profile that runs COMMS answers the
commands it accepts**; a profile with `downlink.lora: false` still says nothing, which today is
`MAINTENANCE` alone and it runs no COMMS. What this gives up, knowingly: "listen but transmit
nothing under any circumstances" is no longer reachable at runtime. Since only holders of the
`CubeSat` key can ask, silence is achieved by not asking.

**`restart_service` is the one new capability.** It closes a real gap: `SAFE` in `FLIGHT`
because a subsystem hung is unrecoverable over the radio today — re-applying the profile does
not restart a unit that is still active. OBC relays the name without checking it and HOSTD
refuses what it does not recognise: which services exist and which units may be touched are one
answer each, on the privileged side, and a second copy in OBC would be a second thing to keep in
step. HOSTD's allowlist and denied set (`obc`, `hostd`, `mosquitto`, `NetworkManager`) already
bound what a radio command can reach.

What OBC does contribute is the one thing only it knows: that the departure about to arrive was
asked for. The restarted service says goodbye on its way out, and the health monitor is built to
act on a goodbye immediately — so the first version of this command latched `SAFE` until a ground
`recover`, which is exactly the outcome it exists to prevent (found on the hardware, 2026-09-01).
OBC now declares the departure before relaying the restart, and the monitor waives that one
goodbye for one loss window. It is a waiver, not an exemption: a service that fails to come back
inside the window is declared lost on the ordinary schedule, and a restart nobody announced stays
a fault.

**There is deliberately no `poweroff`.** `CRITICAL` is the only owner of host power, and a kill
switch on the radio channel points at a satellite its sender by definition cannot reach.
Recovery from a stuck profile is what the TTL and the power cycle are for.

**No `request_id` over the radio.** The JSON form keeps carrying one over MQTT exactly as today,
but the radio ack does not echo it: `re=` plus a fresh `t=` answers
"did it hear *that*" well enough, and ten bytes of correlation is airtime.

### Profile definitions

Profiles are data, not code — a new `config/profiles.yaml`, so that adding a profile does not
mean editing OBC:

```yaml
profiles:
  EXPO:
    mission:            active          # active | standby | none
    network:
      mode:             ap              # client | ap | off
      ssid:             cubesat
      hostname_service: true            # advertise cubesat.local over mDNS
    external_units:     stop            # start | stop | [unit.service, ...]
    cubesat_services:   [obc, eps, adcs, payload, comms]
    dashboard:          true
    persistence:        mission_db      # none | mission_db | diag_db
    downlink:           {lora: true}
    power:              {governor: ondemand}

external_units:                          # the registry of things this repo does not own
  - unit: telegram-bot.service
    requires_internet: true
  - unit: starmap.service
    requires_internet: true
```

The `external_units` registry is the single place where "the services that are not part of the
satellite" are named. `hostd` may start and stop only units listed there, plus the `cubesat@*`
mission instances and the dashboard — an explicit allowlist, so a typo in a profile cannot take
down `sshd`.

A profile then says which of them it wants: `start`, `stop`, or a list naming exactly those units.
The all-or-nothing verbs were enough while the registry held two entries that always travelled
together, and stopped being enough as soon as it did not — one unrelated service belongs on the
desk, another only during a demonstration. The list is validated against the registry at load, so
a mistyped unit fails loudly instead of quietly never starting. Both shorthands are resolved by the
loader, which is what keeps the rule "no policy in HOSTD" true here: the privileged process
receives a set of unit names and has no verb left to interpret.

The allowlist is also the inventory: everything root can `systemctl` is listed in it, including
`avahi-daemon`, which is `hostd`'s own lever for `cubesat.local` and which no profile may name.
A safety property that takes two files to verify is one people stop verifying. Those
hostd-owned units sit outside the subset a profile drives, so applying a profile cannot stop the
mDNS daemon it is about to need.

### The profile is deliberately NOT persisted

**Every boot starts in the default profile, `HOSTED`.** A profile is a statement about the
current situation, and a boot means the situation is no longer known.

The decisive case is the ordinary one: the satellite is carried on a trip in `FLIGHT`, the
battery reaches `CRITICAL`, it shuts itself down, and hours later it is plugged in at a desk.
Resuming `FLIGHT` there would be exactly wrong — Wi-Fi down, no SSH, no dashboard, on a unit
sitting on mains next to its operator. The default profile is the only safe assumption after an
unattended gap.

This also turns a power cycle into a **recovery path**: whatever profile the satellite is stuck
in, pulling power and putting it back brings it up on the home network with SSH reachable. That
is a stronger safety net than any of the three layers below, and it costs nothing.

`hostd` still records the last applied profile to `/var/lib/cubesat/last-profile`, but that file
is **information, never instruction**: it is reported by `cubesat status` and logged at boot, so
"what was it doing before it died?" has an answer. Nothing reads it to decide anything.

The cost is real and worth naming: an unexpected reset mid-trip — a brownout, a watchdog bite —
drops the satellite out of `FLIGHT` into `HOSTED`, where it stops recording and starts hunting
for a home network that is 5 km away. The trip's data ends there, silently. Mitigating that
without giving up the property above needs a boot-reason signal rather than a stored profile —
see [Open questions](#open-questions).

---

## Service inventory

The full per-service reference — responsibilities, MQTT contract, device ownership — lives in
[the README](../README.md#services). What matters here is the tiering, and one split.

| Tier | Units | Runs in |
|---|---|---|
| **Essential bus** | `mosquitto`, `cubesat-hostd`, `cubesat@obc`, `cubesat@eps` | every profile, always |
| Mission | `cubesat@adcs`, `cubesat@payload`, `cubesat@dhs` | active profiles |
| Link | `cubesat@comms` | profiles with a permitted downlink |
| Presentation | `cubesat-dashboard` | `DEMO`, `EXPO`, `DIAG` |
| External | whatever `external_units` lists | `HOSTED`, `DEMO` |

**EPS belongs in the essential tier** even though `HOSTED` has no mission: it is the only source
of the battery telemetry that drives `CRITICAL`, and a satellite that cannot see its own battery
cannot protect its filesystem.

**Persistence moves out of COMMS into its own service, `DHS`** — the Data Handling Subsystem, the
OBDH / mass-memory role on a real spacecraft. The reason is a use case, not taste: in `FLIGHT` and
in `SAFE` the radio goes off, but the GNSS track has to keep recording. With the database owned by
the link service that is impossible — turning off the link turns off the recorder. It also keeps
chart history out of a process busy doing radio I/O with timeouts. After the split, `COMMS` is
purely the link: the LoRa beacon and re-publishing uplinked commands.

The orchestrator is likewise two processes, split along the privilege boundary: `HOSTD` is the
hands (root, no logic, an explicit unit allowlist) and `OBC` is the head (all logic, no
privileges). Three reasons, all practical: flight software never gets root; `HOSTD` holds the
applied profile, so `systemctl restart cubesat@obc` mid-demo does not disturb the access point;
and an unprivileged `OBC` is testable in full on a laptop.

### The shared I2C bus

Every sensor is on bus 1, clamped to 10 kHz for the BNO055's sake, and four processes poll it
independently — a single read costs tens of milliseconds and a GNSS register block far more, so
collisions mid-transaction are a matter of time. **All I2C access goes through an advisory lock**
(`flock` on `/run/cubesat/i2c.lock`) held around each transaction inside `hal/i2c.py`. Cheap,
and it preserves the per-subsystem decomposition that is the educational point of the project.

Fallback if the lock proves insufficient: a single bus-owner process the others query. Cleaner in
theory, but it puts a process in the hot path and breaks the "a subsystem reads its own sensor"
model. See [Hardware Ownership](../README.md#hardware-ownership) for the device/owner table.

---

## Data and downlink per profile

Database files live in `/var/lib/cubesat/` (`CUBESAT_DATA_DIR`), not in the checkout.

| Profile | SQLite | GNSS track | Remote API | LoRa | Photos |
|---|---|---|---|---|---|
| `HOSTED` | no | no | ~~heartbeat only~~ no | **listen only** | no |
| `DEMO` | **no** (a RAM ring for the charts) | no | ~~yes~~ no | yes | on demand, never stored |
| `EXPO` | **no** (a RAM ring for the charts) | no | no (no internet) | yes | on demand, never stored |
| `FLIGHT` | `comms.db` | **yes, the point of the profile** | no | duty-cycled | a frame every 5 min |
| `DIAG` | `diag.db` | yes | no | yes | a frame every 5 min |
| `MAINTENANCE` | no | no | no | no | no |

**There is no remote ground-station API.** No deployment was ever made, and the ground segment is
being rebuilt as an interface over the satellite's own dashboard rather than a service the
satellite reports into — so `downlink` names one channel, and the column that used to track the
other is gone with it. And `HOSTED` is not silent:
COMMS runs there to *listen* — every boot lands in `HOSTED`, a field reboot included, and an
uplinked `set_profile` over LoRa is then the only way back in without SSH. `STANDBY` has no row in
the beacon table, so on the desk the radio hears everything and transmits nothing on its own. The
one deliberately deaf profile is `MAINTENANCE` (COMMS is not running at all — reflashing the Heltec
needs the serial port free).

Two changes to today's behaviour follow from this table:

**Persistence stops being gated on `SCIENCE`.** The pre-rewrite COMMS wrote to SQLite only while
the OBC state was `SCIENCE` and `aggregation_enabled` was on. Under this concept the *profile*
decides whether persistence is permitted, and the *state* decides how often rows are written. Keeping
the `SCIENCE` gate would mean `FLIGHT` records nothing unless someone remembers to send a command
before leaving the house. Removing the gate is also what left the state itself with nothing to do,
which is why it is gone as well — see [Mission states](#mission-states).

**The telemetry table needs position columns.** The current schema keeps GNSS data only inside
`raw_json` — there are no `lat`/`lon` columns (see the [SQLite schema](../README.md#dhs) in
the README). `FLIGHT` exists to record a track, so `lat`, `lon`, `alt`, `speed`, `fix` and
`satellites` have to become real columns, or every chart and export has to parse JSON per row.

---

## Mission sessions

Recording is not one endless stream. Each continuous run of an active profile is a **mission** —
a first-class row in the database, with an identity every telemetry row points at. That is what
lets the dashboard answer "show me the walk to work on Tuesday" instead of "show me all telemetry
since March".

A mission is not the same thing as a mission *state*. States (`NOMINAL`, `LOW_POWER`, `SAFE`) come
and go **inside** a mission and are recorded per row — which is exactly what makes a timeline
interesting to look at: here is where the battery started sagging, here is where it went quiet.

### Naming: why the profile stays `FLIGHT`

Calling the field profile `MISSION` was considered and rejected, for the same reason `OBDH` was
rejected next to `OBC`: this document already uses "mission state" for the other axis, and
`MISSION` profile / mission state / mission session in one system is three meanings for one word.

More importantly, missions are not exclusive to one profile. A `DEMO` at home and an `EXPO` at a
library are both sessions worth keeping and comparing. Reserving "mission" for **the recorded
session** makes it the concept that spans profiles, which is what the dashboard actually needs.
`FLIGHT` stays the name of the profile that records one autonomously.

### Lifecycle

| Event | Effect |
|---|---|
| Profile applied, persistence permitted, state reaches `NOMINAL` | `DHS` opens a mission |
| Profile changed, or `MAINTENANCE`/`HOSTED` entered | Mission closed, `end_reason = profile_change` |
| Graceful shutdown | Mission closed, `end_reason = shutdown` |
| `CRITICAL` | `DHS` flushes and closes, `end_reason = battery_critical` |
| Power loss, watchdog, kernel panic | Nothing closes it — see below |

**Orphan recovery matters more than the happy path.** A satellite that dies on battery never gets
to close its mission, so at startup `DHS` finds every mission with a null `ended_at`, sets it to
the timestamp of that mission's last telemetry row, and marks `end_reason = interrupted`. Without
this, one hard power loss leaves an open-ended mission that every later query has to work around.

Because the profile does not survive a reboot, a trip interrupted by a reset becomes **two**
missions rather than one resumed session. That is the honest representation of what happened: there
is a gap in the data, and pretending otherwise would put a straight line across a map where the
satellite was actually switched off.

### Identity and labels

A mission is identified by an integer primary key, and it always carries a **label**: the one
supplied when the profile was applied, or — decided 2026-09-01 — the minute it started, like
`2026-09-01 07:12`. The default exists because the alternative was worse: an unlabelled mission used
to list as its own profile, so every trip ever taken read as "FLIGHT" in the archive.

```
cubesat profile flight --mission "walk to work"
```

Labels are for grouping in the dashboard, not for identity — two runs labelled the same are still
two missions. Photos are filed per mission (`photos/<mission_id>/`) so a gallery groups the
same way the charts do.

### Schema

The telemetry table is renamed from `comms_log` to `telemetry` in the rewrite — `COMMS` no longer
owns it — and gains `mission_id`. Alongside it:

| `missions` column | Purpose |
|---|---|
| `id` | Primary key, referenced by every telemetry row |
| `label` | Optional operator-supplied name |
| `profile` | Which profile recorded it |
| `started_at`, `ended_at` | ISO-8601 UTC; `ended_at` null while running |
| `end_reason` | `profile_change` · `shutdown` · `battery_critical` · `interrupted` |
| `rows` | Telemetry row count, filled in on close |
| `first_fix_at` | When GNSS first had a fix — null for an indoor session |
| `distance_m` | Track length, computed on close |
| `notes` | Free text, for anything the operator wants to add afterwards |

`rows` and `distance_m` are derived values kept on the mission row on purpose: a listing of forty
missions should not have to scan the telemetry table forty times to render.

`DIAG` sessions are missions too, in `diag.db` — same schema, so the same dashboard code
renders a bench run and a trip without a special case.

### What this does not include

The **timeline UI** — scrubbing a mission, replaying a track, comparing two runs — belongs to the
dashboard and the groundstation client, not here. The satellite's job ends at giving each mission
an identity, a start, an end, and a reason it ended.

---

## Local dashboard

The satellite carries **no UI code**. It exposes a transport; the interface itself stays in
[cubesat-groundstation](https://github.com/miksrv/cubesat-groundstation) and is deployed onto
the Pi as a built artifact.

On board, the `cubesat-dashboard` service:

- serves the static React build
- answers a small **read-only** REST surface: the current session for the charts, the mission
  archive, one mission with its telemetry, attitude and radio traffic, and the mission photographs.
  The database it reads follows DHS from the retained `dhs_status`, so a `DIAG` rehearsal is read
  out of `diag.db` rather than out of last week's trip
- and **pushes nothing.** There is deliberately no WebSocket: browsers subscribe to mosquitto's own
  WebSocket listener and receive every retained message the moment they connect, so this project
  contains no MQTT→WebSocket bridge to write, to test, or to keep in step with the topic list. The
  original design here had a `/ws`; it was removed once it became clear the broker already does that
  job better, and the browser fence moved into `config/mosquitto/acl.conf` with it
- a command from the dashboard is published by the browser onto `cubesat/command` over that same
  broker connection — the same path any other ground client uses, so this service needs no write
  path at all

Off board, the groundstation client is reworked into one interface over several data sources: this
local service, a mission exported to a static file for a public demo with no backend at all, and
later a Meshtastic receiver on USB. One dashboard codebase for all of them, and no PHP or MySQL
anywhere.

**Address.** `http://cubesat` alone is unreliable: in client mode (`DEMO`) resolution goes
through mDNS, so `http://cubesat.local` is the address that actually works from a phone or a
tablet, and that is what should be printed on the demo card. In AP mode (`EXPO`) `hostd`'s
`dnsmasq` can additionally answer the bare `cubesat`, since it owns DNS for that network.

---

## Safety and recovery

**`FLIGHT` turns off Wi-Fi, which means no SSH.** A mistyped profile must not require opening
the frame. Four layers, in order of preference — the first is a consequence of
[not persisting the profile](#the-profile-is-deliberately-not-persisted):

0. **Power cycle.** Pull power, put it back. The satellite boots into `HOSTED`, joins the home
   network, and SSH is reachable. No radio, no timer, no tooling — this is why the profile is
   not persisted.
1. **LoRa uplink.** `set_profile` arrives over the radio through COMMS' existing re-publish
   path. This is the primary way back, and it is the reason the LoRa work is a prerequisite for
   `FLIGHT` rather than a nice-to-have.
2. **Profile TTL.** A profile may carry an expiry (`ttl_minutes`). On expiry the satellite falls
   back to `HOSTED`, bringing Wi-Fi and SSH back. `FLIGHT` gets a default TTL sized to a trip
   (a working day, say); `HOSTED` has none — it is where an expiry lands, so an expiry on it
   would be a loop with nowhere to go.

   The duration travels with the request and comes back as an absolute deadline: `hostd`
   publishes `ttl_expires_at` in its retained status, and `obc` decides what reaching it means.
   That division is the same one as everywhere else — `hostd` holds state and executes, `obc`
   decides — and it has a concrete payoff: an `obc` restart mid-flight recovers the deadline from
   the retained message instead of quietly dropping the safety net. Timing it inside `obc` alone
   would mean a `FLIGHT` profile whose expiry evaporates the first time that process restarts.
3. **Mains as a signal.** EPS already detects external power via the X728 PLD pin. Plugging the
   satellite in is an unambiguous "I am back at a desk" gesture, and can be made to request
   `HOSTED` — deliberately a *request* through the normal path, not a silent override, so the
   behaviour is visible in the logs.

**`CRITICAL` outranks the profile.** It is the only state permitted to change host power. Every
other conflict resolves in the profile's favour.

**Time.** The DS1307 RTC on the X728 is now enabled (`dtoverlay=i2c-rtc,ds1307`), so `FLIGHT`
timestamps are trustworthy with no network and no NTP. Without it, a track recorded offline
would be stamped from the epoch of the last boot.

---

## Known traps

- **Meshtastic's 240-byte limit — settled.** The pre-rewrite driver truncated the payload to 28
  bytes and transmitted rubbish. The rewrite sends a compact `key=value` beacon instead of chunked
  telemetry: one message is one complete observation, LoRa airtime is duty-cycle limited, and what
  the radio owes a satellite you cannot see is *alive, and where* — not the whole record, which is
  in DHS. It is never truncated; it drops whole optional fields in priority order instead.
- **The Heltec cannot be powered down, and that is the right way round.** It is fed from the Pi's
  5 V pin, so "radio off" can only ever mean "stop talking to it". Cutting that rail — a MOSFET on
  it, driven from a spare pin on the IO Expansion HAT — was considered and **rejected on
  2026-09-02**: the radio is the one subsystem the satellite cannot afford to lose. It is the only
  way into `FLIGHT`, where there is no Wi-Fi and no SSH, and it is the recovery path out of `SAFE`,
  which is reachable from there. A switch on that line would put "the satellite can no longer be
  commanded" one GPIO fault, one wrong policy or one brownout away, and no power saving is worth
  that. So the modem stays powered in every state and every profile, and what the state machine
  rations is transmission — `beacon` and the duty cycle — never the hardware. This is the same rule
  as *Quiet is not deaf*, one layer down.
- **SD card wear in `FLIGHT`.** Continuous SQLite writes plus journald on a card, unattended,
  on battery. The database half is done — `dhs/schema.py` opens WAL with `synchronous = NORMAL`,
  which also lets DASHBOARD read the file while DHS writes it — and narrowing persistence to
  `FLIGHT` and `DIAG` removed the demonstrations that were wearing the card for nothing. What is
  still open is journald: `Storage=volatile` in that profile has not been set.
- ~~**Sensor cadence has nowhere to come from.**~~ **Closed.** It was the trap that made
  `LOW_POWER` meaningless: ADCS did not subscribe to `cubesat/obc/status` at all and its 0.5 s
  interval was hardcoded. Every service now derives its interval from the retained state through
  `common/cadence.py` and a `cadence_key` on the service class, so a state change is a cadence
  change everywhere at once.
- **`mosquitto` must never be in a profile's unit list.** Every control path in this design runs
  through the broker; stopping it strands the satellite in whatever profile it was mid-way
  through applying. The same holds for `NetworkManager`, one step further out: every network mode
  is applied through `nmcli`, so stopping it takes away the way back to a reachable profile — and
  `FLIGHT`, where that would be noticed, has neither Wi-Fi nor SSH to fix it from. Both are in
  `DENIED_UNITS`, so this is enforced rather than remembered.
- **No GPIO recovery switch, by choice.** The three-layer recovery above covers it, but if
  `FLIGHT` ever gets used somewhere a LoRa uplink cannot reach, a long-press button wired to a
  free HAT pin is the cheapest insurance available.

---

## Open questions

- **`BMP280` (`0x76`) duplicates the SEN0501 pressure reading.** Still undecided in the README's
  address map. `DIAG` is the natural place to settle it: log both and compare them over a few
  hours.
- **Recovering a trip after an unexpected reset.** Not persisting the profile means a brownout or
  a watchdog bite mid-trip silently ends the recording. Solving it without reintroducing a stored
  profile means acting on a **boot reason** instead: if mains is absent at boot, the satellite is
  demonstrably not on a desk, and re-entering the last active profile is the safe reading rather
  than the dangerous one. `EPS` already has that signal on the X728 PLD pin. Deliberately deferred
  until `FLIGHT` has been used enough to know whether spurious resets actually happen — designing
  around a hypothetical failure is how the stored-profile trap got built in the first place.
- ~~**`FLIGHT` timelapse.**~~ **Answered 2026-09-01: a mission photographs itself, and there is no
  timelapse.** Photos on a walk are the point of taking the satellite on one, so they are not an
  opt-in parameter and not a command: while a mission is open, a frame is taken every
  `photos.mission_interval_sec` (300 s) and filed under it. The ground-commanded timelapse — an
  interval somebody had to choose, on a verb that had to survive a radio link — was removed: the
  automatic series was the only use it had.
- ~~**The remote ground-station API.**~~ **Answered 2026-08-25: nothing beyond LoRa and the local
  dashboard.** The PHP groundstation becomes an interface over several data sources and keeps no
  backend of its own, so `CloudApi`, the cloud half of COMMS and the `downlink.api` flag were
  removed outright rather than left switchable.
- **Profile change while `SAFE`.** Should a human be allowed to move a fault-latched satellite
  into `EXPO` to show it to an audience? Probably yes, with the fault clearly displayed — but
  it needs deciding rather than falling out of the implementation.
- ~~**Should `DEMO` and `EXPO` record at all, or is a mission the privilege of `FLIGHT`?**~~
  **Answered 2026-09-01: a mission is the privilege of `FLIGHT`** (and of `DIAG`, which rehearses
  it into a separate file). Raised 2026-08-31 because three of the first eleven missions on the
  satellite came from demonstrations rather than trips, and because both original reasons for the
  wide setting had expired: the public demo now runs on generated data, and export to the hosting is
  planned from `FLIGHT` only. The deciding argument is the use case, not the bookkeeping — `FLIGHT`
  is entered *because* the satellite is being taken somewhere, and a satellite standing on a desk has
  no track to record, while the SD card is the one component here that wears out by being written to.

  What made this more than a one-line change is a coincidence in the profile table: **`FLIGHT` is the
  one recording profile with no dashboard, and `DEMO`/`EXPO` are the only ones that have one.** So
  narrowing persistence naively would have put the record where nothing displays it and the display
  where there is nothing to record. Three things were built to keep the display whole:

  - **`cubesat/dhs/telemetry`.** DHS assembles the wide row on its tick whether or not it is
    recorded, and publishes it. This turned out to be worth doing on its own account: the host's own
    CPU, RAM, swap, disk, uptime and SoC temperature are collected by `psutil` *inside* DHS and appear
    in no other message, so before this topic existed the only way to read them was to poll the
    archive — which meant a profile that does not record could not report the state of the machine it
    was running on.
  - **A bounded ring in `DASHBOARD`** (`dashboard/live.py`), fed by that topic, holding
    `dashboard.live_history_rows` (720, about six hours at the `NOMINAL` cadence). Deliberately on
    the satellite rather than in the browser: `EXPO` means visitors arriving one at a time and
    opening the page one at a time, so a per-page buffer would give each of them a chart starting
    from zero points, lose everything on a reload, and make "what the satellite is showing" a
    function of who opened a tab when.
  - **A meaning for `/api/telemetry`.** It now answers *the current session* — the open mission from
    the database while one is being recorded, the ring otherwise. The endpoint used to mean "the last
    N rows of the table", which on 2026-09-01 was measured returning 33 rows from that day's mission
    and 27 from one two days earlier, drawn as a single continuous line and, on the ground track, as
    a single path joining two days' positions.

  Photographs went the same way, and the argument is the same: with no mission open a frame is
  written to a tmpfs, published as pixels on the retained `payload_photo`, and deleted. That retired
  `photos/unfiled/` — which retention was forbidden to touch, making the one directory guaranteed to
  grow without limit the one holding the photographs least likely to be wanted.

  The measurement that framed the decision is worth keeping: of what was actually being written,
  **96 % was the attitude track** — 5537 `attitude` rows against 201 `telemetry` rows. So
  `dhs.attitude_min_interval_sec` remains the cheap lever if the goal is ever fewer card writes
  within a profile that does record; turning persistence off was the expensive one, and it was taken
  for the use case rather than for the byte count.

---

## Implementation plan

Ordered so that each phase is independently useful and testable.

| Phase | Scope | Delivers |
|---|---|---|
| **P0** ✅ | Finish `PLAN.md` stage 6: `lora.py` on top of `meshtastic`, compact beacon or chunking, config keys, tests | A radio link the radio-only profiles can be built on |
| **P1** ✅ | **Skeleton.** `pyproject.toml` and the `src/cubesat/` package layout (no more `src.*` imports or `PYTHONPATH=.`); `common/service.py` base class, `states.py` enums, `cadence.py`; `hal/` with `typing.Protocol` interfaces, real drivers for the four Gravity modules, mocks behind `CUBESAT_MOCK_HARDWARE`, and the shared I2C lock; runtime data moved to `/var/lib/cubesat`; `tests/` mirroring the package | The whole stack runnable and testable on a laptop — the prerequisite for every phase below |
| **P2** ✅ | `config/profiles.yaml`; `cubesat-hostd` (unit allowlist, `systemctl`, state file); `set_profile` in OBC; `cubesat/host/*` topics; `cubesat` CLI; profiles `HOSTED`, `MAINTENANCE`, `DEMO` without the AP | Switching between "desk" and "demo" without touching systemd by hand |
| **P3** ✅ | `cubesat-dhs` split out of COMMS: `comms_log` → `telemetry` with position, `profile` and `mission_id` columns; the `missions` table, its lifecycle and orphan recovery at startup; writes gated on profile; retention | A recorder that keeps working when the radio is off, and history that is divided into missions |
| **P4** ✅ | `STANDBY` and `CRITICAL` states; real `DEPLOY` self-test; every service derives its cadence from `obc/status`; `LOW_POWER` knobs; recovery hysteresis; graceful `poweroff` | The state machine finally does something measurable |
| **P5** ✅ | AP mode in `hostd` (NetworkManager + `dnsmasq` + mDNS); `cubesat-dashboard` service — read-only REST and **no WebSocket**, which is where the phase as agreed changed: browsers subscribe to mosquitto's own listener instead, so there is no bridge to keep in step; groundstation client reworked for a local backend; profile `EXPO` | A satellite that can be shown to a room with no internet |
| **P6** `[~]` | Power saving; profile TTL; mains-as-signal; GNSS track verified end to end; profile `FLIGHT` | The autonomous logging profile |
| ~~**P7**~~ | ~~Profile `DIAG`: I2C sweep, full-rate polling, self-test report, separate persistence~~ **Retired 2026-09-01.** The sweep and the self-test report are what `DEPLOY` does on every ascent in every profile; the full-rate polling was `cadence_scale: 0.2`, i.e. ADCS at 10 Hz on a 10 kHz bus, and was removed rather than built on. `DIAG` keeps its separate database and becomes a rehearsal of `FLIGHT` — see the profile table | — |
| **P8** ✅ | Docs and tests kept in line as each change lands rather than as a phase of its own. Closed 2026-09-01 with the sweep that removed the last places a test asserted a shipped configuration value instead of computing from it — cadences, the SAFE listen ratio, and the power thresholds now all derive from the constant they are about | Tests that do not break when a legitimate setting changes |

`ROADMAP.md` items H1–H7 (the hardware abstraction layer) stopped being a nice-to-have and became
phase P1, which is what `src/cubesat/hal/` is: `hostd`, both state machines and the cadence logic
are the first parts of this system that can be fully tested on a laptop, and none of them can be
tested at all without mocked sensors.

**Every phase but `P6` is closed**, and `P6` is written — what is left of it is a walk with the
satellite, which is the one thing no phase can deliver from a desk. `ROADMAP.md` carries that
remainder, together with the bench checks and the decisions still open.

---

## Impact on what exists today

*This table is the plan as it was agreed, kept as the record of it. All of it has since landed —
read it as "what the rewrite changed", not as work outstanding.*

| What | Change |
|---|---|
| `src/cubesat/obc/` | Two new states; two machines in separate modules; the commented-out `publish_control` stubs become the real `cubesat/host/command` publisher |
| `src/cubesat/obc/commands.py`, `power_policy.py` | `set_profile`; `CRITICAL` threshold; recovery hysteresis |
| `src/cubesat/comms/` | Loses persistence entirely — becomes the link only (the LoRa beacon + uplink re-publish) |
| `src/cubesat/dhs/` | **New** — owns the database: packet assembly, writes gated on profile, cadence from state, position columns, the `missions` table and its lifecycle, retention |
| `src/cubesat/hal/` | **New** — `typing.Protocol` interfaces, real drivers, mocks, and the shared I2C advisory lock |
| layout | `src` becomes a real src layout: the package is `cubesat`, installed editable, launched as `python -m cubesat.<service>`. No more `src.*` imports or `PYTHONPATH=.` |
| runtime data | Out of the checkout: `/var/lib/cubesat/` (database, photos, `last-profile`), `/run/cubesat/` (bus lock, socket), created at boot by `systemd-tmpfiles` from `config/tmpfiles.d/cubesat.conf` — not by `StateDirectory`/`RuntimeDirectory`, which re-chown the tree to the starting unit's own user and so let root-owned HOSTD lock the unprivileged services out |
| `src/cubesat/comms/mesh.py` | Rewritten on `meshtastic` (P0, already planned in `PLAN.md`) |
| `src/cubesat/adcs/`, `src/cubesat/payload/` | Subscribe to `obc/status`; interval from state via `common/cadence.py` |
| `src/cubesat/common/` | New `service.py` base class, `states.py` enums, `topics.py`, `cadence.py`; profile loading; LoRa keys replaced |
| `config/` | New `profiles.yaml` |
| `systemd/` | A `cubesat@.service` template for the six identical units, plus `cubesat-hostd.service` (root) and `cubesat-dashboard.service` |
| `scripts/`, `pyproject.toml` | `cubesat` CLI as a console script; `start.sh`/`stop.sh` retired — a profile is the unit of operation |
| `README.md` | Profiles section; updated topic map, command table and SQLite schema |
| `ROADMAP.md` | O1/O2 retired in favour of P0–P8 above |
