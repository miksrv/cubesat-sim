"""EPS — Electrical Power System.

Reads the battery and mains state and publishes them. That is all it does: it
sets no thresholds, makes no decisions and knows nothing about mission states.
Deciding what 38% means belongs to OBC's power policy, in one place, where it
can be tested against every state.

What EPS adds to the gauge's reading is two slopes, and both are measurements
rather than decisions: the X728's gauge reports no rate of its own (see
``slopes.py``), so EPS fits one to the state of charge and one to the terminal
voltage. What a slope *means* — whether −0.5 %/h on mains is a failed charger —
is still the policy's call. The voltage one exists because the percentage one
turned out to describe a model rather than the pack: it drifted downwards for an
hour on mains while the voltage did not move at all.

EPS runs in **every** profile, including HOSTED where there is no mission at
all. It is the only source of the telemetry that drives LOW_POWER, SAFE and
CRITICAL, and a satellite that cannot see its own battery cannot protect its
own filesystem.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from cubesat.common import config
from cubesat.common.service import Service
from cubesat.eps.slopes import SlopeEstimator
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
        self._rate = SlopeEstimator(
            config.EPS_CHARGE_RATE_WINDOW_SEC,
            config.EPS_CHARGE_RATE_MIN_SPAN_SEC,
            clock=clock,
        )
        # The same window over the voltage, fed in millivolts so the estimator's
        # two-decimal rounding lands well below the policy's threshold instead of
        # quantising it to 10 mV/h.
        self._volts = SlopeEstimator(
            config.EPS_CHARGE_RATE_WINDOW_SEC,
            config.EPS_CHARGE_RATE_MIN_SPAN_SEC,
            clock=clock,
        )

    def on_start(self) -> None:
        # Report the gauge's absence loudly and then carry on. The service stays
        # up so that its silence on eps_status is what OBC reacts to, rather
        # than a process that vanished and took its heartbeat with it.
        if not self._monitor.probe():
            self.log.error("fuel gauge is not answering; battery telemetry will be unavailable")

    def tick(self) -> None:
        reading = self._monitor.read()
        if reading.charge_rate is None:
            # A gauge that measures its own rate is believed; this one does not,
            # so the rate is fitted to the history of the level it does measure.
            reading = replace(
                reading,
                charge_rate=self._rate.observe(reading.battery_percent, reading.external_power),
            )
        if reading.voltage_rate is None:
            # No gauge here reports this one, so it is always fitted. It is the
            # slope the power policy consults first: this part's state of charge
            # is a model that was measured drifting on mains, and the voltage is
            # not (2026-09-03, see hal/interfaces.py -> Power.voltage_rate).
            reading = replace(
                reading,
                voltage_rate=self._volts.observe(
                    reading.voltage * 1000.0, reading.external_power
                ),
            )
        self.publish("eps_status", qos=1, **reading.as_dict())
        self.log.debug(
            "battery %.2f%% at %.3f V, external_power=%s, charge_rate=%s, voltage_rate=%s",
            reading.battery_percent,
            reading.voltage,
            reading.external_power,
            reading.charge_rate,
            reading.voltage_rate,
        )

    def on_stop(self) -> None:
        close = getattr(self._monitor, "close", None)
        if close is not None:
            close()
