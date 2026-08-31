"""The Meshtastic driver, against a fake library.

No serial port and no ``meshtastic`` import: the package installs in the ``rpi``
extra only, so both it and ``pubsub`` are stood up in ``sys.modules`` here. What
that fake reproduces is the shape the bench actually produced on 2026-08-23 —
text packets selected by ``portnum``, a sender in ``fromId``, ``rxRssi`` missing
where ``rxSnr`` is present, and a first open that comes back with nothing.

Everything the driver reads out of the library beyond that is best-effort and
tested from both sides: the node id and the region populate a status field, and
a library that does not hand them over must cost a display string rather than
the bring-up.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from cubesat.common import config
from cubesat.hal.interfaces import MAX_RADIO_MESSAGE_BYTES, Device
from cubesat.hal.rpi import meshtastic_radio
from cubesat.hal.rpi.meshtastic_radio import (
    RECEIVE_TOPIC,
    TEXT_PORTNUM,
    MeshtasticRadio,
    RadioError,
)

#: The node this project actually talks to, from docs/hardware-heltec-lora32-v4.md.
NODE_ID = "!698204b0"
#: The protobuf enum number the fake maps to "US". Its value is arbitrary; what
#: matters is that the driver reads a *name* out of a field that holds a number.
US_REGION = 3


class FakePub:
    """pypubsub, reduced to what the driver uses."""

    def __init__(self) -> None:
        self.subscriptions: list[tuple[object, str]] = []
        self.unsubscribe_error: Exception | None = None

    def subscribe(self, listener, topic):
        self.subscriptions.append((listener, topic))

    def unsubscribe(self, listener, topic):
        if self.unsubscribe_error is not None:
            raise self.unsubscribe_error
        self.subscriptions.remove((listener, topic))

    def deliver(self, packet):
        """Hand a packet to every subscriber, as the interface's thread would."""
        for listener, _topic in self.subscriptions:
            listener(packet=packet, interface=None)


class _EnumValue:
    def __init__(self, name: str) -> None:
        self.name = name


class _Descriptor:
    """Just enough protobuf to hold an enum name behind a number."""

    def __init__(self) -> None:
        enum_type = types.SimpleNamespace(values_by_number={US_REGION: _EnumValue("US")})
        self.fields_by_name = {"region": types.SimpleNamespace(enum_type=enum_type)}


class FakeLoraConfig:
    DESCRIPTOR = _Descriptor()

    def __init__(self, region: int) -> None:
        self.region = region


class FakeInterface:
    def __init__(self, path: str, bench: Bench) -> None:
        self.path = path
        self._bench = bench
        self.sent: list[tuple[str, int]] = []
        self.closed = False
        self.close_error: Exception | None = None
        self.localNode = types.SimpleNamespace(
            localConfig=types.SimpleNamespace(lora=FakeLoraConfig(bench.region))
        )

    def sendText(self, text, channelIndex=0):
        self.sent.append((text, channelIndex))

    def getMyNodeInfo(self):
        if self._bench.node_info_error is not None:
            raise self._bench.node_info_error
        return self._bench.node_info

    def close(self):
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class Bench:
    """The fake library, plus the knobs each test needs."""

    def __init__(self) -> None:
        self.pub = FakePub()
        self.opened: list[FakeInterface] = []
        #: Popped one per open attempt, so a test can make the first N fail.
        self.errors: list[Exception] = []
        self.node_info: object = {"user": {"id": NODE_ID}}
        self.node_info_error: Exception | None = None
        self.region = US_REGION

    def open(self, path):
        if self.errors:
            raise self.errors.pop(0)
        interface = FakeInterface(path, self)
        self.opened.append(interface)
        return interface

    @property
    def interface(self) -> FakeInterface:
        return self.opened[-1]


