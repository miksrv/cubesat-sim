"""The Meshtastic channel, and the uplink contract every channel shares.

Two things live here. ``MeshChannel`` wraps the ``Radio`` the HAL hands over and
makes it unable to hurt the service: a radio that is unplugged, a board in a
boot loop or a library raising something nobody has seen before costs a beacon,
never the process. Everything below catches broadly and on purpose — COMMS is
the only way back into a satellite in ``FLIGHT``, and a link service that exits
on a bad packet has removed the recovery path it exists to provide.

``uplink_command`` is the other half, and it is deliberately not a mesh-only
function. **Every command works identically over MQTT and over LoRa**, because
COMMS re-publishes an uplink verbatim onto ``cubesat/command`` and nothing
downstream knows or cares which channel it arrived on. One validator, applied to
both channels, is what keeps that true — two would eventually disagree about what
a command is, and then a command would work over one link and not the other.

**Commands arrive as JSON text.** Not a compact binary encoding, for the same
reason the beacon going out is readable: a person can type

    {"command":"set_profile","params":{"profile":"HOSTED"}}

into the Meshtastic phone app — 55 bytes, comfortably inside the 240-byte
message — and recover a satellite whose Wi-Fi is off and which has no SSH. A
translation layer would also break the "verbatim" property: the text that
arrives is the text that is re-published, so there is no encoding step that can
quietly disagree with the decoder on the other side.

**Validation stops at the shape.** It must parse to an object and carry a
``command`` field. That is all — this module does not know what commands exist,
does not check parameters and never acts on one. Whether ``set_profile`` is
legal is OBC's decision, ``take_photo`` is PAYLOAD's, and ``set_comms_config``
comes back around the loop to COMMS itself as an ordinary MQTT message. Deciding
here would mean the LoRa path and the MQTT path validated differently, which is
the one property this design will not give up.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from cubesat.hal.interfaces import Radio, RadioMessage

#: How much of a rejected message is quoted in the log. Truncating *here* is
#: fine and truncating a payload is not: this is a log line for a human, not
#: something anyone will act on, and an uplink that is being dropped anyway is
#: the one place a shortened copy cannot mislead.
LOG_EXCERPT_CHARS = 120


def uplink_command(text: str, log: logging.Logger, *, source: str) -> str | None:
    """Return ``text`` unchanged if it is a command, else None.

    Returning the original string rather than the parsed object is the point:
    what gets re-published is the bytes that arrived, so a field this build does
    not know about still reaches the service that does.
    """
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("%s: dropping a message that is not JSON: %s", source, _excerpt(text))
        return None
    if not isinstance(data, dict) or not isinstance(data.get("command"), str):
        # Ordinary mesh chat looks exactly like this, and so does a fat-fingered
        # command typed into a phone. Worth a warning either way: the second
        # case is somebody standing in a field wondering why nothing happened.
        log.warning("%s: dropping a message with no command field: %s", source, _excerpt(text))
        return None
    return text


def _excerpt(text: str) -> str:
    return repr(text if len(text) <= LOG_EXCERPT_CHARS else text[:LOG_EXCERPT_CHARS] + "…")


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
