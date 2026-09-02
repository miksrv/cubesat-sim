# Gravity 10 DOF IMU AHRS — BNO055 + BMP280

Two chips on one Gravity board, on separate I2C addresses:

- **BNO055** (`0x28`) — 9-axis absolute orientation sensor: 3-axis accelerometer, gyroscope and magnetometer plus an onboard Cortex-M0 running Bosch's sensor fusion. It outputs a fused quaternion and Euler angles directly, so the host does not have to implement an AHRS filter.
- **BMP280** (`0x76`) — barometric pressure and temperature.

Intended consumer is **ADCS** orientation, replacing `src/common/imu_qmi8658_ak09918.py` (QMI8658 + AK09918 on the ~~[Sense HAT (C)](hardware-sense-hat-c.md)~~, which is out of the design). Note that the BMP280 duplicates the pressure measurement already provided by the [SEN0501](hardware-sen0501-environmental-sensor.md) — decide which one Payload should publish rather than shipping both.

> **Status:** bench-verified on 2026-08-23. Both chips answer, fusion runs, and all vectors are self-consistent. No driver exists in `src/` yet; the bench script lives on the Pi at `~/test/bno055_bmp280_read.py`.

> **This board imposes a system-wide constraint:** the I2C bus clock must be lowered to 10 kHz for the BNO055 to be readable at all. See [The clock-stretching problem](#the-clock-stretching-problem) — this is the single most important thing in this document.

- **Product:** [DFRobot SEN0253](https://www.dfrobot.com/product-1793.html)
- **Official docs:** [DFRobot Wiki — SEN0253](https://wiki.dfrobot.com/SEN0253)
- **Chip datasheet:** [Bosch BNO055](https://www.bosch-sensortec.com/products/smart-sensor-systems/bno055/)

## Specification

| | |
|---|---|
| I2C addresses | `0x28` (BNO055), `0x76` (BMP280) — both verified on the bus |
| Supply | 3.3–5V, ~5mA |
| BNO055 fusion outputs | Quaternion, Euler angles, linear acceleration, gravity vector |
| BNO055 raw outputs | Accelerometer, gyroscope, magnetometer, chip temperature |
| **Required bus clock** | **≤ 10 kHz** — `dtparam=i2c_arm_baudrate=10000` |
| BMP280 configuration used | `osrs_t` ×2, `osrs_p` ×16, normal mode (`0xF4 = 0x57`) |

Scaling of the BNO055 output registers:

| Quantity | Scale |
|---|---|
| Euler angles | 16 LSB per degree |
| Quaternion | 2^14 (16384) = 1.0 |
| Accelerometer, linear acceleration, gravity | 100 LSB per m/s² |
| Gyroscope | 16 LSB per °/s |
| Magnetometer | 16 LSB per µT |
| Chip temperature | 1 LSB per °C, single byte |

## Wiring

Any of the three Gravity I2C sockets on the [IO Expansion HAT (DFR0566)](https://www.dfrobot.com/product-1930.html) — all on Pi bus 1, all supplying 3.3V. The sensor end of the Gravity cable is keyed; only the DuPont end needs attention. Colours per [DFRobot's cable specification](https://www.dfrobot.com/product-1581.html):

| Wire | Signal | HAT pin |
|---|---|---|
| red | VCC (3.3V) | `+` |
| black | GND | `-` |
| green | SDA | `D` |
| blue | SCL | `C` |

Blue is the clock, not the data line — the opposite of the more common convention.

## The clock-stretching problem

**Symptom:** byte reads from the BNO055 come back with **bit 7 forced to 1** — intermittently, roughly 60–70% of reads. Registers whose bit 7 is legitimately set (`CHIP_ID = 0xA0`, `ACC_ID = 0xFB`) look perfectly healthy, which makes the fault easy to miss.

Measured on this Pi at the default 100 kHz bus clock, 50 reads per register:

| Register | Expected | Bad reads | What came back instead |
|---|---|---|---|
| BMP280 `ID` | `0x58` | 0/50 | — |
| BNO055 `MAG_ID` | `0x32` | 34/50 | `0xB2` = `0x32 \| 0x80` |
| BNO055 `GYR_ID` | `0x0F` | 30/50 | `0x8F` = `0x0F \| 0x80` |
| BNO055 `CHIP_ID` | `0xA0` | 0/50 | — (bit 7 already set, corruption invisible) |

**Cause.** The BNO055 does not fully respect the I2C specification: it stretches the clock at the start of a data byte. The Pi's BCM2835 I2C controller mishandles clock stretching — it samples SDA while the line is still released high, and reads the first transmitted bit (bit 7) as a 1. The BMP280 sits on the same cable, same socket and same bus and is completely unaffected, which rules out wiring, pull-ups and cable length.

**Fix.** Lower the bus clock so the sensor never needs to stretch it:

```
dtparam=i2c_arm_baudrate=10000     # /boot/firmware/config.txt, then reboot
```

After this change the same test gives **0 bad reads out of 200**. The cost is a slower bus for every I2C peripheral, which is irrelevant here — the other consumers (`0x36` fuel gauge, `0x76`, `0x22`) are all low-rate. `vcgencmd get_config i2c_arm_baudrate` reports `unknown`, so verify empirically rather than by reading the config back.

**Using repeated-start versus split transactions does not help.** Reading via two separate `i2c_rdwr` messages instead of `read_byte_data` was measured as *worse* (20/20 corrupted versus 14/20). This is not a repeated-start problem.

**The corruption cannot be worked around in software.** Bit 7 is significant in real sensor data, so a corrupted value is indistinguishable from a legitimate one — there is nothing to mask and nothing to validate against. The bus has to be fixed.

**10 kHz reduces the corruption; it does not end it.** Measured on the assembled satellite on
2026-08-28: roughly **one read in ten** still came back with bit 7 of one high byte flipped, in the
Euler, acceleration *and* quaternion blocks alike, and it was reproduced with a bare `i2cget`
(`0x167F` read back as `0x16FF`). A flipped high-byte bit 7 moves a value by half the 16-bit range —
+33 g on an accelerometer axis, −2000° on an angle — so `hal/rpi/bno055.py` validates every block
against physical plausibility and re-reads (up to five attempts) rather than publishing a confident
impossibility. A flipped *low* byte slips through: 8°, 0.13 g, or 0.008 in a quaternion component,
which no range check can catch. Do not read the 10 kHz requirement as a fix — read it as the setting
that makes the sensor usable with a validating driver behind it.

**Measured on the assembled satellite in `DEMO`, 2026-09-01, ADCS at 2 Hz.** Detectable (high-byte)
flips: 66 rejected reads in the first six minutes, ≈ one read in eleven, spread across heading,
roll, quaternion norm, temperature (−100 °C) and |accel| (33.4 g) — all caught and re-read. The
*undetectable* low-byte flips were then counted from the published stream instead: over 65 samples
with the satellite at rest, `gyro_x` read exactly 8.0 °/s between neighbours of 0.06 once, and
`acc_x` stepped from 0.07 g to 0.20 g and straight back twice — three samples in 65, ≈ 5 %, each an
error of exactly +128 LSB. They pass validation because they are physically plausible, and DHS
records them into `attitude` as measured. This is the first half of the outstanding comparison
above; the `i2c-gpio` half has not been run.

**Alternative fix, if 10 kHz is ever too slow:** a bit-banged software bus (`dtoverlay=i2c-gpio`)
honours clock stretching correctly and leaves the hardware bus at full speed, at the cost of moving
the sensor to different GPIO pins. It is also the candidate real fix for the residual flips above —
the software bus has no clock-stretch bug at all — and measuring both corruption rates is the
outstanding bench task here.

The bench script's `--selftest` mode exists specifically to catch a regression here:

```bash
~/test/venv/bin/python ~/test/bno055_bmp280_read.py --selftest
```

## Configuration sequence

**A full reset is mandatory, not optional.** A BNO055 left in a half-configured state — which is exactly what happens if it was configured while reads were being corrupted — reports `SYS_STATUS = 1` (system error) with `SYS_ERR = 9` (fusion configuration error) and returns **all-zero magnetometer data** while still claiming `CALIB_STAT` mag = 3. That combination cost real debugging time; it is a stale-state artefact, not a hardware fault.

```python
write(0x3D, 0x00)        # OPR_MODE = CONFIGMODE
write(0x3F, 0x20)        # SYS_TRIGGER = reset
sleep(1.0)               # power-on reset takes ~650ms
wait_until(read(0x00) == 0xA0)
write(0x07, 0x00)        # PAGE_ID = 0
write(0x3E, 0x00)        # PWR_MODE = normal
write(0x3F, 0x00)        # internal oscillator
write(0x3D, 0x0C)        # OPR_MODE = NDOF (9-axis fusion)
sleep(0.3)
```

Healthy state afterwards: `OPR_MODE = 0x0C`, `PWR_MODE = 0x00`, `ST_RESULT = 0x0F` (all four self-tests passed), `SYS_STATUS = 5` (fusion running), `SYS_ERR = 0`.

Useful diagnostic registers: `ST_RESULT` `0x36`, `SYS_STATUS` `0x39`, `SYS_ERR` `0x3A`, `CALIB_STAT` `0x35`, `OPR_MODE` `0x3D`, `PWR_MODE` `0x3E`. `SYS_STATUS` values: 0 idle, 1 system error, 2 initializing peripherals, 3 system initialization, 4 executing selftest, 5 fusion running, 6 running without fusion. `SYS_ERR` values 0–10, where 9 is a fusion configuration error and 3 a failed self-test.

To read the magnetometer without fusion — useful when isolating a fault — use `OPR_MODE = 0x02` (MAGONLY) or `0x07` (AMG: accel + mag + gyro, no fusion).

## Calibration

`CALIB_STAT` (`0x35`) packs four 2-bit fields: bits 6–7 system, 4–5 gyroscope, 2–3 accelerometer, 0–1 magnetometer. A value of 3 means that subsystem is fully calibrated.

- **Gyroscope** — hold the board still.
- **Accelerometer** — rotate it slowly through six faces, pausing on each.
- **Magnetometer** — trace a figure-8 in the air.

Readings are usable before calibration completes, but **`heading` is meaningless until the magnetometer field reaches 3** — before that it reads a constant, typically `0.00`. Calibration is lost on reset, so the bench script offers `--no-reset` to keep an accumulated calibration between runs. A production driver should save and restore the calibration profile registers instead.

## Magnetometer readings depend on the environment, not just the sensor

Two magnitudes were measured during bring-up:

| Setup | Magnitude | Magnetometer calibration |
|---|---|---|
| On a desk beside a laptop and an active LoRa radio | 59.0 µT | reached 3 (calibrated) |
| Mounted in the assembled satellite, in a different room | 36.9 µT | 0 (uncalibrated) |

**Neither number supports a conclusion about the frame.** Three variables changed at once — the location, the nearby emitters and the calibration state — so the difference cannot be attributed to the assembly. In particular, an uncalibrated magnetometer carries an arbitrary hard-iron offset, which makes its magnitude meaningless until `CALIB_STAT` mag reaches 3.

For reference, the Earth's total field in northern California is roughly 48 µT. Both readings deviate from it, which is the expected outcome of an uncalibrated sensor sitting near powered electronics.

What follows for ADCS:

- **Calibrate in the final configuration and the final location.** A profile collected in a different place, or beside a laptop and a transmitting radio, encodes the wrong offset.
- Treat `heading` as an estimate rather than a measurement, and do not publish it while `CALIB_STAT` mag is below 3 — before that it reads a constant.
- Field magnitude is a one-sided health check: a value outside 25–65 µT proves something is wrong, but a value inside it proves nothing.
- Do not compare magnitudes across setups to judge the mechanical design. To actually measure the frame's contribution, take both readings in the same place, with the magnetometer calibrated in both, changing only whether it is mounted.

Accelerometer and gyroscope are not subject to any of this — gravity does not care about the surroundings. `|a|` measured 9.54 m/s² in the assembled satellite against 9.42 m/s² on the bench, both consistent with g.

## Usage examples

```bash
/usr/sbin/i2cdetect -y 1     # expect 28 and 76

~/test/venv/bin/python ~/test/bno055_bmp280_read.py --selftest
~/test/venv/bin/python ~/test/bno055_bmp280_read.py
~/test/venv/bin/python ~/test/bno055_bmp280_read.py --loop -i 0.5
~/test/venv/bin/python ~/test/bno055_bmp280_read.py --loop --no-reset   # keep calibration
```

Verified output, 2026-08-23, board resting on a bench:

```
  mode 0x0C  power 0x00  selftest 0x0F (0x0F = all passed)
  sys_status 5 (fusion running)   sys_err 0 (no error)
  heading    0.00   roll   27.19   pitch    7.06  deg
  quaternion  w  0.970  x -0.057  y -0.236  z -0.000
  accel          4.39   -1.00    8.27  m/s2   (|a| 9.42)
  gyro          -0.12   -0.31   -0.19  deg/s
  mag          -56.75   -1.50  -16.06  uT     (|m| 59.00)
  linear acc    -0.02    0.06   -0.38  m/s2
  gravity        4.48   -1.07    8.65  m/s2
  BNO055 temp 31 degC
  BMP280      29.63 degC   1001.62 hPa
```

Sanity checks worth repeating on any new board: `|a|` should be close to 9.8 m/s² when stationary, `|m|` should land in the 25–65 µT range of the Earth's field, `linear acc` should be near zero at rest, and `gravity` plus `linear acc` should reconstruct `accel`.

## Open items

- No driver in `src/` yet. Its home in the rewrite is `src/cubesat/hal/rpi/bno055.py`, behind the
  `IMU` protocol. It replaces `src/common/imu_qmi8658_ak09918.py` wholesale — the chips share no register map — and is consumed by ADCS.
- **The 10 kHz bus clock is now a project-wide requirement**, not a detail of this sensor. Any deployment or install script that writes `config.txt` must set it, and `docs/` for the other I2C peripherals assumes it.
- Decide whether pressure comes from this BMP280 or from the SEN0501, and drop the duplicate.
- Magnetometer calibration must be captured in the final configuration and location, then persisted — see [Magnetometer readings](#magnetometer-readings-depend-on-the-environment-not-just-the-sensor). A profile collected elsewhere encodes the wrong hard-iron offset.
- The frame's actual magnetic contribution is still unmeasured. It needs a controlled comparison: same location, magnetometer calibrated in both cases, only the mounting changed.
- Calibration persistence is unimplemented: the driver should read back the calibration profile once `CALIB_STAT` is 3 and restore it on start, otherwise every reboot begins uncalibrated and heading is unusable until the satellite is manually waved about.
- Tests will need a fake I2C peripheral in `tests/fakes.py`, including a case that reproduces the bit-7 corruption so the driver's ID check is exercised.

## Further reading

- [DFRobot Wiki — SEN0253](https://wiki.dfrobot.com/SEN0253) — board documentation
- [Bosch BNO055](https://www.bosch-sensortec.com/products/smart-sensor-systems/bno055/) — datasheet: full register map, operating modes, calibration profile registers
- [DFRobot DFR0566 wiki](https://wiki.dfrobot.com/dfr0566) — IO Expansion HAT: three I2C groups, 3.3V sensor rail
- [Gravity 4-pin cable](https://www.dfrobot.com/product-1581.html) — wire colour specification
