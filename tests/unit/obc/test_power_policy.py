import pytest

from cubesat.common.states import MissionState
from cubesat.obc import power_policy
from cubesat.obc.power_policy import PowerReading, evaluate, reading_from

NOMINAL = MissionState.NOMINAL
DEPLOY = MissionState.DEPLOY
STANDBY = MissionState.STANDBY
LOW_POWER = MissionState.LOW_POWER
SAFE = MissionState.SAFE
CRITICAL = MissionState.CRITICAL


#: Readings placed relative to the thresholds rather than spelled out, so a
#: policy edit moves one constant instead of every number in this file — and a
#: threshold that moved *without* the behaviour changing does not fail a test
#: that was never about the number.
#:
#: Ten millivolts, because the thresholds are volts as of 2026-09-04. It is well
#: inside every band: the narrowest gap between two thresholds is the 60 mV from
#: LOW_POWER to SAFE.
JUST_UNDER = 0.01

LOW_POWER_EDGE = power_policy.LOW_POWER_VOLTS - JUST_UNDER
SAFE_EDGE = power_policy.SAFE_VOLTS - JUST_UNDER
CRITICAL_EDGE = power_policy.CRITICAL_VOLTS - JUST_UNDER
RECOVERED = power_policy.RECOVERY_VOLTS + JUST_UNDER
#: Above the descent threshold but still inside the band: on battery this is not
#: enough to climb out, which is what makes the mains path worth its own test.
NOT_RECOVERED_YET = power_policy.RECOVERY_VOLTS - JUST_UNDER
#: A pack with nothing left in it, well under every threshold.
FLAT = 3.30
#: A pack with plenty left in it, well over every threshold.
HEALTHY = 4.05


#: Slopes placed relative to the policy's own threshold, for the reason given at
#: JUST_UNDER. FLAT_VOLTAGE is what mains looked like on the hardware: the
#: terminal voltage did not move at all for an hour (2026-09-03).
FLAT_VOLTAGE = 0.0
FALLING_VOLTAGE = power_policy.DRAINING_MV_PER_HOUR - 10.0


def on_battery(volts):
    return PowerReading(voltage=volts, external_power=False, voltage_rate=-200.0)


def on_mains(volts, voltage_rate=FLAT_VOLTAGE):
    return PowerReading(voltage=volts, external_power=True, voltage_rate=voltage_rate)


# ── descent ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", [DEPLOY, NOMINAL])
def test_a_battery_under_the_throttling_threshold_calls_for_low_power(state):
    assert evaluate(on_battery(LOW_POWER_EDGE), state) is LOW_POWER


@pytest.mark.parametrize("state", [DEPLOY, NOMINAL])
def test_a_charging_satellite_is_not_throttled(state):
    # There is no battery life to stretch while mains is present, and the rule
    # has to be symmetrical with recovery below: if plugging in recovers at a
    # level, descending at that same level while plugged in would flap on every
    # EPS message.
    assert evaluate(on_mains(LOW_POWER_EDGE), state) is None


def test_standby_is_not_throttled():
    # A satellite that is already idle saves nothing by polling its idleness
    # more slowly, and LOW_POWER would only obscure why it is not working.
    assert evaluate(on_battery(LOW_POWER_EDGE), STANDBY) is None


@pytest.mark.parametrize("state", list(MissionState))
def test_a_battery_under_the_safe_threshold_reaches_safe_from_anywhere(state):
    expected = None if state in (SAFE, CRITICAL) else SAFE
    assert evaluate(on_battery(SAFE_EDGE), state) is expected


@pytest.mark.parametrize("state", list(MissionState))
def test_a_battery_under_the_critical_threshold_reaches_critical_from_anywhere(state):
    expected = None if state is CRITICAL else CRITICAL
    assert evaluate(on_battery(CRITICAL_EDGE), state) is expected


def test_the_descents_read_volts_and_not_the_gauge_percentage():
    # The point of the 2026-09-04 change, stated as a test: a payload whose
    # percentage says "empty" and whose voltage says "half full" descends
    # nowhere, because the percentage is a derived courtesy and the voltage is
    # the measurement. This is the shape of the drift that was measured on the
    # hardware — a model falling with the voltage flat — and it must not be able
    # to reach CRITICAL from any direction.
    lying = PowerReading(
        voltage=3.77, external_power=False, voltage_rate=-200.0, battery_percent=4.0
    )
    assert evaluate(lying, NOMINAL) is None


# ── mains outranks every descent ─────────────────────────────────────────────


