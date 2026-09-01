import textwrap
from dataclasses import fields

import pytest

from cubesat.common import profiles
from cubesat.common.profiles import DownlinkSpec
from cubesat.common.states import MissionMode, NetworkMode, Persistence, Profile


def write(tmp_path, body: str):
    path = tmp_path / "profiles.yaml"
    path.write_text(textwrap.dedent(body))
    return path


MINIMAL = """
    default_profile: HOSTED
    profiles:
      HOSTED:
        mission: standby
        network: { mode: client }
        external_units: start
        services: []
        persistence: none
"""

#: The same, with a unit registry — the only way a profile can name units.
REGISTERED = """
    default_profile: HOSTED
    external_units:
      - unit: telegram-bot.service
      - unit: starmap.service
      - unit: syncthing.service
    profiles:
      HOSTED:
        mission: standby
        network: { mode: client }
        external_units: [telegram-bot.service]
        services: []
        persistence: none
"""


# ── the real file in this repository ─────────────────────────────────────────


def test_repository_profiles_load():
    cfg = profiles.load()
    assert cfg.default is Profile.HOSTED
    assert set(cfg.profiles) == set(Profile)


def test_flight_is_offline_records_and_expires():
    flight = profiles.load().get(Profile.FLIGHT)
    assert flight.network.mode is NetworkMode.OFF
    assert flight.records
    # FLIGHT kills Wi-Fi, so a TTL is the timed way back to a reachable host.
    assert flight.ttl_minutes and flight.ttl_minutes > 0


def test_hosted_listens_on_lora_but_records_nothing():
    # COMMS runs even on the desk, because every boot lands in HOSTED — a field
    # reboot included, where an uplinked set_profile over LoRa is the only way
    # back in. It listens only: STANDBY has no row in the beacon table.
    hosted = profiles.load().get(Profile.HOSTED)
    assert hosted.services == ("comms",)
    assert hosted.downlink.lora
    assert hosted.persistence is Persistence.NONE
    assert not hosted.records


def test_lora_is_the_only_downlink_channel_a_profile_can_name():
    # The cloud API is gone: no ground station was ever deployed, and the ground
    # segment is now an interface over the satellite's own dashboard rather than
    # a service the satellite reports into. A profile naming a channel that does
    # not exist would be a promise nothing keeps.
    #
    # `beacon` is not a second channel — it is the starting state of the one
    # channel there is, which is why it lives in this block rather than beside
    # it.
    assert {f.name for f in fields(DownlinkSpec)} == {"lora", "beacon"}


def test_the_desk_profiles_listen_without_beaconing():
    # Decided 2026-09-01. In DEMO and EXPO the satellite is a metre from its
    # operator with the dashboard open, so beaconing at them over a shared mesh
    # channel is noise — while still *hearing* an uplinked set_profile is what
    # makes the radio worth having there at all. Quiet, not deaf.
    cfg = profiles.load()
    for profile in (Profile.DEMO, Profile.EXPO):
        assert cfg.get(profile).downlink.lora is True
        assert cfg.get(profile).downlink.beacon is False


def test_the_profiles_that_are_away_from_their_operator_beacon():
    # FLIGHT is the case the beacon exists for: Wi-Fi is down and the beacon is
    # the only thing saying the satellite is still alive. DIAG rehearses it.
    cfg = profiles.load()
    for profile in (Profile.FLIGHT, Profile.DIAG):
        assert cfg.get(profile).downlink.beacon is True


def test_exactly_one_profile_is_deaf_on_lora():
    # MAINTENANCE frees the serial port for reflashing the Heltec, and that is
    # the only reason a profile may be deaf: everywhere else the radio listens,
    # so a satellite is reachable over LoRa without SSH — including from SAFE,
    # which FLIGHT can descend into with no other way in.
    #
    # DIAG used to be here too, on the argument that the bench has the LAN. It
    # was removed when DIAG became a rehearsal of FLIGHT (2026-09-01): the
    # beacon and the uplink are precisely what FLIGHT cannot show you, so a
    # rehearsal with the radio off rehearses the wrong thing.
    cfg = profiles.load()
    deaf = {p for p in Profile if not cfg.get(p).downlink.lora}
    assert deaf == {Profile.MAINTENANCE}


