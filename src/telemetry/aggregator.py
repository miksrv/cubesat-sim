import json
import time
import logging
import sqlite3
from datetime import datetime
import psutil

from ..common.mqtt_client import get_mqtt_client  # предполагаем общий MQTT-хелпер
from ..common.config import DB_PATH, TOPICS

logger = logging.getLogger(__name__)

class TelemetryAggregator:
    def __init__(self):
        self.mqtt_client = get_mqtt_client(client_id="cubesat-telemetry")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

        # Кэш последних данных от подсистем (обновляется по MQTT)
        self.latest = {
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
                obc_state TEXT,
                raw_json TEXT
            )
        ''')
        self.conn.commit()

    def on_mqtt_connect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.error(f"Ошибка подключения MQTT → rc = {rc}")
            return

        logger.info(f"MQTT подключён (rc={rc}, client_id={client._client_id.decode()})")

        client.subscribe(TOPICS["obc_status"], qos=1)
        client.subscribe(TOPICS["eps_status"], qos=1)
        client.subscribe(TOPICS["adcs_status"], qos=1)

    def on_mqtt_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            data = json.loads(payload)

            if topic == "cubesat/eps/status":
                self.latest["eps"] = data
            elif topic == "cubesat/adcs/status":
                self.latest["adcs"] = data
            elif topic == "cubesat/payload/data":
                self.latest["payload"] = data
            # Добавь обработку других топиков

            logger.debug(f"Обновлены данные из {topic}")
        except Exception as e:
            logger.error(f"Ошибка обработки MQTT {topic}: {e}")

    def collect_system_metrics(self):
        """Собирает метрики Raspberry Pi"""
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_percent": psutil.virtual_memory().percent,
            "swap_percent": psutil.swap_memory().percent if hasattr(psutil, 'swap_memory') else 0.0,
            "disk_percent": psutil.disk_usage('/').percent,
            "uptime_seconds": int(time.time() - psutil.boot_time()),
        }

    def aggregate(self):
        """Собирает полный телеметрический пакет"""
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

        # Публикация в MQTT
        self.client.publish("cubesat/telemetry", json.dumps(packet), qos=1, retain=True)

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
                           uptime_seconds, obc_state, raw_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ''', (
                           packet["timestamp"],
                           packet["eps"].get("battery"),
                           packet["eps"].get("voltage"),
                           1 if packet["eps"].get("external_power") else 0,
                           packet["adcs"].get("roll"),
                           packet["adcs"].get("pitch"),
                           packet["adcs"].get("yaw"),
                           packet["payload"].get("temperature") or packet["adcs"].get("temperature"),  # пример
                           packet["payload"].get("humidity"),
                           packet["payload"].get("pressure"),
                           system["cpu_percent"],
                           system["ram_percent"],
                           system["swap_percent"],
                           system["disk_percent"],
                           system["uptime_seconds"],
                           packet["obc_state"],
                           json.dumps(packet)
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
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.conn.close()
            logger.info("Telemetry Aggregator завершил работу")