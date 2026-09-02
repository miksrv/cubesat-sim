"""What the battery level means. Pure functions, no I/O, no MQTT.

EPS publishes numbers and makes no decisions; every threshold in the system
lives here, in one place, so that "what happens at 38 %" has exactly one answer
and that answer is testable against every state without a broker.

The thresholds themselves are not arbitrary. The unit runs off an X728 UPS in
``EXPO`` and ``FLIGHT``, and a Raspberry Pi that browns out mid-write risks the
SD card — so there has to be a level at which the satellite stops working and
saves itself, and a level below that at which it powers the host down while it
still can.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cubesat.common.states import MissionState

#: Throttle everything: stretch the poll intervals, refuse the camera.
#:
#: Lowered from 40 % on 2026-09-02. 40 % of an 18650 pair is a long way from an
#: emergency, and throttling there cost the most interesting part of a trip — the
#: second half of it — for no gain: the descent that actually protects the card
#: is SAFE at 20 % and CRITICAL at 10 %, and both are still where they were. What
#: 30 % buys is a wider band of full-rate recording before anything is given up.
LOW_POWER_PERCENT = 30.0
#: Sensors only, radio quiet.
SAFE_PERCENT = 20.0
#: Flush the recorder and power the host off while that is still possible.
CRITICAL_PERCENT = 10.0
#: Below this, a pack is going down even if the mains pin says it is plugged in —
#: a faulty charger, a dead barrel jack, or a PLD pin that is simply lying. The
#: threshold is not zero because the rate is fitted to a quantised reading and
#: carries a few hundredths of noise, and a pack can dip slightly for minutes
#: after a plug-in; treating either as a mains failure would throw away the
#: protection it is meant to preserve.
DRAINING_PERCENT_PER_HOUR = -1.0

#: Climb back out at 40 %, ten points above the level that got us here.
#:
#: The band is the whole point. Recovering at the same level that triggers
#: LOW_POWER makes the state flap every time the reading crosses it, and the
#: pre-rewrite handler avoided that by only ever recovering on external power —
#: which meant that on battery, in FLIGHT, the satellite could never recover at
#: all no matter how much the pack had charged.
#:
#: Ten points, and it tracked LOW_POWER down when that moved from 40 % to 30 %
#: (2026-09-02). The alternative was leaving it at 50 % and living with a
#: twenty-point band, which on this pack means recovery that never arrives on a
#: walk: the descent is minutes of load, the climb back is a charger the X728 is
#: not currently delivering (V13). A band wide enough to be unreachable is not
#: hysteresis, it is a one-way door.
RECOVERY_PERCENT = 40.0

#: LOW_POWER only applies where power is actually being spent. STANDBY is
#: already idle, and throttling it would change nothing.
LOW_POWER_SOURCES = frozenset({MissionState.DEPLOY, MissionState.NOMINAL})
#: States a healthy battery can climb out of. CRITICAL is not among them: the
#: poweroff is already in flight, and reversing that decision half-way is worse
#: than completing it.
RECOVERABLE = frozenset({MissionState.LOW_POWER, MissionState.SAFE})


@dataclass(frozen=True)
class PowerReading:
    """The part of an ``eps_status`` payload the policy actually decides on."""

    battery_percent: float
    external_power: bool
    #: Signed percent per hour, as EPS publishes it: positive charging, negative
    #: draining, None while EPS has too little history to say (its first minutes,
    #: and the minutes after the mains pin changed).
    charge_rate: float | None = None

    @property
    def on_mains(self) -> bool:
        """Whether the satellite is *effectively* on mains.

        Not just the PLD pin. The pin alone would let one failure mode disable
        every protection below: a charger that has stopped charging still reads
        as external power, and a pack that keeps falling would then never reach
        CRITICAL. The charge rate is the second opinion — fitted by EPS to the
        state-of-charge history, because this gauge measures no rate itself.

        A missing rate falls back to trusting the pin: EPS that has not yet seen
        enough history must not cause a plugged-in satellite to power itself off.
        """
        if not self.external_power:
            return False
        return self.charge_rate is None or self.charge_rate > DRAINING_PERCENT_PER_HOUR


def reading_from(payload: dict[str, Any]) -> PowerReading | None:
    """Extract a reading, or None if the payload carries no usable battery level.

    ``None`` means "no verdict is possible", which is very different from "the
    battery is fine": a gauge that has stopped answering must not read as 0 % and
    power the satellite off, nor as 100 % and keep it running.

    One spelling of the battery field, not two. Accepting an alias as well would
    mean a typo in a publisher still worked here and stopped working somewhere
    else, which is the kind of divergence that survives review.
    """
    raw = payload.get("battery_percent")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    rate = payload.get("charge_rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        rate = None
    return PowerReading(
        battery_percent=float(raw),
        external_power=bool(payload.get("external_power", False)),
        charge_rate=None if rate is None else float(rate),
    )


def evaluate(reading: PowerReading, state: MissionState) -> MissionState | None:
    """The state this reading calls for, or None to stay where we are.

    Ordered from the most severe downwards, so a battery that has fallen through
    two thresholds between two EPS messages lands on the lower one.
    """
    battery = reading.battery_percent

    if reading.on_mains:
        # On mains there is no power emergency of any kind: the pack cannot die,
        # so there is nothing for CRITICAL to save the SD card from and nothing
        # for LOW_POWER to stretch. Skipping the descents here is not a
        # convenience — CRITICAL on mains is actively harmful. The satellite comes
        # home with a flat pack, is plugged in, reports 5 %, powers the host off —
        # and the X728 never brings it back, because mains never left. The normal
        # recovery gesture would brick the unit until someone pulled the plug.
        return MissionState.NOMINAL if state in RECOVERABLE else None

    if battery < CRITICAL_PERCENT:
        return None if state is MissionState.CRITICAL else MissionState.CRITICAL
    if battery < SAFE_PERCENT:
        return None if state in (MissionState.SAFE, MissionState.CRITICAL) else MissionState.SAFE

    # Mains is handled above, in one place, so the descent and the recovery
    # cannot disagree about what "plugged in" means — if they could, the state
    # would flap between them on every EPS message in the band where they differ.
    if state in RECOVERABLE:
        return MissionState.NOMINAL if battery >= RECOVERY_PERCENT else None

    if battery < LOW_POWER_PERCENT and state in LOW_POWER_SOURCES:
        return MissionState.LOW_POWER
    return None