@pytest.mark.parametrize("state", [NOMINAL, DEPLOY, STANDBY])
def test_a_flat_pack_on_mains_is_not_a_power_emergency(state):
    # The sequence this prevents: the satellite comes home with a flat pack, is
    # plugged in, reads 3.3 V, powers the host off — and the X728 never brings it
    # back, because mains never left. The recovery gesture would brick the unit.
    assert evaluate(on_mains(FLAT), state) is None


@pytest.mark.parametrize("state", [SAFE, LOW_POWER])
def test_plugging_in_a_flat_satellite_takes_it_to_nominal(state):
    assert evaluate(on_mains(FLAT), state) is MissionState.NOMINAL


def test_a_pack_that_keeps_falling_on_mains_still_reaches_critical():
    # A charger that has stopped charging still reads as external power, and this
    # is what the second opinion is for: without it one failure mode would
    # disable every protection below. A real one is visible in the voltage — the
    # pack is delivering the load current, so the terminal voltage falls.
    draining = {"voltage_rate": FALLING_VOLTAGE}
    assert evaluate(on_mains(CRITICAL_EDGE, **draining), NOMINAL) is CRITICAL
    assert evaluate(on_mains(SAFE_EDGE, **draining), NOMINAL) is SAFE
    assert evaluate(on_mains(LOW_POWER_EDGE, **draining), NOMINAL) is LOW_POWER


def test_a_flat_voltage_on_mains_never_descends_however_low_the_pack_reads():
    # Measured on the satellite 2026-09-03, and the case the whole voltage-first
    # design exists for: the satellite sat plugged in with its charge LEDs lit
    # and its terminal voltage flat to the millivolt for an hour, while the
    # gauge's modelled state of charge drifted down at 8-10 %/h. SAFE and
    # CRITICAL do not ask what state the satellite is in, so believing that model
    # was hours from powering off a satellite that was on mains the whole time.
    assert evaluate(on_mains(CRITICAL_EDGE), NOMINAL) is None
    assert evaluate(on_mains(SAFE_EDGE), NOMINAL) is None
    assert evaluate(on_mains(FLAT), STANDBY) is None


def test_a_falling_voltage_alone_is_now_enough_to_condemn_mains():
    # This inverts a test written on 2026-09-03, and the inversion is the point.
    # The first fix required the modelled percentage to agree with the voltage
    # before the pack counted as draining. Once the percentage became a function
    # of the voltage (common/battery.py), so did its slope, and "both agree"
    # became a condition that could not be false. One measured slope decides.
    assert evaluate(on_mains(CRITICAL_EDGE, voltage_rate=FALLING_VOLTAGE), NOMINAL) is CRITICAL


def test_no_voltage_history_yet_trusts_the_pin():
    # EPS publishes no voltage slope for its first five minutes, and for five
    # minutes after the mains pin changes. That window must not be the one where
    # a satellite powers itself off while plugged in — which is also what an EPS
    # older than this change looks like mid-upgrade.
    assert evaluate(on_mains(FLAT, voltage_rate=None), NOMINAL) is None


def test_a_pack_settling_after_a_plug_in_is_still_on_mains():
    # The terminal voltage sags for minutes after a charge starts and dips with
    # the load; a small negative slope is not a failed charger. The threshold is
    # not zero for that reason, and it is six times below the idle discharge it
    # has to catch.
    assert evaluate(on_mains(HEALTHY, voltage_rate=-10.0), NOMINAL) is None
    assert power_policy.DRAINING_MV_PER_HOUR == -30.0


def test_a_battery_that_fell_through_two_thresholds_lands_on_the_lower_one():
    # EPS may publish every 60 s in LOW_POWER, so two thresholds can be crossed
    # between messages. The severe verdict has to win.
    assert evaluate(on_battery(FLAT), NOMINAL) is CRITICAL


def test_critical_is_not_left_once_entered():
    # The poweroff is already in flight. Reversing it half-way is worse than
    # completing it, whatever the gauge says next — plugging in included.
    assert evaluate(on_mains(HEALTHY), CRITICAL) is None
    assert evaluate(on_battery(HEALTHY), CRITICAL) is None


# ── recovery, and the band that makes it possible ────────────────────────────


def test_recovery_needs_more_than_undoing_the_descent():
    # Without the band the state flaps every time the reading crosses the
    # threshold that got it here, so climbing just back over it is not enough.
    # The band is wider in volts than the ten points it replaced, because a
    # voltage moves with the load and a modelled percentage did not.
    assert evaluate(on_battery(power_policy.LOW_POWER_VOLTS + JUST_UNDER), LOW_POWER) is None
    assert evaluate(on_battery(RECOVERED), LOW_POWER) is MissionState.NOMINAL


