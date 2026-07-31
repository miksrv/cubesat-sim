import logging
import json
import time
import sqlite3
from datetime import datetime, timedelta
import requests

from src.common import get_mqtt_client
from src.common.config import (
    DB_PATH, TOPICS, MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE,
    COMMS_API_KEY, COMMS_API_URL, COMMS_LOOP_INTERVAL_SEC,
    COMMS_API_ENABLED, COMMS_LORA_ENABLED, COMMS_AGGREGATION_ENABLED,
    COMMS_DB_RETENTION_DAYS,
)
from src.common.system_metrics import SystemMetricsCollector
from src.comms.lora import LoRaModule

logger = logging.getLogger(__name__)

CLEANUP_EVERY_N_LOOPS = 120  # ~1 hour at the default 30s loop interval


class CommsService:
    def __init__(self):
        self.mqtt_client = get_mqtt_client("cubesat-comms")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

        # Cache for latest subsystem data (updated via MQTT)
        self.latest = {
            "obc": {},       # from OBC (On-Board Computer)
            "eps": {},       # from EPS
            "adcs": {},      # from ADCS
            "payload": {},   # from Payload (science data)
        }

        self.system_collector = SystemMetricsCollector()
        self.lora = LoRaModule()

        # Runtime-toggleable channels. Reset to these config defaults on every
        # restart — a ground `set_comms_config` command flips them in-memory
        # only, nothing is persisted to disk.
        self.api_enabled = bool(COMMS_API_ENABLED)
        self.lora_enabled = bool(COMMS_LORA_ENABLED)
        self.aggregation_enabled = bool(COMMS_AGGREGATION_ENABLED)

        # Initialize database
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._create_table()

        self._loop_count = 0

    def _create_table(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS comms_log (
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
                command = data.get("command")
                if command == "get_telemetry":
                    packet = self.build_comms_packet()
                    packet["request_id"] = data.get("request_id")
                    self.mqtt_client.publish(
                        TOPICS["comms_data"],
                        json.dumps(packet),
                        qos=1,
                        retain=True
                    )
                elif command == "set_comms_config":
                    self._apply_config(data.get("params", {}))

            logger.debug(f"Updated data from {topic}")
        except Exception as e:
            logger.error(f"Error processing MQTT {topic}: {e}")

    def _apply_config(self, params):
        if "api_enabled" in params:
            self.api_enabled = bool(params["api_enabled"])
        if "lora_enabled" in params:
            self.lora_enabled = bool(params["lora_enabled"])
        if "aggregation_enabled" in params:
            self.aggregation_enabled = bool(params["aggregation_enabled"])
        logger.info(
            f"COMMS config updated: api_enabled={self.api_enabled}, "
            f"lora_enabled={self.lora_enabled}, aggregation_enabled={self.aggregation_enabled}"
        )

    def _republish_command(self, cmd: dict):
        """Re-injects a command received over an external channel (LoRa/API) onto the
        local cubesat/command topic, unchanged, so existing routing (OBC/Payload/COMMS)
        handles it exactly like a command that arrived directly over MQTT."""
        self.mqtt_client.publish(TOPICS["command"], json.dumps(cmd), qos=1)

    def build_comms_packet(self):
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
        """Collects and stores a full COMMS packet"""
        packet = self.build_comms_packet()
        self._log_to_db(packet)

        logger.debug(f"COMMS packet aggregated: {packet['timestamp']}")

    def _log_to_db(self, packet):
        cursor = self.conn.cursor()
        adcs   = packet.get("adcs", {})
        accel  = adcs.get("accel_g", {})
        gyro   = adcs.get("gyro_dps", {})

        cursor.execute('''
            INSERT INTO comms_log (
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

    def _cleanup_old_records(self):
        cutoff = (datetime.utcnow() - timedelta(days=COMMS_DB_RETENTION_DAYS)).isoformat() + "Z"
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM comms_log WHERE timestamp < ?", (cutoff,))
        self.conn.commit()
        if cursor.rowcount:
            logger.info(f"COMMS DB cleanup: purged {cursor.rowcount} rows older than {COMMS_DB_RETENTION_DAYS} days")

    def send_to_remote_api(self, packet):
        """Send a COMMS packet to the remote API server."""
        if not COMMS_API_KEY:
            logger.warning("Remote COMMS API key not set; skipping send.")
            return
        url = f"{COMMS_API_URL}/api/cubesat/comms"
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": COMMS_API_KEY
        }
        try:
            response = requests.post(url, headers=headers, json=packet, timeout=5)
            if response.status_code == 201:
                logger.info(f"COMMS packet sent to remote API: {packet['timestamp']}")
            else:
                logger.error(f"Remote API error: {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"Failed to send COMMS packet to remote API: {e}")

    def poll_remote_commands(self):
        """Fetches ground commands queued on the remote API and re-injects them onto
        cubesat/command. Assumes a GET .../api/cubesat/commands/pending endpoint
        returning a JSON array of command envelopes — this is a contract this repo
        expects from cubesat-groundstation, not something implemented there yet."""
        try:
            headers = {"X-API-Key": COMMS_API_KEY} if COMMS_API_KEY else {}
            response = requests.get(f"{COMMS_API_URL}/api/cubesat/commands/pending", headers=headers, timeout=5)
            if response.status_code != 200:
                return
            for cmd in response.json():
                self._republish_command(cmd)
        except Exception as e:
            logger.error(f"Failed to poll remote commands: {e}")

    def _check_internet(self):
        """Checked every loop iteration (not just once at startup) — the API channel
        is meant for ground/debug use, where connectivity can come and go."""
        try:
            requests.get(f"{COMMS_API_URL}/api/cubesat/comms/latest", timeout=3)
            return True
        except Exception:
            return False

    def _poll_lora(self):
        packet_bytes = self.lora.receive()
        if packet_bytes is None:
            return
        try:
            cmd = json.loads(packet_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Received malformed LoRa command packet, discarding")
            return
        self._republish_command(cmd)

    def run(self):
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.mqtt_client.loop_start()

        logger.info("COMMS service started")

        try:
            while True:
                self._loop_count += 1
                obc_state = self.latest.get("obc", {}).get("status", "")

                if obc_state == "SCIENCE" and self.aggregation_enabled:
                    self.aggregate()
                    if self._loop_count % CLEANUP_EVERY_N_LOOPS == 0:
                        self._cleanup_old_records()

                if self.api_enabled and self._check_internet():
                    packet = self.build_comms_packet()
                    self.send_to_remote_api(packet)
                    self.poll_remote_commands()

                if self.lora_enabled:
                    packet = self.build_comms_packet()
                    self.lora.send(json.dumps(packet).encode("utf-8"))
                    self._poll_lora()

                time.sleep(COMMS_LOOP_INTERVAL_SEC)
        except KeyboardInterrupt:
            logger.info("COMMS service stopped by Ctrl+C")
        except Exception as e:
            logger.exception("Critical error in main COMMS loop")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.conn.close()
            logger.info("COMMS service stopped")
