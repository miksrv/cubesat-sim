# src/payload/main.py
import paho.mqtt.client as mqtt
import json
import logging
import sys
import time
import os

from .camera import PayloadCamera
from .science import ScienceCollector  # если используешь

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/cubesat/payload.log')
    ]
)
logger = logging.getLogger(__name__)

class PayloadService:
    def __init__(self):
        self.mqtt_client = mqtt.Client(client_id="cubesat-payload")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect

        self.camera = PayloadCamera()  # Инициализация камеры
        self.science = ScienceCollector()  # если нужно

        self.obc_state = None  # текущее состояние OBC

        # Подписки
        self.subscriptions = [
            ("cubesat/command/payload", 1),
            ("cubesat/command/photo", 1),     # для прямых запросов фото от Telegram/OBC
            ("cubesat/obc/status", 1),        # подписка на состояние OBC
        ]

    def connect_mqtt(self):
        broker = "localhost"  # или из конфига
        port = 1883
        try:
            self.mqtt_client.connect(broker, port, keepalive=60)
            self.mqtt_client.loop_start()
        except Exception as e:
            logger.error(f"Не удалось подключиться к MQTT: {e}")
            time.sleep(5)
            sys.exit(1)

    def on_mqtt_connect(self, client, userdata, flags, rc):
        logger.info(f"MQTT подключён (rc={rc})")
        for topic, qos in self.subscriptions:
            client.subscribe(topic, qos)
            logger.info(f"Подписан на {topic} (QoS={qos})")

    def on_mqtt_disconnect(self, client, userdata, rc):
        logger.warning(f"MQTT отключён (rc={rc}) → переподключение...")
        time.sleep(3)
        self.connect_mqtt()

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload_str = msg.payload.decode('utf-8')
            topic = msg.topic
            data = json.loads(payload_str)

            logger.info(f"Получено сообщение в {topic}: {data}")

            if topic == "cubesat/obc/status":
                # Сохраняем состояние OBC
                state = data.get("state", "UNKNOWN")
                if state:
                    self.obc_state = state
                    logger.info(f"OBC state обновлён: {self.obc_state}")
                return

            if topic in ["cubesat/command/payload", "cubesat/command/photo"]:
                action = data.get("action")
                request_id = data.get("request_id", f"req_{int(time.time())}")

                # Проверка состояния OBC перед take_photo
                if (action == "take_photo" or topic == "cubesat/command/photo"):
                    if self.obc_state != "NOMINAL":
                        response = {
                            "status": "error",
                            "request_id": request_id,
                            "reason": f"Photo capture not allowed: OBC state is '{self.obc_state}'"
                        }
                        self.publish(f"cubesat/response/photo/{request_id}", json.dumps(response))
                        logger.warning(f"Запрос фото отклонён: OBC state = {self.obc_state}")
                        return
                    overlay = data.get("overlay", False)
                    path = self.camera.take_photo(overlay=overlay)
                    response = {
                        "status": "ok",
                        "request_id": request_id,
                        "path": path,
                        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0
                    }
                    self.publish(f"cubesat/response/photo/{request_id}", json.dumps(response))

                # Другие действия (timelapse, start_science и т.д.)
                elif action == "start_timelapse":
                    interval = data.get("interval", 60)  # секунды
                    self.camera.start_timelapse(interval)

                elif action == "stop_timelapse":
                    self.camera.stop_timelapse()

        except json.JSONDecodeError:
            logger.error(f"Невалидный JSON в {topic}")
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения {topic}: {e}")

    def publish(self, topic, payload, qos=1, retain=False):
        try:
            self.mqtt_client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as e:
            logger.error(f"Ошибка публикации в {topic}: {e}")

    def run(self):
        self.connect_mqtt()
        logger.info("Payload сервис запущен")

        try:
            while True:
                # Здесь можно добавить периодические задачи, например сбор science данных
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Payload остановлен по Ctrl+C")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.camera.cleanup()

if __name__ == "__main__":
    service = PayloadService()
    service.run()