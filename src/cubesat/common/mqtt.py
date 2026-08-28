"""MQTT client factory.

One place that knows the broker settings, the protocol version and the
reconnect policy. Services never construct a client themselves.

Two deliberate choices:

* ``connect_async`` rather than ``connect``. At boot the broker may not be
  listening yet; a service must wait for it, not crash and be restarted by
  systemd in a loop.
* A **last will** on the heartbeat topic. When a service dies ungracefully the
  broker announces it immediately, so OBC learns in milliseconds instead of
  waiting out three missed heartbeats.
"""

from __future__ import annotations

import json

import paho.mqtt.client as mqtt

from cubesat.common import config
from cubesat.common.topics import TOPICS

RECONNECT_MIN_DELAY_SEC = 1
RECONNECT_MAX_DELAY_SEC = 60


def make_client(service: str) -> mqtt.Client:
    """Return a configured, unconnected MQTTv5 client for ``service``."""
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"cubesat-{service}",
        protocol=mqtt.MQTTv5,
    )
    client.reconnect_delay_set(
        min_delay=RECONNECT_MIN_DELAY_SEC,
        max_delay=RECONNECT_MAX_DELAY_SEC,
    )
    client.will_set(
        TOPICS["heartbeat"],
        json.dumps({"service": service, "alive": False}),
        qos=1,
        retain=False,
    )
    return client


def connect(client: mqtt.Client) -> None:
    """Start connecting in the background and run the network loop."""
    client.connect_async(config.MQTT_BROKER, config.MQTT_PORT, keepalive=config.MQTT_KEEPALIVE)
    client.loop_start()
