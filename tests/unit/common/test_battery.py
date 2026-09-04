import pytest

from cubesat.common import battery

# Points taken from the curve itself rather than written out again, so that
# replacing the inferred table with a measured one (ROADMAP V15) changes the
# table and not this file. What is being tested here is the arithmetic and the
# refusals, never the particular millivolts.
FULL = battery.FULL_VOLTS
EMPTY = battery.EMPTY_VOLTS
#: A voltage in the middle of the plateau, where a satellite spends most of a
#: trip and where every rate conversion below is anchored. Deliberately *between*
#: two points of the table rather than on one, because that is the case the
#: interpolation exists for.
MID = 3.82


def test_the_ends_of_the_curve_are_full_and_empty():
    assert battery.percent_from_voltage(FULL) == 100.0
    assert battery.percent_from_voltage(EMPTY) == 0.0


def test_the_curve_is_clamped_rather_than_extrapolated():
    # A pack over 4.2 V is full and one under 3.0 V is empty. A percentage
    # outside 0-100 is a chart artefact, and a negative one on a beacon would
    # read as a corrupt packet.
    assert battery.percent_from_voltage(FULL + 0.5) == 100.0
    assert battery.percent_from_voltage(EMPTY - 0.5) == 0.0


def test_the_curve_only_ever_rises():
    # Monotonic by construction: a non-monotonic table would make one voltage
    # ambiguous and the inverse below meaningless.
    percents = [battery.percent_from_voltage(v) for v, _ in battery.CURVE]
    assert percents == sorted(percents)
    assert len(set(percents)) == len(percents)


def test_interpolation_lands_between_the_points_it_sits_between():
    lower, upper = 3.90, 3.95
    middle = battery.percent_from_voltage((lower + upper) / 2)
    assert battery.percent_from_voltage(lower) < middle < battery.percent_from_voltage(upper)


def test_the_inverse_round_trips_through_the_curve():
    for volts, _ in battery.CURVE:
        assert battery.voltage_from_percent(battery.percent_from_voltage(volts)) == volts


def test_a_rate_in_percent_is_the_voltage_slope_through_the_local_gradient():
    # The one conversion with a number in it, spelled out because it is the one
    # a reader will want to check by hand. At 3.82 V the enclosing segment runs
    # 3.80-3.85 V for 55-62 %, which is 50 mV over 7 points: 7.143 mV per point.
    # So -100 mV/h is -14 %/h.
    assert battery.percent_per_hour(-100.0, MID) == pytest.approx(-14.0, abs=0.01)
    assert battery.percent_per_hour(100.0, MID) == pytest.approx(14.0, abs=0.01)


def test_the_same_slope_means_more_of_the_pack_at_the_knee_than_on_the_plateau():
    # Why the policy compares volts and the estimates are computed in percent:
    # the curve's gradient is not constant, so millivolts per hour are not a
    # fraction of the pack until they are converted where the pack actually is.
    plateau = battery.percent_per_hour(-100.0, MID)
    knee = battery.percent_per_hour(-100.0, 3.42)
    assert abs(knee) < abs(plateau)


def test_a_flat_or_missing_slope_yields_no_estimate():
    # Withheld rather than infinite. "Never" is not a time remaining, and a
    # dashboard that renders one as a number has to be argued with later.
    assert battery.seconds_to_voltage(MID, None, EMPTY) is None
    assert battery.seconds_to_voltage(MID, 0.0, EMPTY) is None


def test_time_to_empty_needs_a_falling_pack_and_time_to_full_a_rising_one():
    assert battery.seconds_to_voltage(MID, -100.0, EMPTY) is not None
    assert battery.seconds_to_voltage(MID, 100.0, EMPTY) is None
    assert battery.seconds_to_voltage(MID, 100.0, FULL) is not None
    assert battery.seconds_to_voltage(MID, -100.0, FULL) is None


def test_a_target_already_reached_yields_no_estimate():
    assert battery.seconds_to_voltage(EMPTY, -100.0, EMPTY) is None
    assert battery.seconds_to_voltage(FULL, 100.0, FULL) is None
    # Below the floor the answer is not "a negative number of hours".
    assert battery.seconds_to_voltage(EMPTY - 0.2, -100.0, EMPTY) is None


def test_the_estimate_is_the_percentage_gap_over_the_percentage_rate():
    # At 3.82 V the pack is at 57.8 % and -100 mV/h is -14 %/h, so it is about
    # four hours from empty. Computed in the percentage domain on purpose:
    # dividing the 820 mV gap by 100 mV/h would have said eight hours, because
    # it would have assumed the curve keeps its plateau gradient all the way
    # down. Over-stating the time remaining is the dangerous direction.
    seconds = battery.seconds_to_voltage(MID, -100.0, EMPTY)
    assert battery.percent_from_voltage(MID) == pytest.approx(57.8, abs=0.1)
    assert seconds == pytest.approx(57.8 / 14.0 * 3600.0, rel=0.01)


def test_a_slower_slope_takes_longer():
    fast = battery.seconds_to_voltage(MID, -200.0, EMPTY)
    slow = battery.seconds_to_voltage(MID, -50.0, EMPTY)
    assert slow > fast > 0


def test_a_slope_that_rounds_away_to_nothing_yields_no_estimate():
    # percent_per_hour rounds to two decimals, so a slope small enough lands on
    # zero and the division that would follow is a division by nothing. A
    # satellite holding its voltage to a hundredth of a percent per hour has no
    # time remaining worth naming.
    assert battery.percent_per_hour(-0.01, MID) == 0.0
    assert battery.seconds_to_voltage(MID, -0.01, EMPTY) is None
