import logging
import json
import time
import sqlite3
from datetime import datetime
import psutil

from src.common import get_mqtt_client
from src.common.config import DB_PATH, TOPICS, MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE
from src.common.system_metrics import SystemMetricsCollector

logger = logging.getLogger(__name__)

class TelemetryAggregator:
    def __init__(self):
        self.mqtt_client = get_mqtt_client("cubesat-telemetry")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

        # Кэш последних данных от подсистем (обновляется по MQTT)
        self.latest = {
            "obc": {},       # от OBC (On-Board Computer)
            "eps": {},       # от EPS
            "adcs": {},      # от ADCS
            "payload": {},   # от Payload (научные данные)
            # можно добавить другие
        }

        # Инициализация БД
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
                temperature REAL, humidity REAL, pressure REAL,
                cpu_percent REAL,
                ram_percent REAL,
                swap_percent REAL,
                disk_percent REAL,
                uptime_seconds INTEGER,
                cpu_temperature REAL,
                gpu_temperature REAL,
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
        client.subscribe(TOPICS["command_telemetry"], qos=1)

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
            elif topic == TOPICS["command_telemetry"]:
                # При получении команды собираем и публикуем телеметрию
                request_id = None
                if "request_id" in data:
                    request_id = data["request_id"]

                now = datetime.utcnow().isoformat() + "Z"
                system = self.collect_system_metrics()
                packet = {
                    "timestamp": now,
                    "obc_state": self.latest.get("obc", {}).get("state", "UNKNOWN"),
                    "eps": self.latest.get("eps", {}),
                    "adcs": self.latest.get("adcs", {}),
                    "payload": self.latest.get("payload", {}),
                    "system": system,
                }

                if request_id is not None:
                    packet["request_id"] = request_id

                self.mqtt_client.publish(
                    TOPICS["telemetry_data"],
                    json.dumps(packet),
                    qos=1,
                    retain=True
                )

            logger.debug(f"Обновлены данные из {topic}")
        except Exception as e:
            logger.error(f"Ошибка обработки MQTT {topic}: {e}")

    def collect_system_metrics(self) -> dict:
        """Собирает метрики системы через отдельный класс"""
        return SystemMetricsCollector.collect(with_interval=0.8)

    def aggregate(self):
        """Собирает полный телеметрический пакет"""
        now    = datetime.utcnow().isoformat() + "Z"
        system = self.collect_system_metrics()

        packet = {
            "timestamp": now,
            "obc_state": self.latest.get("obc", {}).get("state", "UNKNOWN"),
            "eps": self.latest.get("eps", {}),
            "adcs": self.latest.get("adcs", {}),
            "payload": self.latest.get("payload", {}),
            "system": system,
        }

        # Запись в БД
        self._log_to_db(packet, system)

        logger.info(f"Агрегирована телеметрия: {now}")

    def _log_to_db(self, packet, system):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO telemetry_log (
                timestamp, battery, voltage, external_power,
                roll, pitch, yaw,
                temperature, humidity, pressure,
                cpu_percent, ram_percent, swap_percent, disk_percent,
                uptime_seconds, cpu_temperature, gpu_temperature,
                obc_state, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    packet["timestamp"],
                    packet["eps"].get("battery", None),
                    packet["eps"].get("voltage", None),
                    1 if packet["eps"].get("external_power", False) else 0,
                    packet["adcs"].get("roll", None),
                    packet["adcs"].get("pitch", None),
                    packet["adcs"].get("yaw", None),
                    packet.get("temperature", None),
                    packet["payload"].get("humidity", None),
                    packet["payload"].get("pressure", None),
                    system.get("cpu_percent", None),
                    system.get("ram_percent", None),
                    system.get("swap_percent", None),
                    system.get("disk_percent", None),
                    system.get("uptime_seconds", None),
                    system.get("cpu_temperature", None),
                    system.get("gpu_temperature", None),
                    packet.get("obc_state", None),
                    json.dumps(packet, ensure_ascii=False)
                ))
        self.conn.commit()

    def run(self):
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.mqtt_client.loop_start()

        logger.info("Telemetry Aggregator запущен")

        try:
            while True:
                self.aggregate()
                time.sleep(30)  # интервал агрегации — можно сделать конфигурируемым
        except KeyboardInterrupt:
            logger.info("Остановка Telemetry Aggregator")
        except Exception as e:
            logger.exception("Критическая ошибка в главном цикле Telemetry Aggregator")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.conn.close()
            logger.info("Telemetry Aggregator завершил работу")