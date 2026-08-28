"""The uplink contract, and a radio that cannot take the service down with it.

``uplink_command`` is the gate every inbound command passes through, from the
radio and from the cloud queue alike, so it is tested as the shape check it is:
it must parse to an object and carry a ``command`` field, and then the *original
text* comes back out. Returning the text rather than the parsed object is what
makes "verbatim" true — a field this build has never heard of still reaches the
service that has.

``MeshChannel`` is tested for one property above all: every way a radio can fail
is a return value. COMMS is the only way back into a satellite in ``FLIGHT``,
and a link service that exits on a bad packet has removed the recovery path it
exists to provide.
"""

from __future__ import annotations

import json
import logging

import pytest

from cubesat.comms import mesh
from cubesat.comms.mesh import LOG_EXCERPT_CHARS, MeshChannel
from cubesat.hal.interfaces import RadioMessage
from cubesat.hal.mock.radio import MockRadio

LOG = logging.getLogger("test-mesh")

RECOVER = '{"command": "recover"}'
SET_PROFILE = '{"command":"set_profile","params":{"profile":"HOSTED"}}'


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


# ── the uplink contract ─────────────────────────────────────────────────────


def test_a_command_comes_back_exactly_as_it_arrived():
    # Byte for byte, so there is no re-encoding step that can quietly disagree
    # with whoever composed the message on the other side.
    assert mesh.uplink_command(SET_PROFILE, LOG, source="lora") == SET_PROFILE


def test_a_field_this_build_has_never_heard_of_survives_the_relay():
    text = '{"command":"set_profile","params":{"profile":"EXPO"},"invented_tomorrow":true}'
    assert mesh.uplink_command(text, LOG, source="lora") == text


def test_the_ground_can_type_a_command_into_a_phone_and_have_it_fit():
    # 55 bytes, comfortably inside the 240-byte message. This is the recovery
    # path for FLIGHT, where Wi-Fi is down and there is no SSH.
    assert len(SET_PROFILE.encode("utf-8")) < 240
    assert mesh.uplink_command(SET_PROFILE, LOG, source="lora") is not None


@pytest.mark.parametrize(
    "text",
    [
        "hello from the mesh",
        "",
        "[1, 2, 3]",
        '"just a string"',
        "null",
        '{"params": {"profile": "EXPO"}}',
        '{"command": 42}',
    ],
)
def test_anything_that_is_not_a_command_is_dropped(text, caplog):
    with caplog.at_level(logging.WARNING):
        assert mesh.uplink_command(text, LOG, source="lora") is None
    assert "lora: dropping" in caplog.text


def test_a_rejected_message_is_quoted_in_the_log_but_not_at_full_length(caplog):
    # Truncating here is fine and truncating a payload is not: this is a line
    # for a human about something already being discarded.
    with caplog.at_level(logging.WARNING):
        mesh.uplink_command("x" * (LOG_EXCERPT_CHARS * 3), LOG, source="api")
    assert "…" in caplog.text
    assert len(caplog.text) < LOG_EXCERPT_CHARS * 3


def test_the_channel_a_message_came_in_on_is_named_in_the_log(caplog):
    with caplog.at_level(logging.WARNING):
        mesh.uplink_command("nonsense", LOG, source="api")
    assert "api: dropping" in caplog.text


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
    # A driver may raise rather than return False. COMMS still comes up — the
    # cloud channel may well be the working one.
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


def test_an_api_queue_item_is_held_to_the_same_standard_as_a_radio_message():
    # One validator for both channels. Two would eventually disagree about what
    # a command is, and then a command would work over one link and not the
    # other — the one property this design will not give up.
    queued = {"command": "safe_mode", "request_id": "req_010"}
    assert mesh.uplink_command(json.dumps(queued), LOG, source="api") is not None
    assert mesh.uplink_command(json.dumps({"note": "hello"}), LOG, source="api") is None
