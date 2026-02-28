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
        self.mqtt_client = get_mqtt_client("cubesat-payload")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

        self.camera    = PayloadCamera()
        self.science   = ScienceCollector()
        self.obc_state = None

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

            # Обновление состояния OBC
            if topic == TOPICS["obc_status"]:
                state = data.get("state", "UNKNOWN")
                if state:
                    self.obc_state = state
                    logger.info(f"OBC state обновлён: {self.obc_state}")
                return

            # Обработка команды на фото
            if topic == TOPICS["command_photo"]:
                request_id = data.get("request_id", f"req_{int(time.time())}")

                # Проверка состояния OBC
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

                logger.info(f"take_photo вернул path = {path!r}")

                if path and os.path.exists(path):
                    logger.info(f"Файл существует, размер = {os.path.getsize(path)} байт")
                    try:
                        with open(path, "rb") as f:
                            photo_bytes = f.read()
                            logger.info(f"Прочитано {len(photo_bytes)} байт из файла")
                            photo_base64 = base64.b64encode(photo_bytes).decode('utf-8')

                        response = {
                            "status": "ok",
                            "request_id": request_id,
                            "path": path,
                            "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "size_bytes": len(photo_bytes),
                            "photo_base64": photo_base64,
                            "mime_type": "image/jpeg"
                        }

                        # Публикуем полный ответ с фото в основной топик для бота
                        self.mqtt_client.publish(
                            "cubesat/payload/photo",           # ← основной топик для Telegram-бота
                            json.dumps(response),
                            qos=1,
                            retain=False                       # retain=False для больших сообщений
                        )

                        # Опционально: короткий статус без base64 (если нужно для логов/мониторинга)
                        status_only = {
                            "status": "ok",
                            "request_id": request_id,
                            "path": path,
                            "taken_at": response["taken_at"],
                            "size_bytes": response["size_bytes"]
                        }
                        self.mqtt_client.publish(
                            TOPICS["payload_status"],          # ← статус без фото
                            json.dumps(status_only),
                            qos=1,
                            retain=True
                        )

                        logger.info(f"Фото успешно отправлено в MQTT: {path}, size={response['size_bytes']} bytes")
                    except Exception as e:
                        logger.error(f"Ошибка при чтении/кодировании фото {path}: {e}")
                        self._send_error_response(request_id, "Failed to encode photo")
                else:
                    self._send_error_response(request_id, "Failed to capture photo")
                    logger.error(f"take_photo вернул некорректный путь или файл не существует: {path}")

        except json.JSONDecodeError:
            logger.error(f"Невалидный JSON в {topic}")
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения {topic}: {e}")

    def _send_error_response(self, request_id, reason):
        """Вспомогательный метод для отправки ошибки"""
        response = {
            "status": "error",
            "request_id": request_id,
            "reason": reason
        }
        self.mqtt_client.publish(
            "cubesat/payload/photo",
            json.dumps(response),
            qos=1,
            retain=False
        )
        self.mqtt_client.publish(
            TOPICS["payload_status"],
            json.dumps(response),
            qos=1,
            retain=True
        )
        logger.warning(f"Отправлена ошибка по фото: {reason}")

    def run(self):
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.mqtt_client.loop_start()

        logger.info("Payload сервис запущен")

        try:
            while True:
                science_data = self.science.collect()
                self.mqtt_client.publish(
                    TOPICS["payload_data"],
                    json.dumps(science_data),
                    qos=1,
                    retain=False
                )
                time.sleep(60)
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