def test_external_power_recovers_immediately_whatever_the_charge():
    # A pack on a desk charger may sit just under the recovery level for an hour.
    # Staying throttled for that hour is the wrong answer to "I am plugged in" —
    # this is the one path that does not wait for the band.
    assert evaluate(on_mains(NOT_RECOVERED_YET), LOW_POWER) is MissionState.NOMINAL


def test_safe_recovers_on_a_charged_pack_with_no_mains_at_all():
    # The pre-rewrite handler only ever left LOW_POWER when external power
    # appeared, which in FLIGHT meant it could never recover: there is no mains
    # on a walk no matter how much the pack has charged.
    assert evaluate(on_battery(RECOVERED), SAFE) is MissionState.NOMINAL


@pytest.mark.parametrize("state", [STANDBY, NOMINAL, DEPLOY])
def test_a_healthy_battery_changes_nothing_in_a_healthy_state(state):
    assert evaluate(on_mains(HEALTHY), state) is None
    assert evaluate(on_battery(HEALTHY), state) is None


def test_the_thresholds_are_ordered_as_named():
    # Reordering these by accident would make CRITICAL unreachable.
    assert (
        power_policy.CRITICAL_VOLTS
        < power_policy.SAFE_VOLTS
        < power_policy.LOW_POWER_VOLTS
        < power_policy.RECOVERY_VOLTS
    )


def test_critical_leaves_room_above_the_hardware_cutoff():
    # The X728 cuts its own 5 V output at 3.0 V. CRITICAL exists to flush the
    # recorder and power the host down *before* that happens, so the margin is
    # the whole point of where it sits — 450 mV, against a measured idle
    # discharge of 197 mV/h.
    assert power_policy.CRITICAL_VOLTS - 3.0 >= 0.3


# ── reading an eps_status payload ────────────────────────────────────────────


def test_the_reading_comes_from_the_fields_eps_actually_publishes():
    reading = reading_from(
        {
            "battery_percent": 42.5,
            "voltage": 3.75,
            "voltage_median": 3.76,
            "external_power": True,
            "voltage_rate": -20.0,
        }
    )
    assert reading == PowerReading(
        voltage=3.76, external_power=True, voltage_rate=-20.0, battery_percent=42.5
    )


def test_the_median_is_preferred_over_the_raw_sample():
    # The median is what survives a photograph being encoded, which is worth
    # tens of millivolts on this pack. Preferring it is the difference between
    # descending on the pack's level and descending on one sample of it.
    reading = reading_from({"voltage": 3.40, "voltage_median": 3.70})
    assert reading.voltage == 3.70


def test_the_raw_sample_is_used_when_there_is_no_median_yet():
    # EPS' first tick, and an EPS older than this change. One un-smoothed
    # measurement beats having no power protection at all.
    assert reading_from({"voltage": 3.70}).voltage == 3.70


def test_only_one_spelling_of_the_voltage_field_is_accepted():
    # Two accepted names for one field is how a typo in a publisher keeps working
    # here and stops working three services away.
    assert reading_from({"volts": 3.75}) is None


@pytest.mark.parametrize("rate", [None, "fast", True, [1]])
def test_an_unusable_voltage_rate_is_absent_rather_than_zero(rate):
    # Zero would read as "not draining" and silently re-enable the mains
    # override this field exists to qualify.
    assert reading_from({"voltage": 3.75, "voltage_rate": rate}).voltage_rate is None


@pytest.mark.parametrize("percent", [None, "half", True, [1]])
def test_an_unusable_percentage_is_absent_rather_than_fatal(percent):
    # It is only ever printed. A payload that carries a broken percentage still
    # carries a usable voltage, and refusing the verdict over the decoration
    # would be the tail wagging the dog.
    reading = reading_from({"voltage": 3.75, "battery_percent": percent})
    assert reading is not None
    assert reading.battery_percent is None


def test_external_power_defaults_to_absent_rather_than_present():
    assert reading_from({"voltage": 3.70}).external_power is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"voltage": None}, {"voltage": "3.75"}, {"voltage": True}, {"battery_percent": 38.0}],
)
def test_a_payload_with_no_usable_voltage_yields_no_verdict(payload):
    # None is not "the battery is fine" and not "the battery is empty": a gauge
    # that stopped answering must neither power the satellite off nor be ignored
    # as healthy. It has to be visibly absent instead. A True is refused for a
    # sharper reason: it is an int in Python, so it would arrive as 1.0 V.
    assert reading_from(payload) is None
