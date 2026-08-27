# Raspberry Pi Camera Module V2 (8MP, 1080p)

CSI camera used by the **PAYLOAD** subsystem (`src/cubesat/payload/camera.py`) for on-demand photo capture (`take_photo` command) and timelapse sequences.

Photos are **kept on disk**, filed under the mission that was recording when they were taken (`/var/lib/cubesat/photos/<mission_id>/`). An on-demand capture is additionally published base64-encoded over MQTT (`cubesat/payload/photo`) — that is how a Telegram bot or the dashboard receives it — while a timelapse frame publishes metadata only, because five hundred frames through the broker would crowd out the telemetry the satellite exists to collect. (The pre-rewrite build published the bytes and then deleted the file; nothing keeps the only copy in a message any more.)

- **Product:** [Raspberry Pi Camera Module 2](https://a.co/d/02oyeWg8)
- **Official docs:** [Raspberry Pi Documentation — Camera](https://www.raspberrypi.com/documentation/accessories/camera.html#camera-module-2)

## Specification

| | |
|---|---|
| Sensor | Sony IMX219 |
| Resolution | 8MP, 3280 × 2464 px still |
| Video modes | 1080p47, 1640×1232p41, 640×480p206 |
| Sensor image area | 3.68 × 2.76 mm, 1.12 µm pixel size |
| Connector | 15-pin CSI ribbon (Standard-Standard cable for Pi models up to Pi 4) |
| Variants | Standard (IR-filtered) or NoIR (visible + infrared) |
| Software stack | `libcamera` / `rpicam-apps` (legacy `raspistill`/original `picamera` are deprecated and unsupported) |
| Python library | `picamera2` (this project's dependency, see `CLAUDE.md`) |

## Requirements to run on Raspberry Pi

1. **Connect the ribbon cable** to the CSI connector (blue side facing the Ethernet/USB ports on most Pi models), then boot.
2. Modern Raspberry Pi OS (Bullseye+) auto-detects the camera via `camera_auto_detect=1` in `/boot/firmware/config.txt` — no manual `raspi-config` step is normally required. For manual/legacy setups:
   ```bash
   sudo raspi-config
   # Interface Options → Legacy Camera → keep disabled (project uses libcamera stack, not the legacy stack)
   ```
3. **Verify detection:**
   ```bash
   rpicam-hello --list-cameras
   ```
4. **Install the Python binding used by this project:**
   ```bash
   sudo apt-get install -y python3-picamera2
   # or, inside the project venv:
   pip install picamera2
   ```
5. This project's `PayloadCamera` (`src/payload/camera.py`) configures a still capture with a low-res preview stream and a 180° flip (`Transform(hflip=1, vflip=1)`) to correct for the camera's mounted orientation in the frame (see `hardware/models/frame/Cubesat_RaspbiCam_Frame.stl`).

## Usage examples

**Bash — list detected cameras and take a quick test photo:**
```bash
rpicam-hello --list-cameras
rpicam-jpeg --output test.jpg
```

**Bash — record a short test video:**
```bash
rpicam-vid -t 10s -o test.h264
```

**Python — as implemented in `src/payload/camera.py`:**
```python
from picamera2 import Picamera2
from libcamera import Transform

picam2 = Picamera2()
config = picam2.create_still_configuration(
    main={"size": (3280, 2464)},
    lores={"size": (640, 480), "format": "YUV420"},
    transform=Transform(hflip=1, vflip=1),
)
picam2.configure(config)
picam2.start()

request = picam2.capture_request()
request.save("main", "photo.jpg")
request.release()

picam2.stop()
picam2.close()
```

**Trigger a photo through the running Payload service (ground command over MQTT):**
```bash
mosquitto_pub -t cubesat/command -m '{"command": "take_photo", "request_id": "req_1", "params": {"overlay": false}}'
```
(Only accepted while OBC state is `NOMINAL` — see `src/payload/main.py`.)

## Further reading

- [Raspberry Pi Documentation — Camera](https://www.raspberrypi.com/documentation/accessories/camera.html#camera-module-2) — full specs and variant comparison
- [Raspberry Pi Documentation — Camera software (`rpicam-apps`, Picamera2)](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- Sony IMX219 datasheet for full sensor register-level detail
