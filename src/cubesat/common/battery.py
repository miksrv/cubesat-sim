"""What a terminal voltage means, for a human reading a screen.

**The voltage is the measurement and the percentage is the presentation.** That
is the whole point of this module, and it is the opposite of how this project
started. Until 2026-09-04 the state of charge came from the X728's fuel gauge
and every power threshold was expressed in it — until the gauge was measured on
the satellite and found to be a MAX17040/41, a part with no shunt and no coulomb
counter, whose state of charge is reconstructed from an internal model. That
model was watched falling at 8–10 %/h for an hour while the satellite sat on
mains with its charge LEDs lit and its terminal voltage flat to the millivolt
(``docs/hardware-x728-ups-hat.md``). So the quantity the descents hung on was
the one quantity here that was never measured.

The fix is not a better percentage. ``obc/power_policy.py`` now compares volts,
because volts are what the gauge's ADC actually reports — 1.25 mV per LSB out of
``VCELL``. Nothing in the satellite's behaviour depends on the table below. It
exists so a dashboard can say "47 %" instead of "3.76 V", and so a beacon can
spend two characters instead of four. If it is wrong by five points, a chart is
wrong by five points and no decision changes. That is a deliberate downgrade in
what a percentage is allowed to do.

**The curve is inferred, not measured** — see ``CURVE``. Do not tidy that
marker away, and do not adjust individual points to make a reading look nicer:
the whole table is replaced at once, by one discharge log, or not at all.
"""

from __future__ import annotations

from bisect import bisect_left

#: Terminal volts against percent remaining, ascending by voltage.
#:
#: **Inferred from a generic 18650 discharge curve at a low C-rate, not measured
#: on this pack** (2026-09-04). It is the shape every Li-ion cell of this
#: chemistry has — flat from 4.0 down to 3.6 V, collapsing below 3.5 — and the
#: one point we do have agrees with it: 3.759 V under the HOSTED load read 47.7 %
#: on the gauge on 2026-09-03, against 47 % here. One coincidence is not a
#: calibration.
#:
#: Two things it deliberately does not model. The first is the load: these are
#: voltages *under the satellite's own draw*, which is dominated by the Pi and
#: varies by perhaps a third between profiles. Transferring the load to the pack
#: was measured at −50 mV on 2026-09-03, so an idle satellite reads a few points
#: low here and one with the camera running a few points high. The second is
#: charging: a pack on a charger sits above its resting voltage, so a percentage
#: read while charging is optimistic. Neither matters for a display, and neither
#: is worth a second table until the first one is measured.
#:
#: What would replace it: one continuous discharge from 4.2 V to the X728's own
#: 3.0 V cutoff, logged from ``eps_status`` in a fixed profile — ROADMAP V15.
#: That log also settles the pack's real capacity and the satellite's endurance,
#: neither of which is known. Until it exists, treat every percentage in this
#: system as an estimate with a rounded number of points of error.
CURVE: tuple[tuple[float, float], ...] = (
    (3.00, 0.0),
    (3.20, 1.0),
    (3.30, 3.0),
    (3.38, 6.0),
    (3.45, 10.0),
    (3.52, 15.0),
    (3.58, 20.0),
    (3.62, 27.0),
    (3.66, 33.0),
    (3.70, 40.0),
    (3.73, 45.0),
    (3.77, 50.0),
    (3.80, 55.0),
    (3.85, 62.0),
    (3.90, 70.0),
    (3.95, 78.0),
    (4.00, 85.0),
    (4.10, 93.0),
    (4.20, 100.0),
)

_VOLTS = tuple(point[0] for point in CURVE)
_PERCENTS = tuple(point[1] for point in CURVE)


def _interpolate(x: float, xs: tuple[float, ...], ys: tuple[float, ...]) -> float:
    """Piecewise-linear lookup, clamped at both ends.

    Clamped rather than extrapolated on purpose: above 4.2 V the cell is full and
    below 3.0 V it is empty, and a percentage outside 0–100 is a chart artefact
    rather than information.
    """
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    index = bisect_left(xs, x)
    x0, x1 = xs[index - 1], xs[index]
    y0, y1 = ys[index - 1], ys[index]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def percent_from_voltage(volts: float) -> float:
    """Percent remaining for a terminal voltage, 0–100."""
    return round(_interpolate(volts, _VOLTS, _PERCENTS), 1)


