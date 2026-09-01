# Gravity GNSS Receiver TEL0157 (GPS / BeiDou / GLONASS)

Satellite positioning for **ADCS**, replacing `src/common/gps_a9g.py` (A9G, NMEA over the 52Pi ~~[IoT Node(A)](hardware-iot-node-a-52pi.md)~~ I2C↔UART bridge).

The module does the GNSS work itself — acquisition, tracking, NMEA parsing — and exposes the result as plain I2C registers. The host reads decimal degrees, satellite count, altitude, speed and course without touching NMEA at all, so `pynmea2` is no longer needed. Raw NMEA remains available on request, which is the only way to inspect satellites in view before a fix exists.

> **Status:** bench-verified on 2026-08-23. Cold start indoors reached time-only; moved to a balcony it acquired a 3D fix with 23 satellites in the solution and HDOP 0.6. The driver is `src/cubesat/hal/rpi/tel0157.py`; the bench script lives on the Pi at `~/test/tel0157_gnss_read.py`. On 2026-08-31 the driver itself was exercised on the assembled satellite for the first time — bring-up write, then `read()` returning a 3D fix with 15 satellites at 37.676955, −121.876546, within metres of the August bench position — but **`ADCS` has still not run on the Pi**: no profile that starts it has been applied, so the driver has only ever been called by hand.

