import logging
from src.common import setup_logging

setup_logging(
    log_level = "INFO",
    log_file  = "obc.log",
    console   = True
)

import json
import time
import sys
import os
from src.obc.state_machine import CubeSatStateMachine
from src.obc.handlers import OBCMessageHandlers
from src.common import get_mqtt_client
from src.common import TOPICS, MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE

logger = logging.getLogger(__name__)

class OBC:
    def __init__(self):
        self.mqtt_client = get_mqtt_client("cubesat-obc")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

        self.state_machine = CubeSatStateMachine(self)
        self.handlers      = OBCMessageHandlers(self)

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            logger.error(f"MQTT connection error → rc = {rc}")
            return

        logger.info(f"MQTT connected (rc={rc}, client_id={client._client_id.decode()})")

        client.subscribe(TOPICS["eps_status"], qos=1)
        client.subscribe(TOPICS["command"],    qos=1)

        self.mqtt_client.publish(
            TOPICS["obc_status"],
            json.dumps({"state": self.state_machine.state, "timestamp": time.time()}),
            qos=1,
            retain=True
        )

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode('utf-8')
            topic = msg.topic

            if topic == TOPICS["eps_status"]:
                self.handlers.handle_eps_status(payload)
            elif topic == TOPICS["command"]:
                self.handlers.handle_command(payload)
            else:
                logger.debug(f"Unhandled topic: {topic}")
        except Exception as e:
            logger.error(f"Error processing message {msg.topic}: {e}")

    def run(self):
        try:
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
            self.mqtt_client.loop_start()

            logger.info(f"OBC started. State: {self.state_machine.state}")

            while True:
                self.mqtt_client.publish(
                    TOPICS["obc_status"],
                    json.dumps({"state": self.state_machine.state, "ts": time.time()}),
                    retain=True
                )
                time.sleep(30)

        except KeyboardInterrupt:
            logger.info("Stopped by Ctrl+C")
        except Exception as e:
            logger.exception("Critical error in OBC main loop")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            logger.info("OBC shutdown complete")

if __name__ == "__main__":
    obc = OBC()
    obc.run()