import paho.mqtt.client as mqtt
import time
import logging
import random

logger = logging.getLogger(__name__)

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("MQTT подключён успешно")
    else:
        logger.error(f"Ошибка подключения MQTT, код: {rc}")

def on_disconnect(client, userdata, rc, properties=None):
    logger.warning(f"MQTT отключён (rc={rc}) — будет попытка переподключения")

def get_mqtt_client(
        client_id: str,
        username: str = None,
        password: str = None,
        reconnect_delay_min: int = 1,
        reconnect_delay_max: int = 120
) -> mqtt.Client:
    """
    Создаёт MQTT-клиент с автоматическим переподключением и экспоненциальной задержкой.
    """
    client = mqtt.Client(
        client_id=client_id + "_" + str(random.randint(1000, 9999)),
        protocol=mqtt.MQTTv5,
        userdata={"reconnect_delay_min": reconnect_delay_min, "reconnect_delay_max": reconnect_delay_max}
    )

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    if username and password:
        client.username_pw_set(username, password)

    # Настройка автоматического переподключения
    client.reconnect_delay_set(min_delay=reconnect_delay_min, max_delay=reconnect_delay_max)

    return client