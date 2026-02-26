import logging
from src.common import setup_logging

setup_logging(
    log_level = "INFO",
    log_file  = "payload.log",
    console   = True
)

import json
import sys
import time
import os

from src.payload.camera import PayloadCamera
from src.payload.science import ScienceCollector
from src.common import get_mqtt_client
from src.common import TOPICS, MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE

logger = logging.getLogger(__name__)

class PayloadService:
    def __init__(self):
        # Один раз создаём клиента с хорошими настройками
        self.mqtt_client = get_mqtt_client("cubesat-payload")

        # Назначаем свои обработчики
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

        self.camera    = PayloadCamera()  # Инициализация камеры
        self.science   = ScienceCollector()  # если нужно
        self.obc_state = None  # текущее состояние OBC

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            logger.error(f"Ошибка подключения MQTT → rc = {rc}")
            return

        logger.info(f"MQTT подключён (rc={rc}, client_id={client._client_id.decode()})")

        client.subscribe(TOPICS["obc_status"],    qos=1)
        client.subscribe(TOPICS["command_photo"], qos=1)

        self.mqtt_client.publish(
            TOPICS["payload_status"],
            f'{{"state": "IDLE", "alive": true, "ts": {time.time()}}}',
            qos=1,
            retain=True
        )

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload_str = msg.payload.decode('utf-8')
            topic = msg.topic
            data = json.loads(payload_str)

            logger.info(f"Получено сообщение в {topic}: {data}")

            if topic == TOPICS["obc_status"]:
                # Сохраняем состояние OBC
                state = data.get("state", "UNKNOWN")
                if state:
                    self.obc_state = state
                    logger.info(f"OBC state обновлён: {self.obc_state}")
                return

            if topic == TOPICS["command_photo"]:
                request_id = data.get("request_id", f"req_{int(time.time())}")

                if self.obc_state != "NOMINAL":
                    response = {
                        "status": "error",
                        "request_id": request_id,
                        "reason": f"Photo capture not allowed: OBC state is '{self.obc_state}'"
                    }
                    self.mqtt_client.publish(
                        TOPICS["payload_status"],
                        json.dumps(response),
                        qos=1,
                        retain=True
                    )
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
                self.mqtt_client.publish(
                    TOPICS["payload_status"],
                    json.dumps(response),
                    qos=1,
                    retain=True
                )

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
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.mqtt_client.loop_start()

        logger.info("Payload сервис запущен")

        try:
            while True:
                # Здесь можно добавить периодические задачи, например сбор science данных
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Payload остановлен по Ctrl+C")
        except Exception as e:
            logger.exception("Критическая ошибка в подсистеме Payload")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.camera.cleanup()
            logger.info("Payload сервис завершил работу")

if __name__ == "__main__":
    service = PayloadService()
    service.run()