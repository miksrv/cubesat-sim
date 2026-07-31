# IoT Node(A) — 52Pi Docker Pi Series (GSM/GPS/LoRa)

Onboard GSM/GPS/LoRa module. **Present on the physical build but not yet wired into any service** — reserved for future ground-link/positioning work. The only trace of it in the codebase today is `crc16_ccitt()` in `src/common/utils.py`, a CRC-16-CCITT helper intended for validating LoRa packets once this module is integrated.

- **Product:** [52Pi EP-0105 IoT Node (A)](https://www.aliexpress.us/item/2251832864586218.html)
- **Official docs:** [52Pi Wiki — EP-0105](https://wiki.52pi.com/index.php?title=EP-0105)

## Specification

| | |
|---|---|
| GSM/GPRS/GPS module | A9G (dual UART presented over an I2C↔UART bridge) |
| GSM bands | 850 / 900 / 1800 / 1900 MHz |
| GPS | GPS/BDS, A-GPS/A-BDS, standard SIM card |
| LoRa transceiver | 433MHz, FSK/GFSK/MSK/GMSK/LoRa modulation |
| LoRa range | ~500m, receiver sensitivity −141dBm |
| Bridge chip | SC16IS752 (I2C ↔ dual UART), I2C bus speed up to 400kHz (~320kbps effective) |
| Bridge I2C address | `0x16` (used as base address for both GSM and LoRa register access in vendor examples) |
| Exposed serial devices | `/dev/ttySC0` (GSM AT commands), `/dev/ttySC1` (GPS/BDS NMEA) |
| LoRa control | Direct register access over I2C, registers `0x01`–`0x20` |
| Power | Low-power standby <1mA; specific voltage/current draw under active GSM/LoRa TX not published — budget for GSM TX current spikes (typically >1A peak) same as any GSM module |

## Requirements to run on Raspberry Pi

1. **Enable I2C:**
   ```bash
   sudo raspi-config
   # Interface Options → I2C → Enable
   ```
2. **Add the SC16IS752 device-tree overlay** (bridges the I2C-attached UARTs to `/dev/ttySC0` / `/dev/ttySC1`):
   ```bash
   # copy the vendor-provided sc16is752-i2c.dtbo to /boot/overlays/
   echo "dtoverlay=sc16is752-i2c" | sudo tee -a /boot/config.txt
   sudo reboot
   ```
3. **Verify the bridge is visible:**
   ```bash
   i2cdetect -y 1   # expect 0x16
   ls /dev/ttySC*   # expect ttySC0, ttySC1 after overlay loads
   ```
4. Insert a SIM card for GSM/GPRS functionality; GPS works standalone (no SIM needed) once powered.
5. Python access needs `smbus`/`smbus2` for direct LoRa register control, or a standard serial library (`pyserial`) against `/dev/ttySC0`/`/dev/ttySC1` for GSM AT commands and GPS NMEA sentences.

## Usage examples

**Bash — confirm the bridge chip and serial devices exist:**
```bash
i2cdetect -y 1
ls -l /dev/ttySC0 /dev/ttySC1
```

**Bash — send AT commands to the GSM module over the bridged UART:**
```bash
echo -e "AT\r" > /dev/ttySC0
cat /dev/ttySC0   # expect "OK"
```

**Python — reset the GSM module (per vendor example):**
```python
import smbus
bus = smbus.SMBus(1)
bus.write_byte_data(0x16, 0x23, 0x40)  # GSM reset
```

**Python — send a LoRa packet by writing payload registers then triggering TX:**
```python
import smbus
from src.common.utils import crc16_ccitt

bus = smbus.SMBus(1)
payload = bytes([170, 85, 165, 90])
crc = crc16_ccitt(payload)  # validate on the receiving end with the same helper

for i, b in enumerate(payload, start=1):
    bus.write_byte_data(0x16, i, b)
bus.write_byte_data(0x16, 0x23, 0x01)  # trigger TX
```

**Python — poll for a received LoRa packet:**
```python
if bus.read_byte_data(0x16, 0x23) & 0x02:
    bus.write_byte_data(0x16, 0x23, 0x00)  # clear RX flag
    recv = [bus.read_byte_data(0x16, r) for r in (0x11, 0x12, 0x13, 0x14)]
```

## Further reading

- [52Pi Wiki — EP-0105](https://wiki.52pi.com/index.php?title=EP-0105) — full register map, overlay files, wiring diagram
- A9G module AT command reference (Ai-Thinker) for GSM/GPRS/GPS commands
- SC16IS752 datasheet (NXP) for the I2C↔UART bridge register map