- **Product:** [DFRobot TEL0157](https://www.dfrobot.com/product-2651.html)
- **Official docs:** [DFRobot Wiki — TEL0157](https://wiki.dfrobot.com/TEL0157)
- **Vendor library:** [DFRobot_GNSS](https://github.com/DFRobot/DFRobot_GNSS) — source of the register map below

## Specification

| | |
|---|---|
| I2C address | `0x20`, fixed — verified on the bus |
| Interface | I2C (used here) or UART |
| Supply | 3.3–5.5V, ~40mA |
| Constellations | GPS, BeiDou, GLONASS, in any combination (mode register, values 1–7) |
| Antenna | External, IPEX1 connector — the module reports its state in a `$GPTXT` sentence |
| Extras | Onboard RGB LED, software-controlled; power-down (sleep) register |

## Wiring

Any of the three Gravity I2C sockets on the [IO Expansion HAT (DFR0566)](https://www.dfrobot.com/product-1930.html) — all on Pi bus 1 at 3.3V. Cable colours per [DFRobot's specification](https://www.dfrobot.com/product-1581.html):

| Wire | Signal | HAT pin |
|---|---|---|
| red | VCC (3.3V) | `+` |
| black | GND | `-` |
| green | SDA | `D` |
| blue | SCL | `C` |

Blue is the clock, not the data line.

## Register map

Single-byte registers, read as blocks. Multi-byte quantities are big-endian; the third byte of the altitude/speed/course triplets is hundredths.

| Register | Length | Contents |
|---|---|---|
| `0`–`3` | 4 | Year (high, low), month, day |
| `4`–`6` | 3 | Hour, minute, second (UTC) |
| `7`–`12` | 6 | Latitude: degrees, minutes, minute-fraction ×10⁵ (24-bit), hemisphere |
| `13`–`18` | 6 | Longitude: degrees, minutes, minute-fraction ×10⁵ (24-bit), hemisphere |
| `19` | 1 | Satellites used in the solution |
| `20`–`22` | 3 | Altitude, metres |
| `23`–`25` | 3 | Speed over ground, knots |
| `26`–`28` | 3 | Course over ground, degrees |
| `29` | 1 | Write `0x55` to latch the raw NMEA buffer |
| `30` | 1 | Device ID — reads `0x20` |
| `31`–`32` | 2 | Length of the latched NMEA buffer |
| `33` | — | Raw NMEA data, read in chunks |
| `34` | 1 | Constellation mode: 1 GPS, 2 BeiDou, 3 GPS+BeiDou, 4 GLONASS, 5 GPS+GLONASS, 6 BeiDou+GLONASS, 7 all three |
| `35` | 1 | Power: 0 on, 1 off (data stops refreshing) |
| `36` | 1 | RGB LED: `0x05` on, `0x02` off. **Write-only** — reads back `0x00` regardless of what was written (measured 2026-08-31), so the setting cannot be read back or confirmed in software |

Conversion to decimal degrees:

```
degrees = dd + mm / 60 + mmmmm / 100000 / 60
```

## The onboard LED

The module carries an RGB LED that the driver turns **off** at bring-up, in the same
`_ensure_started()` write that sets the constellation mode: inside a sealed satellite it
illuminates the inside of the shell and nothing else. `tel0157_gnss_read.py --rgb on` turns it back
on for bench work; the next start of `ADCS` darkens it again.

**Verified 2026-08-31, by eye and then by a reading.** `0x02` darkened the LED on the assembled
satellite, and the module went on working: a fix with 15 satellites, constellation mode still 7,
power register still 0. So the off value darkens the indicator rather than idling the receiver —
worth confirming explicitly, because register 35 one along *does* stop the module.

Two things still to know:

- **Register 36 is write-only.** It reads back `0x00` whatever was written, so neither the driver
  nor a script can confirm the setting took — only that the module accepted the write without a
  NACK. That is why the check above had to be visual.
- **`0x05` (on) is still untested.** Only the off path has been exercised on the satellite. It is
  the value the bench script writes, and getting it wrong costs a dark LED, not bad data.

## Gotchas

These matter more than the register map — every one of them produces plausible-looking wrong data rather than an error.

**The hemisphere bytes are swapped by the firmware.** The latitude block's last byte carries the *longitude* hemisphere and vice versa: at a location in California the latitude register held `'W'` (0x57) and the longitude register held `'N'` (0x4E). Determine the hemisphere from the byte's *content* — whichever of the two is `N`/`S` belongs to the latitude — rather than from its position. Code that trusts the position will decide a perfectly valid fix is invalid.

**The vendor library never applies the sign.** `DFRobot_GNSS.get_lat()` returns an unsigned magnitude and reports the hemisphere in a separate field, so a southern latitude or western longitude comes out looking northern and eastern. Combined with the swap above, this is why the bug has gone unnoticed upstream.

**No fix reads as tidy zeros, not as an error.** Latitude and longitude both come back `0.000000`, which is a real place in the Gulf of Guinea. Validate a fix explicitly — satellites used greater than zero *and* both hemisphere characters present — before publishing a position.

**Course is stale at zero speed.** At rest the module reported `speed 0.00 knots` with `course 210.87 deg`, retaining its last value. Course over ground is only meaningful while moving.

**The default constellation mode is 3 (GPS + BeiDou).** Setting it to 7 measurably helped: satellites in view went from 6 to 9 immediately and SNR values rose (PRN01 25 → 39, PRN02 26 → 42). Measured afterwards: the mode **did** survive a full power cycle of the assembled satellite, and so did the ephemeris — a fix came back within a minute of boot with 8 satellites, rather than repeating the multi-minute cold start. A driver should still write the mode on every start, since that costs one register write and removes the assumption entirely.

**A cold start takes minutes, and satellite count alone does not indicate progress.** The module knew the correct UTC time long before it had a position — decoding time needs one satellite, a 3D fix needs decoded ephemeris from at least four. The tell is in the `GSV` sentences: entries with an SNR but *empty* elevation and azimuth fields mean the signal is being tracked but the ephemeris has not arrived yet. Worst case for a full almanac is around 12.5 minutes.

**The vendor's `get_all_gnss()` is Python 2 only** — it uses `len / 32` where Python 3 needs `//`, so it raises on any Python 3 install. The bench script implements the NMEA read itself.

## Requirements to run on Raspberry Pi

1. I2C enabled (`dtparam=i2c_arm=on`) and the bus clock held at 10 kHz — see the [I2C address map](../README.md#i2c-address-map). The 10 kHz limit comes from the BNO055, not from this module, but it applies to the shared bus.
2. `i2c-tools` for scanning; it installs into `/usr/sbin`, outside a non-interactive SSH `PATH`:
   ```bash
   /usr/sbin/i2cdetect -y 1     # expect 20
   ```
3. Python access needs only `smbus2`.
4. The antenna needs sky view. Indoors expect time-only; a balcony or window is enough for a fix.

## Usage examples

```bash
~/test/venv/bin/python ~/test/tel0157_gnss_read.py                 # single reading
~/test/venv/bin/python ~/test/tel0157_gnss_read.py --loop -i 5
~/test/venv/bin/python ~/test/tel0157_gnss_read.py --nmea          # + raw NMEA, satellites in view
~/test/venv/bin/python ~/test/tel0157_gnss_read.py --set-mode 7    # all three constellations
~/test/venv/bin/python ~/test/tel0157_gnss_read.py --rgb off
```

Verified output with a fix, 2026-08-23, antenna on a balcony:

```
TEL0157 at 0x20, ID 0x20
  constellation     7 (GPS + BeiDou + GLONASS)
  power             on
  UTC from module   2026-08-23 20:24:00
  satellites used   23
  position          37.676896, -121.876561  (N / W)
  altitude          116.59 m
  speed / course    0.00 knots / 210.87 deg
```

The corresponding NMEA, which is what to look at when there is no fix:

```
$GNGGA,202200.000,3740.61346,N,12152.59287,W,1,20,0.6,116.3,M,-31.9,M,,*77
$GNRMC,202202.000,A,3740.61344,N,12152.59291,W,0.00,210.88,230826,,,A,V*11
$GPTXT,01,01,01,ANTENNA OK*35
```

What to read from it: in `GNGGA` the field after the longitude hemisphere is fix quality (`0` none, `1` GPS fix), then satellites used, then HDOP — `0.6` is good, `25.5` means no solution. In `GNRMC` the status field is `A` for valid and `V` for invalid. `$GPTXT ... ANTENNA OK` confirms the antenna is detected — a fast way to rule out a cabling problem.

## Open items

- No driver in `src/` yet. Its home in the rewrite is `src/cubesat/hal/rpi/tel0157.py`, behind the
  `GNSS` protocol. It replaces `src/common/gps_a9g.py` and is consumed by ADCS. Keep that module's contract: return the last known fix with `fix: false` rather than blocking the ADCS loop while there is no signal — the same behaviour is needed here, and the module's "tidy zeros" make it easy to get wrong.
- `pynmea2` loses its only consumer once this lands; `pyserial` too, unless the Heltec link needs it. Decide whether to drop them from `requirements.txt`.
- Altitude is now available from three sources: this module, the BMP280 and the SEN0501. Pick one per quantity.
- Tests need a fake I2C peripheral in `tests/fakes.py`, including a no-fix case (all zeros) and a southern/western position to catch a sign regression.

## Further reading

- [DFRobot Wiki — TEL0157](https://wiki.dfrobot.com/TEL0157) — official documentation
- [DFRobot_GNSS](https://github.com/DFRobot/DFRobot_GNSS) — vendor library; `python/raspberrypi/DFRobot_GNSS.py` holds the register constants
- [DFRobot DFR0566 wiki](https://wiki.dfrobot.com/dfr0566) — IO Expansion HAT
- [Gravity 4-pin cable](https://www.dfrobot.com/product-1581.html) — wire colour specification
