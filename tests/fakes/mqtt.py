"""A fake MQTT client.

Records what was published and subscribed, and lets a test hand a message to the
service as if the broker had delivered it. No sockets, no threads, no broker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class Published:
    topic: str
    payload: str
    qos: int
    retain: bool

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self.payload)


class _Message:
    def __init__(self, topic: str, payload: str) -> None:
        self.topic = topic
        self.payload = payload.encode("utf-8")


class FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[Published] = []
        self.subscribed: list[str] = []
        self.will: Published | None = None
        self.loop_running = False
        self.disconnected = False
        self.connected_to: tuple | None = None
        #: What a real broker would hold as retained and replay on subscribe.
        #: Kept so a test can stage the satellite's state *before* the code under
        #: test connects — which is the normal shape for the CLI, whose whole
        #: data source is what the broker replays the moment it subscribes.
        self._retained: dict[str, str] = {}
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    # ── the paho surface the Service base class uses ────────────────────────

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append(Published(topic, payload, qos, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = Published(topic, payload, qos, retain)

    def connect_async(self, *_args, **_kwargs):
        return None

    def connect(self, host=None, port=None, keepalive=None):
        """The blocking connect the CLI uses.

        Calls ``on_connect`` straight away, because the CLI waits for it before
        doing anything and a test should not sit out that timeout. Anything
        needing a refusal replaces this method.

        Then replays whatever was delivered before the connection, as a broker
        replays its retained messages on subscribe. Without this a test would
        have to interleave its staging with the code under test.
        """
        self.connected_to = (host, port, keepalive)
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0, None)
        if self.on_message is not None:
            for topic, body in self._retained.items():
                self.on_message(self, None, _Message(topic, body))

    def loop_start(self):
        self.loop_running = True

    def loop_stop(self):
        self.loop_running = False

    def disconnect(self):
        self.disconnected = True

    # ── test helpers ────────────────────────────────────────────────────────

    def bind(self, service) -> None:
        """Attach the service's callbacks as ``run()`` would, without running it."""
        service.client = self
        self.on_connect = service._on_connect
        self.on_disconnect = service._on_disconnect
        self.on_message = service._on_message

    def connect_ok(self) -> None:
        """Simulate a successful broker connection, triggering subscriptions."""
        self.on_connect(self, None, None, 0, None)

    def connect_refused(self, reason: int = 5) -> None:
        self.on_connect(self, None, None, reason, None)

    def deliver(self, topic: str, payload: dict[str, Any] | str) -> None:
        """Hand a message to the service as the broker would."""
        body = payload if isinstance(payload, str) else json.dumps(payload)
        self._retained[topic] = body
        if self.on_message is not None:
            self.on_message(self, None, _Message(topic, body))

    def payloads(self, topic: str) -> list[dict[str, Any]]:
        """Every message published on ``topic``, decoded.

        An empty payload is skipped rather than decoded: in MQTT that is not a
        message but the instruction to forget a retained one, and PAYLOAD sends
        exactly that to clear the last photograph when it starts. Handing a
        caller a JSONDecodeError for it would make every assertion about a topic
        depend on whether anything had ever been cleared on it.
        """
        return [p.data for p in self.published if p.topic == topic and p.payload]

    def last(self, topic: str) -> dict[str, Any]:
        return self.payloads(topic)[-1]
