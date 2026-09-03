"""The Meshtastic channel: a radio that cannot take the service down with it.

``MeshChannel`` wraps the ``Radio`` the HAL hands over and makes it unable to
hurt the service: a radio that is unplugged, a board in a boot loop or a library
raising something nobody has seen before costs a beacon, never the process.
Everything below catches broadly and on purpose — COMMS is the only way back
into a satellite in ``FLIGHT``, and a link service that exits on a bad packet
has removed the recovery path it exists to provide.

There was a second half here until 2026-09-03: ``uplink_command``, a JSON shape
check that let a hand-composed command be relayed off the air byte for byte. It
went with the JSON uplink itself. **The radio vocabulary is now the compact
table in ``compact.py`` and nothing else** — one parser for the air instead of
two, so there is no second opinion about what a command is, and no shape check
here for the compact table to drift away from.

Which traffic is even considered is decided earlier still, in ``service.py`` →
``_refuse_uplink``: only the private ``CubeSat`` channel is acted on. That rule
is COMMS' policy and deliberately not this module's — nothing here looks at the
mesh channel at all.
"""

from __future__ import annotations

import logging
from typing import Any

from cubesat.hal.interfaces import Radio, RadioMessage


class MeshChannel:
    """The radio, with every way it can fail turned into a return value."""

    def __init__(self, radio: Radio, log: logging.Logger) -> None:
        self._radio = radio
        self._log = log
        #: Whether the node last answered. Reported in ``comms_status``, which is
        #: OBC's evidence during DEPLOY that the radio is really there.
        self.present = False

    def open(self) -> bool:
        """Probe the node and remember the answer."""
        try:
            self.present = bool(self._radio.probe())
        except Exception:
            # A driver may raise rather than return False; either way the answer
            # to "is it there?" is no, and COMMS still comes up. The API channel
            # may well be the working one.
            self._log.exception("the radio probe failed")
            self.present = False
        if self.present:
            self._log.info("the Meshtastic node answered")
        else:
            self._log.error("the Meshtastic node is not answering; LoRa is unavailable")
        return self.present

    def send(self, text: str) -> bool:
        """Transmit one message. False means it did not go out.

        A failure marks the node absent, so the retained ``comms_status`` stops
        claiming a working radio the moment one stops working — rather than at
        the next restart, which on a satellite in a backpack is never.
        """
        try:
            self._radio.send(text)
        except Exception:
            self._log.exception("LoRa transmit failed")
            self.present = False
            return False
        self.present = True
        return True

    def receive(self) -> list[RadioMessage]:
        """Everything the node has taken in since the last call."""
        try:
            return list(self._radio.poll())
        except Exception:
            self._log.exception("reading the radio inbox failed")
            return []

    def close(self) -> None:
        try:
            self._radio.close()
        except Exception:
            self._log.exception("closing the radio failed")

    def describe(self) -> dict[str, Any]:
        """The ``radio`` object in ``comms_status``.

        ``node`` and ``region`` are read off the driver with ``getattr`` because
        they are not part of the ``Radio`` protocol — they are two strings for an
        operator to look at, not a capability anything depends on, and requiring
        every implementation to carry them would put display fields into a
        hardware contract. The mock has neither, and reports null for both.
        """
        return {
            "present": self.present,
            "node": getattr(self._radio, "node_id", None),
            # UNSET is the most common reason a board that flashed cleanly
            # transmits nothing, so it is worth having on the status topic.
            "region": getattr(self._radio, "region", None),
        }
