"""EPS — Electrical Power System.

Reads the battery and mains state and publishes them. That is all it does: it
sets no thresholds, makes no decisions and knows nothing about mission states.
Deciding what 38% means belongs to OBC's power policy, in one place, where it
can be tested against every state.

The one thing EPS adds to the gauge's reading is the charge rate, and that is a
measurement, not a decision: the X728's gauge reports no rate of its own (see
``charge_rate.py``), so EPS derives it from how the state of charge moves. What
the rate *means* — whether −0.5 %/h on mains is a failed charger — is still the
policy's call.

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
from cubesat.eps.charge_rate import ChargeRateEstimator
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
        self._rate = ChargeRateEstimator(
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
        self.publish("eps_status", qos=1, **reading.as_dict())
        self.log.debug(
            "battery %.2f%% at %.3f V, external_power=%s, charge_rate=%s",
            reading.battery_percent,
            reading.voltage,
            reading.external_power,
            reading.charge_rate,
        )

    def on_stop(self) -> None:
        close = getattr(self._monitor, "close", None)
        if close is not None:
            close()
