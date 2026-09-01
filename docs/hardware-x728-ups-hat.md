# X728 V2.5 UPS HAT

Battery power management HAT used by the **EPS** subsystem (`src/eps/`). Provides LiPo/18650 fuel-gauge readings and AC-loss detection so the OBC state machine can react to a degrading power budget (`NOMINAL` → `LOW_POWER` → `SAFE`, see `src/obc/state_machine.py`).

- **Product:** [Geekworm X728 V2.5](https://www.aliexpress.us/item/3256804825472151.html)
- **Official docs:** [Geekworm Wiki — X728](https://wiki.geekworm.com/X728), [X728-script (current setup guide)](https://wiki.geekworm.com/X728-script), [firmware/scripts repo](https://github.com/geekworm-com/x728-script)

## Specification

| | |
|---|---|
| Fuel gauge | **MAX17048** — confirmed on this unit, not the MAX17040: the `CRATE` register (`0x16`) responds, and it exists only on the MAX17048. `VERSION` (`0x08`) reads `0x0002` |
| Fuel gauge interface | I2C, address `0x36` |
| Battery voltage register | `0x02` (`VCELL`, word, MSB first) — `voltage_V = raw >> 4 * 0.00125` |
| Battery SOC register | `0x04` (`SOC`, word, MSB first) — `percent = raw / 256.0` |
| Input power | 5V ±5%, ≥3A via USB-C or ≥4A via DC5521 barrel jack |
| Output power | 5.1V ±5%, up to 5A |
| Charge current | 2.3–3.2A |
| Battery cutoff / recharge threshold | 4.24V (terminal) / 4.1V (recharge) |
| Onboard RTC | DS1307, I2C address `0x68` — physically present and confirmed, but never initialised. See [The DS1307 at 0x68](#the-ds1307-at-0x68) |
| Shutdown-signal GPIO (BCM, official scripts) | GPIO5 (pulse input, read by the power daemon) |
| Boot-indicator GPIO (BCM, official scripts) | GPIO12 (driven high once boot completes) |
| Software-shutdown trigger GPIO (BCM, official scripts) | GPIO26 (V2.0–V2.5; GPIO13/16 on v1.2/v1.3) — pulled high to signal the HAT's MCU to cut 5V rail |
| Charge-control jumper (V2.5 only) | GPIO16 — for advanced use, controls charging when the "CHG Ctrl" jumper is open |
| AC power-loss detection (PLD) | Dedicated GPIO input; `0` = AC present, `1` = AC lost (this project reads it on **BCM GPIO6**, see `src/eps/power_monitor.py` — `PLD_PIN = 6`) |
| Buzzer control (V2.1+) | GPIO20 — drives the onboard buzzer, not used by this project |

> The GPIO5/12/26 pins above are Geekworm's own safe-shutdown daemon (`xPWR.sh`/`xSoft.sh`), which this project does **not** currently wire up — the EPS service only *reads* battery and AC status over MQTT; it does not drive a graceful auto-poweroff sequence itself. AC-loss detection is read on GPIO6, matching `EPSMonitor.get_external_power()`.

### GPIO pins reserved by this HAT (stacking compatibility)

Since X728 is a HAT that sits on the Raspberry Pi's 40-pin header, any other HAT stacked on top of it (or plugged into a pass-through header) shares the same physical GPIO pins. Before wiring a sensor/breakout to a specific GPIO through another HAT, check it doesn't collide with the pins below:

| BCM GPIO | Physical pin | Used for |
|---|---|---|
| GPIO2 / GPIO3 | 3 / 5 | I2C bus (fuel gauge `0x36`, optional RTC `0x68`) |
| GPIO5 | 29 | Power-button / shutdown-daemon pulse input |
| GPIO6 | 31 | AC power-loss detection (PLD) — read by this project |
| GPIO12 | 32 | Boot-indicator (driven high once boot completes) |
| GPIO13 | 33 | Software-shutdown trigger (v1.2/v1.3 only) |
| GPIO16 | 36 | Charge-control jumper (v2.5) / software-shutdown trigger (v1.2/v1.3) |
| GPIO20 | 38 | Buzzer control (v2.1+) |
| GPIO26 | 37 | Software-shutdown trigger (v2.0–v2.5, replaces GPIO13) |

Exact pin usage depends on the board revision (v1.2/v1.3 vs. v2.0+) — check silkscreen/jumper markings on the specific unit before assuming a pin is free.

## Requirements to run on Raspberry Pi

1. **Enable I2C:**
   ```bash
   sudo raspi-config
   # Interface Options → I2C → Enable
   sudo reboot
   ```
2. **Install I2C tooling and Python bindings:**
   ```bash
   sudo apt-get install -y i2c-tools python3-smbus
   pip install smbus2 RPi.GPIO
   ```
3. **Verify the fuel gauge is visible on the bus:**
   ```bash
   i2cdetect -y 1   # expect 0x36 (and 0x68 if the RTC is populated)
   ```
4. GPIO access requires the user to be in the `gpio` group (default on Raspberry Pi OS) or run as root — `RPi.GPIO` in this project sets up `PLD_PIN` (BCM 6) as an input in `EPSMonitor.__init__`.

## Usage examples

**Bash — confirm the device is on the bus:**
```bash
i2cdetect -y 1
```

**Bash — read raw registers with i2c-tools (sanity check outside Python):**
```bash
i2cget -y 1 0x36 0x02 w   # VCELL
i2cget -y 1 0x36 0x04 w   # SOC
```

**Python — as implemented in this project (`src/eps/power_monitor.py`):**
```python
import smbus2
import RPi.GPIO as GPIO

I2C_BUS, BATTERY_I2C_ADDR = 1, 0x36
REG_VCELL, REG_SOC = 0x02, 0x04
PLD_PIN = 6

bus = smbus2.SMBus(I2C_BUS)
GPIO.setmode(GPIO.BCM)
GPIO.setup(PLD_PIN, GPIO.IN)

def read_word(reg):
    msb = bus.read_byte_data(BATTERY_I2C_ADDR, reg)
    lsb = bus.read_byte_data(BATTERY_I2C_ADDR, reg + 1)
    return (msb << 8) | lsb

voltage = round((read_word(REG_VCELL) >> 4) * 0.00125, 3)
percent = round(read_word(REG_SOC) / 256.0, 2)
external_power = GPIO.input(PLD_PIN) == 0  # 0 = AC OK, 1 = AC lost

print(f"Battery: {percent}% @ {voltage}V, external_power={external_power}")
```

**Run the EPS service directly:**
```bash
source venv/bin/activate
PYTHONPATH=. python -m src.eps.main
```

## The DS1307 at 0x68

`0x68` on the I2C bus is this HAT's onboard DS1307 real-time clock. It was an unexplained address for a while, because it predates every sensor in the design and is not mentioned in the fuel-gauge documentation.

Confirmed by four independent signs, read on 2026-08-23:

| Evidence | Value |
|---|---|
| `CH` bit (register `0x00`, bit 7) | `1` — oscillator halted |
| Time registers `0x00`–`0x06` | `2000-01-01 00:00:00`, the DS1307 power-on default |
| `CONTROL` register `0x07` | `0x03` |
| Battery-backed NVRAM from `0x08` | readable — 56 bytes, a DS1307 signature |

**It is now enabled** (2026-08-23). Before that it was unused: no `i2c-rtc` overlay was loaded, there was no `/dev/rtc`, `timedatectl` reported `RTC time: n/a`, and the system clock came only from NTP — which for a satellite simulation means no notion of time at all once the network goes away, and telemetry timestamped `2000-01-01`.

What was needed:

```
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=10000
dtoverlay=i2c-rtc,ds1307        # /boot/firmware/config.txt, then reboot
```

What to expect afterwards:

- `dmesg` shows `rtc-ds1307 1-0068: registered as rtc0`, and `/dev/rtc → rtc0` appears.
- `rtc-ds1307 1-0068: hctosys: unable to read the hardware clock` on the first boot is **normal** — the clock had never been set, so there was nothing to read.
- **`0x68` disappears from `i2cdetect` and shows as `UU`.** The kernel driver has claimed the address; this is the expected result, not a fault. It also means user-space code must not poke `0x68` over `smbus2` any more — go through `/dev/rtc` instead.
- **`hwclock` does not exist on Raspberry Pi OS Bookworm** by default (it lives in the `util-linux-extra` package) and is not needed: `systemd-timesyncd` writes the system time into the RTC by itself once `timedatectl show -p NTPSynchronized` turns `yes`. No manual `hwclock -w` step was required.
- `fake-hwclock` is not installed on this image, so there is no second component competing to set the clock at boot. On images that do have it, remove it once a real RTC is working.

Verifying the oscillator actually started — the `CH` bit in register `0x00` had been set, halting it:

```bash
cat /sys/class/rtc/rtc0/since_epoch    # read twice, a few seconds apart
```

If the value advances by the elapsed time, the clock is running. Measured: +5 over a 5 second gap. `timedatectl` then showed `RTC time` one second behind `Universal time`, which is simply the DS1307's one-second resolution, not drift.

## Verified readings

Measured on the assembled satellite, 2026-08-23, running on mains power:

```
VCELL (0x02) raw=51760 -> 4.044 V
SOC   (0x04) raw=20379 -> 79.61 %
CRATE (0x16)          -> -0.21 %/hour   (idle; the sign indicates discharge)
GPIO6 (PLD)           -> low = AC present
```

4.044 V against 79.6 % is self-consistent for a Li-ion cell, and `CRATE` near zero matches a battery that is neither charging nor meaningfully loaded. `CRATE` is a cheap way to distinguish "on mains" from "running down" without waiting for `SOC` to move, and this project does not read it yet.

### SOC drifts down while the voltage stands still (2026-08-31, unresolved)

On the first run of the flight software, with the satellite on mains and freshly powered on, EPS
published this over eight minutes:

```
t+0s    battery_percent 72.41   voltage 3.905 V   charge_rate -0.208 %/h   external_power true
t+209s  battery_percent 71.43   voltage 3.906 V   charge_rate -0.208 %/h   external_power true
t+498s  battery_percent 70.28   voltage 3.906 V   charge_rate -0.208 %/h   external_power true
```

That is ≈ **−15 %/hour** by SOC against a terminal voltage that does not move and a `CRATE` of
exactly one LSB — the same "about zero" the 2026-08-23 bench reading recorded. The three do not
agree, and the voltage is the one to believe: a Li-ion cell shedding 15 %/hour in this part of the
curve would drop several millivolts, and 3.905 → 3.906 is one LSB of noise upward.

Most likely the gauge is still converging after power-on — the MAX17048's ModelGauge algorithm
estimates from the open-circuit voltage and settles over time, and this project never issues a
quick-start. The absolute numbers are not absurd (79.6 % at 4.044 V in August against 70 % at
3.905 V now is a consistent pair), so this looks like drift rather than a broken scale factor.

**It is not settled, and the constant must not be adjusted to make it look better** — see V12 in
[`ROADMAP.md`](../ROADMAP.md). What would settle it is a multi-hour log of all three fields on
mains, then the same on battery. Note also that nothing about this is currently dangerous: the
power policy suppresses every descent while `external_power` is true and `charge_rate` is above
−1.0 %/hour, so a false SOC is inert — until `CRATE` crosses that line.

### Is it charging at all? (2026-08-31, open)

Later the same evening, after several hours plugged in:

```
voltage 3.920 V   battery_percent 66.14   charge_rate -0.208 %/h   external_power true
```

and the board's own charge-level LEDs agreeing with roughly that level. Compared against the
first sample of the evening, **the voltage rose 3.905 → 3.920 V while the SOC fell 72.41 → 66.14 %
over 85 minutes** — opposite directions, which settles that the SOC register is not describing the
cell. It also hints the pack may be taking a trickle rather than nothing at all.

But it is not charging in earnest, and by this document's own specification table it should be:
the recharge threshold is **4.1 V** and 3.92 V is well below it. So "the UPS deliberately holds a
partial charge" does not fit the numbers. Things to check, cheapest first:

1. **The `CHG Ctrl` jumper.** On the V2.5 board, opening it moves charge control to **GPIO16**.
   Nothing in this repository drives that pin — the only GPIO the code touches is the PLD input on
   GPIO6 — so an open jumper means charging is gated by a floating input. Check it physically.
2. **The cells.** Unbranded 18650s that no longer hold a charge look exactly like this.
3. Ruled out already: the 5 V supply. `vcgencmd get_throttled` reads `0x0`, so the rail is not
   sagging.

If the jumper is the answer, it is worth keeping open rather than closing: GPIO16 would let EPS
decide *when* to charge. Holding a partial charge on the desk is genuinely better for a Li-ion
cell than sitting at 4.2 V for weeks, and a deliberate top-up before a `FLIGHT` outing is exactly
the behaviour wanted. That turns a puzzle into a feature — but only after the jumper is looked at,
because right now nobody knows which of the three explanations is true.

Tracked as V13 in [`ROADMAP.md`](../ROADMAP.md). **Do not interpret the gauge drift above until
this is settled:** a cell that is being trickle-charged and one that is not are different problems.

Note that the I2C bus runs at 10 kHz project-wide (see the [I2C address map](../README.md#i2c-address-map)); this is well within the DS1307's and MAX17048's standard-mode range and needs no adjustment here.

## Further reading

- [Geekworm Wiki — X728](https://wiki.geekworm.com/X728)
- [Geekworm Wiki — X728-script (setup/install steps, V2.5 GPIO16 charge-control note)](https://wiki.geekworm.com/X728-script)
- [geekworm-com/x728-script on GitHub](https://github.com/geekworm-com/x728-script) — `xPWR.sh`, `xSoft.sh`, systemd unit
- MAX17048 datasheet (Maxim Integrated) for the full fuel-gauge register map, including `CRATE` (`0x16`, 0.208 %/hour per LSB) and `STATUS` (`0x1A`), neither of which this project reads yet
- DS1307 datasheet (Maxim Integrated) for the RTC register map and the `CH` clock-halt bit
