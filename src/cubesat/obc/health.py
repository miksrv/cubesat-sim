"""Subsystem liveness, from the one shared heartbeat topic.

Every service publishes ``{"service": ..., "alive": true}`` to
``cubesat/heartbeat`` on a fixed interval that is independent of its poll
cadence — a subsystem told to poll every 300 s in ``LOW_POWER`` must still prove
it is alive more often than that, or it would be declared lost for doing exactly
what it was told.

``alive: false`` on the same topic arrives two ways: the MQTT **last will**, when
a process dies ungracefully, and an explicit goodbye when it shuts down cleanly.
Either way it is acted on immediately — there is nothing to be gained by waiting
out a timeout for a service that has already announced it is gone.

The watch set comes from the **active profile**, plus EPS which runs in every
profile. Monitoring a service the profile never started would put a healthy
satellite in ``SAFE`` for not running the things it was told not to run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from cubesat.common import config

#: Runs in every profile and is outside profile control, so it is always watched.
ALWAYS_WATCHED = frozenset({"eps"})


class HealthMonitor:
    """Tracks the last heartbeat per watched service."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        interval: float | None = None,
        threshold: int | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.log = log or logging.getLogger("obc.health")
        self._clock = clock
        self._interval = config.HEARTBEAT_INTERVAL_SEC if interval is None else interval
        self._threshold = config.HEARTBEAT_MISS_THRESHOLD if threshold is None else threshold
        self._last: dict[str, float] = {}
        self._departed: set[str] = set()

    @property
    def grace(self) -> float:
        """How long silence is tolerated before a service is declared lost."""
        return self._interval * self._threshold

    @property
    def watched(self) -> frozenset[str]:
        return frozenset(self._last)

    def watch(self, services: Iterable[str]) -> None:
        """Set the watch list from the active profile's services, plus EPS.

        A service that has just been started has not had time to say anything
        yet, so its clock starts now rather than at some earlier zero — otherwise
        every profile switch would declare its own new subsystems lost.
        """
        now = self._clock()
        wanted = set(services) | set(ALWAYS_WATCHED)
        self._last = {svc: self._last.get(svc, now) for svc in wanted}
        self._departed &= wanted
        self.log.info("watching %s", ", ".join(sorted(wanted)))

    def note(self, payload: dict[str, Any]) -> None:
        """Absorb one heartbeat message."""
        service = payload.get("service")
        if not isinstance(service, str) or service not in self._last:
            return
        if payload.get("alive") is False:
            if service not in self._departed:
                self._departed.add(service)
                self.log.warning("%s announced it is gone", service)
            return
        # A heartbeat after a goodbye means the service was restarted. Clearing
        # the flag lets it be healthy again; the mission state stays in SAFE
        # until something decides otherwise, which is not this module's call.
        self._departed.discard(service)
        self._last[service] = self._clock()

    def lost(self) -> tuple[str, ...]:
        """Watched services that are gone or have gone quiet for too long."""
        now = self._clock()
        stale = {svc for svc, seen in self._last.items() if now - seen > self.grace}
        return tuple(sorted(self._departed | stale))
