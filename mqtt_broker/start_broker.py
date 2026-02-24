# mqtt_broker/start_broker.py
import asyncio
from hbmqtt.broker import Broker

# Конфигурация брокера
config = {
    'listeners': {
        'default': {
            'type': 'tcp',
            'bind': '0.0.0.0:1883',  # MQTT по умолчанию
        },
    },
    'sys_interval': 10,
    'auth': {
        'allow-anonymous': True,  # для прототипа
        # можно добавить username/password позже
    },
    'topic-check': {
        'enabled': False,
    }
}

async def start_broker():
    broker = Broker(config)
    await broker.start()
    print("MQTT Broker started on port 1883")

if __name__ == "__main__":
    asyncio.run(start_broker())