@pytest.fixture
def bench(monkeypatch):
    """Install a fake ``meshtastic`` and ``pubsub`` for the duration of a test."""
    stand = Bench()

    serial_interface = types.ModuleType("meshtastic.serial_interface")
    serial_interface.SerialInterface = stand.open
    meshtastic = types.ModuleType("meshtastic")
    meshtastic.serial_interface = serial_interface

    pub = types.ModuleType("pubsub.pub")
    pub.subscribe = stand.pub.subscribe
    pub.unsubscribe = stand.pub.unsubscribe
    pubsub = types.ModuleType("pubsub")
    pubsub.pub = pub

    for name, module in (
        ("meshtastic", meshtastic),
        ("meshtastic.serial_interface", serial_interface),
        ("pubsub", pubsub),
        ("pubsub.pub", pub),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return stand


@pytest.fixture
def radio(bench):
    # retry_delay=0: the retry itself is the behaviour under test, not the wait.
    return MeshtasticRadio(port="/dev/serial0", retry_delay=0.0)


def text_packet(text=NODE_ID, sender=NODE_ID, **extra):
    packet = {"decoded": {"portnum": TEXT_PORTNUM, "text": text}, "fromId": sender}
    packet.update(extra)
    return packet


# ── the contract ────────────────────────────────────────────────────────────


def test_the_driver_satisfies_the_device_protocol(radio):
    assert isinstance(radio, Device)


def test_the_port_comes_from_configuration_when_none_is_named(bench, monkeypatch):
    # Read when the driver is built, not when the module is imported, so a
    # deployment can point it somewhere else without an import-order rule.
    monkeypatch.setattr(config, "LORA_PORT", "/dev/ttyAMA0")
    MeshtasticRadio(retry_delay=0.0).probe()
    assert bench.interface.path == "/dev/ttyAMA0"


# ── opening ─────────────────────────────────────────────────────────────────


def test_a_first_open_that_comes_back_with_nothing_is_retried(bench, radio, caplog):
    # Bench-observed: the CLI's first --info over the UART returned exit code 0
    # and not one line, and the immediate retry produced the whole configuration.
    # Rubbish at the head of the stream — the Heltec's TX pin floats and its ROM
    # bootloader writes a boot log onto the same pins — is the explanation.
    bench.errors.append(TimeoutError("Timed out waiting for config"))
    with caplog.at_level(logging.WARNING):
        assert radio.probe() is True

    assert len(bench.opened) == 1
    assert "attempt 1 of 3" in caplog.text


def test_a_node_that_never_answers_is_reported_absent_rather_than_raising(bench, radio, caplog):
    # COMMS still has to come up: the cloud channel may be the working one, and
    # a process that vanished takes its heartbeat with it, leaving OBC unable to
    # tell a dead radio from a dead service.
    bench.errors.extend(OSError("no such device") for _ in range(3))
    with caplog.at_level(logging.ERROR):
        assert radio.probe() is False

    assert bench.errors == []
    assert "did not answer" in caplog.text


def test_giving_up_raises_a_radio_error_naming_the_port(bench, radio):
    bench.errors.extend(OSError("no such device") for _ in range(3))
    with pytest.raises(RadioError, match="/dev/serial0 after 3 attempts"):
        radio.send("hello")


def test_a_missing_meshtastic_library_says_how_to_run_without_hardware(monkeypatch):
    # None in sys.modules makes the lazy import raise ImportError even on a
    # machine where meshtastic IS installed (the Pi) — deleting the entry only
    # works where the library is genuinely absent, which made this test pass on
    # a laptop and fail on the satellite.
    monkeypatch.setitem(sys.modules, "meshtastic", None)
    monkeypatch.setitem(sys.modules, "meshtastic.serial_interface", None)
    with pytest.raises(RadioError, match="CUBESAT_MOCK_HARDWARE=1"):
        MeshtasticRadio(port="/dev/serial0").send("hello")


def test_the_inbox_is_subscribed_before_the_interface_is_opened(bench, radio):
    # A node that has been listening while we were not may deliver its backlog
    # during bring-up, and a command half a second early is still a command.
    bench.errors.extend(OSError("no such device") for _ in range(3))
    radio.probe()
    assert [topic for _listener, topic in bench.pub.subscriptions] == [RECEIVE_TOPIC]


def test_the_port_is_opened_once_however_often_it_is_used(bench, radio):
    radio.probe()
    radio.send("one")
    radio.send("two")
    assert len(bench.opened) == 1


def test_a_baud_rate_the_library_cannot_honour_is_argued_with(bench, radio, caplog):
    # The library opens the port hard-coded at 115200. A configuration value
    # that quietly does nothing is worse than one that answers back.
    with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "LORA_BAUDRATE", 38400)
        radio.probe()
    assert "cannot be told otherwise" in caplog.text


# ── identity ────────────────────────────────────────────────────────────────


