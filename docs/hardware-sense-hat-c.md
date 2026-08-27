# Sense HAT (C)

> **⚠️ No longer in the design.** The Sense HAT (C) was removed from the satellite: its
> QMI8658 + AK09918 pair was replaced by a [BNO055](hardware-bno055-bmp280-imu.md) (on-chip fusion,
> so no AHRS filter is needed at all) and its LPS22HB + SHTC3 pair by a
> [SEN0501](hardware-sen0501-environmental-sensor.md). This file is kept for reference — the
> register maps and the wiring notes are still accurate for the board itself.

> **Naming note:** the product is a Waveshare **Sense HAT (C)**, not "(B)" — the (B) revision uses a different IMU (ICM-20948) and does not carry the QMI8658/AK09918 pair this project's driver targets. Confirmed against `src/common/imu_qmi8658_ak09918.py`, which implements exactly the QMI8658 + AK09918 register map below.

Multi-sensor HAT feeding two subsystems:
- **ADCS** (`src/adcs/main.py`) — orientation via `src/common/imu_qmi8658_ak09918.py` (QMI8658 accel/gyro + AK09918 magnetometer)
- **Payload** science data (`src/payload/science.py`) — LPS22HB pressure + SHTC3 humidity/temperature

- **Product:** [Waveshare Sense HAT (C)](https://www.aliexpress.us/item/3256811354242582.html)
- **Official docs:** [Waveshare Wiki — Sense HAT (C)](https://www.waveshare.com/wiki/Sense_HAT_(C))

## Specification

| Sensor | Function | I2C address | Used by |
|---|---|---|---|
| QMI8658C | 3-axis accelerometer + 3-axis gyroscope | `0x6B` | ADCS (orientation, `imu_qmi8658_ak09918.py`) |
| AK09918C | 3-axis magnetometer | `0x0C` | ADCS (yaw/heading) |
| LPS22HB | Barometric pressure + temperature | `0x5C` | Payload science data |
| SHTC3 | Humidity + temperature | `0x70` | Payload science data |
| TCS34725 | Color sensor | `0x29` | Not used by this project |
| SGM58031 | External ADC | `0x48` | Not used by this project |

All sensors share the single Raspberry Pi I2C-1 bus and can be read simultaneously since each has a distinct address.

QMI8658 key registers (see `src/common/imu_qmi8658_ak09918.py`):

| Register | Purpose |
|---|---|
| `0x00` (`WHO_AM_I`) | Expect `0x05` |
| `0x02` (`CTRL1`) | Mode select (`0x60` = I2C mode) |
| `0x03` (`CTRL2`) | Accel range/ODR (`0x00`=±2g, ORed with rate) |
| `0x04` (`CTRL3`) | Gyro range/ODR |
| `0x06` (`CTRL5`) | Filter config |
| `0x08` (`CTRL7`) | Enable accel/gyro (`0x01`/`0x02`, `0x80`=apply) |
| `0x35` (`AX_L`) | Start of 12-byte accel+gyro burst read |
| `0x33` (`TEMP_L`) | Die temperature |

AK09918 key registers:

| Register | Purpose |
|---|---|
| `0x01` (`WIA2`) | Expect `0x0C` |
| `0x10` (`ST1`) | Data-ready status |
| `0x11` (`HXL`) | Start of magnetometer burst read |
| `0x31` (`CNTL2`) | Measurement mode (`0x04` = continuous 20Hz) |
| `0x32` (`CNTL3`) | Soft reset (`0x01`) |

## Requirements to run on Raspberry Pi

1. **Enable I2C:**
   ```bash
   sudo raspi-config
   # Interface Options → I2C → Enable
   sudo reboot
   ```
2. **Install dependencies:**
   ```bash
   sudo apt-get install -y i2c-tools python3-smbus
   pip install smbus2 lgpio
   ```
3. **Verify all four sensors enumerate on the bus:**
   ```bash
   i2cdetect -y 1   # expect 0x0c, 0x5c, 0x6b, 0x70
   ```
4. SHTC3 in this project is driven over `lgpio`'s I2C helpers (`src/payload/science.py`) rather than `smbus2`, so both libraries must be installed side by side.

## Usage examples

**Bash — confirm sensors are visible:**
```bash
i2cdetect -y 1
```

**Python — orientation, as implemented in `src/adcs/main.py`:**
```python
from src.common.imu_qmi8658_ak09918 import IMU

imu = IMU()  # inits QMI8658 + AK09918, calibrates gyro offset
ori = imu.get_orientation_deg()
print(f"roll={ori['roll']:.1f} pitch={ori['pitch']:.1f} yaw={ori['yaw']:.1f}")
print(f"imu_temp={imu.read_imu_temp():.2f}C")
```

**Python — pressure/humidity, as implemented in `src/payload/science.py`:**
```python
from src.payload.science import ScienceCollector

science = ScienceCollector()  # inits LPS22HB (smbus2) + SHTC3 (lgpio)
data = science.collect()
print(data)  # {'temperature': ..., 'pressure': ..., 'humidity': ...}
```

**Run the ADCS / Payload services directly:**
```bash
source venv/bin/activate
PYTHONPATH=. python -m src.adcs.main
PYTHONPATH=. python -m src.payload.main
```

## Further reading

- [Waveshare Wiki — Sense HAT (C)](https://www.waveshare.com/wiki/Sense_HAT_(C)) — demo code bundle (`bcm2835`, `wiringPi`, Python), full register maps
- QMI8658C datasheet (QST) for the complete accelerometer/gyroscope register map
- AK09918 datasheet (AKM) for the complete magnetometer register map
- LPS22HB datasheet (STMicroelectronics)
- SHTC3 datasheet (Sensirion)
