"""The charge rate, computed from the state-of-charge history.

The X728's gauge is a MAX17040/41 (verified on the assembled satellite,
2026-09-01 — see ``docs/hardware-x728-ups-hat.md``). It reports voltage and
state of charge and nothing else: the ``CRATE`` register the power policy was
designed around exists on the MAX17048 and not on this part, and reading its
address returns ``0xFFFF``, which the driver used to decode into a confident
``−0.208 %/h`` that never changed. So the rate is derived here, from the one
quantity the gauge does measure — how the state of charge moves over time.

Why a least-squares slope over a window rather than two readings: the SOC
register moves in steps of 1/256 %, and the policy's threshold is −1 %/h. Two
readings 30 s apart can differ by one step and read as −0.5 %/h of pure noise;
a slope fitted through ten minutes of them cannot. Why ``None`` until the window
holds enough: a rate from a minute of data is a guess with two decimals, and the
consumer treats ``None`` as "trust the pin", which is the honest degradation.

Why the history is discarded when the mains pin changes: the slope describes one
power regime. Carrying a battery-time slope of −20 %/h across a plug-in would
report "still draining" for as long as the window is, and the power policy would
spend those minutes refusing to believe the satellite is on mains — which, for a
flat pack just brought home, is exactly the window in which it must not power
itself off. Resetting to ``None`` hands the decision back to the pin at once.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

#: A slope needs three points to be more than a line through two.
MIN_SAMPLES = 3


class ChargeRateEstimator:
    """Signed percent per hour from a sliding window of SOC readings."""

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

    def observe(self, battery_percent: float, external_power: bool) -> float | None:
        """Absorb one reading; return the rate in %/h, or None if not yet known."""
        now = self._clock()
        if external_power != self._external:
            # A new power regime: whatever the pack was doing before the plug
            # went in or out says nothing about what it is doing now.
            self._samples.clear()
            self._external = external_power
        self._samples.append((now, battery_percent))
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
