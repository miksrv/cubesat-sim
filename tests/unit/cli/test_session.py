"""The CLI's one short conversation with the broker.

Nothing here connects to anything: the fake client answers ``connect`` by
calling back immediately, which is what the real broker does on localhost and
what keeps these tests from sitting out real timeouts.
"""

from __future__ import annotations

import json
import threading

import pytest

from cubesat.cli.session import BrokerUnavailable, Session
from cubesat.common.topics import TOPICS


@pytest.fixture
def session(fake_client):
    with Session(client=fake_client, collect_window=0.01, apply_timeout=0.05) as live:
        yield live


def test_it_sets_no_last_will(fake_client):
    # make_client() does, and rightly: a service dying is a fault OBC must hear
    # about. A person running a command and exiting is not, and publishing
    # "cli is gone" on the topic OBC watches would be noise on every invocation.
    with Session(client=fake_client):
        pass
    assert fake_client.will is None


def test_it_disconnects_on_the_way_out(fake_client):
    with Session(client=fake_client):
        assert fake_client.loop_running is True
    assert fake_client.loop_running is False
    assert fake_client.disconnected is True


def test_a_broker_that_refuses_the_socket_is_a_message_not_a_traceback(fake_client):
    def refuse(*_args, **_kwargs):
        raise OSError("Connection refused")

    fake_client.connect = refuse
    with pytest.raises(BrokerUnavailable, match="cannot reach the broker"), Session(
        client=fake_client
    ):
        pass


def test_a_broker_that_accepts_the_socket_and_says_nothing_times_out(fake_client):
    # A TCP connection is not a CONNACK. Waiting forever here would be a command
    # that hangs with no output, which is worse than a refusal.
    fake_client.connect = lambda *_a, **_k: None
    with pytest.raises(BrokerUnavailable, match="did not answer"), Session(
        client=fake_client, clock=_fast_clock()
    ):
        pass


def test_collect_returns_the_retained_state_it_was_given(session, fake_client):
    fake_client.deliver(TOPICS["obc_status"], {"status": "NOMINAL"})
    fake_client.deliver(TOPICS["host_status"], {"profile": "DEMO"})

    seen = session.collect("obc_status", "host_status")

    assert seen["obc_status"]["status"] == "NOMINAL"
    assert seen["host_status"]["profile"] == "DEMO"
    assert TOPICS["obc_status"] in fake_client.subscribed


def test_a_topic_nobody_published_is_absent_rather_than_waited_out(session, fake_client):
    # PAYLOAD's status does not exist in HOSTED, where the service is not
    # running. That is an answer, and the caller renders it as one.
    fake_client.deliver(TOPICS["obc_status"], {"status": "STANDBY"})
    seen = session.collect("obc_status", "payload_status")
    assert "payload_status" not in seen


def test_a_malformed_payload_is_dropped_rather_than_half_read(session, fake_client):
    # This topic is open to every ground client, so somebody else's broken
    # payload must not become a printed fact.
    fake_client.deliver(TOPICS["obc_status"], "{not json")
    fake_client.deliver(TOPICS["host_status"], json.dumps([1, 2, 3]))
    assert session.collect("obc_status", "host_status") == {}


def test_a_command_carries_a_request_id_that_can_be_quoted(session, fake_client):
    request_id = session.send("set_profile", profile="DEMO")

    published = fake_client.payloads(TOPICS["command"])[-1]
    assert published["command"] == "set_profile"
    assert published["params"] == {"profile": "DEMO"}
    # The same id appears in the satellite's logs, which is how a switch that
    # went wrong is traced afterwards.
    assert published["request_id"] == request_id
    assert request_id.startswith("cli-")


def test_a_command_with_no_parameters_carries_no_params_block(session, fake_client):
    session.send("recover")
    assert "params" not in fake_client.payloads(TOPICS["command"])[-1]


def test_await_message_accepts_one_that_already_arrived(session, fake_client):
    # Retained messages land on subscribe, which can be before the caller looks.
    fake_client.deliver(TOPICS["host_status"], {"profile": "DEMO"})
    answer = session.await_message("host_status", lambda payload: payload.get("profile") == "DEMO")
    assert answer is not None


def test_await_message_takes_one_that_arrives_while_it_waits(session, fake_client):
    # The normal case for a profile switch: the confirmation lands seconds after
    # the command, from another thread as far as this code is concerned.
    threading.Timer(
        0.02, lambda: fake_client.deliver(TOPICS["host_status"], {"profile": "EXPO"})
    ).start()

    answer = session.await_message(
        "host_status", lambda payload: payload.get("profile") == "EXPO", timeout=2.0
    )

    assert answer is not None


def test_await_message_gives_up_rather_than_hanging(session):
    assert session.await_message("host_status", lambda _payload: True, timeout=0.01) is None


def test_await_message_ignores_a_payload_that_does_not_qualify(session, fake_client):
    fake_client.deliver(TOPICS["host_status"], {"profile_requested": "EXPO"})
    answer = session.await_message(
        "host_status", lambda payload: payload.get("profile_requested") == "FLIGHT", timeout=0.01
    )
    assert answer is None


def test_the_real_client_is_built_with_no_will_and_its_own_id():
    # Two clients on one broker with the same id disconnect each other, and a
    # person may well run two commands at once from two terminals.
    from cubesat.cli.session import _plain_client

    first, second = _plain_client(), _plain_client()
    assert first._client_id != second._client_id
    assert first._client_id.startswith(b"cubesat-cli-")


def _fast_clock():
    """A clock that runs out immediately, so a timeout test costs nothing."""
    ticks = iter([0.0, 99.0, 99.0, 99.0])

    def clock() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 99.0

    return clock
