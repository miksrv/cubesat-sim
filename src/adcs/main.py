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

logger = logging.getLogger(__name__)

class ADCS:
    def __init__(self):
        self.mqtt_client = get_mqtt_client("cubesat-adcs")
        self.imu = IMU()
        logger.info("ADCS подсистема инициализирована")

    def publish_status(self):
        try:
            ori = self.imu.get_orientation_deg()
            temp = self.imu.read_imu_temp()

            packet = {
                "timestamp": time.time(),
                "roll": ori["roll"],
                "pitch": ori["pitch"],
                "yaw": ori["yaw"],
                "imu_temp": round(temp, 2),
                "accel_g": ori["accel_g"],
                "gyro_dps": ori["gyro_dps"]
            }

            self.mqtt_client.publish(
                TOPICS["adcs_status"],
                json.dumps(packet),
                qos=1
            )
            logger.debug(f"Опубликована ориентация: {packet}")
        except Exception as e:
            logger.error(f"Ошибка чтения/публикации ADCS: {e}")

    def run(self):
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self.mqtt_client.loop_start()

        try:
            while True:
                self.publish_status()
                time.sleep(0.5)  # 1 Гц — можно сделать 5–10 Гц, подстрой halfT в IMU
        except KeyboardInterrupt:
            logger.info("Остановка ADCS")
        finally:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

if __name__ == "__main__":
    adcs = ADCS()
    adcs.run()