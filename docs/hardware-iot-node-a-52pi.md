# IoT Node(A) — 52Pi Docker Pi Series (GSM/GPS/LoRa)

Onboard GSM/GPS/LoRa module. GPS is wired into **ADCS** (`src/common/gps_a9g.py`, NMEA over `/dev/ttySC1`) and LoRa is wired into **COMMS** (`src/comms/lora.py`, register access over the SC16IS752 I2C bridge) — both use `crc16_ccitt()` in `src/common/utils.py` for framing/validation. GSM/GPRS (AT commands over `/dev/ttySC0`) remains unused; only the GPS and LoRa halves of this module are currently wired up.

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
5. Python access uses `smbus2` for direct LoRa register control (`src/comms/lora.py`) and `pyserial` + `pynmea2` against `/dev/ttySC1` for GPS NMEA sentences (`src/common/gps_a9g.py`); `/dev/ttySC0` (GSM AT commands) is not used by this project.

## Usage examples

**Bash — confirm the bridge chip and serial devices exist:**
```bash
i2cdetect -y 1
ls -l /dev/ttySC0 /dev/ttySC1
```

**Bash — send AT commands to the GSM module over the bridged UART (unused by this project):**
```bash
echo -e "AT\r" > /dev/ttySC0
cat /dev/ttySC0   # expect "OK"
```

**Python — read the current GPS fix, as implemented in `src/common/gps_a9g.py`:**
```python
from src.common.gps_a9g import GPS

gps = GPS()  # opens /dev/ttySC1 at 9600 baud
fix = gps.read_position()
print(fix)  # {"lat": ..., "lon": ..., "alt": ..., "speed": ..., "fix": True}
```

**Python — send and receive a LoRa packet, as implemented in `src/comms/lora.py`:**
```python
from src.comms.lora import LoRaModule

lora = LoRaModule()  # smbus2, I2C address 0x16
lora.send(b'{"timestamp": "...", "obc_state": "SCIENCE"}')

packet = lora.receive()  # None if nothing pending; CRC-16-CCITT verified internally
if packet is not None:
    print(packet)
```

`LoRaModule` frames payloads as `[length byte][payload][2-byte CRC-16-CCITT]` within the `0x01`–`0x20` register range and triggers/polls TX/RX via the control register `0x23` — see `src/comms/lora.py` for the exact register offsets, which are a simplified convention layered on top of the vendor's raw register map (adjust against real hardware if it differs).

## Further reading

- [52Pi Wiki — EP-0105](https://wiki.52pi.com/index.php?title=EP-0105) — full register map, overlay files, wiring diagram
- A9G module AT command reference (Ai-Thinker) for GSM/GPRS/GPS commands
- SC16IS752 datasheet (NXP) for the I2C↔UART bridge register map
