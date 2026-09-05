"""The base every service inherits.

Eight processes need the same things: connect to the broker, subscribe, publish
a heartbeat, know the current mission state, poll at a cadence derived from that
state, and die cleanly on SIGTERM. Written eight times, that becomes eight
different reconnect bugs and eight different shutdown paths.

A subclass declares what it is and implements what it does:

    class EpsService(Service):
        name = "eps"
        cadence_key = "eps"

        def tick(self) -> None:
            self.publish("eps_status", battery=..., voltage=...)

Subclass hooks, all optional except ``tick``:

    on_start()              after the broker connection is under way
    tick()                  called every ``interval`` seconds
    on_message(topic, data) a subscribed topic delivered a JSON payload
    on_state_change(old, new)  the mission state changed
    on_stop()               shutting down; flush anything that must survive
"""

from __future__ import annotations

import contextlib
import json
import logging
import signal
import threading
import time
from typing import Any, ClassVar

import paho.mqtt.client as mqtt

from cubesat.common import cadence, config
from cubesat.common import mqtt as mqtt_factory
from cubesat.common.states import MissionState, Profile
from cubesat.common.topics import RETAINED, TOPICS, envelope

#: How long a tick may be skipped for when the cadence table says "do not act"
#: (interval 0, e.g. the radio in SAFE). Short enough to notice a state change.
IDLE_POLL_SEC = 1.0

#: How often to repeat the "cannot reach the broker" warning. Often enough to
#: be noticed, rarely enough not to fill the card on a long outage.
OFFLINE_WARNING_INTERVAL_SEC = 60.0

#: How long ``run()`` waits for the broker before starting to tick.
#:
#: The first tick usually carries the first status message, and OBC's DEPLOY
#: self-test is waiting for exactly that. Publishing it before CONNACK arrives
#: means a qos-0 message is simply dropped, and the next one is a whole cadence
#: away — long enough for a healthy subsystem to fail its own bring-up. Waiting
#: happens only at startup: once running, a tick must never be skipped for want
#: of a broker, because DHS has a flight recorder to keep writing.
STARTUP_CONNECT_TIMEOUT_SEC = 5.0