def test_the_node_id_and_region_are_read_back_for_the_status_topic(bench, radio):
    radio.probe()
    assert radio.node_id == NODE_ID
    # The field holds a number; the name is what an operator can act on, and
    # UNSET is the commonest reason a board that flashed cleanly stays silent.
    assert radio.region == "US"


def test_an_unreadable_node_id_costs_a_display_string_and_not_the_bring_up(bench, radio):
    bench.node_info_error = AttributeError("no nodedb entry yet")
    assert radio.probe() is True
    assert radio.node_id is None


def test_a_node_info_without_a_user_block_reports_no_id(bench, radio):
    bench.node_info = {}
    radio.probe()
    assert radio.node_id is None


def test_a_region_number_the_library_does_not_name_is_reported_as_unknown(bench, radio):
    bench.region = 999
    radio.probe()
    assert radio.region is None


# ── sending ─────────────────────────────────────────────────────────────────


def test_a_message_goes_out_on_the_private_channel(bench, radio):
    # Channel 1 is the CubeSat channel with its own PSK, not the public
    # LongFast primary: bench traffic would otherwise clutter a shared chat, and
    # the uplink path must not be world-writable.
    radio.send("CSAT t=1741863600")
    assert bench.interface.sent == [("CSAT t=1741863600", config.LORA_CHANNEL_INDEX)]


def test_the_channel_comes_from_configuration(bench, monkeypatch):
    # A driver and a ground station that disagree here transmit and receive
    # perfectly and simply never meet, which is the hardest kind of radio fault
    # to diagnose — so the number is configured, not compiled in.
    monkeypatch.setattr(config, "LORA_CHANNEL_INDEX", 3)
    MeshtasticRadio(port="/dev/serial0", retry_delay=0.0).send("x")
    assert bench.interface.sent == [("x", 3)]


def test_the_channel_index_can_still_be_overridden_per_instance(bench):
    MeshtasticRadio(port="/dev/serial0", channel_index=0, retry_delay=0.0).send("x")
    assert bench.interface.sent == [("x", 0)]


def test_an_oversized_payload_is_refused_rather_than_shortened(bench, radio):
    # The bug this whole rewrite exists to remove: the pre-rewrite driver cut
    # every payload to 28 bytes and transmitted the result, so a mangled packet
    # was indistinguishable from a real one on the ground.
    with pytest.raises(ValueError, match=f"at most {MAX_RADIO_MESSAGE_BYTES}"):
        radio.send("x" * (MAX_RADIO_MESSAGE_BYTES + 1))
    assert bench.opened == []


