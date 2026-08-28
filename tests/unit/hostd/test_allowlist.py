"""The allowlist is the reason a root service in this project is acceptable."""

import pytest

from cubesat.common import profiles
from cubesat.hostd.allowlist import (
    DASHBOARD_UNIT,
    DENIED_UNITS,
    HOSTD_OWNED,
    MDNS_UNIT,
    Allowlist,
    Refused,
    unit_for,
)


def test_the_four_mission_services_and_the_dashboard_are_permitted():
    # Exactly the units a profile can ask for. EPS and OBC are always-on and
    # outside profile control, so they are not here.
    allowlist = Allowlist()
    for service in ("adcs", "payload", "dhs", "comms"):
        assert allowlist.permits(unit_for(service))
    assert allowlist.permits(DASHBOARD_UNIT)
    assert not allowlist.permits(unit_for("eps"))


def test_the_registry_in_profiles_yaml_is_what_widens_it():
    allowlist = Allowlist.from_profiles(profiles.load())
    assert allowlist.permits("telegram-bot.service")
    assert allowlist.permits("starmap.service")


def test_a_unit_nobody_named_is_refused_without_a_process():
    # The whole point: a typo in a profile fails to start something, it does not
    # take down sshd.
    allowlist = Allowlist(["telegram-bot.service"])
    assert not allowlist.permits("sshd.service")
    with pytest.raises(Refused, match="not on the allowlist"):
        allowlist.check("sshd.service")


@pytest.mark.parametrize("unit", sorted(DENIED_UNITS))
def test_the_branch_hostd_sits_on_is_denied_even_when_a_profile_names_it(unit, caplog):
    # OBC is the only thing that decides, HOSTD the only thing that acts, and
    # every control path runs through the broker. Stopping any of the three
    # strands the satellite in a half-applied profile.
    with caplog.at_level("ERROR"):
        allowlist = Allowlist([unit])
    assert not allowlist.permits(unit)
    assert unit not in allowlist.permitted
    with pytest.raises(Refused):
        allowlist.check(unit)


def test_external_units_may_not_name_one_of_our_own_units(caplog):
    # The one route by which a config file could reach cubesat@eps.service —
    # always-on, and the only source of the telemetry that drives CRITICAL.
    with caplog.at_level("ERROR"):
        allowlist = Allowlist(["cubesat@eps.service", "cubesat-dashboard.service"])
    assert not allowlist.permits("cubesat@eps.service")
    assert "external_units may only name foreign units" in caplog.text
    # The dashboard is permitted on its own account, not because of the registry.
    assert allowlist.permits(DASHBOARD_UNIT)


def test_the_mdns_daemon_is_listed_here_although_no_profile_names_it(caplog):
    # This file is the whole inventory of what root can systemctl: a reader who
    # concluded from it that HOSTD never touches avahi would be wrong, and a
    # safety property that takes two files to verify is one people stop
    # verifying. Driven by network.advertise_mdns, never by a unit list.
    with caplog.at_level("ERROR"):
        allowlist = Allowlist([MDNS_UNIT])
    assert allowlist.permits(MDNS_UNIT)
    assert MDNS_UNIT in allowlist.permitted
    assert "external_units may only name foreign units" in caplog.text


def test_the_units_hostd_owns_are_not_units_a_profile_starts_and_stops():
    # Otherwise "stop everything this profile did not ask for" would take the
    # mDNS daemon down a step before the network module brings it back up.
    allowlist = Allowlist(["telegram-bot.service"])
    assert allowlist.permitted >= HOSTD_OWNED
    assert not (HOSTD_OWNED & allowlist.profile_units)
    assert allowlist.profile_units == allowlist.permitted - HOSTD_OWNED


def test_network_manager_may_never_be_stopped_however_it_is_named(caplog):
    # Denied for what it carries rather than for what it is: every network mode
    # in this project is applied through nmcli, so stopping NetworkManager takes
    # away the way back to a reachable profile — and the profile where that
    # bites is FLIGHT, which has neither Wi-Fi nor SSH to fix it from. It is a
    # foreign unit, so nothing else in this file would have stopped it: naming
    # it in the registry has to be inert, not merely unusual.
    with caplog.at_level("ERROR"):
        allowlist = Allowlist(["NetworkManager.service", "telegram-bot.service"])
    assert not allowlist.permits("NetworkManager.service")
    assert "NetworkManager.service" not in allowlist.profile_units
    assert "HOSTD may never touch it" in caplog.text
    # The rest of the registry is unaffected: one denied name is not a reason to
    # discard the units a profile legitimately drives.
    assert allowlist.permits("telegram-bot.service")


def test_the_denied_units_are_refused_with_the_reason_said_out_loud():
    with pytest.raises(Refused, match="sawing off the branch it sits on"):
        Allowlist().check("mosquitto.service")


def test_a_service_name_becomes_its_template_instance():
    assert unit_for("adcs") == "cubesat@adcs.service"
