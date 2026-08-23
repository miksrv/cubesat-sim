# Gravity SEN0501 — Multifunctional Environmental Sensor

Five environmental measurements from a single I2C module: temperature, humidity, atmospheric pressure, ambient light and UV. Intended consumer is **Payload** science data, replacing the LPS22HB (pressure) and SHTC3 (humidity) sensors of the ~~[Sense HAT (C)](hardware-sense-hat-c.md)~~ which is out of the design.

> **Status:** bench-verified on 2026-08-23 — the module answers at `0x22` and all five measurements read plausible values. No driver exists in `src/` yet; the bench script lives on the Pi at `~/test/sen0501_read.py` (see [Usage examples](#usage-examples)).

- **Product:** [DFRobot SEN0501](https://www.dfrobot.com/product-2528.html)
- **Official docs:** [DFRobot Wiki — SEN0501](https://wiki.dfrobot.com/SKU_SEN0501_Gravity_Multifunctional_Environmental_Sensor)
- **Vendor library:** [DFRobot_EnvironmentalSensor](https://github.com/DFRobot/DFRobot_EnvironmentalSensor) — source of the register map and conversion formulas below

## Specification

| | |
|---|---|
| Measurements | Temperature, relative humidity, atmospheric pressure, ambient light (lux), UV |
| Interface | I2C (used here) or UART in Modbus RTU mode |
| I2C address | `0x22`, fixed — verified on the bus |
| Supply | 3.3–5V, ~4mA |
| Register width | 16-bit, big-endian, read as a 2-byte block |
| Board revisions | **V1.0 uses an LTR390 UV element, V3.0 uses an S12DS** — the UV conversion formula differs between them, everything else is identical |
| Derived value | Elevation, computed from pressure — not a separate sensor |

## Wiring

Connected to any of the three Gravity I2C sockets on the [IO Expansion HAT (DFR0566)](https://www.dfrobot.com/product-1930.html) — they all sit on Pi bus 1. Those sockets supply **3.3V**, which is what this sensor should run at: powered from 5V its bus pull-ups would drive the Pi's 3.3V I2C lines above spec.

The sensor end of the Gravity cable is keyed and cannot be inserted the wrong way round; only the DuPont end needs attention. Colours are per [DFRobot's cable specification](https://www.dfrobot.com/product-1581.html) (`Red — VCC`, `Black — GND`, `Blue — SCL/RX`, `Green — SDA/TX`):

| Wire | Signal | HAT pin |
|---|---|---|
| red | VCC (3.3V) | `+` |
| black | GND | `-` |
| green | SDA | `D` |
| blue | SCL | `C` |

**Note that blue is the clock, not the data line** — the opposite of the more common convention, and the same mapping that makes green the TX line on Gravity UART cables.

## Requirements to run on Raspberry Pi

1. I2C must be enabled — `dtparam=i2c_arm=on` in `/boot/firmware/config.txt` (the default on this project's Pi).
2. Install `i2c-tools` for bus scanning. It installs into `/usr/sbin`, which is **not** on the `PATH` of a non-interactive SSH shell, so call it by full path:
   ```bash
   sudo apt install -y i2c-tools
   /usr/sbin/i2cdetect -y 1     # expect 22 to appear
   ```
3. Python access needs only `smbus2`. DFRobot's own library additionally requires `RPi.GPIO`, `smbus` and `modbus_tk` (the latter only for UART mode) — none of which are needed to talk to this sensor over I2C, so this project reads the registers directly, the same way `src/common/imu_qmi8658_ak09918.py` does.
4. The login user must be in the `i2c` group to open `/dev/i2c-1` without root.

See the [I2C Address Map](../README.md#i2c-address-map) in the README for the other addresses in use — `0x22` must not collide with them.

## Register map

All registers are 16-bit big-endian, read as a 2-byte block (`read_i2c_block_data(0x22, reg, 2)`).

| Register | Quantity | Conversion |
|---|---|---|
| `0x04` | Device ID | Reads `0x0022`; use it as a presence check |
| `0x10` | UV, raw | See [UV](#uv-depends-on-the-board-revision) below — revision-dependent |
| `0x12` | Ambient light | `lux = raw * (1.0023 + raw * (8.1488e-5 + raw * (-9.3924e-9 + raw * 6.0135e-13)))` |
| `0x14` | Temperature | `degC = -45 + raw * 175 / 1024 / 64` |
| `0x16` | Relative humidity | `% = raw / 1024 * 100 / 64` |
| `0x18` | Atmospheric pressure | `hPa = raw` (divide by 10 for kPa) |

### UV depends on the board revision

The same raw register is interpreted two different ways, and the results are not remotely close — at `raw = 14` one formula gives `0.00` and the other `84.35`. Check the board revision before trusting either.

**V1.0 (LTR390):**
```
volts = 3.0 * raw / 1024
index = (volts - 0.99) * 15.0 / (2.9 - 0.99)
```

**V3.0 (S12DS):**
```
millivolts = 3000.0 * raw / 1024
nanoamps   = millivolts * 1e9 / 4303300
index      = nanoamps / 113
```

At the sensor's indoor baseline the V1.0 formula returns a **negative** index, which is meaningless; DFRobot's library does not clamp it, this project's bench script clamps at zero. A negative raw result from the V1.0 formula indoors, combined with an implausibly large V3.0 result, is itself decent evidence that the board is a V1.0 — but the definitive test is a reading in direct sunlight, where V1.0 should produce a single-digit index and V3.0 hundreds.

## Usage examples

**Bash — confirm the module is on the bus:**
```bash
/usr/sbin/i2cdetect -y 1     # 22 in row 20
```

**Python — read every measurement:**
```python
from smbus2 import SMBus

I2C_ADDR = 0x22

def read_u16(bus, reg):
    high, low = bus.read_i2c_block_data(I2C_ADDR, reg, 2)
    return (high << 8) | low

with SMBus(1) as bus:
    assert read_u16(bus, 0x04) == 0x22, "SEN0501 not responding"

    raw_light = read_u16(bus, 0x12)
    print("temperature: %.2f degC" % (-45.0 + read_u16(bus, 0x14) * 175.0 / 1024.0 / 64.0))
    print("humidity:    %.2f %%"   % (read_u16(bus, 0x16) / 1024.0 * 100.0 / 64.0))
    print("pressure:    %.2f hPa"  % read_u16(bus, 0x18))
    print("light:       %.2f lux"  % (raw_light * (1.0023 + raw_light * (8.1488e-5
          + raw_light * (-9.3924e-9 + raw_light * 6.0135e-13)))))
```

**Bench script on the Pi** — `~/test/sen0501_read.py` prints all five measurements plus derived elevation and both UV interpretations, once or in a loop:

```bash
~/test/venv/bin/python ~/test/sen0501_read.py                    # single reading
~/test/venv/bin/python ~/test/sen0501_read.py --loop             # every 2s
~/test/venv/bin/python ~/test/sen0501_read.py --loop -i 0.5 --raw
```

In `--loop` mode, breathing on the module moves temperature and humidity and covering it drops the lux reading — a quick check that the values are live rather than constants.

Verified output, 2026-08-23 indoors:

```
SEN0501 found at 0x22 on bus 1 (ID 0x22)
  Temperature                 27.96 degC   (raw 27324)
  Humidity                    41.21 %      (raw 27008)
  Pressure                  1000.00 hPa    (raw 1000)
  Elevation                  125.42 m      (raw 1000)
  Luminosity                 388.96 lux    (raw 377)
  UV index (V1.0/LTR390)       0.00        (raw 14)
  UV index (V3.0/S12DS)       84.35        (raw 14)
```

## Notes on the readings

- **Elevation is not measured.** DFRobot's `get_elevation()` reads the *pressure* register and applies the barometric formula against a hard-coded reference of `1015.0` hPa — not the real local sea-level pressure. As an absolute altitude the number is meaningless; it is only useful for tracking change. If Payload is going to publish altitude, feed it a real reference pressure.
- **Pressure resolution is 1 hPa.** The register holds whole hectopascals, so roughly 8 m of altitude per least-significant bit — coarse for anything but weather trends.
- **Only one UV row is valid.** See [UV](#uv-depends-on-the-board-revision).
- Temperature and humidity share the same fixed-point scaling (`/1024/64`), which is easy to mis-transcribe as a single `/65536`; the two are equivalent, but keep the vendor's form so the formula stays comparable to their library.

## Open items

- No driver in `src/` yet. The natural home is `src/common/` alongside the other sensor drivers, consumed by **Payload** for science data — mirroring how `src/common/imu_qmi8658_ak09918.py` is consumed by ADCS.
- Board revision (V1.0 vs V3.0) still unconfirmed, so the UV field cannot be published yet. Confirm in direct sunlight.
- `requirements.txt` already carries `smbus2`, so no new dependency is needed. Tests will need a fake I2C peripheral in `tests/fakes.py`, the same approach used for the existing IMU.
- Decide whether Payload publishes elevation at all, given the fixed-reference caveat above.

## Further reading

- [DFRobot Wiki — SEN0501](https://wiki.dfrobot.com/SKU_SEN0501_Gravity_Multifunctional_Environmental_Sensor) — official documentation
- [DFRobot_EnvironmentalSensor](https://github.com/DFRobot/DFRobot_EnvironmentalSensor) — vendor library; `python/raspberry/DFRobot_Environmental_Sensor.py` holds the register map, `python/raspberry/examples/V1_0.py` and `V3_0.py` show which UV formula belongs to which revision
- [DFRobot DFR0566 wiki](https://wiki.dfrobot.com/dfr0566) — IO Expansion HAT: three I2C groups, 3.3V sensor rail
- [Gravity 4-pin cable](https://www.dfrobot.com/product-1581.html) — wire colour specification
