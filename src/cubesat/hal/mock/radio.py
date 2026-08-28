"""Mock radio: a loopback mesh node.

Sent messages are recorded so tests can assert on them, and ``inject()`` puts a
message into the receive queue — which is how an uplinked ``set_profile`` is
exercised without a second Meshtastic node on the desk.
"""

from __future__ import annotations

from cubesat.hal.interfaces import MAX_RADIO_MESSAGE_BYTES, RadioMessage

#: Kept as an alias for the tests that name it. The number itself lives beside
#: the Radio protocol: a limit duplicated across a driver, a mock and a payload
#: builder is a limit that will eventually disagree with itself, and the mock
#: disagreeing is the worst version — it would pass on a laptop what the radio
#: refuses in the field.
MAX_MESSAGE_BYTES = MAX_RADIO_MESSAGE_BYTES


class MockRadio:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._inbox: list[RadioMessage] = []

    def probe(self) -> bool:
        return True

    def send(self, payload: str) -> None:
        size = len(payload.encode("utf-8"))
        if size > MAX_MESSAGE_BYTES:
            raise ValueError(
                f"payload is {size} bytes; Meshtastic carries at most {MAX_MESSAGE_BYTES}"
            )
        self.sent.append(payload)

    def poll(self) -> list[RadioMessage]:
        received, self._inbox = self._inbox, []
        return received

    def inject(self, text: str, sender: str = "!test0001", snr: float = 6.0) -> None:
        """Test seam: pretend a message arrived over the air."""
        self._inbox.append(RadioMessage(text=text, sender=sender, snr=snr))

    def close(self) -> None:
        return None
