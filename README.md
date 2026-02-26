# CubeSat Sim

CubeSat Sim is an educational simulation platform for CubeSat systems. It is designed for learning, prototyping, and testing distributed satellite software architectures. The project simulates the onboard computer (OBC), payload, telemetry, power, attitude control, and communication subsystems, using Python and MQTT for inter-service communication.

## Features
- Modular structure for each CubeSat subsystem
- State machine logic for OBC
- MQTT-based command and telemetry exchange
- Logging and configuration management
- Example scripts for testing and simulation

## Service Structure and Responsibilities

1\. **OBC (On-Board Computer)**  
The central controller of the CubeSat. Implements a state machine with transitions: BOOT → DEPLOY → NOMINAL → SCIENCE → LOW\_POWER → SAFE. Manages subsystem coordination and overall mission logic.

2\. **EPS (Electrical Power System)**  
Monitors battery and external power sources. Publishes power status and handles power-related events.

3\. **ADCS (Attitude Determination and Control System)**  
Uses Sense HAT sensors (gyroscope, accelerometer, magnetometer) to determine and control the satellite's orientation.

4\. **Payload**  
Handles camera operations, timelapse photography, and collection of scientific data (e.g., temperature and other measurements).

5\. **Telemetry Aggregator**  
Collects data from all subsystems and compiles it into unified telemetry packets for transmission and logging.

6\. **Communication**  
Provides two communication modes: WiFi/MQTT for local and remote control, and LoRa 433 MHz for long-range, low-power data exchange.

## MQTT Communication

CubeSat Sim uses MQTT as the primary protocol for inter-service communication. Each subsystem publishes and subscribes to specific topics for commands, status updates, telemetry, and data exchange. This enables modular, distributed operation and easy integration with external tools.

### Main MQTT Topics

- `cubesat/command`: General commands for the CubeSat, sent to the OBC.
- `cubesat/command/photo`: Commands related to photo capture, handled by the payload subsystem.
- `cubesat/command/payload`: Payload-specific commands for science operations.
- `cubesat/obc/status`: Status updates from the OBC.
- `cubesat/eps/status`: Power system status messages.
- `cubesat/adcs/status`: Attitude control system status.
- `cubesat/payload/data`: Scientific and camera data from the payload.
- `cubesat/telemetry`: Aggregated telemetry packets from all subsystems.
- `cubesat/request/telemetry`: Requests for telemetry data (e.g., from ground station).
- `cubesat/response/telemetry`: Responses with requested telemetry.
- `cubesat/control/#`: Control messages for subsystem management, sent by OBC.
- `cubesat/response/photo/#`: Responses to photo commands, including image metadata or status.

Each topic is used for a specific purpose, allowing clear separation of responsibilities and reliable message routing between services.

## Directory Structure

```
cubesat/
├── src/                          # main code for all services
│   ├── obc/                      # On-Board Computer — main brain
│   │   ├── __init__.py
│   │   ├── main.py               # OBC service entry point
│   │   ├── state_machine.py      # transitions logic + states
│   │   └── handlers.py           # command and event handlers
│   │
│   ├── eps/                      # Electrical Power System
│   │   ├── __init__.py
│   │   ├── main.py
│   │   └── power_monitor.py      # X728 reading, discharge simulation
│   │
│   ├── adcs/                     # Attitude Determination and Control
│   │   ├── __init__.py
│   │   ├── main.py
│   │   └── sensors.py            # Sense HAT orientation logic
│   │
│   ├── payload/                  # camera, science data, etc.
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── camera.py             # photo / timelapse logic
│   │   └── science.py            # "science" data collection
│   │
│   ├── telemetry/                # telemetry aggregator
│   │   ├── __init__.py
│   │   ├── main.py
│   │   └── aggregator.py
│   │
│   ├── comm/                     # Communication (WiFi MQTT + LoRa)
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── mqtt_handler.py
│   │   └── lora_handler.py       # serial / LoRa module logic
│   │
│   └── common/                   # shared code used by multiple services
│       ├── __init__.py
│       ├── mqtt_client.py        # paho-mqtt wrapper with reconnect
│       ├── config.py             # settings, constants, topics
│       ├── logging_setup.py
│       └── utils.py              # helpers (json, crc, time, etc.)
│
├── scripts/                      # auxiliary scripts (not services)
│   ├── test_mqtt.py              # simple publisher/subscriber for debugging
│   ├── simulate_low_battery.py   # force battery discharge for tests
│   ├── deploy_antena_sim.py      # DEPLOY simulation
│   └── check_all_services.sh     # check status of all .service
│
├── config/                       # configuration files
│   ├── config.yaml               # main settings (topics, intervals, paths)
│   ├── logging.yaml              # logging config (if using dictConfig)
│   └── secrets.yaml              # mqtt credentials, serial ports (git ignore!)
│
├── systemd/                      # ready-to-use unit files for copying
│   ├── cubesat-obc.service
│   ├── cubesat-eps.service
│   ├── cubesat-adcs.service
│   ├── cubesat-payload.service
│   ├── cubesat-telemetry.service
│   └── cubesat-comm.service
│
├── logs/                         # log files (can be .gitignore)
│   └── (empty, created automatically)
│
├── data/                         # temporary and persistent data
│   ├── photos/                   # saved camera images
│   ├── telemetry_archive/        # archive of json packets (optional)
│   └── lora_packets/             # logs / saved LoRa packets (optional)
│
├── tests/                        # unit tests (pytest)
│   ├── test_state_machine.py
│   ├── test_mqtt.py
│   └── test_eps.py
│
├── .gitignore
├── README.md
├── requirements.txt              # or pyproject.toml / poetry.lock
└── setup.sh                      # initial setup script (apt, pip, copy service)
```

## Getting Started

1. Clone the repository
2. Install dependencies from `requirements.txt`
3. Run individual subsystem services from `src/`
4. Use scripts in `scripts/` for testing and simulation

## License

See LICENSE file for details.