class Service:
    #: Client id suffix, log file name, heartbeat identity.
    name: ClassVar[str] = "service"
    #: Topic *keys* (not strings) this service subscribes to, beyond obc/status.
    subscriptions: ClassVar[tuple[str, ...]] = ()
    #: Key into the cadence table. None means the service has no periodic work.
    cadence_key: ClassVar[str | None] = None
    #: Most services need the mission state; OBC publishes it and HOSTD does not
    #: care, so both switch this off.
    track_mission_state: ClassVar[bool] = True

    def __init__(self) -> None:
        self.log = logging.getLogger(self.name)
        self.client = mqtt_factory.make_client(self.name)
        self.mission_state: MissionState | None = None
        self.profile: Profile | None = None
        #: From the active profile's ``power.cadence_scale``.
        self.cadence_scale: float = 1.0
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._last_offline_warning = 0.0

    # ── properties ──────────────────────────────────────────────────────────

    @property
    def interval(self) -> float:
        """Seconds until the next tick, from the cadence table and the state."""
        if self.cadence_key is None:
            return IDLE_POLL_SEC
        return cadence.interval_for(self.cadence_key, self.mission_state, self.cadence_scale)

    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    # ── publishing ──────────────────────────────────────────────────────────

    def publish(self, topic_key: str, *, qos: int = 0, **fields: Any) -> None:
        """Publish a timestamped JSON payload to ``TOPICS[topic_key]``.

        The retain flag comes from the topic, not the caller: whether a topic is
        retained is a property of the topic, and letting each publisher decide
        is how one of them eventually forgets.
        """
        topic = TOPICS[topic_key]
        self.client.publish(topic, envelope(**fields), qos=qos, retain=topic in RETAINED)

    def publish_raw(self, topic_key: str, payload: str, *, qos: int = 0) -> None:
        """Publish an already-serialised payload — used when relaying verbatim."""
        topic = TOPICS[topic_key]
        self.client.publish(topic, payload, qos=qos, retain=topic in RETAINED)

    # ── hooks for subclasses ────────────────────────────────────────────────

    def on_start(self) -> None:
        """Called once, after the connection attempt has been started."""

    def on_connected(self) -> None:
        """Called after every successful connect, including reconnects.

        This is where a retained status gets republished. A broker restart takes
        every retained message with it, and a service that only publishes its
        status when something changes will then stay silent indefinitely — so
        OBC would see no `payload_status`, DEPLOY would have no evidence, and a
        healthy satellite would fail its own bring-up because the *broker*
        bounced. Publishing on connect costs one message and closes that.
        """

    def tick(self) -> None:
        """The periodic work. Called every ``interval`` seconds."""

    def on_message(self, topic: str, data: dict[str, Any]) -> None:
        """A subscribed topic delivered a decoded JSON object."""

    def on_state_change(self, previous: MissionState | None, current: MissionState) -> None:
        """The mission state changed. Cadence is already updated."""

    def report_in(self) -> None:
        """DEPLOY has begun and OBC is waiting for fresh evidence — publish it.

        The bring-up self-test counts only status messages that arrive inside
        its window, and rightly: a retained status proves the hardware answered
        *once*, not that it still does. A service that was just started reports
        in from ``on_start``; the hole is the service that **survived** the
        profile switch — COMMS runs across every profile change by design — and
        publishes its status only on change, so a healthy radio would sit
        silent through the whole window and fail the self-test (bench-found on
        the first hardware run, 2026-08-28).

        The default is a no-op: a service whose status already streams on its
        DEPLOY cadence row (ADCS at 2 Hz, DHS every 2 s) needs nothing extra.
        Override where status is published only on change.
        """

    def on_stop(self) -> None:
        """Called once during shutdown, before disconnecting."""

    # ── lifecycle ───────────────────────────────────────────────────────────

    def run(self) -> None:
        self._install_signal_handlers()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        mqtt_factory.connect(self.client)
        heartbeat = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        heartbeat.start()

        self.log.info("%s started (broker %s:%s)", self.name, config.MQTT_BROKER, config.MQTT_PORT)
        self._await_broker()
        try:
            self.on_start()
            self._main_loop()
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Ask the service to shut down. Safe to call from any thread."""
        self._stop.set()

    def _await_broker(self) -> None:
        """Give the broker a moment before the first tick — but not at the cost
        of a slow shutdown: a SIGTERM arriving during startup must be honoured
        now, not in five seconds, or systemd waits for us on every stop."""
        deadline = time.monotonic() + STARTUP_CONNECT_TIMEOUT_SEC
        while not self._connected.is_set() and not self._stop.is_set():
            if time.monotonic() >= deadline:
                # Not fatal: carry on and let the reconnect loop catch up. Said
                # out loud because the first status message is about to go
                # nowhere, and OBC may be waiting for exactly that message.
                self.log.warning(
                    "broker not reachable within %.0fs; starting anyway",
                    STARTUP_CONNECT_TIMEOUT_SEC,
                )
                return
            self._stop.wait(0.02)

    def _main_loop(self) -> None:
        while self.running:
            interval = self.interval
            if interval <= 0:
                # The cadence table says do nothing in this state. Keep waking
                # up anyway, because the state that silenced us can change.
                self._stop.wait(IDLE_POLL_SEC)
                continue
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                # A bad sensor read must not take a subsystem off the bus. Log
                # it and keep the cadence; OBC decides what a failing subsystem
                # means, and it needs the heartbeats to keep coming to do so.
                self.log.exception("tick failed")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, interval - elapsed))

    def _shutdown(self) -> None:
        try:
            self.on_stop()
        except Exception:
            self.log.exception("on_stop failed")
        # Say goodbye explicitly: a clean exit should not look like a crash to
        # OBC, and the last will only fires on an ungraceful disconnect.
        self.client.publish(
            TOPICS["heartbeat"],
            json.dumps({"service": self.name, "alive": False, "timestamp": time.time()}),
            qos=1,
        )
        self.client.loop_stop()
        self.client.disconnect()
        self.log.info("%s stopped", self.name)

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            # ValueError means this is not the main thread (a test, or an
            # embedded run); the caller is then responsible for calling stop().
            with contextlib.suppress(ValueError):
                signal.signal(sig, lambda *_: self.stop())

    # ── MQTT callbacks ──────────────────────────────────────────────────────

    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason: Any,
                    _props: Any = None) -> None:
        if reason != 0:
            self.log.error("broker refused connection: %s", reason)
            return
        self._connected.set()
        for key in self._all_subscriptions():
            client.subscribe(TOPICS[key], qos=1)
        subscribed = ", ".join(self._all_subscriptions()) or "nothing"
        self.log.info("connected; subscribed to %s", subscribed)
        try:
            self.on_connected()
        except Exception:
            self.log.exception("on_connected failed")

    def _on_disconnect(self, _client: mqtt.Client, _userdata: Any, _flags: Any = None,
                       reason: Any = None, _props: Any = None) -> None:
        self._connected.clear()
        # paho reconnects on its own with backoff; this is informational.
        self.log.warning("disconnected from broker (%s); reconnecting", reason)

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        # An empty payload is not malformed JSON: with the retain flag it is how
        # MQTT says "forget this topic", and PAYLOAD clears the last photograph
        # exactly that way on every start (``_clear_retained_photo``). Warning
        # about it put an "undecodable payload on cubesat/payload/photo" in the
        # COMMS log at every profile change — a routine erasure reading as a
        # fault, and noise that a real decode failure would have hidden behind.
        if not message.payload:
            return
        try:
            data = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.log.warning("undecodable payload on %s", message.topic)
            return
        if not isinstance(data, dict):
            self.log.warning("non-object payload on %s", message.topic)
            return

        if self.track_mission_state and message.topic == TOPICS["obc_status"]:
            self._absorb_obc_status(data)

        try:
            self.on_message(message.topic, data)
        except Exception:
            self.log.exception("handler failed for %s", message.topic)

    def _absorb_obc_status(self, data: dict[str, Any]) -> None:
        """Track mission state and profile so cadence follows them for free."""
        raw_profile = data.get("profile")
        if raw_profile is not None:
            try:
                self.profile = Profile(raw_profile)
            except ValueError:
                self.log.warning("unknown profile in obc/status: %r", raw_profile)
        scale = data.get("cadence_scale")
        if isinstance(scale, (int, float)) and scale > 0:
            self.cadence_scale = float(scale)

        raw_state = data.get("status")
        if raw_state is None:
            return
        try:
            state = MissionState(raw_state)
        except ValueError:
            self.log.warning("unknown mission state in obc/status: %r", raw_state)
            return
        if state is self.mission_state:
            return
        previous, self.mission_state = self.mission_state, state
        self.log.info("mission state %s -> %s", previous.value if previous else "?", state.value)
        try:
            self.on_state_change(previous, state)
        except Exception:
            self.log.exception("on_state_change failed")
        if state is MissionState.DEPLOY:
            # OBC is waiting for fresh evidence, and a service that survived the
            # profile switch will produce none on its own — see report_in.
            try:
                self.report_in()
            except Exception:
                self.log.exception("report_in failed")

    def _all_subscriptions(self) -> tuple[str, ...]:
        keys = list(self.subscriptions)
        if self.track_mission_state and "obc_status" not in keys:
            keys.append("obc_status")
        return tuple(keys)

    # ── heartbeat ───────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """Publish liveness on a fixed interval, independent of the cadence.

        A service polling every 300 seconds in LOW_POWER must still prove it is
        alive more often than that, or OBC would declare it lost for doing
        exactly what it was told.

        While the broker is unreachable this is also the only place that says
        so. paho retries a background connect silently, so without this a
        service that can reach nothing looks exactly like a healthy one in the
        log — the most misleading state a distributed system can present.
        """
        offline_since: float | None = None
        while self.running:
            if self._connected.is_set():
                if offline_since is not None:
                    self.log.info("broker reachable again")
                    offline_since = None
                self.client.publish(
                    TOPICS["heartbeat"],
                    json.dumps(
                        {"service": self.name, "alive": True, "timestamp": time.time()}
                    ),
                    qos=0,
                )
            else:
                now = time.monotonic()
                if offline_since is None:
                    offline_since = now
                    self._last_offline_warning = 0.0
                if now - self._last_offline_warning >= OFFLINE_WARNING_INTERVAL_SEC:
                    self._last_offline_warning = now
                    self.log.warning(
                        "no broker connection for %.0fs (%s:%s); still retrying",
                        now - offline_since,
                        config.MQTT_BROKER,
                        config.MQTT_PORT,
                    )
            self._stop.wait(config.HEARTBEAT_INTERVAL_SEC)
