import pytest

from cubesat.common import cadence, config
from cubesat.common.states import MissionState


def test_state_specific_interval_wins():
    assert cadence.interval_for("adcs", MissionState.NOMINAL) == 0.5
    assert cadence.interval_for("adcs", MissionState.LOW_POWER) == 5.0


def test_low_power_is_slower_than_nominal_for_every_service():
    # The whole point of LOW_POWER: if any service polls at the same rate, the
    # state is a label rather than a behaviour.
    for service, table in config.CADENCE.items():
        if MissionState.LOW_POWER.value in table and MissionState.NOMINAL.value in table:
            assert table[MissionState.LOW_POWER.value] >= table[MissionState.NOMINAL.value], service


def test_default_key_used_when_state_absent():
    assert cadence.interval_for("obc", MissionState.SAFE) == 30.0


def test_unknown_service_falls_back():
    fallback = cadence.FALLBACK_INTERVAL_SEC
    assert cadence.interval_for("nonexistent", MissionState.NOMINAL) == fallback


def test_no_state_yet_falls_back_to_default():
    assert cadence.interval_for("eps", None) == cadence.FALLBACK_INTERVAL_SEC


def test_scale_speeds_polling_up():
    assert cadence.interval_for("adcs", MissionState.NOMINAL, scale=0.2) == pytest.approx(0.1)


def test_zero_means_do_not_act_and_ignores_scale(monkeypatch):
    # A zero means "do not act in this state at all", and scaling nothing is
    # still nothing. Patched rather than taken from the shipped table: no
    # profile uses a zero today, and the one that did — the radio in SAFE —
    # turned out to silence receiving along with transmitting, which is how a
    # satellite ends up deaf in the state where a recover command matters most.
    monkeypatch.setitem(config.CADENCE, "quiet", {"SAFE": 0})
    assert cadence.interval_for("quiet", MissionState.SAFE) == 0.0
    assert cadence.interval_for("quiet", MissionState.SAFE, scale=0.1) == 0.0


def test_scaling_never_produces_a_hot_loop():
    assert cadence.interval_for("adcs", MissionState.NOMINAL, scale=1e-9) >= 0.01
