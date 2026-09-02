"""The mission state machine — what the satellite is doing right now.

One of the two orthogonal axes. The other one, the platform profile, is a
request/reconcile loop and lives in ``profile_machine.py``; merging them into a
single flat machine is the mistake ``docs/concept.md`` argues against at length.

The transition table below is the whole specification of legal movement, which
is the reason for using ``transitions`` here rather than a hand-written
dispatch: a state that can be reached from anywhere (``SAFE``) and a state that
can be reached from almost anywhere (``CRITICAL``) are one line each, and the
illegal moves are the ones that are *not* written down.

Two properties the table encodes deliberately:

* **Nothing outranks ``CRITICAL``.** It is the only state permitted to change
  host power, so once entered no trigger leaves it — not a profile change, not a
  ground command. The host is going down; the only question left is whether the
  recorder got to close its mission first.
* **No reflexive transitions.** Every source list excludes its own destination,
  so ``safe_mode`` twice fires one state change, not two. The status topic is
  retained and DHS acts on transitions into ``CRITICAL``; a spurious repeat is a
  spurious event downstream.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from transitions import Machine

from cubesat.common.states import MissionState

#: Triggers, by name, for callers that would otherwise spell them as strings.
BOOT_COMPLETE = "boot_complete"
BEGIN_DEPLOY = "begin_deploy"
DEPLOY_COMPLETE = "deploy_complete"
STAND_DOWN = "stand_down"
ENTER_LOW_POWER = "enter_low_power"
ENTER_SAFE = "enter_safe"
ENTER_CRITICAL = "enter_critical"
RECOVER = "recover"


def _all_but(*excluded: MissionState) -> list[MissionState]:
    return [state for state in MissionState if state not in excluded]


TRANSITIONS: list[dict[str, Any]] = [
    # Boot is over as soon as the service is up and on the bus. There is nothing
    # to self-test yet: DEPLOY is what tests the hardware, and it only runs once
    # a profile has said which hardware this run is supposed to have.
    {"trigger": BOOT_COMPLETE, "source": MissionState.BOOT, "dest": MissionState.STANDBY},
    # Entering an active profile is the trigger for bring-up. That is what
    # finally gives DEPLOY something to do.
    {"trigger": BEGIN_DEPLOY, "source": MissionState.STANDBY, "dest": MissionState.DEPLOY},
    {"trigger": DEPLOY_COMPLETE, "source": MissionState.DEPLOY, "dest": MissionState.NOMINAL},
    # Leaving an active profile, or entering one that asks for no mission.
    # Reachable from a descended state too: HOSTED after a SAFE is idle, not
    # faulted, because the profile no longer asks for the subsystems that failed.
    {
        "trigger": STAND_DOWN,
        "source": _all_but(MissionState.STANDBY, MissionState.CRITICAL),
        "dest": MissionState.STANDBY,
    },
    # Only from the states that are actually spending power. Dropping STANDBY to
    # LOW_POWER would throttle a satellite that is already doing nothing.
    {
        "trigger": ENTER_LOW_POWER,
        "source": [MissionState.DEPLOY, MissionState.NOMINAL],
        "dest": MissionState.LOW_POWER,
    },
    {
        "trigger": ENTER_SAFE,
        "source": _all_but(MissionState.SAFE, MissionState.CRITICAL),
        "dest": MissionState.SAFE,
    },
    {
        "trigger": ENTER_CRITICAL,
        "source": _all_but(MissionState.CRITICAL),
        "dest": MissionState.CRITICAL,
    },
    # Recovery lands in NOMINAL, not back in DEPLOY: the subsystems never went
    # away, they were only throttled or silenced.
    {
        "trigger": RECOVER,
        "source": [MissionState.LOW_POWER, MissionState.SAFE],
        "dest": MissionState.NOMINAL,
    },
]


class MissionMachine:
    """The mission state, and the only legal ways to move it.

    ``on_change`` is called after every accepted transition, with the previous
    and the new state. OBC uses it to publish the retained status, so a state
    change and its announcement cannot drift apart.
    """

    #: Set and maintained by ``transitions``; declared here for type checkers.
    state: MissionState
    #: Also supplied by ``transitions``: whether a named trigger would be legal.
    may_trigger: Callable[[str], bool]

    def __init__(
        self,
        on_change: Callable[[MissionState, MissionState], None] | None = None,
        *,
        log: logging.Logger | None = None,
    ) -> None:
        self.log = log or logging.getLogger("obc.mission")
        self._on_change = on_change
        self._previous: MissionState = MissionState.BOOT
        # ignore_invalid_triggers turns an illegal move into a False return
        # instead of an exception: a ground command that does not apply in the
        # current state is a refusal to log, never a reason to take OBC down.
        # auto_transitions is off so that the table above is the only way to
        # move — a generated to_CRITICAL() would bypass every rule in it.
        self._machine = Machine(
            model=self,
            states=list(MissionState),
            transitions=TRANSITIONS,
            initial=MissionState.BOOT,
            auto_transitions=False,
            ignore_invalid_triggers=True,
            after_state_change="_announce",
        )

    def fire(self, trigger: str) -> bool:
        """Attempt a transition. Returns whether it was accepted.

        Legality is asked before the attempt rather than after: ``transitions``
        logs a warning of its own for a trigger it refuses, and a refusal here is
        routine — a ground command that does not apply in the current state. A
        WARNING for that would train whoever reads the log to ignore warnings.
        """
        previous = self.state
        if not self.may_trigger(trigger):
            self.log.info("%s refused in %s", trigger, previous.value)
            return False
        self._previous = previous
        getattr(self, trigger)()
        return True

    def _announce(self) -> None:
        self.log.info("mission state %s -> %s", self._previous.value, self.state.value)
        if self._on_change is not None:
            self._on_change(self._previous, self.state)
