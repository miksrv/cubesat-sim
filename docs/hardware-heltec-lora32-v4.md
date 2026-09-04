# Heltec WiFi LoRa 32 V4 (Meshtastic)

LoRa ground link for **COMMS**, replacing the LoRa half of the [52Pi IoT Node(A)](hardware-iot-node-a-52pi.md). The board runs **stock Meshtastic firmware** and is a self-contained radio: the Raspberry Pi talks to it over UART using Meshtastic's Serial module in `PROTO` mode, and Meshtastic handles framing, CRC, retries, acknowledgements and encryption on its own. The custom `length + payload + CRC-16-CCITT` framing used against the 52Pi bridge is therefore obsolete here.

> **Status:** the radio link is bench-verified end to end (Pi → UART → Heltec → air → second Meshtastic node, and back). The driver is `src/cubesat/hal/rpi/meshtastic_radio.py`, built on the `meshtastic` library — the old `smbus2`/SC16IS752 implementation is gone, and with it the hand-rolled framing and CRC that Meshtastic already provides. **It ran inside `COMMS` on the assembled satellite on 2026-08-31**, in `HOSTED`: the node was reachable, a beacon went out, and a ground command typed on a phone round-tripped onto `cubesat/command` and was answered. On 2026-09-02 the node was moved onto the community mesh's modem preset and immediately measured there: 72 foreign gateways published its first broadcast, having relayed it up to five times, which settled the hop arithmetic — see [Coverage](#coverage-measured-2026-09-02). What is still unproven is whether any of that relaying happens on the **private channel** the satellite's own telemetry and commands travel on, which is what remains of bench check V10.

