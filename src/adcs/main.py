import logging
from src.common import setup_logging

setup_logging(
    log_level = "INFO",
    log_file  = "adcs.log",
    console   = True
)

import time
import json
from src.common import get_mqtt_client
from src.common.config import TOPICS, MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE
from src.common.imu_qmi8658_ak09918 import IMU
from src.common.gps_a9g import GPS

logger = logging.getLogger(__name__)

class ADCS:
    def __init__(self):
        self.mqtt_client = get_mqtt_client("cubesat-adcs")
        self.imu = IMU()
        self.gps = GPS()
        logger.info("ADCS subsystem initialized")

    def publish_status(self):
        try:
            ori = self.imu.get_orientation_deg()
            temp = self.imu.read_imu_temp()
            gps_fix = self.gps.read_position()

            packet = {
                "timestamp": time.time(),
                "roll": ori["roll"],
                "pitch": ori["pitch"],
                "yaw": ori["yaw"],
                "imu_temp": round(temp, 2),
                "accel_g": ori["accel_g"],
                "gyro_dps": ori["gyro_dps"],
                "gps": gps_fix
            }

            self.mqtt_client.publish(
                TOPICS["adcs_status"],
                json.dumps(packet),
                qos=1
            )
        except Exception as e:
            logger.error(f"Error reading/publishing ADCS: {e}")

    def run(self):
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.mqtt_client.loop_start()

        try:
            while True:
                self.publish_status()
                time.sleep(0.5)  # 2 Hz update rate
        except KeyboardInterrupt:
            logger.info("ADCS stopped")
        except Exception as e:
            logger.exception("Critical error in main ADCS loop")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

if __name__ == "__main__":
    adcs = ADCS()
    adcs.run()