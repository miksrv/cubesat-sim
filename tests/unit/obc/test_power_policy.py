import pytest

from cubesat.common.states import MissionState
from cubesat.obc import power_policy
from cubesat.obc.power_policy import PowerReading, evaluate, reading_from

NOMINAL = MissionState.NOMINAL
SCIENCE = MissionState.SCIENCE
DEPLOY = MissionState.DEPLOY
STANDBY = MissionState.STANDBY
LOW_POWER = MissionState.LOW_POWER
SAFE = MissionState.SAFE
CRITICAL = MissionState.CRITICAL


#: Readings placed relative to the thresholds rather than spelled out, so a
#: policy edit moves one constant instead of every number in this file — and a
#: threshold that moved *without* the behaviour changing does not fail a test
#: that was never about the number.
JUST_UNDER = 1.0

LOW_POWER_EDGE = power_policy.LOW_POWER_PERCENT - JUST_UNDER
SAFE_EDGE = power_policy.SAFE_PERCENT - JUST_UNDER
CRITICAL_EDGE = power_policy.CRITICAL_PERCENT - JUST_UNDER
RECOVERED = power_policy.RECOVERY_PERCENT + JUST_UNDER


def on_battery(percent):
    return PowerReading(battery_percent=percent, external_power=False, charge_rate=-2.0)


def on_mains(percent, charge_rate=1.5):
    return PowerReading(
        battery_percent=percent, external_power=True, charge_rate=charge_rate
    )


# ── descent ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", [DEPLOY, NOMINAL, SCIENCE])
def test_a_battery_under_forty_percent_calls_for_low_power(state):
    assert evaluate(on_battery(LOW_POWER_EDGE), state) is LOW_POWER


@pytest.mark.parametrize("state", [DEPLOY, NOMINAL, SCIENCE])
def test_a_charging_satellite_is_not_throttled(state):
    # There is no battery life to stretch while mains is present, and the rule
    # has to be symmetrical with recovery below: if plugging in recovers at 35 %,
    # descending at 35 % while plugged in would flap on every EPS message.
    assert evaluate(on_mains(LOW_POWER_EDGE), state) is None


def test_standby_is_not_throttled():
    # A satellite that is already idle saves nothing by polling its idleness
    # more slowly, and LOW_POWER would only obscure why it is not working.
    assert evaluate(on_battery(LOW_POWER_EDGE), STANDBY) is None


@pytest.mark.parametrize("state", list(MissionState))
def test_a_battery_under_twenty_percent_reaches_safe_from_anywhere(state):
    expected = None if state in (SAFE, CRITICAL) else SAFE
    assert evaluate(on_battery(SAFE_EDGE), state) is expected


@pytest.mark.parametrize("state", list(MissionState))
def test_a_battery_under_ten_percent_reaches_critical_from_anywhere(state):
    expected = None if state is CRITICAL else CRITICAL
    assert evaluate(on_battery(CRITICAL_EDGE), state) is expected


# ── mains outranks every descent ─────────────────────────────────────────────


@pytest.mark.parametrize("state", [NOMINAL, SCIENCE, DEPLOY, STANDBY])
def test_a_flat_pack_on_mains_is_not_a_power_emergency(state):
    # The sequence this prevents: the satellite comes home with a flat pack, is
    # plugged in, reports 5 %, powers the host off — and the X728 never brings it
    # back, because mains never left. The recovery gesture would brick the unit.
    assert evaluate(on_mains(5.0), state) is None


@pytest.mark.parametrize("state", [SAFE, LOW_POWER])
def test_plugging_in_a_flat_satellite_takes_it_to_nominal(state):
    assert evaluate(on_mains(5.0), state) is MissionState.NOMINAL


def test_a_pack_that_keeps_falling_on_mains_still_reaches_critical():
    # A charger that has stopped charging still reads as external power. The
    # charge rate is the second opinion, and this is what it is for: without it
    # one failure mode would disable every protection below.
    assert evaluate(on_mains(9.0, charge_rate=-3.0), NOMINAL) is CRITICAL
    assert evaluate(on_mains(19.0, charge_rate=-3.0), NOMINAL) is SAFE
    assert evaluate(on_mains(LOW_POWER_EDGE, charge_rate=-3.0), NOMINAL) is LOW_POWER


