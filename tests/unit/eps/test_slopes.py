"""The slope fitted from the pack's voltage, and the median of that voltage.

Every window and span here is the test's own; nothing reads the shipped
configuration, so a retuned config.yaml cannot silently change what is proved.
"""

import pytest

from cubesat.eps.slopes import MIN_SAMPLES, MedianWindow, SlopeEstimator

WINDOW = 600.0
MIN_SPAN = 300.0


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def est():
    clock = Clock()
    return SlopeEstimator(WINDOW, MIN_SPAN, clock=clock), clock


def feed(est, clock, levels, step_sec):
    """Observe each level in turn, `step_sec` apart; return the last verdict."""
    verdict = None
    for i, level in enumerate(levels):
        if i:
            clock.advance(step_sec)
        verdict = est.observe(level, external_power=False)
    return verdict


def test_nothing_is_claimed_before_the_window_holds_enough_history(est):
    estimator, clock = est
    # Two readings a minute apart differ by one SOC step: that is noise, not a
    # rate, and publishing −0.23 %/h from it would be a guess with two decimals.
    assert estimator.observe(60.0, external_power=False) is None
    clock.advance(60)
    assert estimator.observe(59.996, external_power=False) is None
    assert estimator.span_sec == 60


def test_a_steady_discharge_fits_to_its_true_slope(est):
    estimator, clock = est
    # 0.1 % every 30 s is 12 %/h — the order of what the satellite really draws.
    levels = [60.0 - 0.1 * i for i in range(11)]  # 300 s of history
    assert feed(estimator, clock, levels, 30) == pytest.approx(-12.0)


def test_charging_reads_positive(est):
    estimator, clock = est
    levels = [40.0 + 0.05 * i for i in range(11)]
    assert feed(estimator, clock, levels, 30) == pytest.approx(6.0)


def test_soc_quantisation_averages_out_rather_than_alternating(est):
    estimator, clock = est
    # A pack losing 0.5 %/h moves one 1/256 % step every 28 s. Sampled every 30 s
    # the readings look like a staircase; the fitted slope must still be ~−0.5
    # and never read as the −0.47/−0.0 a two-point difference would alternate
    # between. This is the regime the policy's −1 %/h threshold lives in.
    lsb = 1 / 256
    levels = []
    for i in range(21):  # 600 s
        true = 60.0 - 0.5 * (30 * i) / 3600
        levels.append(round(round(true / lsb) * lsb, 2))
    rate = feed(estimator, clock, levels, 30)
    assert rate == pytest.approx(-0.5, abs=0.1)


def test_the_window_forgets_what_is_older_than_it(est):
    estimator, clock = est
    # Ten minutes flat, then a steep fall: once the flat samples have aged out,
    # the fit describes the fall alone.
    feed(estimator, clock, [60.0] * 21, 30)  # 600 s at rest
    clock.advance(30)
    verdict = None
    for i in range(1, 22):  # another 600 s, 0.1 % per 30 s
        verdict = estimator.observe(60.0 - 0.1 * i, external_power=False)
        clock.advance(30)
    assert verdict == pytest.approx(-12.0)
    assert estimator.span_sec <= WINDOW


def test_a_change_of_power_source_discards_the_history(est):
    estimator, clock = est
    levels = [60.0 - 0.2 * i for i in range(11)]
    assert feed(estimator, clock, levels, 30) == pytest.approx(-24.0)
    # Plugged in: the −24 %/h of battery time must not be reported as "still
    # draining" for the next ten minutes — that is the window in which a flat
    # pack brought home must not be allowed to power itself off.
    clock.advance(30)
    assert estimator.observe(58.0, external_power=True) is None
    assert estimator.span_sec == 0
    # And the new regime is measured from scratch.
    clock.advance(30)
    assert estimator.observe(58.0, external_power=True) is None


def test_reset_forgets_everything_including_the_power_source(est):
    estimator, clock = est
    feed(estimator, clock, [60.0 - 0.1 * i for i in range(11)], 30)
    estimator.reset()
    assert estimator.span_sec == 0
    assert estimator.observe(59.0, external_power=False) is None


def test_it_needs_at_least_three_points_even_over_a_long_span():
    clock = Clock()
    estimator = SlopeEstimator(WINDOW, min_span_sec=10.0, clock=clock)
    assert MIN_SAMPLES == 3
    estimator.observe(60.0, external_power=False)
    clock.advance(200)
    # Two points spanning far more than the minimum are still just a line
    # through two readings.
    assert estimator.observe(59.0, external_power=False) is None
    clock.advance(200)
    # 1 % per 200 s is 18 %/h.
    assert estimator.observe(58.0, external_power=False) == pytest.approx(-18.0)


def test_the_rate_is_rounded_to_two_decimals(est):
    estimator, clock = est
    levels = [60.0 - 0.0137 * i for i in range(11)]
    rate = feed(estimator, clock, levels, 30)
    assert rate == round(rate, 2)


@pytest.mark.parametrize("window, span", [(0, 1), (600, 0), (600, 601), (-1, -1)])
def test_a_nonsensical_window_is_refused_at_construction(window, span):
    with pytest.raises(ValueError):
        SlopeEstimator(window, span)


# ── the level, as a median over a short window ───────────────────────────────


def test_the_median_of_one_sample_is_that_sample():
    # Never None, deliberately: withholding the level for the first two minutes
    # after every start would leave the satellite with no power protection in
    # exactly the window where a flat pack is most likely.
    window = MedianWindow(60.0, clock=Clock())
    assert window.observe(3.75, external_power=False) == 3.75


def test_a_transient_dip_does_not_move_the_median():
    # A photograph being encoded is worth tens of millivolts on this pack, and
    # the thresholds are 60 mV apart.
    clock = Clock()
    window = MedianWindow(60.0, clock=clock)
    for _ in range(3):
        window.observe(3.70, external_power=False)
        clock.advance(10)
    assert window.observe(3.40, external_power=False) == 3.70


def test_samples_older_than_the_window_are_dropped():
    # Otherwise the median would carry the level from before a real fall for as
    # long as the process had been running.
    clock = Clock()
    window = MedianWindow(60.0, clock=clock)
    window.observe(4.10, external_power=False)
    clock.advance(600)
    assert window.observe(3.50, external_power=False) == 3.50


def test_the_median_starts_over_when_the_mains_pin_changes():
    # The plug moving is worth more millivolts than anything else that happens
    # to this pack: 50 of them, measured at an unplug on 2026-09-03.
    clock = Clock()
    window = MedianWindow(600.0, clock=clock)
    for _ in range(3):
        window.observe(3.70, external_power=False)
        clock.advance(10)
    assert window.observe(3.75, external_power=True) == 3.75


def test_a_reset_forgets_the_window_and_the_pin():
    clock = Clock()
    window = MedianWindow(600.0, clock=clock)
    window.observe(3.70, external_power=False)
    window.reset()
    assert window.observe(3.50, external_power=False) == 3.50


@pytest.mark.parametrize("window", [0, -1])
def test_a_nonsensical_level_window_is_refused_at_construction(window):
    with pytest.raises(ValueError):
        MedianWindow(window)
