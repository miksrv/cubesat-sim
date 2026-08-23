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

**It is unused.** No `i2c-rtc` overlay is loaded, so there is no `/dev/rtc`, `timedatectl` reports `RTC time: n/a`, and the system clock comes from NTP.

That is worth revisiting. A satellite simulation that loses its network has no way to know the time, and a stopped clock is exactly what produces telemetry timestamped `2000-01-01`. Enabling it:

```
dtoverlay=i2c-rtc,ds1307       # /boot/firmware/config.txt, then reboot
```

Three caveats before doing so:

- **The RTC needs its own backup cell** on the HAT (a coin cell). Without one it loses time the moment the Pi is unpowered, which defeats the purpose.
- **Once the kernel claims it, the address disappears from `i2cdetect`** — it shows as `UU` rather than `68`. That is normal, not a fault.
- The clock has never been set; `hwclock -w` after a good NTP sync is needed once, and the `CH` bit clears when the kernel driver starts it.

## Verified readings

Measured on the assembled satellite, 2026-08-23, running on mains power:

```
VCELL (0x02) raw=51760 -> 4.044 V
SOC   (0x04) raw=20379 -> 79.61 %
CRATE (0x16)          -> -0.21 %/hour   (idle; the sign indicates discharge)
GPIO6 (PLD)           -> low = AC present
```

4.044 V against 79.6 % is self-consistent for a Li-ion cell, and `CRATE` near zero matches a battery that is neither charging nor meaningfully loaded. `CRATE` is a cheap way to distinguish "on mains" from "running down" without waiting for `SOC` to move, and this project does not read it yet.

Note that the I2C bus runs at 10 kHz project-wide (see the [I2C address map](../README.md#i2c-address-map)); this is well within the DS1307's and MAX17048's standard-mode range and needs no adjustment here.

## Further reading

- [Geekworm Wiki — X728](https://wiki.geekworm.com/X728)
- [Geekworm Wiki — X728-script (setup/install steps, V2.5 GPIO16 charge-control note)](https://wiki.geekworm.com/X728-script)
- [geekworm-com/x728-script on GitHub](https://github.com/geekworm-com/x728-script) — `xPWR.sh`, `xSoft.sh`, systemd unit
- MAX17048 datasheet (Maxim Integrated) for the full fuel-gauge register map, including `CRATE` (`0x16`, 0.208 %/hour per LSB) and `STATUS` (`0x1A`), neither of which this project reads yet
- DS1307 datasheet (Maxim Integrated) for the RTC register map and the `CH` clock-halt bit