def test_expo_brings_its_own_network_with_an_ssid():
    expo = profiles.load().get(Profile.EXPO)
    assert expo.network.mode is NetworkMode.AP
    assert expo.network.ssid


def test_diag_rehearses_flight_into_a_database_of_its_own():
    """DIAG is FLIGHT with the network and the dashboard kept.

    Asserted as agreement with FLIGHT rather than against literals, because the
    point of the profile is that it behaves like the one it rehearses: a
    rehearsal that polls at a different rate, or with the radio off, is a
    rehearsal of something else. What it must *not* share is the database — a
    desk run has no business in the archive of real trips.
    """
    cfg = profiles.load()
    diag = cfg.get(Profile.DIAG)
    flight = cfg.get(Profile.FLIGHT)

    assert diag.power.cadence_scale == flight.power.cadence_scale
    assert diag.downlink.lora == flight.downlink.lora
    assert diag.services == flight.services
    assert diag.mission is flight.mission

    assert diag.persistence is Persistence.DIAG_DB
    assert flight.persistence is Persistence.MISSION_DB
    # The two differences that make it watchable at all.
    assert diag.dashboard and not flight.dashboard
    assert diag.network.mode is not flight.network.mode


def test_allowlist_is_exactly_the_declared_external_units():
    cfg = profiles.load()
    assert cfg.unit_allowlist == {u.unit for u in cfg.external_units}


# ── loading and validation ───────────────────────────────────────────────────


def test_minimal_profile_gets_documented_defaults(tmp_path):
    cfg = profiles.load(write(tmp_path, MINIMAL))
    spec = cfg.get(Profile.HOSTED)
    assert spec.mission is MissionMode.STANDBY
    assert spec.power.governor == "ondemand"
    assert spec.power.cadence_scale == 1.0
    assert spec.downlink.lora is False
    assert spec.ttl_minutes is None
    assert spec.dashboard is False


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(profiles.ProfileError, match="not found"):
        profiles.load(tmp_path / "absent.yaml")


def test_empty_file_is_an_error(tmp_path):
    with pytest.raises(profiles.ProfileError, match="no profiles"):
        profiles.load(write(tmp_path, "profiles: {}\n"))


def test_unknown_profile_name_is_rejected(tmp_path):
    with pytest.raises(profiles.ProfileError, match="unknown profile name"):
        profiles.load(write(tmp_path, MINIMAL + "      ORBIT:\n        mission: standby\n"))


def test_missing_required_key_is_named(tmp_path):
    body = """
        profiles:
          HOSTED:
            mission: standby
            network: { mode: client }
    """
    with pytest.raises(profiles.ProfileError, match="missing required key 'external_units'"):
        profiles.load(write(tmp_path, body))


def test_bad_network_mode_is_rejected(tmp_path):
    with pytest.raises(profiles.ProfileError, match="bad network mode"):
        profiles.load(write(tmp_path, MINIMAL.replace("mode: client", "mode: carrier-pigeon")))


def test_access_point_without_ssid_is_rejected(tmp_path):
    # An AP with no SSID would come up unreachable, which is the one thing EXPO
    # cannot afford.
    with pytest.raises(profiles.ProfileError, match="requires an ssid"):
        profiles.load(write(tmp_path, MINIMAL.replace("mode: client", "mode: ap")))


def test_bad_external_units_verb_is_rejected(tmp_path):
    with pytest.raises(profiles.ProfileError, match="'start', 'stop', or a list"):
        body = MINIMAL.replace("external_units: start", "external_units: maybe")
        profiles.load(write(tmp_path, body))


def test_external_units_that_is_neither_a_verb_nor_a_list_is_rejected(tmp_path):
    body = MINIMAL.replace("external_units: start", "external_units: { telegram-bot: true }")
    with pytest.raises(profiles.ProfileError, match="'start', 'stop', or a list"):
        profiles.load(write(tmp_path, body))


def test_a_profile_may_name_exactly_the_external_units_it_wants(tmp_path):
    # The reason the list spelling exists: one unrelated service belongs on the
    # desk, another only during a demonstration, and "all or nothing" cannot say
    # so. What HOSTD receives is the resolved list — the verb never reaches it.
    spec = profiles.load(write(tmp_path, REGISTERED)).get(Profile.HOSTED)
    assert spec.external_units == ("telegram-bot.service",)


