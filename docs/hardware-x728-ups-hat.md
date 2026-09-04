# X728 V2.5 UPS HAT

Battery power management HAT used by the **EPS** subsystem (`src/cubesat/eps/`). Provides LiPo/18650 fuel-gauge readings and AC-loss detection so the OBC state machine can react to a degrading power budget (`NOMINAL` → `LOW_POWER` → `SAFE` → `CRITICAL` — the thresholds live in `src/cubesat/obc/power_policy.py`, the legal moves in `mission_machine.py`).

- **Product:** [Geekworm X728 V2.5](https://www.aliexpress.us/item/3256804825472151.html)
- **Official docs:** [Geekworm Wiki — X728](https://wiki.geekworm.com/X728), [X728-script (current setup guide)](https://wiki.geekworm.com/X728-script), [firmware/scripts repo](https://github.com/geekworm-com/x728-script)

## Specification

| | |
|---|---|
| Fuel gauge | **MAX17040/41** (corrected 2026-09-01; the 2026-08-23 bench note called it a MAX17048 because `CRATE` "responded" — it responds `0xFFFF`, which is what an unimplemented register returns, see [The gauge is a MAX17040/41](#the-gauge-is-a-max1704041-not-a-max17048-2026-09-01)). `VERSION` (`0x08`) reads `0x0002`, `CONFIG` (`0x0C`) `0x9700` |
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
| AC power-loss detection (PLD) | Dedicated GPIO input; `0` = AC present, `1` = AC lost (this project reads it on **BCM GPIO6**, see `src/cubesat/hal/rpi/max17048.py` — `PLD_PIN = 6`, read by the same driver that reads the gauge, because both are this one HAT) |
| Buzzer control (V2.1+) | GPIO20 — drives the onboard buzzer, not used by this project |

> The GPIO5/12/26 pins above are Geekworm's own safe-shutdown daemon (`xPWR.sh`/`xSoft.sh`), which this project does **not** wire up: EPS only *reads* the pack and the mains pin and publishes them. Powering the host down belongs to `CRITICAL` and travels through HOSTD, which is the only privileged process here — see `docs/concept.md`.

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
4. GPIO access requires the service account to be in the `gpio` group — the `cubesat@eps` unit grants it — or to run as root. `RPi.GPIO` (in practice the `python3-rpi-lgpio` shim) sets `PLD_PIN` (BCM 6) up as an input on the driver's first read, not at construction, so importing the module off a Pi costs nothing.

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

**Python — the shape of the driver in `src/cubesat/hal/rpi/max17048.py`:**
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

4.044 V against 79.6 % is self-consistent for a Li-ion cell. The `CRATE` line, however, was misread:
`-0.21 %/hour` is `0xFFFF` decoded as a signed word, and the register does not exist on this part —
see the 2026-09-01 section below. There is no charge-rate register to read on this board.

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

Most likely the gauge is still converging after power-on — the ModelGauge algorithm this family
runs estimates from the open-circuit voltage and settles over time, and this project never issues a
quick-start. The absolute numbers are not absurd (79.6 % at 4.044 V in August against 70 % at
3.905 V now is a consistent pair), so this looks like drift rather than a broken scale factor.

**It is not settled, and the constant must not be adjusted to make it look better.** The `CRATE`
half of the puzzle turned out to be a misidentified part rather than a measurement — see the next
section — so what remains of this reading is now V13's question in
[`ROADMAP.md`](../ROADMAP.md): whether the pack was being charged at all. What would settle it is a
multi-hour log of voltage, SOC and the fitted rate on mains, then the same on battery.

**How the mechanics changed on 2026-09-01, and it is not in this reading's favour.** While
`charge_rate` came off the register it was a constant `−0.208 %/h`, comfortably above
`DRAINING_PERCENT_PER_HOUR` (−1.0), so `on_mains` was satisfied unconditionally and a false SOC was
inert. EPS now *fits* the rate to the SOC history, so a drift like the one above computes as roughly
−15 %/h, which is below the threshold: the satellite would read as "plugged in but draining", the
suppression of the power-driven descents would lift, and a lying gauge could walk it down towards
`CRITICAL`. That is the clause working exactly as designed — one failed charger must not disable the
protection for ever — but it is why the drift is worth understanding rather than filing.

### The gauge is a MAX17040/41, not a MAX17048 (2026-09-01)

Read on the assembled satellite under the bus lock (`~/test/venv`, `smbus2`, block reads of two
bytes), three times eight seconds apart, on battery:

```
VCELL  (0x02) 0xBE70 -> 3.809 V
SOC    (0x04) 0x3E19 -> 62.10 %
MODE   (0x06) 0x0000
VERSION(0x08) 0x0002
HIBRT  (0x0A) 0xFFFF      <- MAX17048/49 only
CONFIG (0x0C) 0x9700
CRATE  (0x16) 0xFFFF      <- MAX17048/49 only
STATUS (0x1A) 0xFFFF      <- MAX17048/49 only
```

Every register the MAX17040/41 shares with the MAX17048 reads a plausible value, and every register
that exists *only* on the MAX17048/49 reads all ones — the bus-level signature of an address nobody
answers. `CONFIG = 0x9700` is the factory `RCOMP` of `0x97` over an empty low byte, which is the
MAX17040/41 layout (the MAX17043/44 and MAX17048 keep an alert threshold there and default to
`0x971C`). Geekworm's X728 software page links the `MAX17040-MAX17041` datasheet as the gauge's
documentation. The 2026-08-23 identification rested on "`CRATE` responds" — it responds with `0xFFFF`.

What this changes:

- **`charge_rate` has never been a measurement.** `-0.208 %/h` is `0xFFFF` as a signed 16-bit word,
  `-1 LSB × 0.208`. It read that at rest, on mains, and now on battery while `SOC` was falling at
  ≈ 22 %/hour under the full `DEMO` load. The "~70× disagreement" in the section above was a
  comparison against a constant.
- **`SOC` on battery looks honest.** 63.4 → 61.7 % in five minutes with eight services and the camera
  pipeline running is what the pack size predicts; the still-unexplained drift is the one *on mains*,
  which is the question below.
- **The power policy's mains check was half-dead.** `on_mains` treats a rate above `−1.0 %/h` as
  "not draining", and a constant `−0.208` satisfied that unconditionally, so the clause meant to
  notice a failed charger could never fire. Fixed the same day: the driver no longer reads `0x16` and
  reports no rate; **EPS computes `charge_rate` itself** as a least-squares slope over ten minutes
  of `SOC` readings (`eps/charge_rate.py`), publishing `null` until it has five minutes of history
  and again after the mains pin changes. With the rate a real measurement, the second half of the
  check means something again.
- The driver file is still named `max17048.py`; the register map it actually uses (`VCELL`, `SOC`,
  `VERSION`, `CONFIG`) is the one both families share, so it is correct as far as it reads.

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

1. ~~**The `CHG Ctrl` jumper.**~~ **Checked 2026-09-01: the jumper is installed.** Per Geekworm's
   X728-script page, *shorted* means "battery automatic charging when power adapter connected" and
   *open* hands control to GPIO16 (high = charging enabled, low = disabled). So charging is not
   gated by a floating input, and this explanation is ruled out. The GPIO16 idea below still
   stands as a *future* feature, no longer as a diagnosis.
2. **The adapter and where it is plugged in.** The X728 charges only from its own DC jack, and the
   charger wants 2.3–3.2 A on top of the Pi's load; a 5 V supply feeding the Pi's USB-C, or one too
   weak for both, keeps the Pi up and the pack flat. `PLD` reading "AC present" proves the jack sees
   power, not that it sees enough of it. `vcgencmd get_throttled` = `0x0` clears the 5 V *output*
   only.
3. **The cells.** Unbranded 18650s that no longer hold a charge look exactly like this.

The decisive check is now cheap, because `charge_rate` is a measurement: plug the satellite in
and watch `voltage` and `charge_rate` for ten minutes. A charger delivering current lifts the
terminal voltage by tens of millivolts within seconds and turns the fitted rate positive after the
five-minute window; the 2026-08-31 evening on mains showed neither (3.905 → 3.920 V over 85 minutes,
SOC falling), which is what "not charging" looks like from the bus.

**Measured 2026-09-01, 21:53–22:08, CanaKit 5.1 V / 3.5 A adapter on the X728's USB-C input,
`DEMO` running (eight services, camera pipeline warm), jumper installed.** The pack *is* charging,
and slowly:

```
21:53:03  50.39 %  3.774 V  on battery, -18.65 %/h
21:53:33  50.39 %  3.824 V  plugged in  (+50 mV at once: the load came off the cell)
22:02:33  50.39 %  3.844 V
22:04:33  50.78 %  3.846 V  first SOC step upwards, 11 minutes in
22:08:03  50.78 %  3.848 V  charge_rate +3.28 %/h
```

LEDs: one steady, one blinking, two dark — the board's own "charging at the 25–50 % level". So the
charger works and the cells accept charge; what is missing is current. +3 %/h into a two-cell pack
is on the order of 150 mA, against the 2.3–3.2 A the board advertises, and the terminal voltage
rising 17 mV in a quarter of an hour agrees — real charging current lifts a cell at 3.8 V far more
than that. Two readings of this are consistent with the numbers: the USB-C input is rated "≥ 3 A"
and the Pi's own load comes out of the same 3.5 A before the charger sees anything; or the charger
throttles when the adapter's 5 V sags under load, as most such controllers do. Both point the same
way — **try a 5.1 V, ≥ 4 A supply on the DC 5.5 × 2.1 jack**, which is the input Geekworm rates for
the full charging current. At the measured rate, 50 → 100 % takes about seventeen hours; a
satellite that comes home flat is not ready for the next morning.

The 2026-08-31 evening reading (SOC *falling* on mains) still does not fit this picture and is left
recorded above as observed; one difference is that today's plug-in followed a real discharge, while
yesterday's gauge may have been re-converging its estimate after a long idle.

### The SOC register drifts on mains, and it nearly powered the satellite off — 2026-09-03

That 2026-08-31 anomaly is not an anomaly. It was reproduced deliberately, and it turned out to be
the normal behaviour of this gauge: **on mains the modelled state of charge falls while the cell
does nothing at all.**

Measured in `HOSTED` (three services, idle bus), USB-C on the X728, LEDs one steady and one
blinking — the board's own "charging" — as one continuous series across an unplug and back:

```
on mains      50.39 → 48.09 %   3.806…3.809 V   fitted:   0 mV/h,  SOC −8…−10 %/h
unplugged     47.71 %           3.809 → 3.759 V  −50 mV inside one publish
on the pack   47.71 → 44.46 %   3.759 → 3.729 V  fitted: −197 mV/h, SOC −24.5 %/h
```

Three things fall out of it, and the third one is a defect:

1. **The SOC steps are quantised and regular** — ~0.39 % (100 LSB of the `word / 256` decode) every
   two to three minutes on mains, arriving as discrete jumps rather than as a drift. That is a model
   settling, not a cell discharging. This part has no shunt and no coulomb counter: state of charge
   is reconstructed from voltage and an internal model, so it is the one quantity here that is *not*
   measured.
2. **Voltage separates the two regimes by two orders of magnitude**, 0 mV/h against −197 mV/h, and
   the discharge slope is 8.0 mV per percent at this level. SOC separates them barely: −8 %/h and
   −24 %/h are both "falling".
3. **`charge_rate` fitted to that model walked the power policy towards `CRITICAL` on a desk.**
   `on_mains` required the pin *and* a rate above −1 %/h, so with the pin true and the fitted rate
   at −9 %/h the satellite considered itself on battery while plugged in. `SAFE` (20 %) and
   `CRITICAL` (10 %) do not ask what state the satellite is in, so from 49 % this was ≈3.5 h from
   `SAFE` and ≈5 h from powering the host off — with mains present the whole time, which is exactly
   the case that leaves the X728 unable to bring it back, because mains never left.

   The irony is worth recording: this was a **regression introduced by fixing something else**.
   Until 2026-09-03 `charge_rate` was the decoded `0xFFFF` constant −0.208 %/h, which happened to
   sit above the −1 %/h threshold and so passed the test for the wrong reason. Replacing the
   constant with an honest fit made the number real — and it was really measuring the model.

**The fix keeps both slopes and asks the measured one first**: EPS now also publishes
`voltage_rate` in mV/h, fitted over the same window, and `on_mains` treats the pack as draining only
when the voltage *and* the charge agree that it is (`obc/power_policy.py` → `DRAINING_MV_PER_HOUR`,
−30 mV/h). A charger that has genuinely died moves both, because the pack then supplies the load; a
settling model moves only one. No test could have caught this — the mock gauge reports whatever the
test hands it, and what was wrong was the register map, not the logic.

*That fix lasted one day and was then finished properly; see
[The percentage is now derived from the voltage](#the-percentage-is-now-derived-from-the-voltage-and-the-thresholds-are-volts--2026-09-04)
below.*

The charging question itself is unchanged and still open: the DC 5.5 × 2.1 jack with a 5.1 V ≥ 4 A
supply has still not been tried.

If the jumper is ever opened on purpose, GPIO16 would let EPS decide *when* to charge. Holding a
partial charge on the desk is genuinely better for a Li-ion cell than sitting at 4.2 V for weeks,
and a deliberate top-up before a `FLIGHT` outing is exactly the behaviour wanted. Note what the
2026-09-04 test below implies about the cost: charging on this board appears to require the board to
be powered on, so handing the decision to EPS would mean a satellite that only ever charges while
its software is running.

### Charging is an activity of the powered-on state — 2026-09-04

Ten hours plugged in with the Pi shut down (`poweroff`, then a 10-second press on the X728's button
to cut the 5 V rail — the LEDs on the sensor boards were still lit before that press, which is worth
knowing on its own: a `poweroff` alone leaves the whole 5 V bus powered). Result: **the charge LEDs
read the same in the morning as the night before — one steady, one blinking, two dark — and the
terminal voltage had barely moved.** Even the low charge rate measured on 2026-09-01 would have put
about 25 % into the pack over that time and moved the indicator a level.

Two things follow.

**The overnight test measured nothing about charging current**, which is what it was meant to do. It
was run precisely because there was no load competing for the adapter's current, so a result of
"nothing happened" cannot distinguish a starved charger from a disabled one.

**Geekworm's own documentation contradicts itself here, and the observation picks a side.**
[X728-script](https://wiki.geekworm.com/X728-script) says a shorted `CHG Ctrl` means "battery
automatic charging when power adapter connected", with no mention of the host's state, while
[X728](https://wiki.geekworm.com/X728) describes the V2.5 feature as "choose to charge when boot up
by short *CHG EN* and stop charging at shutdown". The jumper on this unit is shorted, and the
satellite did not charge while off, which matches the second wording. The most likely mechanism is
that the enable line is fed from a rail that the long press takes down with everything else.

The practical rule for operating this satellite: **leave it running while it charges.** `HOSTED` is
the right profile for it — three services, minimal load, and the pack still gets most of whatever
the input can spare. "Put it on charge overnight and switch it off" does not work on this board in
any jumper configuration: shorted, the charger follows the board's own state; open, it follows
GPIO16, which needs a running Pi to hold high.

Also corrected here: the "~150 mA" charging figure recorded above came from the gauge's percentage
and is therefore not a measurement. Taken from the voltage instead — +17 mV in 15 minutes, i.e.
68 mV/h, against a curve gradient of about 7 mV per point — the same series reads as roughly 8 %/h
and 400–500 mA. Still far below the board's rating, but the difference matters for diagnosis: half
an amp with the Pi drawing about one from a 3.5 A adapter is the signature of a starved input, not
of a broken charger.

### The percentage is now derived from the voltage, and the thresholds are volts — 2026-09-04

The end of the story that began with a misidentified part. The 2026-09-03 fix took the gauge's
modelled state of charge out of the *mains* decision but left it deciding `LOW_POWER`, `SAFE` and
`CRITICAL` — so the two thresholds whose whole job is to protect the filesystem were still comparing
a number this board reconstructs from a model with no current sense behind it. The remedy is not a
better percentage:

- **`obc/power_policy.py` compares volts.** `LOW_POWER_VOLTS` 3.64, `SAFE_VOLTS` 3.58,
  `CRITICAL_VOLTS` 3.45, `RECOVERY_VOLTS` 3.75. `CRITICAL` sits 450 mV above the X728's own 3.0 V
  cutoff, which at the measured idle discharge is over two hours of margin for a flush and a
  poweroff.
- **`common/battery.py` holds one voltage-to-percentage curve**, used for display only: the
  dashboard, the beacon's `b` field, `cubesat status`, the `battery` column, and the two
  time-remaining estimates. It is **inferred from a generic 18650 curve, not measured on this pack**,
  and it is marked as such at the constant. One point agrees with it — 3.759 V read 47.7 % on the
  gauge, against 47 % on the curve — which is a coincidence rather than a calibration.
- **The gauge's own figure is still published and still recorded**, as `gauge_percent` and in its own
  column. Two reasons, both about the record: the pair over a few missions is what will confirm or
  replace the inferred curve, and a gauge that has already been wrong in one known way is best
  watched for going wrong in another.
- **One slope, not two.** With the percentage derived from the voltage, its slope is the voltage
  slope restated, so `on_mains` asking both was a condition that could not be false. `charge_rate`
  is now `voltage_rate` through the curve's local gradient, published because %/h is what a person
  reads, and consulted by nothing.
- **The level compared is a 120 s median** (`eps/slopes.py` → `MedianWindow`). A modelled percentage
  changed slowly by construction; a terminal voltage drops the moment a load appears, and this
  satellite's camera pipeline is worth tens of millivolts against thresholds 60–130 mV apart. The
  raw sample is published and recorded beside the median, so a chart can still show the dip that the
  policy is not allowed to act on.

**What this does not fix.** Nothing here makes the pack charge faster, and nothing here measures the
curve. Both wait on the bench: V13 for the charging input, and **V15** for one full discharge from
4.2 V to cutoff, which is what turns the curve and the four thresholds from estimates into
measurements — and which will also give the first real answer about this pack's capacity, currently
estimated at about 3.3 Ah for the pair against 3.5 Ah each on the label.

Tracked as V13 in [`ROADMAP.md`](../ROADMAP.md). **Do not interpret the gauge drift above until
this is settled:** a cell that is being trickle-charged and one that is not are different problems.

Note that the I2C bus runs at 10 kHz project-wide (see the [I2C address map](../README.md#i2c-address-map)); this is well within the DS1307's and the gauge's standard-mode range and needs no adjustment here.

## Further reading

- [Geekworm Wiki — X728](https://wiki.geekworm.com/X728)
- [Geekworm Wiki — X728-script (setup/install steps, V2.5 GPIO16 charge-control note)](https://wiki.geekworm.com/X728-script)
- [geekworm-com/x728-script on GitHub](https://github.com/geekworm-com/x728-script) — `xPWR.sh`, `xSoft.sh`, systemd unit
- MAX17040/MAX17041 datasheet (Maxim Integrated / Analog Devices) — the register map this board actually implements: `VCELL`, `SOC`, `MODE`, `VERSION`, `CONFIG`, `COMMAND`. The MAX17048 datasheet's `CRATE`, `HIBRT` and `STATUS` are not present on this part (verified 2026-09-01)
- DS1307 datasheet (Maxim Integrated) for the RTC register map and the `CH` clock-halt bit