- **Product:** [Heltec WiFi LoRa 32 V4](https://heltec.org/project/wifi-lora-32-v4/)
- **Official docs:** [Heltec Wiki — WiFi LoRa 32 V4](https://wiki.heltec.org/docs/devices/open-source-hardware/esp32-series/lora-32/wifi-lora-32-v4/)
- **Pin map:** [V4_pinmap.png](https://resource.heltec.cn/download/WiFi_LoRa_32_V4/Pinmap/V4_pinmap.png)
- **Firmware:** [Meshtastic](https://meshtastic.org/docs/getting-started/) — [web flasher](https://flasher.meshtastic.org/)

## Specification

| | |
|---|---|
| MCU | ESP32-S3**R2** — 2MB embedded quad PSRAM (the flasher reports `Embedded PSRAM 2MB (AP_3v3)`) |
| LoRa transceiver | SX1262 |
| Flash / partitions | 16MB scheme: `nvs` `0x9000`, `otadata` `0xe000`, `app0` `0x10000`, `app1` `0x650000`, `spiffs` `0xc90000`, `coredump` `0xff0000` |
| Firmware | Meshtastic **≥ 2.7.20** (earlier builds do not support V4); verified on `2.7.26.54e0d8d`, target `heltec-v4` |
| Region | `US` — 915 MHz (region must match on every node) |
| Modem preset | `MEDIUM_FAST` on frequency slot **45** (set 2026-09-02; the factory default is `LONG_FAST` on slot 20). Preset *and* slot must match on receiving nodes — see [Modem preset and the local mesh](#modem-preset-and-the-local-mesh) |
| Link to Raspberry Pi | UART `115200 8N1` on `GPIO43` (`U0TXD`) / `GPIO44` (`U0RXD`), via Meshtastic's Serial module in `PROTO` mode |
| Max payload | **240 bytes per message** over the Serial module |
| Power | 5V on header pin `J2-2`, or USB-C. Peak TX current on 915 MHz can brown out a Pi 5V rail |
| Antenna | 915 MHz, **must be attached before the board is ever powered** |
| Reserved GPIO | `GPIO8–14` SX1262 · `GPIO17/18/21` OLED · `GPIO34`, `GPIO38–42` GNSS connector · `GPIO19/20` USB · `GPIO2`, `GPIO7`, `GPIO46` FEM · `GPIO36` Vext_Ctrl · `GPIO37` ADC_Ctrl · `GPIO1` VBAT sense |
| Free alternative UART | `GPIO47` / `GPIO48` (`J2-13` / `J2-14`) |

## Wiring

Connected through an [IO Expansion HAT (DFR0566)](https://www.dfrobot.com/product-1930.html), whose Gravity UART connector is a direct passthrough of the Pi's own 3.3V UART pins. No level shifter is needed — both sides are 3.3V.

**Header J2 numbering: pin 1 is the one closest to the USB connector**, counting upwards to 18. Solder against the silkscreen (`43`, `44`), not by counting holes.

| Wire | Heltec signal | J2 pin | HAT (DFR0566) | Raspberry Pi |
|---|---|---|---|---|
| green | `U0TXD` / `GPIO43` (transmits) | 6 | `R` | `GPIO15` (RXD), physical 10 |
| blue | `U0RXD` / `GPIO44` (receives) | 5 | `T` | `GPIO14` (TXD), physical 8 |
| black | `GND` | 1 | `-` | GND |
| red | `5V` | 2 | dedicated `5V` pin | 5V |

TX and RX are **crossed**; ground must be common.

Three things that are easy to get wrong:

- **`+` on the HAT's UART connector is 3.3V, not 5V** (DFRobot documents it as "3.3V Positive"). The red wire belongs on one of the dedicated `5V` pins — feeding 3.3V into the Heltec `5V` input leaves its regulator unable to produce a stable 3.3V rail.
- **Do not power from Pi 5V and USB at the same time.** `J2-2` is tied to USB VBUS, so two supplies end up in parallel and can back-feed the host USB port. While debugging, prefer USB power and leave the red wire unconnected — this also rules out brownouts (see [Troubleshooting](#troubleshooting)).
- **`J2-3` and `J2-4` (`Ve`) are the Vext output**, i.e. switched power *out* for external peripherals, not a power input. Never feed the board through `3V3` either — that is the regulator's output.

The `T` / `R` silkscreen on Gravity HATs is documented inconsistently by DFRobot; if the link does not come up, check continuity to Pi physical pins 8 and 10 before suspecting anything else.

## Flashing

Done once, from a computer over USB — not from the Pi.

1. **Attach the antenna first.** USB is already power, and once `lora.region` is set the node starts transmitting on its own (it broadcasts node info without being asked). Transmitting without an antenna risks the PA.
2. Open the [Meshtastic web flasher](https://flasher.meshtastic.org/) in **Chrome or Edge** — Safari has no Web Serial support, so the board never appears in the list.
3. Select target **`heltec-v4`**. Do **not** pick `heltec-v4-r8-oled` / `heltec-v4-r8-tft` (those are for the ESP32-S3**R8** with 8MB octal PSRAM) or the `-tft` variants.
4. **Choose "Full erase and install", not "Update".** This is the single most common way to end up with a boot loop — see [Troubleshooting](#troubleshooting).
5. On macOS the board enumerates as `/dev/cu.usbmodemNNN` (native USB — there is no CP2102 on V4, so it is never `usbserial`). The number changes between reflashes.

If the board is not detected, put it into download mode manually: **hold `PRG`/BOOT, tap `RST`, release BOOT.** The V4 firmware manifest is marked `requiresDfu: true`, so this is the normal path rather than a recovery hack.

**Flashing from the command line instead** (useful when the browser flasher misbehaves — this writes the bootloader, partition table and app in one shot):

```bash
python3 -m venv esp-venv && ./esp-venv/bin/pip install esptool
# grab firmware-heltec-v4-<version>.factory.bin from the release's firmware-esp32s3-<version>.zip
./esp-venv/bin/esptool.py --chip esp32s3 --port /dev/cu.usbmodem101 erase_flash
./esp-venv/bin/esptool.py --chip esp32s3 --port /dev/cu.usbmodem101 --baud 921600 \
    write_flash 0x0 firmware-heltec-v4-<version>.factory.bin
```

**Success criterion:** in the serial console the ROM bootloader log (`ESP-ROM:esp32s3-...`) appears **once**, followed by Meshtastic log lines. If the ROM log repeats forever, the flash is wrong — do not continue.

## Initial configuration over USB

Install the CLI (a venv works fine and needs no root):

```bash
python3 -m venv mesh-venv && ./mesh-venv/bin/pip install "meshtastic[cli]"
PORT=/dev/cu.usbmodem101   # substitute your own
```

**Step 1 — radio first, wire second.** Configure and prove the radio while still on USB; otherwise a later failure is ambiguous between the radio link and the UART.

```bash
meshtastic --port $PORT --info
meshtastic --port $PORT \
    --set position.gps_mode NOT_PRESENT \
    --set lora.region US
meshtastic --port $PORT --sendtext "test from laptop"
```

- `lora.region` defaults to `UNSET`, and in that state the board transmits **nothing**. This is the usual explanation for "flashed it, but silence".
- `position.gps_mode NOT_PRESENT` is the correct value for a board with no GNSS module attached: `DISABLED` means "hardware present but switched off", `NOT_PRESENT` means "no hardware", which stops the firmware probing for it. (`position.gps_enabled` is the legacy flag — leave it alone.)
- **The display cannot be switched off, and does not need to be.** `display` only exposes cosmetics (`screen_on_secs`, `oled`, `displaymode`, `flip_screen`) — there is no enable/disable field. The firmware probes I2C at boot, finds no panel and runs headless; the "no display" log line is normal.
- **The heartbeat LED can be, and on the assembled satellite it is.** `device.led_heartbeat_disabled true` stops the firmware's blink (**set 2026-08-31**, read back as `True` after the reboot that the write triggers). Inside a closed shell it lights nothing, and it blinks once a second for the life of the mission. Note this is the *firmware's* LED only — any power or charge indicator wired to the board's own regulator stays lit and cannot be reached from software.

  The write needs `/dev/serial0`, which `COMMS` holds whenever it is running. `MAINTENANCE` is the profile that frees it, but it also stops the `external_units` — on this satellite that means the unrelated home services — so for a single setting it is gentler to `sudo systemctl stop cubesat@comms`, make the change, and start it again. Expect `OBC` to enter `SAFE` while COMMS is gone (it is a watched subsystem, and its last will fires); `recover` clears it, because a fault-latched `SAFE` does not lift on its own.

Confirm the test message arrives on a second Meshtastic node before going further. Receiving nodes must agree on region (`US`), modem preset, **frequency slot** and channel PSK — two nodes fresh from the flasher converge by themselves on `LONG_FAST` and the default key, which is exactly why a mismatch is only ever met later, after somebody has changed a preset. See [Modem preset and the local mesh](#modem-preset-and-the-local-mesh): this satellite is on `MEDIUM_FAST` slot 45, not on the default.

**Step 2 — enable the Serial module** (one batch: every `--set` triggers a save and a reboot):

```bash
meshtastic --port $PORT \
    --set device.role CLIENT \
    --set network.wifi_enabled false \
    --set serial.enabled true \
    --set serial.mode PROTO \
    --set serial.rxd 44 \
    --set serial.txd 43 \
    --set serial.baud BAUD_115200
```

Why this is required: the V4 has native USB, so the protobuf API lives on USB CDC and `GPIO43/44` stay silent in the stock configuration. Only the Serial module in `PROTO` mode brings an API up on those pins — in `src/modules/SerialModule.cpp` that is `Serial2.begin(baud, SERIAL_8N1, rxd, txd)` with no pin-range check (the documented "rxd 1–39, txd 1–33" limits are legacy classic-ESP32), and the class itself is `SerialModule : StreamAPI(&Serial2)`. `override_console_serial_port` does not help here — it only applies to the NMEA and CALTOPO modes.

`BAUD_115200` is not optional: the module defaults to 38400, while the Meshtastic Python library opens the port hard-coded at 115200 (`serial.Serial(dev_path, 115200, ...)` in `serial_interface.py`).

**Leave Bluetooth enabled for now** — it is the way back if the UART does not come up. Enabling the Serial module does not break the USB API (`override_console_serial_port` stays `false`), so a rollback is always possible.

## Modem preset and the local mesh

**A modem preset is the physical layer, not a preference.** It fixes bandwidth, spreading factor and
coding rate together, and Meshtastic states the requirement without softening it: nodes in one mesh
must share region *and* preset. Nodes on different presets do not hear each other badly — they do
not demodulate each other at all.

Both this satellite and the operator's personal node ran the factory `LONG_FAST` until 2026-09-02,
and they talked to each other perfectly, which is why nothing on the bench ever hinted at the
problem: **the Bay Area community mesh is not on LongFast.** It migrated to Medium Range Fast, and
`https://meshview.bayme.sh/map` shows every node in the operator's own city on `MediumFast`. So the
multi-hop coverage that is the whole reason `FLIGHT` uses LoRa was coverage this satellite could not
reach — the routers that would relay it were on another waveform.

Applied to the satellite's node on **2026-09-02** and read back after the reboot each write triggers:

| Setting | Value | Why |
|---|---|---|
| `lora.modem_preset` | `MEDIUM_FAST` | What the local mesh runs. Read back as bandwidth 250 kHz, SF 9, CR 5 — `LONG_FAST` was SF 11 |
| `lora.channel_num` | `45` | The slot bayme.sh names explicitly. **Not optional here** — see below |
| `lora.hop_limit` | `6` | Their figure for a personal/chat node; the factory default is 3 |
| `lora.config_ok_to_mqtt` | `true` | Makes the node visible on the community map at all — see below |
| `lora.region` | `US` | Unchanged |

```bash
# /dev/serial0 must be free first: COMMS holds it whenever it runs.
cubesat profile maintenance
meshtastic --port /dev/serial0 \
    --set lora.modem_preset MEDIUM_FAST \
    --set lora.channel_num 45 \
    --set lora.hop_limit 6 \
    --set lora.config_ok_to_mqtt true
cubesat profile hosted
```

**The frequency slot moves with the preset, and that is the easy half to miss.** With
`lora.channel_num` at 0 the slot is derived from a hash of the *primary channel's name* — and this
node's primary has an empty name, so the name used is the preset's own, which changed from
`LongFast` to `MediumFast`. Changing the preset and leaving the slot on automatic therefore lands a
pair of nodes on a private frequency, together and alone: they still hear each other, which looks
exactly like success from the bench. Setting the slot explicitly is what makes the change mean
anything.

**`config_ok_to_mqtt` is what makes the node visible, and it defaults to `false`.** A foreign
gateway checks the `OK_TO_MQTT` bit of the packet itself before publishing to a public broker
(`DontMqttMeBro` in `MQTT::onSend`) and silently drops it when the bit is absent. Without the flag
the satellite can be heard perfectly by the community mesh and still never appear on
`meshview.bayme.sh` — which looks identical to having no coverage. It affects nothing else: not the
link, not the beacon, not commands. What it costs is set out under
[Why a secondary channel](#why-a-secondary-channel): the V4 has no GNSS and nothing here feeds it
one, so no position is ever broadcast and a public map can place the node only as coarsely as the
gateway that heard it. The flag can be cleared for a real trip without touching anything else.

**Channel 1 `CubeSat` and its PSK are untouched by all of this**, and were verified byte-for-byte
with `--info` after the change: a secondary channel is a key layered over the radio configuration,
so the preset moves underneath it and nothing in `comms/` or `hal/rpi/meshtastic_radio.py` changes.
Verify it anyway after any preset change — a satellite on a channel the ground segment cannot read
is a silent failure that looks like a dead radio.

**Both nodes were audited against each other on 2026-09-03, and they agree.** The personal node had
been configured by hand over several sessions and had never been compared to the satellite, so a
setting that had drifted there would have been indistinguishable from a satellite fault. Read back
with `--info` on both and compared field for field — preset, `channel_num`, `hop_limit`, region, and
channel 1 `CubeSat` with its PSK — nothing differs. Confirmed on the air rather than only on paper:
a message from the personal node reached `CubeSat` and the reply came back, so the ground link is
proven in both directions on the new preset.

### Coverage, measured 2026-09-02

The node appeared on `meshview.bayme.sh` within a minute of the preset change, and the packets it
had already exchanged were enough to measure the link rather than merely assert it. Everything below
is read from meshview's own record (`/api/packets_seen/<packet id>`), which reports every gateway
that published a copy of one packet, with the hop fields as that gateway received them.

| Packet (2026-09-02) | Gateways that published it | Hops consumed |
|---|---|---|
| NodeInfo broadcast, 18:08:45 — the first after the change | **72** | 0 ×1, 1 ×21, 2 ×13, 3 ×23, 4 ×9, 5 ×5 |
| Traceroute reply to `Troy`, 18:09:43 | 58 | 0 ×1, 1 ×20, 2 ×15, 3 ×6, 4 ×12, 5 ×4 |
| NodeInfo to `KK6DAC-Actual-8647`, 18:19:03 | 46 | 0 ×1, 1 ×1, 2 ×5, 3 ×8, 4 ×5, 5 ×13, 6 ×13 |

**One node hears this satellite directly, and everything else is that node relaying.** On all three
packets exactly one gateway reported zero hops: `!2a9db163` — **W6SRR Sunol Ridge**, a bayme.sh
`ROUTER` — at **−5.25 dB SNR / −94 dBm** (−7.00 dB on the later two). **The straight-line distance
is 7.50 km** (measured on the map, 2026-09-02), with the satellite indoors on a desk and Sunol Ridge
being what its name says. For scale: `MEDIUM_FAST` is SF 9, whose demodulation floor is around
−12.5 dB SNR — a datasheet-class figure, not one measured here — so the link is working with roughly
7 dB of margin rather than scraping in. It is also a single point of failure
dressed up as broad coverage: 72 gateways is what the mesh does with a packet *after* Sunol Ridge
has it, and if that one path drops there is no second neighbour behind it. Re-measuring the direct
receivers from wherever `FLIGHT` actually goes is the useful reading, not the gateway count.

A traceroute exchange with `Troy` (`!648f66da`, a `T_ECHO` client) completed in both directions
unprompted — a stranger's node probing the new arrival. The recorded route names bayme.sh routers
well north of the Bay Area (`Davis Home Base`, `Yuba Mesh - Bear`, `Sutter Buttes Router`,
`Butte SAR`), which is what a mesh with ridge-top routers is for. Note the SNR fields inside a
traceroute payload are **quarter-dB integers** — `snr_towards: 43` is 10.75 dB, `-32` is −8 dB — and
not the same encoding as the `rx_snr` meshview reports beside them.

**This settles the hop arithmetic (V10, primary channel).** `hops = hopStart − hopLimit` was read
from the library's documentation and had never been exercised, because every packet heard on the
bench arrived direct and reads 0 whichever interpretation is right. Here one packet is reported by
72 receivers: `hopStart` stays at the 6 this node now transmits with, `hopLimit` arrives as
everything from 6 down to 0, and the difference gives the whole ladder 0…6 — one value per relay
the copy had taken. meshview computes the same subtraction independently. The satellite's own
`hal/rpi/meshtastic_radio.py:_hops` reads those two fields from packets it receives, and its marker
has been updated from inferred to measured.

**And the receiving half was measured the same evening.** Everything above is this node's *outgoing*
traffic counted by other people's gateways; the driver's own `_hops` had still only ever seen direct
packets, all reading 0. At 2026-09-02 19:31 a chat message from the community mesh arrived on the
primary channel with **`hops: 6`** (sender `!167ff893`, SNR 0.25 dB, RSSI −73 dBm, 89 bytes) — the
first non-direct packet this satellite has received, and a plausible integer against the hop budgets
in use here. `radio_log.hops`, the field the check exists for, has therefore now carried a relayed
value produced by our own code path rather than by meshview's.

**What is left of V10 is the private channel, not the arithmetic.** All of the above is the primary
channel; see below.

**What this does not settle.** Everything above happens on the primary channel, and reaching the
community mesh there is necessary rather than sufficient. A foreign node relays a packet it *cannot*
decrypt only while its `rebroadcast_mode` is the default `ALL`; one set to `LOCAL_ONLY` relays its
own channels and drops `CubeSat` without a trace. So the satellite can sit happily in a public node
list having proved nothing about the channel its telemetry and commands actually travel on. That
second measurement is V10 in [`ROADMAP.md`](../ROADMAP.md), taken on channel 1.

**And the internet bridge is not a way in.** A community MQTT gateway cannot uplink the private
channel even in principle: the firmware gates the publish on that channel's own `uplink_enabled` and
builds the topic out of the channel's *name*, neither of which a stranger's node has for `CubeSat`.
bayme.sh separately tells its operators never to enable downlink, so there is no MQTT → RF path
back. Internet reach, if it is ever wanted, means the operator's own gateway node and broker with
`CubeSat` configured on both ends. PKI direct messages are the one exception — they bypass
`uplink_enabled` and publish under a `PKI` topic — and they are outbound-only for the same reason.

## Node identity and channels

Configured on 2026-08-23:

| | |
|---|---|
| Long name | `CubeSat CTM-1` |
| Short name | `CSAT` — 4 characters maximum, this is what appears on maps and in node lists |
| Channel 0 (primary) | stock, default key `AQ==`, empty name — left untouched. An empty name means the firmware uses the preset's own (`LongFast` until 2026-09-02, `MediumFast` since), which is what the automatic frequency slot is hashed from; this node sets the slot explicitly instead |
| Channel 1 (secondary) | `CubeSat`, own randomly generated PSK |

```bash
meshtastic --port /dev/serial0 --set-owner "CubeSat CTM-1" --set-owner-short "CSAT"
```

A new node name propagates on the next node-info broadcast, which defaults to every 3 hours (`nodeInfoBroadcastSecs: 10800`) — it is not immediate.

### Why a secondary channel

**All CubeSat traffic — telemetry and ground commands — goes on channel 1, not on the public primary channel.** Two reasons: bench tests would otherwise clutter the shared primary chat that every node in range reads, and the ground-command path needs to not be world-writable.

Be clear about what this does and does not hide. A secondary channel is a separate encryption key layered on the *same* radio configuration — same frequency, same modem preset. Neighbouring nodes still see that packets are being transmitted, and ordinary mesh housekeeping still goes out on the primary channel in the clear — the primary is the stock public channel with the well-known default key `AQ==`, so "encrypted" there means nothing. What they cannot do is read the contents of channel 1.

What that housekeeping actually contains, since it decides what a public map can show:

- **`NodeInfo`** — node id, `CubeSat CTM-1`, `CSAT`, hardware model, role and the node's public key. Broadcast every `nodeInfoBroadcastSecs` (3 h) and on request. This is the packet a map or a node list is built from; there is no switch that turns it off, and without it the node is unidentifiable to its neighbours.
- **Device telemetry** — battery, voltage, channel utilisation, uptime, roughly every half hour from the telemetry module.
- **Routing and traceroute replies.**
- **No position.** `Position` is a packet type of its own, and the V4 has no GNSS receiver, so the node has nothing to put in one. Nothing in this repository supplies it either: `COMMS` never calls `sendPosition`, and the TEL0157 fix travels inside the beacon *text* on channel 1, encrypted (`comms/beacon.py`). It would take `position.fixed_position` with hand-entered coordinates, or a client app feeding the node its own GPS — a phone does that, the Python library over serial does not.

So a public map can place the node only as coarsely as the gateway that heard it, with the times it was heard. Not a track.

```bash
meshtastic --port /dev/serial0 --ch-add CubeSat            # creates it with a random PSK
meshtastic --port /dev/serial0 --sendtext "..." --ch-index 1
```

### Uplink filtering, measured 2026-09-03

The channel filter in `comms/service.py` decides what may command the satellite, and until this
reading its discrimination rested on an inference about one protobuf key. Measured in `HOSTED`,
`lora_listening: true`, `command_channel: 1`, by sending `!ping` three ways from the operator's
phone and reading the COMMS log:

| Sent on | Arrived as | Outcome |
|---|---|---|
| Public primary channel | `channel 0` | Refused, silently. `refused a LoRa message on channel 0`, 5 bytes, no airtime spent |
| `CubeSat` (channel 1) | `channel 1` | Accepted, and answered — `ack sent: re=ping` **116 ms** after the message was logged |
| Direct message to `CSAT` | `channel 0` | Refused by the same rule. Nobody had looked at what a DM carries before; it needs no rule of its own |

Two things worth keeping beside the table. **The filter met real foreign traffic in the same
window**, unprompted: at 19:27:09, ninety seconds before the test, a stranger's 12-byte line on the
primary (`!1aff93f2`, SNR 6.0) was refused exactly like the test messages — and so never reached the
command parser, the dashboard's Radio Link Log, or `radio_log` on the card. And the sender field was
populated on every one of these, unlike the relayed chat of 2026-09-02 that arrived with a null
`fromId`, which is the observation that made the channel key the credential rather than the node id.

What this reading does **not** settle is whether `channel` is physically absent on the primary or
present as `0`: the log line prints the value the driver has already resolved, so both look
identical from outside. It does not matter — both resolve to the primary index, which is not the
command channel either way — and the dangerous version of the inference is ruled out, because
channel 1 was seen arriving as an explicit `1`. Reading the raw packet dict would need a bench
script with `/dev/serial0` free, i.e. `MAINTENANCE`. The reasoning lives at `CHANNEL_KEY` in
`src/cubesat/hal/rpi/meshtastic_radio.py`.

### The channel URL is a secret — keep it out of this repository

Sharing the channel with a phone or a second node is done with the URL from:

```bash
meshtastic --port /dev/serial0 --qr-all      # prints "Complete URL (includes all channels)"
```

**That URL embeds the channel's pre-shared key, and it is deliberately not recorded here.** Anyone holding it can not only decrypt telemetry but *send commands* — COMMS republishes anything arriving over LoRa onto `cubesat/command`, so a leaked key means a stranger can trigger `safe_mode`, `take_photo` or `set_profile`. Treat it as a secret: environment or personal notes, never `config.yaml`, never a committed document. Regenerate with `--ch-set psk random --ch-index 1` if it is ever exposed, then re-import on every node.

Two practical notes when importing:

- The URL contains **both** channels, so a phone that already has the default primary reports `Channel already exists` if you choose *Add*. Choosing *Replace* is safe as long as the primary in the URL is the stock `LongFast` with the default key (`psk: "AQ=="`, `name: ""` in `--info`) — the result is the same primary plus the new secondary.
- **Renaming a channel changes its hash**, because Meshtastic identifies channels by name *and* key. After a rename every other node must re-import, and until then messages silently fail to decrypt rather than reporting an error. Delete the old entry on the phone so two channels do not sit in the list with only one working.

## Requirements to run on Raspberry Pi

Verified on a Raspberry Pi 4 Model B, Raspberry Pi OS Bookworm, kernel 6.12.

1. **Free up the UART:**
   ```bash
   sudo raspi-config
   # Interface Options → Serial Port → login shell over serial: No, serial hardware: Yes
   ```
2. **`/boot/firmware/config.txt`** — hand the PL011 to GPIO14/15:
   ```
   enable_uart=1
   dtoverlay=disable-bt
   ```
   Without `disable-bt`, Bluetooth owns the PL011 and `/dev/serial0` points at `ttyS0`, the mini-UART, whose baud rate drifts with the core clock.
3. **`/boot/firmware/cmdline.txt`** — remove `console=serial0,115200`, keep `console=tty1`. The kernel console otherwise writes into the same UART and corrupts the protobuf stream. Symptom: `--info` fails with `device reports readiness to read but returned no data (device disconnected or multiple access on port?)`.
4. **Migrating from the 52Pi IoT Node(A)?** Remove `dtoverlay=sc16is752-i2c` — with the board gone it creates no `/dev/ttySC*` devices and only adds confusion.
5. `systemctl disable hciuart` is **not** needed on Bookworm — the unit does not exist (`not-found`); Bluetooth is brought up by `bluetooth.service`, and after `disable-bt` no HCI adapter appears at all.
6. Reboot, then verify:
   ```bash
   ls -l /dev/serial0        # expect: /dev/serial0 -> ttyAMA0
   dmesg | grep ttyAMA       # expect: fe201000.serial: ttyAMA0 ... is a PL011 AXI
   ```
   Before the fixes, that same `fe201000.serial` shows up as claimed by Bluetooth (`hci_uart_bcm serial0-0`).
7. The login user must be in the `dialout` group to open `/dev/serial0` without root.
8. Install the CLI on the Pi — no root required:
   ```bash
   python3 -m venv ~/mesh-venv
   ~/mesh-venv/bin/pip install "meshtastic[cli]"
   ```
   A CLI slightly older than the firmware is fine (2.7.11 against firmware 2.7.26 works).

There is no conflict with GNSS: the 52Pi module is out of the design and the replacement TEL0157 receiver is on I2C.

## Usage examples

**Bash — verify the link and read the whole configuration:**

```bash
ls -l /dev/serial0
~/mesh-venv/bin/meshtastic --port /dev/serial0 --info
```

A healthy reply starts with `Connected to radio` and dumps ~300 lines: `My info`, `Metadata`, the node database and every config block. Useful fields: `pioEnv` (must be `heltec-v4`), `firmwareVersion`, `rebootCount` (climbing on its own means the board is resetting), and `nodedbCount` (> 1 means other nodes are being received).

**Bash — send a message over the air from the Pi:**

```bash
~/mesh-venv/bin/meshtastic --port /dev/serial0 --sendtext "hello from raspberry" --ack
```

For a broadcast, `Received an implicit ACK` means the packet went out and was picked up and rebroadcast by some node — it is a good sign but not a delivery receipt. Confirm on a second node.

**Bash — change settings over the UART** (this also proves the write path, not just reads). Do this only once the two commands above work; it is the last step, because it removes the Bluetooth fallback:

```bash
~/mesh-venv/bin/meshtastic --port /dev/serial0 --set bluetooth.enabled false
```

Verify with `--info` by looking at the `bluetooth.enabled` field, **not** `Metadata.hasBluetooth` — the latter reports the presence of hardware and stays `true`.

Note that `bluetooth.enabled` is the Heltec's own Bluetooth, while `dtoverlay=disable-bt` is the Raspberry Pi's. Different things; both are wanted here.

**Python — send and receive, the shape `src/cubesat/comms/mesh.py` takes:**

```python
import time
from pubsub import pub
from meshtastic.serial_interface import SerialInterface


def on_receive(packet, interface):
    decoded = packet.get("decoded", {})
    print(decoded.get("portnum"), decoded.get("payload"))


pub.subscribe(on_receive, "meshtastic.receive")

iface = SerialInterface("/dev/serial0")
iface.sendText("hello from comms")   # 240 bytes max per message
time.sleep(30)                       # callbacks fire on the interface's own thread
iface.close()
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| ROM log (`ESP-ROM:esp32s3-...`, `rst:0x3 (RTC_SW_SYS_RST)`) repeats forever, no Meshtastic output | The web flasher was run in **Update** mode: it writes only `firmware-heltec-v4-<ver>.bin` (app, 2117408 bytes) to `0x10000` and leaves Heltec's factory bootloader and partition table in place. The app cannot find the partitions it expects and calls `esp_restart()` | Reflash with **Full erase and install**, or `erase_flash` + `write_flash 0x0 ...factory.bin` |
| Same boot loop after a full install | Wrong variant — an `-r8` build (ESP32-S3R8, octal PSRAM) on an S3R2 board | Use target `heltec-v4`; check the flasher log says `Embedded PSRAM 2MB` |
| Flashed fine, but nothing is ever transmitted | `lora.region` is `UNSET` (the default) | `--set lora.region US` |
| `--info` hangs until timeout with no clear error | Baud mismatch, or `T`/`R` swapped | Confirm `serial.baud BAUD_115200`; ring out `T`/`R` against Pi physical pins 8/10 |
| `--info` returns nothing at all, exit code 0, then works on a retry | Garbage at the start of the stream: before the Serial module initialises, the Heltec TX pin floats and the ROM bootloader writes its boot log to those same `GPIO43/44` | Retry. The CLI resynchronises on the frame header; **application code must handle this explicitly** |
| `Resource busy` / `multiple access on port` | Another process holds the port — a browser tab with the web flasher or a serial monitor on the host; on the Pi, the kernel console | Close the tab; remove `console=serial0,115200` from `cmdline.txt` |
| Board reboots during transmission (`rebootCount` climbing) | 915 MHz TX current peaks sagging the Pi 5V rail | Power over USB while debugging, or add a 100–470µF electrolytic across the Heltec supply. **Measured on the assembled satellite this did not occur:** powered from the Pi 5V rail with all four I2C sensors attached, `rebootCount` was 6 before and 6 after a transmission and `vcgencmd get_throttled` returned `0x0`. Watch for it rather than pre-empting it |
| The second node stops hearing the satellite entirely — no packets, not even garbled ones — after a working session | The two nodes are on different modem presets or different frequency slots. They do not demodulate each other at all, so the symptom is silence rather than errors, and `--info` on either node looks perfectly healthy | Compare `lora.modem_preset` **and** `lora.channel_num` on both with `--info`, node against node rather than against a wiki page. The satellite has been on `MEDIUM_FAST` slot 45 since 2026-09-02 |
| `airUtilTx` / `uptimeSeconds` look stale or zero | `deviceMetrics` come from the node database entry and only refresh when the node broadcasts telemetry (default: every 30 minutes) | Do not use them to check whether a transmit happened — confirm on another node, or watch the live log (`--listen`, `--noproto`) |

## Open items

Both of the items that used to be here are closed.

- **The channel index is a configuration value.** `config.LORA_CHANNEL_INDEX` (env `LORA_CHANNEL_INDEX`), next to `LORA_PORT`. If the driver and the ground station disagree, messages transmit and receive perfectly and simply never meet — the hardest kind of radio fault to diagnose, and not one to leave to a constant buried in a driver.
- **Packet size is settled: a compact beacon, not chunking.** The Serial module carries at most 240 bytes and a full telemetry packet runs to several hundred, so the radio sends a single readable `key=value` line — around 101 bytes typically, 122 in the worst plausible case — and the full record stays in DHS to be collected when the satellite is back on a network. One message is one complete observation, where a lost chunk would void a whole packet, and LoRa airtime is duty-cycle limited. It is **never truncated**: when space runs short, whole optional fields are dropped in priority order. The old driver's silent 28-byte truncation is what this replaced.

The rewrite those items waited on has landed: the driver is `hal/rpi/meshtastic_radio.py`, the service is `src/cubesat/comms/`, `config.py` carries `LORA_PORT`/`LORA_BAUDRATE`/`LORA_CHANNEL_INDEX` rather than an I2C address, the tests fake a serial peripheral instead of `smbus`, and `crc16_ccitt()` went with the framing it existed for.

What remains is bench work, not design — see the verification table in [`ROADMAP.md`](../ROADMAP.md):

- **V10, now only the private channel.** The arithmetic `hops = hopStart − hopLimit` was measured on 2026-09-02 — see [Coverage](#coverage-measured-2026-09-02) — but on the primary channel, which carries none of this satellite's traffic. Whether a foreign node relays channel 1 at all depends on its `rebroadcast_mode`, and that reading still wants a packet reaching the personal node from beyond direct range.
- A two-way ground link needs an SX1262 receiver attached to the ground station as well — a USB Meshtastic node, in the current plan for the ground segment.

## Further reading

- [Heltec Wiki — WiFi LoRa 32 V4](https://wiki.heltec.org/docs/devices/open-source-hardware/esp32-series/lora-32/wifi-lora-32-v4/) — board overview and resources
- [V4 pin map](https://resource.heltec.cn/download/WiFi_LoRa_32_V4/Pinmap/V4_pinmap.png) — authoritative J2/J3 pinout
- [Meshtastic Serial module](https://meshtastic.org/docs/configuration/module/serial/) — modes, pins, baud rates
- [Meshtastic LoRa region config](https://meshtastic.org/docs/configuration/radio/lora/) — regions and modem presets
- [Meshtastic Python CLI](https://meshtastic.org/docs/software/python/cli/installation/) — installation and command reference
- [SerialModule.cpp](https://github.com/meshtastic/firmware/blob/master/src/modules/SerialModule.cpp) — firmware-side implementation of the `PROTO` API
- [DFRobot DFR0566 wiki](https://wiki.dfrobot.com/dfr0566) — IO Expansion HAT pinout and Gravity connector voltages