def test_start_is_shorthand_for_every_registered_unit(tmp_path):
    body = REGISTERED.replace("external_units: [telegram-bot.service]", "external_units: start")
    spec = profiles.load(write(tmp_path, body)).get(Profile.HOSTED)
    assert set(spec.external_units) == {
        "telegram-bot.service",
        "starmap.service",
        "syncthing.service",
    }


def test_stop_is_shorthand_for_none_of_them(tmp_path):
    body = REGISTERED.replace("external_units: [telegram-bot.service]", "external_units: stop")
    assert profiles.load(write(tmp_path, body)).get(Profile.HOSTED).external_units == ()


def test_a_unit_named_twice_is_wanted_once(tmp_path):
    body = REGISTERED.replace(
        "external_units: [telegram-bot.service]",
        "external_units: [telegram-bot.service, telegram-bot.service]",
    )
    spec = profiles.load(write(tmp_path, body)).get(Profile.HOSTED)
    assert spec.external_units == ("telegram-bot.service",)


def test_a_profile_may_not_name_a_unit_outside_the_registry(tmp_path):
    # The registry is the allowlist. HOSTD would refuse to start such a unit
    # anyway, so a typo has to fail at load rather than become a service that
    # silently never comes up.
    body = REGISTERED.replace(
        "external_units: [telegram-bot.service]", "external_units: [telegram-bott.service]"
    )
    with pytest.raises(profiles.ProfileError, match="not declared in the external_units registry"):
        profiles.load(write(tmp_path, body))


def test_unknown_service_is_rejected(tmp_path):
    # HOSTD refuses to touch units outside the allowlist, so a profile naming a
    # service that does not exist must fail loudly at load instead.
    with pytest.raises(profiles.ProfileError, match=r"unknown services \['telemetry'\]"):
        profiles.load(write(tmp_path, MINIMAL.replace("services: []", "services: [telemetry]")))


def test_unknown_governor_is_rejected(tmp_path):
    body = MINIMAL + "        power: { governor: turbo }\n"
    with pytest.raises(profiles.ProfileError, match="unknown CPU governor"):
        profiles.load(write(tmp_path, body))


def test_non_positive_cadence_scale_is_rejected(tmp_path):
    body = MINIMAL + "        power: { cadence_scale: 0 }\n"
    with pytest.raises(profiles.ProfileError, match="cadence_scale must be positive"):
        profiles.load(write(tmp_path, body))


def test_non_positive_ttl_is_rejected(tmp_path):
    body = MINIMAL + "        ttl_minutes: 0\n"
    with pytest.raises(profiles.ProfileError, match="ttl_minutes must be positive"):
        profiles.load(write(tmp_path, body))


def test_bad_mission_mode_is_rejected(tmp_path):
    with pytest.raises(profiles.ProfileError, match="not a valid MissionMode"):
        profiles.load(write(tmp_path, MINIMAL.replace("mission: standby", "mission: cruising")))


def test_persistence_without_an_active_mission_is_rejected(tmp_path):
    # Otherwise the profile promises a recording that nothing will ever produce.
    with pytest.raises(profiles.ProfileError, match="persistence requires mission 'active'"):
        body = MINIMAL.replace("persistence: none", "persistence: mission_db")
        profiles.load(write(tmp_path, body))


def test_default_profile_must_be_defined(tmp_path):
    with pytest.raises(profiles.ProfileError, match="is not defined"):
        body = MINIMAL.replace("default_profile: HOSTED", "default_profile: EXPO")
        profiles.load(write(tmp_path, body))


def test_bad_default_profile_name_is_rejected(tmp_path):
    with pytest.raises(profiles.ProfileError, match="bad default_profile"):
        body = MINIMAL.replace("default_profile: HOSTED", "default_profile: LUNAR")
        profiles.load(write(tmp_path, body))


def test_getting_an_undefined_profile_is_an_error(tmp_path):
    cfg = profiles.load(write(tmp_path, MINIMAL))
    with pytest.raises(profiles.ProfileError, match="not defined"):
        cfg.get(Profile.EXPO)


def test_getting_a_nonsense_profile_is_an_error(tmp_path):
    cfg = profiles.load(write(tmp_path, MINIMAL))
    with pytest.raises(profiles.ProfileError, match="unknown profile"):
        cfg.get("ORBITING")
