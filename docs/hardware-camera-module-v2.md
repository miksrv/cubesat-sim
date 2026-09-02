# Raspberry Pi Camera Module V2 (8MP, 1080p)

CSI camera used by the **PAYLOAD** subsystem (`src/cubesat/payload/camera.py`) for on-demand photo capture (the `take_photo` command) and for a mission's own photography — a frame every `photos.mission_interval_sec` while a mission is open. There is no timelapse command: `start_timelapse`/`stop_timelapse` were retired on 2026-09-01, because a ground-commanded series was a control for something a mission should simply do.

> **Status:** ran on the assembled satellite on 2026-09-01, in `DEMO` — captures on demand and as mission frames, watched live in the dashboard. Bench check V11 was settled in the same session: a cold capture (the sensor closed by `camera.idle_close_sec` between frames) and a warm one came out indistinguishable, luma 94.2 against 94.6; what a cold capture costs is latency, 0.97 s against 0.21 s. The camera still needs re-aiming — the first test frame was mostly two of the satellite's own frame struts.

Photos are **kept on disk**, filed under the mission that was recording when they were taken (`/var/lib/cubesat/photos/<mission_id>/`). An on-demand capture is additionally published base64-encoded over MQTT (`cubesat/payload/photo`) — that is how the dashboard or a bot receives it — while a mission frame publishes metadata only, because a hundred frames through the broker would crowd out the telemetry the satellite exists to collect. (The pre-rewrite build published the bytes and then deleted the file; nothing keeps the only copy in a message any more.) With **no** mission open a photograph is written to `/run/cubesat/photo` — a tmpfs — published as pixels, and deleted: nothing unfiled reaches the card.

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
5. This project's driver (`src/cubesat/hal/rpi/camera.py`) configures a still capture with a low-res preview stream — Picamera2 needs somewhere to run its auto-exposure loops — and a 180° flip (`Transform(hflip=1, vflip=1)`) to correct for the camera's mounted orientation in the frame (see `hardware/models/frame/Cubesat_RaspbiCam_Frame.stl`). The still size is the `camera.resolution` setting, deliberately below the sensor's 3280 × 2464 maximum: full resolution is about 24 MB a frame off the bus and onto the card.

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

**Python — as implemented in `src/cubesat/hal/rpi/camera.py`:**
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
(Accepted only while the mission state permits the camera — `NOMINAL`, per `CAMERA_ALLOWED_STATES` in `src/cubesat/common/states.py` — and only above the `photos.min_free_mb` floor. A refusal is published on the same topic with the reason, so nothing waits for a photo that will never come.)

## Further reading

- [Raspberry Pi Documentation — Camera](https://www.raspberrypi.com/documentation/accessories/camera.html#camera-module-2) — full specs and variant comparison
- [Raspberry Pi Documentation — Camera software (`rpicam-apps`, Picamera2)](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- Sony IMX219 datasheet for full sensor register-level detail
