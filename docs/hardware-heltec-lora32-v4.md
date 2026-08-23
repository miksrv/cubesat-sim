# Heltec WiFi LoRa 32 V4 (Meshtastic)

LoRa ground link for **COMMS**, replacing the LoRa half of the [52Pi IoT Node(A)](hardware-iot-node-a-52pi.md). The board runs **stock Meshtastic firmware** and is a self-contained radio: the Raspberry Pi talks to it over UART using Meshtastic's Serial module in `PROTO` mode, and Meshtastic handles framing, CRC, retries, acknowledgements and encryption on its own. The custom `length + payload + CRC-16-CCITT` framing used against the 52Pi bridge is therefore obsolete here.

> **Status:** the radio link is bench-verified end to end (Pi → UART → Heltec → air → second Meshtastic node, and back). `src/comms/lora.py` still contains the old `smbus2`/SC16IS752 implementation and has **not** been rewritten against `meshtastic` yet — see [Open items](#open-items) and `ROADMAP.md` item G10.

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
| Modem preset | `LONG_FAST` (default) — must also match on receiving nodes |
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

Confirm the test message arrives on a second Meshtastic node before going further. Receiving nodes must agree on region (`US`), modem preset (`LONG_FAST`) and channel PSK — the default `LongFast` channel with the default key converges by itself.

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

**Python — send and receive, the shape `src/comms/lora.py` should take:**

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
| `airUtilTx` / `uptimeSeconds` look stale or zero | `deviceMetrics` come from the node database entry and only refresh when the node broadcasts telemetry (default: every 30 minutes) | Do not use them to check whether a transmit happened — confirm on another node, or watch the live log (`--listen`, `--noproto`) |

## Open items

- **Packet size is unresolved and blocks `COMMS_LORA_ENABLED=1`.** The Serial module carries up to 240 bytes per message, while an aggregated COMMS packet (eps + adcs + payload + system) runs to several hundred. `src/comms/lora.py:33` currently truncates the payload to 28 bytes, so `src/comms/service.py:293` transmits a silently mangled packet. Decide between a compact beacon field set and chunking before enabling the channel.
- `src/comms/lora.py` still targets `smbus2`/SC16IS752 and needs rewriting on top of `meshtastic`. `src/common/config.py` and `config/config.yaml` still carry `LORA_I2C_ADDRESS` instead of `LORA_PORT` / `LORA_BAUDRATE`.
- `tests/test_comms_lora.py` mocks `smbus`; it needs reworking (`tests/test_common_gps_a9g.py` is a good model for faking a serial peripheral).
- `crc16_ccitt()` in `src/common/utils.py` loses its only consumer once the rewrite lands — decide whether to keep it.
- A two-way ground link needs an SX1262 receiver attached to the ground station as well.

## Further reading

- [Heltec Wiki — WiFi LoRa 32 V4](https://wiki.heltec.org/docs/devices/open-source-hardware/esp32-series/lora-32/wifi-lora-32-v4/) — board overview and resources
- [V4 pin map](https://resource.heltec.cn/download/WiFi_LoRa_32_V4/Pinmap/V4_pinmap.png) — authoritative J2/J3 pinout
- [Meshtastic Serial module](https://meshtastic.org/docs/configuration/module/serial/) — modes, pins, baud rates
- [Meshtastic LoRa region config](https://meshtastic.org/docs/configuration/radio/lora/) — regions and modem presets
- [Meshtastic Python CLI](https://meshtastic.org/docs/software/python/cli/installation/) — installation and command reference
- [SerialModule.cpp](https://github.com/meshtastic/firmware/blob/master/src/modules/SerialModule.cpp) — firmware-side implementation of the `PROTO` API
- [DFRobot DFR0566 wiki](https://wiki.dfrobot.com/dfr0566) — IO Expansion HAT pinout and Gravity connector voltages
