"""EPS — Electrical Power System.

Reads the battery and mains state and publishes them. That is all it does: it
sets no thresholds, makes no decisions and knows nothing about mission states.
Deciding what 38% means belongs to OBC's power policy, in one place, where it
can be tested against every state.

EPS runs in **every** profile, including HOSTED where there is no mission at
all. It is the only source of the telemetry that drives LOW_POWER, SAFE and
CRITICAL, and a satellite that cannot see its own battery cannot protect its
own filesystem.
"""

from __future__ import annotations

from cubesat.common.service import Service
from cubesat.hal import registry
from cubesat.hal.interfaces import PowerMonitor


class EpsService(Service):
    name = "eps"
    cadence_key = "eps"

    def __init__(self, monitor: PowerMonitor | None = None) -> None:
        super().__init__()
        self._monitor = monitor if monitor is not None else registry.power_monitor()

    def on_start(self) -> None:
        # Report the gauge's absence loudly and then carry on. The service stays
        # up so that its silence on eps_status is what OBC reacts to, rather
        # than a process that vanished and took its heartbeat with it.
        if not self._monitor.probe():
            self.log.error("fuel gauge is not answering; battery telemetry will be unavailable")

    def tick(self) -> None:
        reading = self._monitor.read()
        self.publish("eps_status", qos=1, **reading.as_dict())
        self.log.debug(
            "battery %.2f%% at %.3f V, external_power=%s",
            reading.battery_percent,
            reading.voltage,
            reading.external_power,
        )

    def on_stop(self) -> None:
        close = getattr(self._monitor, "close", None)
        if close is not None:
            close()