def voltage_from_percent(percent: float) -> float:
    """The inverse, for the mock and for reading a threshold in familiar units.

    Not used by the power policy: its thresholds are volts, written as volts, so
    that the numbers the satellite acts on do not travel through this table.
    """
    return round(_interpolate(percent, _PERCENTS, _VOLTS), 3)


def percent_per_hour(mv_per_hour: float, volts: float) -> float:
    """Convert a voltage slope into a percentage slope at this point on the curve.

    The conversion is local: the curve's gradient runs from about 6 mV per point
    on the plateau to 14 mV per point at the knee, so the same millivolts per
    hour mean very different fractions of the pack depending on where it is. That
    is exactly why the policy does not work in percent — but a rate in %/h is
    what a person reads, so it is computed here and published beside the slope it
    came from.
    """
    gradient = _gradient_mv_per_percent(volts)
    return round(mv_per_hour / gradient, 2)


def _gradient_mv_per_percent(volts: float) -> float:
    """Millivolts per percentage point at this voltage, from the enclosing segment."""
    if volts <= _VOLTS[0]:
        index = 1
    elif volts >= _VOLTS[-1]:
        index = len(_VOLTS) - 1
    else:
        index = bisect_left(_VOLTS, volts)
        index = max(index, 1)
    span_mv = (_VOLTS[index] - _VOLTS[index - 1]) * 1000.0
    span_percent = _PERCENTS[index] - _PERCENTS[index - 1]
    return span_mv / span_percent


#: The floor of the curve, and what "empty" means in ``seconds_to_voltage``.
#: It is also where the X728 cuts its own output (``docs/hardware-x728-ups-hat.md``),
#: so a satellite that reached it would already have been powered off by
#: CRITICAL a long way above — the number is for a person reading a gauge, not a
#: deadline anything acts on.
EMPTY_VOLTS = CURVE[0][0]
#: The top of the curve: the X728's terminal voltage, where charging stops.
FULL_VOLTS = CURVE[-1][0]


def seconds_to_voltage(volts: float, mv_per_hour: float | None, target: float) -> float | None:
    """How long until the pack reaches ``target`` volts, or None if it never will.

    Works in both directions — down to empty on a discharge, up to full on a
    charge — and refuses whenever the slope points away from the target.

    Computed in the percentage domain rather than by dividing the voltage gap by
    the voltage slope, and the difference is not cosmetic. A satellite drawing
    roughly constant power loses roughly constant *charge* per hour; what it does
    not lose is constant *volts* per hour, because the curve steepens as the pack
    empties. Extrapolating the current millivolts per hour straight down to the
    threshold therefore over-states the time remaining, and over-states it worst
    exactly where the answer matters — approaching the knee, where CRITICAL lives.

    Two honest limits on the answer, both worth repeating wherever it is shown.
    The curve is inferred, so this inherits its error. And the charging direction
    ignores the constant-voltage tail: a real charger tapers its current above
    roughly 4.0 V and spends a long time there, so a time-to-full fitted on the
    plateau is optimistic about the last few points. It is published anyway
    because the failure mode it has to survive is the opposite one — this pack's
    charger currently delivers so little that the useful information is the order
    of magnitude of the wait, and "about seventeen hours" is that information.

    None whenever the answer would be a guess dressed as a number: no slope yet,
    a pack that is holding, a slope pointing the wrong way, or a target already
    passed.
    """
    if mv_per_hour is None or mv_per_hour == 0.0:
        return None
    here = percent_from_voltage(volts)
    there = percent_from_voltage(target)
    rate = percent_per_hour(mv_per_hour, volts)
    if rate == 0.0:
        return None
    remaining = there - here
    if remaining == 0.0 or (remaining > 0.0) != (rate > 0.0):
        return None
    return round(remaining / rate * 3600.0, 1)
