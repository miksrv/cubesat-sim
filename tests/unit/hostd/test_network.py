"""Bringing up an access point is the fiddliest thing in this project.

Which is why it is its own module: testable apart from unit management, and
reporting failures rather than raising them — a profile whose services started
but whose AP never came up is a real state, and the one OBC most needs to see.
"""

from __future__ import annotations

from cubesat.common.profiles import NetworkSpec
from cubesat.common.states import NetworkMode
from cubesat.hostd.allowlist import MDNS_UNIT, Allowlist, Refused
from cubesat.hostd.executor import RecordingExecutor
from cubesat.hostd.network import (
    HOTSPOT_CONNECTION,
    UNKNOWN_MODE,
    Network,
    NetworkState,
)
from tests.unit.hostd.test_executor import ScriptedExecutor

TWO_STATIONS = """Station aa:bb:cc:dd:ee:ff (on wlan0)
\tinactive time:\t100 ms
Station 11:22:33:44:55:66 (on wlan0)
\tinactive time:\t8 ms
"""


class PermitsNothing:
    """An allowlist that refuses everything.

    Defence in depth: a real ``Allowlist`` always permits the mDNS unit, so the
    only way to exercise the refusal path is to hand the module one that does
    not — which is what a future edit to the allowlist would look like.
    """

    def check(self, unit):
        raise Refused(f"{unit} is not on the allowlist")


def build(allowlist=None, **kwargs):
    executor = ScriptedExecutor(**kwargs)
    return Network(executor, allowlist or Allowlist()), executor


def test_client_mode_turns_the_radio_on_and_lets_networkmanager_reconnect():
    # The home network's credentials are not in profiles.yaml and must not be:
    # NetworkManager already has them and autoconnects once the hotspot
    # connection is out of the way.
    network, executor = build()
    state = network.apply(NetworkSpec(mode=NetworkMode.CLIENT))

    assert state == NetworkState(mode="client", ssid=None, clients=None, errors=())
    assert ("nmcli", "radio", "wifi", "on") in executor.calls
    assert ("nmcli", "connection", "down", HOTSPOT_CONNECTION) in executor.calls


def test_no_hotspot_to_take_down_is_the_ordinary_case_and_not_an_error():
    # Switching from one client profile to another. Errors on host_status have
    # to mean something.
    network, _ = build(fails=[("nmcli", "connection", "down")])
    assert network.apply(NetworkSpec(mode=NetworkMode.CLIENT)).errors == ()


def test_ap_mode_asks_networkmanager_for_a_hotspot_with_its_own_dhcp():
    network, executor = build(outputs={("iw",): TWO_STATIONS})
    state = network.apply(
        NetworkSpec(mode=NetworkMode.AP, ssid="cubesat", advertise_mdns=True)
    )

    assert state.mode == "ap"
    assert state.ssid == "cubesat"
    assert state.clients == 2
    assert state.errors == ()
    assert (
        "nmcli", "device", "wifi", "hotspot", "ifname", "wlan0", "ssid", "cubesat",
    ) in executor.calls
    assert ("systemctl", "start", MDNS_UNIT) in executor.calls


def test_an_access_point_with_no_ssid_is_refused_before_nmcli_is_called():
    # profiles.py refuses this at load; this is the second half of the same
    # guarantee, at the point where the value would reach a command line.
    network, executor = build()
    state = network.apply(NetworkSpec(mode=NetworkMode.AP, ssid=None))

    assert state.mode == UNKNOWN_MODE
    assert "without an ssid" in state.errors[0]
    assert not any("hotspot" in call for call in executor.calls)


def test_off_means_the_radio_is_down():
    network, executor = build()
    state = network.apply(NetworkSpec(mode=NetworkMode.OFF))
    assert state.mode == "off"
    assert ("nmcli", "radio", "wifi", "off") in executor.calls
    # FLIGHT has no dashboard to advertise, and nothing to advertise it on.
    assert ("systemctl", "stop", MDNS_UNIT) in executor.calls


def test_a_radio_that_will_not_come_up_is_reported_not_raised():
    network, executor = build(fails=[("nmcli", "radio")])
    state = network.apply(NetworkSpec(mode=NetworkMode.AP, ssid="cubesat"))

    assert state.mode == UNKNOWN_MODE
    assert state.errors and "radio wifi on" in state.errors[0]
    # And the hotspot was not attempted on a radio that is not on.
    assert not any("hotspot" in call for call in executor.calls)


def test_a_hotspot_that_does_not_come_up_leaves_the_mode_unknown():
    # Not "ap": host_status is the one topic that exists to be believed.
    network, _ = build(fails=[("nmcli", "device", "wifi", "hotspot")])
    state = network.apply(NetworkSpec(mode=NetworkMode.AP, ssid="cubesat"))
    assert state.mode == UNKNOWN_MODE
    assert state.clients is None
    assert "hotspot" in state.errors[0]


def test_mdns_that_will_not_start_is_an_error_but_not_a_wrong_mode():
    # The radio is exactly where it was asked to be; avahi is missing.
    network, _ = build(fails=[("systemctl", "start", MDNS_UNIT)])
    state = network.apply(
        NetworkSpec(mode=NetworkMode.CLIENT, advertise_mdns=True)
    )
    assert state.mode == "client"
    assert MDNS_UNIT in state.errors[0]


def test_failing_to_stop_an_mdns_daemon_that_is_not_installed_is_not_an_error():
    # Otherwise every profile on an image without avahi — including the default
    # one applied at every boot — would report itself partially applied.
    network, _ = build(fails=[("systemctl", "stop", MDNS_UNIT)])
    assert network.apply(NetworkSpec(mode=NetworkMode.CLIENT)).errors == ()


def test_the_client_count_is_null_rather_than_a_guessed_zero():
    # "0 visitors" that actually means "we cannot tell" is worse than nothing.
    network, _ = build(fails=[("iw",)])
    assert network.client_count() is None


def test_the_client_count_reads_the_stations_iw_reports():
    network, _ = build(outputs={("iw",): TWO_STATIONS})
    assert network.client_count() == 2
    assert Network(RecordingExecutor(), Allowlist()).client_count() == 0


def test_the_mdns_unit_is_checked_against_the_allowlist_like_any_other():
    # Same check, same words, before the argv exists — the allowlist is the one
    # place that says what root may systemctl, avahi included.
    network, executor = build(allowlist=PermitsNothing())
    state = network.apply(NetworkSpec(mode=NetworkMode.CLIENT, advertise_mdns=True))
    assert not any(MDNS_UNIT in call for call in executor.calls)
    assert "not on the allowlist" in state.errors[0]
    # And the mode is still what was asked for: this is not a radio failure.
    assert state.mode == "client"


def test_a_refused_mdns_stop_is_as_quiet_as_a_failed_one():
    network, _ = build(allowlist=PermitsNothing())
    assert network.apply(NetworkSpec(mode=NetworkMode.CLIENT)).errors == ()


def test_the_published_network_object_is_the_three_documented_fields():
    state = NetworkState(mode="ap", ssid="cubesat", clients=2, errors=("x",))
    assert state.as_dict() == {"mode": "ap", "ssid": "cubesat", "clients": 2}
