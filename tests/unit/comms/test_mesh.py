"""A radio that cannot take the service down with it.

``MeshChannel`` is tested for one property above all: every way a radio can fail
is a return value. COMMS is the only way back into a satellite in ``FLIGHT``,
and a link service that exits on a bad packet has removed the recovery path it
exists to provide.

The uplink contract that used to be tested here went with ``uplink_command`` on
2026-09-03: the radio takes compact verbs and nothing else now, so what an
inbound line means is ``compact.py``'s to decide and ``test_service.py``'s to
check. Nothing in this module looks at what a message says.
"""

from __future__ import annotations

import logging

from cubesat.comms.mesh import MeshChannel
from cubesat.hal.interfaces import RadioMessage
from cubesat.hal.mock.radio import MockRadio

LOG = logging.getLogger("test-mesh")

RECOVER = "recover"


class BrokenRadio:
    """Every method raises. Nothing here may reach the caller."""

    def __init__(self, failure: Exception | None = None) -> None:
        self._failure = failure or OSError("device disconnected")

    def probe(self):
        raise self._failure

    def send(self, payload):
        raise self._failure

    def poll(self):
        raise self._failure

    def close(self):
        raise self._failure


class SilentRadio(MockRadio):
    def probe(self):
        return False


# ── the channel ─────────────────────────────────────────────────────────────


def test_a_radio_that_answers_is_open_for_business(caplog):
    channel = MeshChannel(MockRadio(), LOG)
    with caplog.at_level(logging.INFO):
        assert channel.open() is True
    assert channel.present is True
    assert "answered" in caplog.text


def test_a_radio_that_says_no_is_reported_absent(caplog):
    channel = MeshChannel(SilentRadio(), LOG)
    with caplog.at_level(logging.ERROR):
        assert channel.open() is False
    assert "not answering" in caplog.text


def test_a_probe_that_raises_is_the_same_answer_as_one_that_says_no(caplog):
    # A driver may raise rather than return False. Either way the answer to
    # "is it there?" is no, and COMMS still comes up to say so.
    channel = MeshChannel(BrokenRadio(), LOG)
    with caplog.at_level(logging.ERROR):
        assert channel.open() is False
    assert "probe failed" in caplog.text


def test_a_message_reaches_the_radio():
    radio = MockRadio()
    channel = MeshChannel(radio, LOG)
    assert channel.send("CSAT t=1") is True
    assert radio.sent == ["CSAT t=1"]


def test_a_transmit_that_fails_marks_the_radio_absent(caplog):
    # So the retained comms_status stops claiming a working radio the moment one
    # stops working, rather than at the next restart — which on a satellite in a
    # backpack is never.
    channel = MeshChannel(MockRadio(), LOG)
    channel.open()
    channel._radio = BrokenRadio()
    with caplog.at_level(logging.ERROR):
        assert channel.send("CSAT t=1") is False

    assert channel.present is False
    assert "transmit failed" in caplog.text


def test_an_oversized_payload_is_a_failed_send_and_not_a_crash(caplog):
    # Defence in depth: beacon.py guarantees the line fits, and if it ever did
    # not, the radio refuses rather than truncating and this contains it.
    channel = MeshChannel(MockRadio(), LOG)
    with caplog.at_level(logging.ERROR):
        assert channel.send("x" * 241) is False
    assert "transmit failed" in caplog.text


def test_receiving_hands_over_what_arrived():
    radio = MockRadio()
    radio.inject(RECOVER)
    received = MeshChannel(radio, LOG).receive()
    assert [message.text for message in received] == [RECOVER]
    assert isinstance(received[0], RadioMessage)


def test_a_radio_inbox_that_cannot_be_read_costs_a_cycle_and_not_the_service(caplog):
    channel = MeshChannel(BrokenRadio(), LOG)
    with caplog.at_level(logging.ERROR):
        assert channel.receive() == []
    assert "radio inbox" in caplog.text


def test_closing_gives_the_radio_back():
    closed = []
    radio = MockRadio()
    radio.close = lambda: closed.append(True)
    MeshChannel(radio, LOG).close()
    assert closed == [True]


def test_a_radio_that_will_not_close_is_logged_and_let_go(caplog):
    with caplog.at_level(logging.ERROR):
        MeshChannel(BrokenRadio(), LOG).close()
    assert "closing the radio failed" in caplog.text


# ── what the status topic is told ───────────────────────────────────────────


def test_the_status_reports_whether_the_node_answered():
    channel = MeshChannel(MockRadio(), LOG)
    assert channel.describe()["present"] is False
    channel.open()
    assert channel.describe()["present"] is True


def test_the_node_name_and_region_are_reported_when_the_driver_knows_them():
    radio = MockRadio()
    radio.node_id = "!698204b0"
    radio.region = "US"
    assert MeshChannel(radio, LOG).describe() == {
        "present": False,
        "node": "!698204b0",
        "region": "US",
    }


def test_a_driver_without_them_reports_nulls_rather_than_failing():
    # They are two strings for an operator to look at, not a capability
    # anything depends on, so they are not in the Radio protocol. The mock has
    # neither.
    described = MeshChannel(MockRadio(), LOG).describe()
    assert described["node"] is None and described["region"] is None