def test_a_gauge_that_cannot_report_a_rate_is_believed_about_mains():
    # Otherwise an EPS that has not yet seen enough history for a rate would power a
    # plugged-in satellite off — a second failure mode caused by the fix for the
    # first one.
    assert evaluate(on_mains(5.0, charge_rate=None), NOMINAL) is None


def test_a_pack_settling_after_a_charge_is_still_on_mains():
    # Gauges report small negative rates while a charge settles. Reading that as
    # a mains failure would throw away the protection it is meant to preserve.
    assert evaluate(on_mains(95.0, charge_rate=-0.4), NOMINAL) is None
    assert power_policy.DRAINING_PERCENT_PER_HOUR == -1.0


def test_a_battery_that_fell_through_two_thresholds_lands_on_the_lower_one():
    # EPS may publish every 60 s in LOW_POWER, so two thresholds can be crossed
    # between messages. The severe verdict has to win.
    assert evaluate(on_battery(8.0), NOMINAL) is CRITICAL


def test_critical_is_not_left_once_entered():
    # The poweroff is already in flight. Reversing it half-way is worse than
    # completing it, whatever the gauge says next — plugging in included.
    assert evaluate(on_mains(95.0), CRITICAL) is None
    assert evaluate(on_battery(95.0), CRITICAL) is None


# ── recovery, and the band that makes it possible ────────────────────────────


def test_recovery_needs_ten_points_more_than_the_descent_took():
    # Without the band the state flaps every time the reading crosses 40 %.
    assert evaluate(on_battery(41.0), LOW_POWER) is None
    assert evaluate(on_battery(50.0), LOW_POWER) is MissionState.NOMINAL


def test_external_power_recovers_immediately_whatever_the_charge():
    # A pack on a desk charger may read 35 % for an hour. Staying throttled for
    # that hour is the wrong answer to "I am plugged in".
    assert evaluate(on_mains(35.0), LOW_POWER) is MissionState.NOMINAL


def test_safe_recovers_on_a_charged_pack_with_no_mains_at_all():
    # The pre-rewrite handler only ever left LOW_POWER when external power
    # appeared, which in FLIGHT meant it could never recover: there is no mains
    # on a walk no matter how much the pack has charged.
    assert evaluate(on_battery(RECOVERED), SAFE) is MissionState.NOMINAL


@pytest.mark.parametrize("state", [STANDBY, NOMINAL, SCIENCE, DEPLOY])
def test_a_healthy_battery_changes_nothing_in_a_healthy_state(state):
    assert evaluate(on_mains(88.0), state) is None
    assert evaluate(on_battery(88.0), state) is None


def test_the_thresholds_are_ordered_as_named():
    # Reordering these by accident would make CRITICAL unreachable.
    assert (
        power_policy.CRITICAL_PERCENT
        < power_policy.SAFE_PERCENT
        < power_policy.LOW_POWER_PERCENT
        < power_policy.RECOVERY_PERCENT
    )


# ── reading an eps_status payload ────────────────────────────────────────────


def test_the_reading_comes_from_the_fields_eps_actually_publishes():
    reading = reading_from(
        {"battery_percent": 42.5, "voltage": 3.9, "external_power": True, "charge_rate": -0.2}
    )
    assert reading == PowerReading(
        battery_percent=42.5, external_power=True, charge_rate=-0.2
    )


def test_only_one_spelling_of_the_battery_field_is_accepted():
    # Two accepted names for one field is how a typo in a publisher keeps working
    # here and stops working three services away.
    assert reading_from({"battery": 42.5}) is None


@pytest.mark.parametrize("rate", [None, "fast", True, [1]])
def test_an_unusable_charge_rate_is_absent_rather_than_zero(rate):
    # Zero would read as "not draining" and silently re-enable the mains
    # override this field exists to qualify.
    assert reading_from({"battery_percent": 50.0, "charge_rate": rate}).charge_rate is None


def test_external_power_defaults_to_absent_rather_than_present():
    assert reading_from({"battery_percent": 30.0}).external_power is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"battery_percent": None}, {"battery_percent": "38"}, {"battery_percent": True}],
)
def test_a_payload_with_no_usable_level_yields_no_verdict(payload):
    # None is not "the battery is fine" and not "the battery is empty": a gauge
    # that stopped answering must neither power the satellite off nor be ignored
    # as healthy. It has to be visibly absent instead.
    assert reading_from(payload) is None
