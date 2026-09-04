"""Where the pack is and how it is moving: the level, and slopes fitted to it.

The X728's gauge is a MAX17040/41 (verified on the assembled satellite,
2026-09-01 — see ``docs/hardware-x728-ups-hat.md``). It reports voltage and
state of charge and nothing else: the ``CRATE`` register the power policy was
designed around exists on the MAX17048 and not on this part, and reading its
address returns ``0xFFFF``, which the driver used to decode into a confident
``−0.208 %/h`` that never changed. So every rate in this system is derived, and
this is where.

**One slope, over the voltage** (2026-09-04). There were briefly two, one over
each quantity the gauge reports, because the state of charge on this part is not
measured — there is no shunt and no coulomb counter — and the policy wanted a
measured opinion beside the modelled one. That model was watched drifting
downwards at 8–10 %/h on a satellite sitting on mains, with the charge LEDs lit
and the terminal voltage flat to the millivolt for the whole hour, so a slope
fitted to it says as much about the model settling as about the charge. The
percentage is now derived from the voltage instead (``common/battery.py``),
which makes a second slope over it a restatement of the first rather than a
second opinion — so it is gone, and the published ``charge_rate`` is this slope
converted through the pack curve.

Why a least-squares slope over a window rather than two readings: ``VCELL``
moves in steps of 1.25 mV, and the policy's threshold is −30 mV/h. Two readings
30 s apart can differ by one step and read as −150 mV/h of pure noise; a slope
fitted through ten minutes of them cannot. Why ``None`` until the window holds
enough: a rate from a minute of data is a guess with two decimals, and the
consumer treats ``None`` as "trust the pin", which is the honest degradation.

Why the history is discarded when the mains pin changes: a slope describes one
power regime. Carrying a battery-time slope of −200 mV/h across a plug-in would
report "still draining" for as long as the window is, and the power policy would
spend those minutes refusing to believe the satellite is on mains — which, for a
flat pack just brought home, is exactly the window in which it must not power
itself off. Resetting to ``None`` hands the decision back to the pin at once.

``MedianWindow`` below is the same idea applied to the level rather than its
slope, and it exists for the same reason the thresholds are volts.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from statistics import median

#: A slope needs three points to be more than a line through two.
MIN_SAMPLES = 3


class SlopeEstimator:
    """Signed units-per-hour from a sliding window of readings of one quantity.

    Unit-agnostic on purpose: EPS runs one of these over the terminal voltage,
    in millivolts, and the same class would fit a slope to anything else that
    arrives one sample at a time. The caller owns the unit and the threshold that
    goes with it — this class owns the fit and the rule about when there is not
    yet enough history to answer.
    """

    def __init__(
        self,
        window_sec: float,
        min_span_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_sec <= 0 or min_span_sec <= 0 or min_span_sec > window_sec:
            raise ValueError(
                f"charge-rate window must satisfy 0 < min_span ({min_span_sec}) "
                f"<= window ({window_sec})"
            )
        self._window = float(window_sec)
        self._min_span = float(min_span_sec)
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque()
        self._external: bool | None = None

    def observe(self, value: float, external_power: bool) -> float | None:
        """Absorb one reading; return the rate in %/h, or None if not yet known."""
        now = self._clock()
        if external_power != self._external:
            # A new power regime: whatever the pack was doing before the plug
            # went in or out says nothing about what it is doing now.
            self._samples.clear()
            self._external = external_power
        self._samples.append((now, value))
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        return self._slope()

    def reset(self) -> None:
        self._samples.clear()
        self._external = None

    @property
    def span_sec(self) -> float:
        """How much history the window currently holds."""
        if len(self._samples) < 2:
            return 0.0
        return self._samples[-1][0] - self._samples[0][0]

    def _slope(self) -> float | None:
        if len(self._samples) < MIN_SAMPLES or self.span_sec < self._min_span:
            return None
        n = len(self._samples)
        t0 = self._samples[0][0]
        ts = [t - t0 for t, _ in self._samples]
        ys = [y for _, y in self._samples]
        t_mean = sum(ts) / n
        y_mean = sum(ys) / n
        var = sum((t - t_mean) ** 2 for t in ts)
        cov = sum((t - t_mean) * (y - y_mean) for t, y in zip(ts, ys, strict=True))
        # var is positive here: span >= min_span > 0 guarantees two distinct times.
        per_second = cov / var
        return round(per_second * 3600.0, 2)


class MedianWindow:
    """The level itself, over a short window, as a median rather than a sample.

    This exists because the thresholds moved into volts (2026-09-04). A modelled
    state of charge changed slowly by construction — the part's internal filter
    did the smoothing, which was the one favour it did us. A terminal voltage
    does not: it drops the moment a load appears, and the load here is a Pi whose
    camera pipeline and CPU governor move it by tens of millivolts. Fifty of them
    were measured at a single unplug on 2026-09-03, and SAFE to CRITICAL is only
    130 mV apart, so one unlucky sample taken while a photograph is being encoded
    could descend a state on its own.

    A median rather than a mean, because the transient is exactly the shape a
    mean carries into the answer and a median throws away: three samples at
    3.60 V and one at 3.42 V are a satellite at 3.60 V that took a photograph,
    and the median says so while the mean says 3.56 V.

    The window is short on purpose — long enough to outlast a capture, short
    enough that a genuine fall is not hidden. At the 30 s NOMINAL cadence the
    default 120 s holds four samples and lags a real discharge by about a
    minute, which at the −197 mV/h measured at idle is 3 mV. Do not stretch it
    to smooth the charts; the charts have the raw ``voltage`` for that, and this
    number is what the descents compare.

    History is discarded when the mains pin changes, for the same reason the
    slope's is: the plug moving is worth more millivolts than anything else that
    happens to this pack, so samples from the other side of it describe a
    different regime.
    """

    def __init__(
        self,
        window_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_sec <= 0:
            raise ValueError(f"level window must be positive, not {window_sec}")
        self._window = float(window_sec)
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque()
        self._external: bool | None = None

    def observe(self, value: float, external_power: bool) -> float:
        """Absorb one reading; return the median of the window it now ends.

        Never None: one sample is a perfectly good median of one sample, and the
        alternative — withholding a level for the first two minutes after every
        start and every plug-in — would leave the satellite with no power
        protection in exactly the window where a flat pack is most likely.
        """
        now = self._clock()
        if external_power != self._external:
            self._samples.clear()
            self._external = external_power
        self._samples.append((now, value))
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        return median(value for _, value in self._samples)

    def reset(self) -> None:
        self._samples.clear()
        self._external = None
