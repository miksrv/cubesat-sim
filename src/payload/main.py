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
import base64

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
            logger.error(f"MQTT connection error → rc = {rc}")
            return

        logger.info(f"MQTT connected (rc={rc}, client_id={client._client_id.decode()})")

        client.subscribe(TOPICS["obc_status"],    qos=1)
        client.subscribe(TOPICS["command_photo"], qos=1)

        self.mqtt_client.publish(
            TOPICS["payload_status"],
            json.dumps({
                "state": "IDLE",
                "alive": True,
                "ts": time.time()
            }),
            qos=1,
            retain=True
        )

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload_str = msg.payload.decode('utf-8')
            topic = msg.topic
            data = json.loads(payload_str)
            logger.info(f"Received message in {topic}: {data}")

            # Update OBC state
            if topic == TOPICS["obc_status"]:
                state = data.get("state", "UNKNOWN")
                if state:
                    self.obc_state = state
                    logger.info(f"OBC state updated: {self.obc_state}")
                return

            # Handle photo command
            if topic == TOPICS["command_photo"]:
                request_id = data.get("request_id", f"req_{int(time.time())}")

                # Check OBC state
                if self.obc_state != "NOMINAL":
                    response = {
                        "status": "ERROR",
                        "request_id": request_id,
                        "reason": f"Photo capture not allowed: OBC state is '{self.obc_state}'"
                    }
                    self.mqtt_client.publish(
                        TOPICS["payload_status"],
                        json.dumps(response),
                        qos=1,
                        retain=True
                    )
                    logger.warning(f"Photo request denied: OBC state = {self.obc_state}")
                    return

                overlay = data.get("overlay", False)
                path = self.camera.take_photo(overlay=overlay)

                logger.info(f"take_photo returned path = {path!r}")

                if path and os.path.exists(path):
                    logger.info(f"File exists, size = {os.path.getsize(path)} bytes")
                    try:
                        with open(path, "rb") as f:
                            photo_bytes = f.read()
                            logger.info(f"Read {len(photo_bytes)} bytes from file")
                            photo_base64 = base64.b64encode(photo_bytes).decode('utf-8')

                        response = {
                            "status": "SUCCESS",
                            "request_id": request_id,
                            "path": path,
                            "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "size_bytes": len(photo_bytes),
                            "mime_type": "image/jpeg"
                        }

                        # Publish full response with photo to main topic for bot
                        self.mqtt_client.publish(
                            TOPICS["payload_photo"],  # ← main topic for Telegram bot
                            json.dumps(response),
                            qos=1,
                            retain=False              # retain=False for large messages
                        )

                        logger.info(f"Photo successfully sent to MQTT: {path}, size={response['size_bytes']} bytes")
                    except Exception as e:
                        logger.error(f"Error reading/encoding photo {path}: {e}")
                        self._send_error_response(request_id, "Failed to encode photo")
                else:
                    self._send_error_response(request_id, "Failed to capture photo")
                    logger.error(f"take_photo returned invalid path or file does not exist: {path}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in {topic}")
        except Exception as e:
            logger.error(f"Error processing message {topic}: {e}")

    def _send_error_response(self, request_id, reason):
        """Helper method to send error response"""
        response = {
            "status": "error",
            "request_id": request_id,
            "reason": reason
        }
        self.mqtt_client.publish(
            TOPICS["payload_photo"],
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
        logger.warning(f"Photo error sent: {reason}")

    def run(self):
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.mqtt_client.loop_start()

        logger.info("Payload service started")

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
            logger.info("Payload stopped by Ctrl+C")
        except Exception as e:
            logger.exception("Critical error in Payload subsystem")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.camera.cleanup()
            logger.info("Payload service stopped")

if __name__ == "__main__":
    service = PayloadService()
    service.run()
