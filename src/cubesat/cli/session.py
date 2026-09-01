"""One short conversation with the broker, then out.

The services here are built on ``common.service.Service``: connect, subscribe,
loop forever. The CLI is the opposite shape — it says one thing, waits for one
answer and exits with a status code — so it does not inherit that, and the
differences are deliberate rather than incidental:

* **No last will.** ``common.mqtt.make_client`` sets one on the heartbeat topic,
  which is right for a service whose sudden death OBC must hear about
  immediately. A command-line tool exiting is not a fault, and announcing
  ``{"service": "cli", "alive": false}`` on every invocation would put noise on
  the topic OBC watches.
* **No reconnect policy.** A service must wait out a broker that is not up yet,
  because systemd will otherwise restart it in a loop. A person typing
  ``cubesat profile`` wants to be told the broker is unreachable, now, not to
  watch a cursor blink.
* **Retained messages are the whole data source.** Every status this tool prints
  is retained, so subscribing *is* the query: the broker replays the current
  situation on subscribe and there is nothing to poll and no request to make.

The one race worth naming: a switch subscribes to ``host_status`` **before**
publishing ``set_profile``, because HOSTD can apply a profile faster than a
subscription is established, and a confirmation that arrives before you are
listening is a confirmation you wait out to the timeout instead.
"""

from __future__ import annotations

import json
import time
import uuid
from types import TracebackType
from typing import Any

import paho.mqtt.client as mqtt

from cubesat.common import config
from cubesat.common.topics import TOPICS, envelope

#: How long to wait for the broker's TCP connection and CONNACK.
CONNECT_TIMEOUT_SEC = 5.0

#: How long to keep collecting retained messages once subscribed. Short: they
#: arrive in one burst on subscribe, and this is only the ceiling for the case
#: where a topic nobody has ever published to never answers.
COLLECT_WINDOW_SEC = 1.5

#: How long to wait for HOSTD to report a profile switch. Applying one runs
#: several `systemctl` calls, and on a Pi that is seconds rather than
#: milliseconds; a partial apply reports itself just as promptly.
APPLY_TIMEOUT_SEC = 30.0

#: Polling granularity for the waits above. Fine enough that a command feels
#: immediate, coarse enough not to spin a CPU on a satellite.
POLL_SEC = 0.05


class BrokerUnavailable(RuntimeError):
    """The broker did not accept a connection. The message is for a human."""


class Session:
    """A connected client with the retained state it has heard so far."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        client: mqtt.Client | None = None,
        clock: Any = time.monotonic,
        collect_window: float = COLLECT_WINDOW_SEC,
        apply_timeout: float = APPLY_TIMEOUT_SEC,
    ) -> None:
        self._host = host if host is not None else config.MQTT_BROKER
        self._port = port if port is not None else config.MQTT_PORT
        self._client = client if client is not None else _plain_client()
        self._clock = clock
        #: The two waits, injectable because a test must not sit out a real
        #: 30-second apply window to prove what happens when nothing answers.
        self._collect_window = collect_window
        self._apply_timeout = apply_timeout
        self._connected = False
        self._messages: dict[str, dict[str, Any]] = {}
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # ── lifecycle ───────────────────────────────────────────────────────────

    def __enter__(self) -> Session:
        try:
            self._client.connect(self._host, self._port, keepalive=config.MQTT_KEEPALIVE)
        except OSError as exc:
            raise BrokerUnavailable(
                f"cannot reach the broker at {self._host}:{self._port} ({exc})"
            ) from exc
        self._client.loop_start()
        if not self._wait(lambda: self._connected, CONNECT_TIMEOUT_SEC):
            self.close()
            raise BrokerUnavailable(f"the broker at {self._host}:{self._port} did not answer")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    # ── the broker's side ───────────────────────────────────────────────────

    def _on_connect(self, _client: Any, _data: Any, _flags: Any, *_rest: Any) -> None:
        self._connected = True

    def _on_message(self, _client: Any, _data: Any, message: Any) -> None:
        try:
            payload = json.loads(bytes(message.payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Somebody else's malformed payload. Dropped: this tool prints
            # facts, and a half-parsed one is not a fact.
            return
        if isinstance(payload, dict):
            self._messages[message.topic] = payload

    # ── what a command asks for ─────────────────────────────────────────────

    def subscribe(self, *topic_keys: str) -> None:
        for key in topic_keys:
            self._client.subscribe(TOPICS[key], qos=0)

    def collect(self, *topic_keys: str, window: float | None = None) -> dict[str, Any]:
        """Subscribe and return whatever retained state arrives, keyed as given.

        A key that is missing from the result is a topic nobody has published —
        PAYLOAD's status in ``HOSTED``, for instance, where the service is not
        running. That is an answer, so it is not waited out beyond the window.
        """
        self.subscribe(*topic_keys)
        self._wait(
            lambda: all(TOPICS[key] in self._messages for key in topic_keys),
            window if window is not None else self._collect_window,
        )
        return {
            key: self._messages[TOPICS[key]]
            for key in topic_keys
            if TOPICS[key] in self._messages
        }

    def send(self, command: str, **params: Any) -> str:
        """Publish a ground command exactly as any other client does.

        Returns the ``request_id`` it stamped, so a caller can quote it — the
        same id appears in the satellite's logs, which is how a switch that went
        wrong is traced afterwards.
        """
        request_id = f"cli-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {"command": command, "request_id": request_id}
        if params:
            payload["params"] = params
        self._client.publish(TOPICS["command"], envelope(**payload), qos=1)
        return request_id

    def await_message(
        self, topic_key: str, predicate: Any, timeout: float | None = None
    ) -> dict[str, Any] | None:
        """The next payload on ``topic_key`` that satisfies ``predicate``.

        None on timeout, which every caller has to render as its own sentence:
        "no answer" means different things for a switch and for a query.
        """
        topic = TOPICS[topic_key]
        seen = self._messages.get(topic)
        if seen is not None and predicate(seen):
            return seen

        def arrived() -> bool:
            current = self._messages.get(topic)
            return current is not None and current is not seen and predicate(current)

        if self._wait(arrived, timeout if timeout is not None else self._apply_timeout):
            return self._messages[topic]
        return None

    # ── waiting ─────────────────────────────────────────────────────────────

    def _wait(self, until: Any, timeout: float) -> bool:
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if until():
                return True
            time.sleep(POLL_SEC)
        return bool(until())


def _plain_client() -> mqtt.Client:
    """An MQTTv5 client with no will and no reconnect policy — see the docstring."""
    return mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"cubesat-cli-{uuid.uuid4().hex[:6]}",
        protocol=mqtt.MQTTv5,
    )