def test_the_limit_is_counted_in_bytes_and_not_in_characters(bench, radio):
    # A degree sign is two bytes on the air. Counting characters would let a
    # beacon carrying non-ASCII through the guard and into a truncated transmit.
    oversized = "°" * (MAX_RADIO_MESSAGE_BYTES // 2 + 1)
    assert len(oversized) <= MAX_RADIO_MESSAGE_BYTES
    with pytest.raises(ValueError, match="at most"):
        radio.send(oversized)


# ── receiving ───────────────────────────────────────────────────────────────


def test_a_text_packet_becomes_a_radio_message(bench, radio):
    radio.probe()
    bench.pub.deliver(text_packet('{"command":"recover"}', rxSnr=6.0))

    received = radio.poll()
    assert len(received) == 1
    assert received[0].text == '{"command":"recover"}'
    assert received[0].sender == NODE_ID
    assert received[0].snr == 6.0


def test_a_packet_with_no_rssi_still_reports_its_snr(bench, radio):
    # rxRssi is absent on some packets; rxSnr is the one that is always there,
    # and it is the better number for a link margin anyway.
    radio.probe()
    bench.pub.deliver(text_packet("hello", rxSnr=-3.5))
    assert radio.poll()[0].snr == -3.5


def test_a_packet_carrying_neither_is_taken_anyway_with_nulls(bench, radio):
    radio.probe()
    bench.pub.deliver({"decoded": {"portnum": TEXT_PORTNUM, "text": "hello"}})
    message = radio.poll()[0]
    assert (message.sender, message.snr) == (None, None)
    assert (message.rssi, message.hops) == (None, None)


def test_link_quality_fields_ride_along_when_the_packet_carries_them(bench, radio):
    # hops = hopStart − hopLimit is inferred from the library, not yet
    # bench-verified — the check is a packet relayed through a third node
    # reading 1 here. 0 means heard directly.
    radio.probe()
    bench.pub.deliver(text_packet("hello", rxSnr=6.0, rxRssi=-96, hopStart=3, hopLimit=2))
    message = radio.poll()[0]
    assert message.rssi == -96
    assert message.hops == 1


def test_an_implausible_hop_arithmetic_is_withheld_rather_than_recorded(bench, radio):
    # A hopLimit above hopStart would read as negative hops: not a measurement,
    # a packet shaped differently from anything the inference understands.
    radio.probe()
    bench.pub.deliver(text_packet("hello", hopStart=1, hopLimit=5))
    assert radio.poll()[0].hops is None


@pytest.mark.parametrize(
    "packet",
    [
        {"decoded": {"portnum": "POSITION_APP", "text": "hello"}},
        {"decoded": {"portnum": TEXT_PORTNUM, "text": b"not a string"}},
        {"decoded": {"portnum": TEXT_PORTNUM}},
        {},
    ],
)
def test_anything_that_is_not_readable_text_is_dropped_in_silence(bench, radio, packet):
    # The mesh carries position broadcasts, node info and telemetry between
    # every node in range. Warning about each would bury the one that matters.
    radio.probe()
    bench.pub.deliver(packet)
    assert radio.poll() == []


def test_a_packet_shaped_like_nothing_on_the_bench_does_not_kill_the_receive_thread(
    bench, radio, caplog
):
    # If this raised, the library's receive thread would die and take the whole
    # uplink with it — which in FLIGHT is the only way back into the satellite.
    radio.probe()
    with caplog.at_level(logging.ERROR):
        bench.pub.deliver(object())

    assert "unreadable Meshtastic packet" in caplog.text
    assert radio.poll() == []


def test_polling_hands_each_message_over_exactly_once(bench, radio):
    radio.probe()
    bench.pub.deliver(text_packet("one"))
    bench.pub.deliver(text_packet("two"))

    assert [message.text for message in radio.poll()] == ["one", "two"]
    assert radio.poll() == []


def test_polling_never_opens_the_port(bench, radio):
    # Polling is what the COMMS loop does every cycle. A profile that forbids
    # LoRa must not end up with a serial port opened by a health check.
    assert radio.poll() == []
    assert bench.opened == []


# ── closing ─────────────────────────────────────────────────────────────────


def test_closing_gives_the_port_back_and_stops_listening(bench, radio):
    radio.probe()
    radio.close()

    assert bench.interface.closed is True
    # pubsub holds a callback for the life of the process: a closed interface
    # delivering into a dead service's queue is a leak with a confusing log line.
    assert bench.pub.subscriptions == []


def test_closing_twice_is_harmless(bench, radio):
    radio.probe()
    radio.close()
    radio.close()
    assert bench.interface.closed is True


def test_closing_a_radio_that_never_opened_is_harmless(bench, radio):
    assert radio.close() is None
    assert bench.opened == []


def test_an_interface_that_will_not_close_is_logged_and_let_go(bench, radio, caplog):
    radio.probe()
    bench.interface.close_error = OSError("device disconnected")
    with caplog.at_level(logging.ERROR):
        radio.close()

    assert "closing the Meshtastic interface failed" in caplog.text
    assert radio._interface is None


def test_an_unsubscribe_that_fails_does_not_stop_the_port_being_given_back(bench, radio, caplog):
    radio.probe()
    bench.pub.unsubscribe_error = RuntimeError("listener already gone")
    with caplog.at_level(logging.ERROR):
        radio.close()

    assert "could not unsubscribe" in caplog.text
    assert bench.interface.closed is True


def test_the_pub_helper_names_the_submodule_because_the_package_does_not_bind_it(bench):
    # importlib rather than an import statement, because pubsub is not in the
    # mypy override list and the object is untyped either way.
    assert meshtastic_radio._pub() is sys.modules["pubsub.pub"]


def test_a_radio_that_answers_on_a_later_attempt_is_not_subscribed_twice(bench, radio):
    # A node that was unplugged at startup and connected afterwards. Two
    # subscriptions would deliver every inbound command to the queue twice, and
    # COMMS would relay each of them twice onto cubesat/command.
    bench.errors.extend(OSError("no such device") for _ in range(3))
    assert radio.probe() is False
    assert radio.probe() is True

    assert len(bench.pub.subscriptions) == 1
    bench.pub.deliver(text_packet("once"))
    assert len(radio.poll()) == 1
