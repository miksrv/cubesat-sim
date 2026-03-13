import logging
import json
import time
import sqlite3
from datetime import datetime
import psutil
import requests

from src.common import get_mqtt_client
from src.common.config import DB_PATH, TOPICS, MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE, TELEMETRY_API_KEY, TELEMETRY_API_URL, TELEMETRY_SEND_INTERVAL_SEC, TELEMETRY_SEND_ENABLED
from src.common.system_metrics import SystemMetricsCollector

logger = logging.getLogger(__name__)

class TelemetryAggregator:
    def __init__(self):
        self.mqtt_client = get_mqtt_client("cubesat-telemetry")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

        # Cache for latest subsystem data (updated via MQTT)
        self.latest = {
            "obc": {},       # from OBC (On-Board Computer)
            "eps": {},       # from EPS
            "adcs": {},      # from ADCS
            "payload": {},   # from Payload (science data)
            # add others if needed
        }

        self.system_collector = SystemMetricsCollector()

        # Initialize database
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._create_table()

    def _create_table(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS telemetry_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                battery REAL,
                voltage REAL,
                external_power INTEGER,
                roll REAL, pitch REAL, yaw REAL,
                imu_temp REAL,
                accel_x REAL, accel_y REAL, accel_z REAL,
                gyro_x REAL, gyro_y REAL, gyro_z REAL,
                temperature REAL, humidity REAL, pressure REAL,
                cpu_percent REAL,
                ram_percent REAL,
                swap_percent REAL,
                disk_percent REAL,
                uptime_seconds INTEGER,
                cpu_temperature REAL,
                obc_state TEXT,
                raw_json TEXT
            )
        ''')
        self.conn.commit()

    def on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logger.error(f"MQTT connection error → rc = {reason_code}")
            return

        logger.info(f"MQTT connected (rc={reason_code}, client_id={client._client_id.decode()})")

        client.subscribe(TOPICS["obc_status"], qos=1)
        client.subscribe(TOPICS["eps_status"], qos=1)
        client.subscribe(TOPICS["adcs_status"], qos=1)
        client.subscribe(TOPICS["payload_data"], qos=1)
        client.subscribe(TOPICS["command"], qos=1)

    def on_mqtt_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            data = json.loads(payload)

            if topic == TOPICS["obc_status"]:
                self.latest["obc"] = data
            elif topic == TOPICS["eps_status"]:
                self.latest["eps"] = data
            elif topic == TOPICS["adcs_status"]:
                self.latest["adcs"] = data
            elif topic == TOPICS["payload_data"]:
                self.latest["payload"] = data
            elif topic == TOPICS["command"]:
                if data.get("command") == "get_telemetry":
                    packet = self.build_telemetry_packet()
                    packet["request_id"] = data.get("request_id")
                    self.mqtt_client.publish(
                        TOPICS["telemetry_data"],
                        json.dumps(packet),
                        qos=1,
                        retain=True
                    )

            logger.debug(f"Updated data from {topic}")
        except Exception as e:
            logger.error(f"Error processing MQTT {topic}: {e}")

    def build_telemetry_packet(self):
        now = datetime.utcnow().isoformat() + "Z"
        system = self.system_collector.collect(with_interval=0.8)
        packet = {
            "timestamp": now,
            "obc_state": self.latest.get("obc", {}).get("status", "UNKNOWN"),
            "eps": self.latest.get("eps", {}),
            "adcs": self.latest.get("adcs", {}),
            "payload": self.latest.get("payload", {}),
            "system": system,
        }
        return packet

    def aggregate(self):
        """Collects a full telemetry packet"""
        packet = self.build_telemetry_packet()
        self._log_to_db(packet)

        logger.debug(f"Telemetry aggregated: {packet['timestamp']}")

    def _log_to_db(self, packet):
        cursor = self.conn.cursor()
        adcs   = packet.get("adcs", {})
        accel  = adcs.get("accel_g", {})
        gyro   = adcs.get("gyro_dps", {})

        cursor.execute('''
            INSERT INTO telemetry_log (
                timestamp, battery, voltage, external_power,
                roll, pitch, yaw,
                imu_temp,
                accel_x, accel_y, accel_z,
                gyro_x, gyro_y, gyro_z,
                temperature, humidity, pressure,
                cpu_percent, ram_percent, swap_percent, disk_percent,
                uptime_seconds, cpu_temperature, obc_state, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    packet["timestamp"],
                    packet["eps"].get("battery", None),
                    packet["eps"].get("voltage", None),
                    1 if packet["eps"].get("external_power", False) else 0,
                    adcs.get("roll", None),
                    adcs.get("pitch", None),
                    adcs.get("yaw", None),
                    adcs.get("imu_temp", None),
                    accel.get("x", None),
                    accel.get("y", None),
                    accel.get("z", None),
                    gyro.get("x", None),
                    gyro.get("y", None),
                    gyro.get("z", None),
                    packet["payload"].get("temperature", None),
                    packet["payload"].get("humidity", None),
                    packet["payload"].get("pressure", None),
                    packet["system"].get("cpu_percent", None),
                    packet["system"].get("ram_percent", None),
                    packet["system"].get("swap_percent", None),
                    packet["system"].get("disk_percent", None),
                    packet["system"].get("uptime_seconds", None),
                    packet["system"].get("cpu_temperature", None),
                    packet.get("obc_state", None),
                    json.dumps(packet, ensure_ascii=False)
                ))
        self.conn.commit()

    def send_to_remote_api(self, packet):
        """Send telemetry packet to remote API server."""
        if not TELEMETRY_API_KEY:
            logger.warning("Remote telemetry API key not set; skipping send.")
            return
        url = f"{TELEMETRY_API_URL}/api/cubesat/telemetry"
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": TELEMETRY_API_KEY
        }
        try:
            response = requests.post(url, headers=headers, json=packet, timeout=5)
            if response.status_code == 201:
                logger.info(f"Telemetry sent to remote API: {packet['timestamp']}")
            else:
                logger.error(f"Remote API error: {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"Failed to send telemetry to remote API: {e}")

    def internet_available(self):
        """Check if internet is available (simple ping to API server)."""
        try:
            requests.get(f"{TELEMETRY_API_URL}/api/cubesat/telemetry/latest", timeout=3)
            return True
        except Exception:
            return False

    def run(self):
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.mqtt_client.loop_start()

        logger.info("Telemetry Aggregator started")

        try:
            remote_enabled = self.internet_available()
            if remote_enabled:
                logger.info("Remote telemetry API available; sending enabled.")
            else:
                logger.warning("Remote telemetry API unavailable; sending disabled.")

            # Option flag from config
            while True:
                self.aggregate()
                # Send to remote API if enabled in config and internet is available
                if TELEMETRY_SEND_ENABLED and remote_enabled:
                    packet = self.build_telemetry_packet()
                    self.send_to_remote_api(packet)
                time.sleep(TELEMETRY_SEND_INTERVAL_SEC)
        except KeyboardInterrupt:
            logger.info("Telemetry Aggregator stopped by Ctrl+C")
        except Exception as e:
            logger.exception("Critical error in main Telemetry Aggregator loop")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.conn.close()
            logger.info("Telemetry Aggregator stopped")
