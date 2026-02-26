import logging
from src.common import setup_logging

setup_logging(
    log_level    = "INFO",
    log_file     = "obc.log",
    console      = True
)

import time
import sys
import os
from state_machine import CubeSatStateMachine
from handlers import OBCMessageHandlers
from src.common import get_mqtt_client
from src.common import TOPICS, MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE

logger = logging.getLogger(__name__)

class OBC:
    def __init__(self):
        # Один раз создаём клиента с хорошими настройками
        self.mqtt_client = get_mqtt_client("cubesat-obc")

        # Назначаем свои обработчики
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

        self.state_machine = CubeSatStateMachine(self)
        self.handlers      = OBCMessageHandlers(self)

    def on_mqtt_connect(self, client, userdata, flags, rc):
        if rc != 0:
            logger.error(f"Ошибка подключения MQTT → rc = {rc}")
            return

        logger.info(f"MQTT подключён (rc={rc}, client_id={client._client_id.decode()})")

        client.subscribe(TOPICS["eps_status"], qos=1)
        client.subscribe(TOPICS["command"],    qos=1)

        self.mqtt_client.publish(
            TOPICS["obc_status"],
            f'{{"state": "{self.state_machine.state}", "alive": true, "ts": {time.time()}}}',
            qos=1,
            retain=True
        )

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode('utf-8')
            topic = msg.topic

            if topic == "cubesat/eps/status":
                self.handlers.handle_eps_status(payload)
            elif topic == "cubesat/command":
                self.handlers.handle_command(payload)
            else:
                logger.debug(f"Необработанный топик: {topic}")
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения {msg.topic}: {e}")

    # def publish_control(self, subtopic, payload):
    #     """Удобный метод для команд управления подсистемами"""
    #     topic = f"cubesat/control/{subtopic}"
    #     self.publish(topic, payload)

    def run(self):
        try:
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
            self.mqtt_client.loop_start()

            logger.info(f"OBC запущен. Состояние: {self.state_machine.state}")

            while True:
                self.mqtt_client.publish(
                    TOPICS["obc_status"],
                    f'{{"state": "{self.state_machine.state}", "ts": {time.time()}}}',
                    retain=True
                )
                time.sleep(30)

        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C")
        except Exception as e:
            logger.exception("Критическая ошибка в главном цикле OBC")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            logger.info("OBC завершил работу")

if __name__ == "__main__":
    obc = OBC()
    obc.run()