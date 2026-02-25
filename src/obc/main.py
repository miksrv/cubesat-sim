# src/obc/main.py
import paho.mqtt.client as mqtt
import time
import logging
import sys
import os

from state_machine import CubeSatStateMachine
from handlers import OBCMessageHandlers

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/cubesat/obc.log')
    ]
)
logger = logging.getLogger(__name__)

class OBC:
    def __init__(self):
        self.mqtt_client = mqtt.Client(client_id="cubesat-obc")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect

        self.state_machine = CubeSatStateMachine(self)
        self.handlers = OBCMessageHandlers(self)

        # Подписки (можно вынести в config)
        self.subscriptions = [
            ("cubesat/eps/status", 1),
            ("cubesat/command", 1),
        ]

    def connect_mqtt(self):
        broker = "localhost"          # или из конфига
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

    def publish(self, topic, payload, qos=1, retain=False):
        try:
            self.mqtt_client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as e:
            logger.error(f"Ошибка публикации в {topic}: {e}")

    def publish_control(self, subtopic, payload):
        """Удобный метод для команд управления подсистемами"""
        topic = f"cubesat/control/{subtopic}"
        self.publish(topic, payload)

    def run(self):
        self.connect_mqtt()
        logger.info("OBC запущен. Текущее состояние: " + self.state_machine.state)

        try:
            while True:
                # Здесь можно добавить периодические действия OBC
                # Например: публикация текущего состояния раз в 30 сек
                self.publish("cubesat/obc/status",
                             f'{{"state": "{self.state_machine.state}"}}',
                             retain=True)
                time.sleep(30)
        except KeyboardInterrupt:
            logger.info("OBC остановлен по Ctrl+C")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

if __name__ == "__main__":
    obc = OBC()
    obc.run()