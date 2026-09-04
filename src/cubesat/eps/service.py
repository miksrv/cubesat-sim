"""EPS — Electrical Power System.

Reads the battery and mains state and publishes them. That is all it does: it
sets no thresholds, makes no decisions and knows nothing about mission states.
Deciding what 38% means belongs to OBC's power policy, in one place, where it
can be tested against every state.

What EPS adds to the gauge's reading is arithmetic, never a threshold: a slope
fitted to the terminal voltage, a median of the same voltage over a shorter
window, and — through ``common/battery.py`` — the percentage and the two
estimates of time remaining that a person actually reads. What any of it *means*
— whether −40 mV/h on mains is a failed charger — is still the policy's call.

**Everything here is anchored to the voltage** (2026-09-04). The gauge's own
state of charge is published as ``gauge_percent`` and used by nothing: it is a
MAX17040/41 reconstruction from an internal model, and it was measured drifting
downwards for an hour on mains while the voltage did not move at all. The
percentage on the wire is derived from the voltage instead, which is why there is
one slope here and not two — a slope over a derived percentage would be the
voltage slope with extra steps.

EPS runs in **every** profile, including HOSTED where there is no mission at
all. It is the only source of the telemetry that drives LOW_POWER, SAFE and
CRITICAL, and a satellite that cannot see its own battery cannot protect its
own filesystem.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from cubesat.common import battery, config
from cubesat.common.service import Service
from cubesat.eps.slopes import MedianWindow, SlopeEstimator
from cubesat.hal import registry
from cubesat.hal.interfaces import PowerMonitor


class EpsService(Service):
    name = "eps"
    cadence_key = "eps"

    def __init__(
        self,
        monitor: PowerMonitor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._monitor = monitor if monitor is not None else registry.power_monitor()
        # Fed in millivolts so the estimator's two-decimal rounding lands well
        # below the policy's threshold instead of quantising it to 10 mV/h.
        self._volts = SlopeEstimator(
            config.EPS_CHARGE_RATE_WINDOW_SEC,
            config.EPS_CHARGE_RATE_MIN_SPAN_SEC,
            clock=clock,
        )
        # A much shorter window, over the level rather than its slope. See
        # slopes.py -> MedianWindow: a threshold in volts has to survive the
        # camera pipeline starting.
        self._level = MedianWindow(config.EPS_LEVEL_WINDOW_SEC, clock=clock)

    def on_start(self) -> None:
        # Report the gauge's absence loudly and then carry on. The service stays
        # up so that its silence on eps_status is what OBC reacts to, rather
        # than a process that vanished and took its heartbeat with it.
        if not self._monitor.probe():
            self.log.error("fuel gauge is not answering; battery telemetry will be unavailable")

    def tick(self) -> None:
        reading = self._monitor.read()
        if reading.voltage_rate is None:
            # No gauge here reports this one, so it is always fitted. It is the
            # slope the power policy decides on: this part's state of charge is a
            # model that was measured drifting on mains, and the voltage is not
            # (2026-09-03, see hal/interfaces.py -> Power.voltage_rate).
            reading = replace(
                reading,
                voltage_rate=self._volts.observe(
                    reading.voltage * 1000.0, reading.external_power
                ),
            )
        level = round(self._level.observe(reading.voltage, reading.external_power), 3)
        if reading.charge_rate is None:
            # A gauge that measures its own rate is believed; this one does not,
            # so the rate a person reads is the measured slope expressed in the
            # units they expect. It is a restatement of voltage_rate, not a
            # second opinion, and the policy consults neither of the two through
            # this field.
            reading = replace(
                reading,
                charge_rate=(
                    None
                    if reading.voltage_rate is None
                    else battery.percent_per_hour(reading.voltage_rate, level)
                ),
            )
        self.publish(
            "eps_status",
            qos=1,
            **reading.as_dict(),
            # The level the descents compare, and the percentage a person reads.
            # Both derived from the median rather than from the raw sample, so
            # that a chart, a beacon and a state change never disagree about
            # which reading they were looking at.
            voltage_median=level,
            battery_percent=battery.percent_from_voltage(level),
            # Estimates, and null wherever the slope does not support one. The
            # target of the first is the pack's own floor rather than any policy
            # threshold: EPS sets no thresholds and does not know where CRITICAL
            # is, and the satellite will have powered itself off well before this
            # number runs out.
            time_to_empty_sec=battery.seconds_to_voltage(
                level, reading.voltage_rate, battery.EMPTY_VOLTS
            ),
            time_to_full_sec=battery.seconds_to_voltage(
                level, reading.voltage_rate, battery.FULL_VOLTS
            ),
        )
        self.log.debug(
            "pack %.3f V (median %.3f V, gauge says %s%%), external_power=%s, "
            "voltage_rate=%s, charge_rate=%s",
            reading.voltage,
            level,
            reading.gauge_percent,
            reading.external_power,
            reading.voltage_rate,
            reading.charge_rate,
        )

    def on_stop(self) -> None:
        close = getattr(self._monitor, "close", None)
        if close is not None:
            close()
