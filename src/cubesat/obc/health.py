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

**Except when OBC asked for it.** A ``restart_service`` relayed to HOSTD makes the
named service say goodbye on its way out, and reading that as a fault defeated the
command outright: the whole point of restarting one service is to fix it without
taking the dashboard from a room, and instead the satellite latched ``SAFE`` and
held there until a ground ``recover`` (found on the hardware, 2026-09-01). So OBC
declares the departure first, with ``expect_restart``, and this module waives that
one goodbye for one bounded window. The window is the ordinary loss grace, so a
service that does not come back is still declared lost on schedule: the protection
is postponed, never switched off. A restart nobody announced — ``systemctl restart``
by hand — is still a fault, and deliberately so.

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
        #: service -> when its waiver expires. See ``expect_restart``.
        self._expected: dict[str, float] = {}

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
        self._expected = {svc: due for svc, due in self._expected.items() if svc in wanted}
        self.log.info("watching %s", ", ".join(sorted(wanted)))

    def expect_restart(self, service: str) -> None:
        """Waive one service's next departure, because OBC asked for it.

        Called when a ``restart_service`` is relayed to HOSTD, and *before* the
        relay: the goodbye can arrive ahead of anything HOSTD says back, which is
        the race that produced the defect this exists to fix.

        Two things happen. The waiver is recorded for one loss grace, so the
        goodbye that follows is a note rather than a fault; and the service's
        silence clock is restarted, so the window is measured from the request
        rather than from its last heartbeat. What is deliberately *not* done is
        anything about the mission state: if the service fails to come back, it
        goes stale inside this same window and is declared lost exactly as it
        would have been.

        A service the profile does not run is not watched, and asking for its
        restart waives nothing — there is no departure to forgive.
        """
        if service not in self._last:
            return
        now = self._clock()
        self._expected[service] = now + self.grace
        self._departed.discard(service)
        self._last[service] = now
        self.log.info("expecting %s to depart and come back", service)

    def note(self, payload: dict[str, Any]) -> None:
        """Absorb one heartbeat message."""
        service = payload.get("service")
        if not isinstance(service, str) or service not in self._last:
            return
        if payload.get("alive") is False:
            if self._waived(service):
                self.log.info("%s is gone as requested; waiting for it to come back", service)
                return
            if service not in self._departed:
                self._departed.add(service)
                self.log.warning("%s announced it is gone", service)
            return
        # A heartbeat after a goodbye means the service was restarted. Clearing
        # the flag lets it be healthy again; the mission state stays in SAFE
        # until something decides otherwise, which is not this module's call.
        # The waiver goes too: it covered one restart, not every future one.
        self._departed.discard(service)
        self._expected.pop(service, None)
        self._last[service] = self._clock()

    def _waived(self, service: str) -> bool:
        """Whether this service's departure was asked for and is still recent."""
        due = self._expected.get(service)
        if due is None:
            return False
        if self._clock() < due:
            return True
        # The window closed without the service ever coming back. Drop the
        # waiver so the goodbye is read for what it now is.
        del self._expected[service]
        return False

    def lost(self) -> tuple[str, ...]:
        """Watched services that are gone or have gone quiet for too long."""
        now = self._clock()
        stale = {svc for svc, seen in self._last.items() if now - seen > self.grace}
        return tuple(sorted(self._departed | stale))
