"""Heltec WiFi LoRa 32 V4 running stock Meshtastic, on ``/dev/serial0``.

The board is a self-contained radio rather than a transceiver we drive: it runs
Meshtastic firmware, and **Meshtastic does the framing, the CRC, the retries,
the acknowledgements and the encryption**. The hand-rolled ``length + payload +
CRC-16-CCITT`` framing this driver replaces existed because the previous radio
was an SC16IS752 I2C↔UART bridge with a raw SX1278 behind it. None of that is
wanted here, and re-implementing any of it on top of a protocol that already has
it is how two framings end up disagreeing about the same bytes.

So this file is thin on purpose. It opens a ``SerialInterface``, sends text,
collects text, and reports what the node calls itself.

**Nothing is ever truncated.** ``send()`` refuses an oversized payload rather
than shortening it, against the one ``MAX_RADIO_MESSAGE_BYTES`` that lives
beside the ``Radio`` protocol, so the failure lands on a laptop instead of going
out over the air as a mangled packet. The
pre-rewrite driver silently cut every payload to 28 bytes and transmitted the
result; that bug is the reason this rewrite exists, and the guard is here rather
than only in the caller so that no future caller can reintroduce it.

**Receiving is a callback, not a read.** Meshtastic delivers inbound packets on
the interface's own thread through ``pubsub``, so ``poll()`` drains a queue that
somebody else filled and never blocks the COMMS loop. The subscription is taken
*before* the interface is constructed: a node that has been listening while we
were not may deliver its backlog during bring-up, and a command that arrives
half a second early is still a command.

``meshtastic`` and ``pubsub`` are imported lazily, exactly as ``hal/i2c.py``
imports ``smbus2`` — they live in the ``rpi`` extra, and merely importing this
module, which the registry does on any machine, must not require them.

Bench-earned behaviour
----------------------

Three things below come from bringing this link up on the real hardware on
2026-08-23, and all three are recorded in ``docs/hardware-heltec-lora32-v4.md``:

* **The first open over UART can come back with nothing.** The CLI's ``--info``
  returned exit code 0 and not one line, then produced the full configuration on
  an immediate retry. So opening retries instead of failing; a radio that needed
  asking twice is not a radio that is absent.
* **The start of the stream is rubbish.** Before the Serial module initialises,
  the Heltec's TX pin floats, and the ROM bootloader writes its boot log onto the
  same ``GPIO43/44``. The library resynchronises on the frame header, which is
  what the retry above is really covering.
* **115200 is not a preference.** The Meshtastic Python library opens the port
  hard-coded at that rate, so ``config.LORA_BAUDRATE`` cannot change it. A
  different value in the configuration is a misunderstanding worth saying out
  loud rather than silently ignoring.

Verified versus inferred
------------------------

Verified on the bench: the port, the baud rate, that ``SerialInterface`` takes
the device path, that inbound text packets are selected by
``decoded["portnum"] == "TEXT_MESSAGE_APP"`` with the text in ``decoded["text"]``
and the sender in ``fromId``, that ``rxRssi`` may be absent so ``rxSnr`` is what
to lean on, and that ``pub.subscribe(..., "meshtastic.receive")`` delivers.

**Not** verified, and marked where they appear:

* **``sendText(..., channelIndex=…)``.** Sending on the private secondary
  channel was proved with the CLI's ``--ch-index 1``; that the Python API spells
  the same thing this way is read from the library, not from a bench run. If it
  is wrong the messages land on the public primary channel — visible to every
  node in range rather than lost, which is why it is worth naming here.
* **``hopStart``/``hopLimit``.** The hop count in ``RadioMessage`` is their
  difference, read from the library's documentation — see ``_hops``. The bench
  check is a packet relayed through a third node reading 1; every packet so far
  has been heard directly.
* **Reading the node id and the region back.** Both are cosmetic: they populate
  ``comms_status`` so an operator can see which node answered. Every accessor is
  best-effort and yields None rather than raising, because a status field is not
  worth a failed bring-up.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any

from cubesat.common import config
from cubesat.hal.interfaces import MAX_RADIO_MESSAGE_BYTES, RadioMessage

logger = logging.getLogger(__name__)

#: The pubsub topic Meshtastic publishes inbound packets on, and the portnum
#: that marks one as human-readable text. Both bench-verified.
RECEIVE_TOPIC = "meshtastic.receive"
TEXT_PORTNUM = "TEXT_MESSAGE_APP"

#: What the Meshtastic Python library hard-codes. See the module docstring.
LIBRARY_BAUDRATE = 115200

#: How many times to ask the node before calling it absent, and how long to wait
#: between attempts. Three is one more than the bench needed — the first attempt
#: came back empty and the second worked — and the delay is long enough for the
#: firmware to finish a reboot it may have started for its own reasons.
OPEN_ATTEMPTS = 3
OPEN_RETRY_DELAY_SEC = 2.0


class RadioError(OSError):
    """The Meshtastic node could not be opened."""


class MeshtasticRadio:
    """One Meshtastic node, opened once and closed once."""

    def __init__(
        self,
        port: str | None = None,
        channel_index: int | None = None,
        *,
        attempts: int = OPEN_ATTEMPTS,
        retry_delay: float = OPEN_RETRY_DELAY_SEC,
    ) -> None:
        # Resolved here rather than in the signature so the configured value is
        # read when the driver is built, not when this module is imported.
        self._port = port if port is not None else config.LORA_PORT
        #: Channel 1 by default: the private ``CubeSat`` channel with its own
        #: PSK, not the public ``LongFast`` primary. Bench traffic would
        #: otherwise clutter the shared chat every node in range reads, and the
        #: uplink path must not be world-writable. Configured rather than
        #: constant because a driver and a ground station that disagree here
        #: transmit and receive perfectly and simply never meet.
        self._channel_index = (
            channel_index if channel_index is not None else config.LORA_CHANNEL_INDEX
        )
        self._attempts = attempts
        self._retry_delay = retry_delay
        #: The SerialInterface handle. Untyped: the library ships no stubs and
        #: only installs on a Raspberry Pi. What is actually checked is the
        #: ``Radio`` protocol this class satisfies.
        self._interface: Any = None
        self._subscribed = False
        #: Filled on the interface's thread, drained on the COMMS loop's.
        self._inbox: list[RadioMessage] = []
        self._lock = threading.Lock()

        #: Reported in ``comms_status`` so an operator can see which node
        #: answered. None until the interface is open, and None forever if the
        #: library does not hand them over — see "Verified versus inferred".
        self.node_id: str | None = None
        self.region: str | None = None

    # ── lifetime ────────────────────────────────────────────────────────────

    def _open(self) -> Any:
        """The live interface, opening it on first use.

        Deferred rather than done in ``__init__`` so that constructing the
        driver — which the registry does before anything has decided to
        transmit — costs nothing and cannot fail.
        """
        if self._interface is not None:
            return self._interface
        try:
            from meshtastic.serial_interface import SerialInterface
        except ImportError as exc:
            raise RadioError(
                "meshtastic is not installed, so there is no radio here. "
                "Set CUBESAT_MOCK_HARDWARE=1 to run without hardware."
            ) from exc

        if config.LORA_BAUDRATE != LIBRARY_BAUDRATE:
            # Said rather than obeyed: the library opens the port at 115200
            # whatever this says, and a configuration that quietly does nothing
            # is worse than one that argues back.
            logger.warning(
                "LORA_BAUDRATE is %d, but the meshtastic library opens the port at %d "
                "and cannot be told otherwise",
                config.LORA_BAUDRATE,
                LIBRARY_BAUDRATE,
            )

        # Subscribed before the interface exists: a node that has been listening
        # while we were not may deliver its backlog during bring-up.
        self._subscribe()
        interface = self._connect(SerialInterface)
        self._interface = interface
        self.node_id = _node_id(interface)
        self.region = _region(interface)
        logger.info(
            "Meshtastic node %s (%s) open on %s, transmitting on channel %d",
            self.node_id or "unnamed",
            self.region or "region unknown",
            self._port,
            self._channel_index,
        )
        return interface

    def _connect(self, factory: Any) -> Any:
        """Open the port, retrying an empty or refused first attempt.

        Broad by intent. The library raises its own timeout type when the node
        never answers, ``pyserial`` raises another when the device node is not
        there, and the bench produced a third symptom — a connection that
        completed with nothing behind it. All three mean "ask again".
        """
        last: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                return factory(self._port)
            except Exception as exc:
                last = exc
                logger.warning(
                    "Meshtastic did not answer on %s (attempt %d of %d): %s",
                    self._port,
                    attempt,
                    self._attempts,
                    exc,
                )
                if attempt < self._attempts:
                    time.sleep(self._retry_delay)
        raise RadioError(
            f"Meshtastic did not answer on {self._port} after {self._attempts} attempts"
        ) from last

    def _subscribe(self) -> None:
        if self._subscribed:
            return
        _pub().subscribe(self._on_receive, RECEIVE_TOPIC)
        self._subscribed = True

    def close(self) -> None:
        """Give the port back. Idempotent, because shutdown paths overlap.

        Unsubscribing first: pubsub holds the callback for the lifetime of the
        process, and a closed interface delivering into a dead service's queue
        is a leak with a confusing log line attached.
        """
        if self._subscribed:
            self._subscribed = False
            try:
                _pub().unsubscribe(self._on_receive, RECEIVE_TOPIC)
            except Exception:
                logger.exception("could not unsubscribe from %s", RECEIVE_TOPIC)
        if self._interface is None:
            return
        interface, self._interface = self._interface, None
        try:
            interface.close()
        except Exception:
            logger.exception("closing the Meshtastic interface failed")
        else:
            logger.info("Meshtastic interface on %s closed", self._port)

    # ── the Radio protocol ──────────────────────────────────────────────────

    def probe(self) -> bool:
        """Whether the node answers at all. The DEPLOY self-test asks this.

        Broad by intent: a missing device node, a baud mismatch, a board in a
        boot loop and a port held by another process all raise different types,
        and every one of them is a "no".
        """
        try:
            self._open()
        except Exception as exc:
            logger.error("the Meshtastic node did not answer: %s", exc)
            return False
        return True

    def send(self, payload: str) -> None:
        """Transmit one text message on the configured channel.

        Refuses an oversized payload instead of shortening it. Truncation here
        is not a degraded transmission, it is a valid-looking packet carrying
        something nobody wrote — which is precisely what the pre-rewrite driver
        did, and what ``comms/beacon.py`` exists upstream of this to prevent.
        """
        size = len(payload.encode("utf-8"))
        if size > MAX_RADIO_MESSAGE_BYTES:
            raise ValueError(
                f"payload is {size} bytes; Meshtastic carries at most "
                f"{MAX_RADIO_MESSAGE_BYTES}"
            )
        # channelIndex: inferred from the library, proved on the bench only via
        # the CLI's --ch-index. See "Verified versus inferred".
        self._open().sendText(payload, channelIndex=self._channel_index)

    def poll(self) -> list[RadioMessage]:
        """Everything received since the last call. Never blocks, never opens.

        Deliberately does not open the interface: polling is what the COMMS loop
        does every cycle, and a profile that forbids LoRa must not end up with a
        serial port opened by a health check.
        """
        with self._lock:
            received, self._inbox = self._inbox, []
        return received

    # ── the callback ────────────────────────────────────────────────────────

    def _on_receive(self, packet: Any = None, interface: Any = None) -> None:
        """One inbound packet, on the interface's own thread.

        ``interface`` is unused but part of the signature pubsub calls with.
        Anything that is not human-readable text is dropped in silence: the mesh
        carries position broadcasts, node info and telemetry between every node
        in range, and warning about each would bury the one message that matters.
        """
        try:
            decoded = packet.get("decoded") or {}
            if decoded.get("portnum") != TEXT_PORTNUM:
                return
            text = decoded.get("text")
            if not isinstance(text, str):
                return
            message = RadioMessage(
                text=text,
                sender=packet.get("fromId"),
                # rxRssi is absent on some packets; rxSnr is the one that is
                # always there, and it is the better number for a link margin.
                snr=packet.get("rxSnr"),
                rssi=packet.get("rxRssi"),
                hops=_hops(packet),
            )
        except Exception:
            # A packet shaped differently from anything seen on the bench must
            # not kill the library's receive thread — that would take the whole
            # uplink down, which in FLIGHT is the only way back in.
            logger.exception("unreadable Meshtastic packet")
            return
        with self._lock:
            self._inbox.append(message)


def _hops(packet: Any) -> int | None:
    """Mesh hops this packet took, or None when the fields are not both there.

    Inferred, not bench-verified: the library documents ``hopStart`` as the
    hop limit the sender transmitted with and ``hopLimit`` as what remains on
    arrival, so their difference is the hops taken — 0 means heard directly.
    The bench check that would confirm it is a packet relayed through a third
    node showing 1 here. Until then the value is best-effort and None whenever
    either field is missing or the difference is not a sane hop count.
    """
    start = packet.get("hopStart")
    limit = packet.get("hopLimit")
    if not isinstance(start, int) or not isinstance(limit, int):
        return None
    hops = start - limit
    return hops if 0 <= hops <= 7 else None


def _pub() -> Any:
    """pypubsub's ``pub`` module, which arrives as a ``meshtastic`` dependency.

    Imported through ``importlib`` rather than with an import statement for one
    narrow reason: ``pyproject.toml`` lists ``meshtastic.*`` among the modules
    mypy may not resolve, and ``pubsub`` is not on that list. Adding it there is
    a change to a file outside this driver, and the object is untyped either
    way. The submodule is named in full because ``import pubsub`` alone does not
    bind ``pub``.
    """
    return importlib.import_module("pubsub.pub")


def _node_id(interface: Any) -> str | None:
    """The node's own ``!xxxxxxxx`` identifier, or None if it cannot be read.

    Cosmetic — it goes into ``comms_status`` so an operator can tell which node
    answered. Inferred from the library, so it fails to None rather than
    raising: a display field is not worth a failed bring-up.
    """
    try:
        info = interface.getMyNodeInfo() or {}
        node = (info.get("user") or {}).get("id")
    except Exception:
        logger.debug("could not read the node id", exc_info=True)
        return None
    return node if isinstance(node, str) else None


def _region(interface: Any) -> str | None:
    """The configured LoRa region as a name, e.g. ``US``.

    Worth reporting because ``UNSET`` is the single most common reason a board
    that flashed cleanly transmits nothing at all. The field is a protobuf enum,
    which reads back as an integer, so the name comes from the descriptor —
    inferred, and best-effort like the node id above.
    """
    try:
        lora = interface.localNode.localConfig.lora
        field = lora.DESCRIPTOR.fields_by_name["region"]
        return str(field.enum_type.values_by_number[lora.region].name)
    except Exception:
        logger.debug("could not read the LoRa region", exc_info=True)
        return None
