"""What the battery level means. Pure functions, no I/O, no MQTT.

EPS publishes numbers and makes no decisions; every threshold in the system
lives here, in one place, so that "what happens at 3.6 V" has exactly one answer
and that answer is testable against every state without a broker.

**The level is a voltage** (2026-09-04). It used to be the fuel gauge's
percentage, until that gauge was identified as a MAX17040/41 whose state of
charge is a model rather than a measurement. Percentages still exist — EPS
derives one through ``common/battery.py`` for the dashboard, the beacon and the
log line below — but nothing in this file compares one, and nothing should.

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

#: **Every threshold here is in volts** (2026-09-04), and that is the whole
#: point of this file's second version.
#:
#: They were percentages for as long as the satellite believed its fuel gauge.
#: The gauge is a MAX17040/41 with no shunt and no coulomb counter: its state of
#: charge is reconstructed from an internal model, and that model was measured
#: falling at 8–10 %/h for an hour while the satellite sat on mains with the
#: terminal voltage flat to the millivolt (2026-09-03,
#: ``docs/hardware-x728-ups-hat.md``). Patching the mains test kept that number
#: out of the *mains* decision, but SAFE and CRITICAL still hung on it, so the
#: two thresholds that exist to protect the SD card and the filesystem were the
#: two still deciding on an unmeasured quantity.
#:
#: So the descents now compare the voltage, which is what ``VCELL`` reports at
#: 1.25 mV per LSB. The percentages below each threshold are what
#: ``common/battery.py`` maps that voltage to; they are written here as a
#: courtesy to whoever remembers the old numbers, and they are **inferred from a
#: generic Li-ion curve, not measured on this pack** — which is why the curve is
#: not in the path of the comparison. One discharge log replaces the annotation
#: without touching the behaviour (ROADMAP V15).
#:
#: Throttle everything: stretch the poll intervals, refuse the camera. Was 40 %,
#: lowered to 30 % on 2026-09-02, because throttling there cost the most
#: interesting part of a trip — the second half of it — for no gain: the descents
#: that actually protect the card are the two below.
LOW_POWER_VOLTS = 3.64  # ≈ 30 %
#: Sensors only, radio quiet.
SAFE_VOLTS = 3.58  # ≈ 20 %
#: Flush the recorder and power the host off while that is still possible.
#:
#: The margin under it is what matters, and it is generous: the X728 cuts its own
#: output at 3.0 V, and from 3.45 V the measured idle discharge of −197 mV/h
#: (2026-09-03) leaves over two hours, a heavy profile well under one. Either is
#: an order of magnitude more than a flush and a poweroff need. Do not chase the
#: last few percent by lowering this — the point of CRITICAL is to spend charge
#: on shutting down cleanly rather than on staying up.
CRITICAL_VOLTS = 3.45  # ≈ 10 %

#: Below this, the terminal voltage is falling: the pack is actually delivering
#: current. **Measured on the satellite 2026-09-03** in HOSTED, one series
#: across a deliberate unplug and back (see ``docs/hardware-x728-ups-hat.md``):
#:
#: * on mains, the voltage held 3.806–3.809 V — a slope of **0 mV/h** — while the
#:   gauge's modelled SOC drifted down at 8–10 %/h;
#: * unplugged, it dropped **50 mV inside one publish** and then fell at
#:   **−197 mV/h** at idle load, with SOC at −24.5 %/h.
#:
#: So the regimes separate by two orders of magnitude in this quantity and barely
#: at all in the other one: −8 %/h and −24 %/h are both under the percentage
#: threshold, which is precisely why that threshold could not tell a desk from a
#: dying pack.
#:
#: −30 mV/h sits between them with margin both ways: four times the fitting noise
#: (one 1.25 mV VCELL step across the 600 s window is ±7.5 mV/h) and six times
#: below the idle discharge it must catch. A heavier profile drains faster and is
#: caught sooner, never later; a charger delivering exactly the load holds the
#: voltage flat, and then there is nothing to catch.
DRAINING_MV_PER_HOUR = -30.0

#: Climb back out at 3.75 V, a 110 mV band above the level that got us here.
#:
#: The band is the whole point. Recovering at the same level that triggers
#: LOW_POWER makes the state flap every time the reading crosses it, and the
#: pre-rewrite handler avoided that by only ever recovering on external power —
#: which meant that on battery, in FLIGHT, the satellite could never recover at
#: all no matter how much the pack had charged.
#:
#: **It is wider in volts than the ten points it replaces** (2026-09-04), and
#: deliberately. Ten points on the plateau is about 60 mV, while the load moving
#: on or off the pack is worth 50 mV all by itself — measured at the unplug on
#: 2026-09-03, and the same order as the camera pipeline starting. A percentage
#: from a slow internal model absorbed that; a voltage does not, so hysteresis
#: has to clear the load swing rather than the fitting noise. 110 mV is twice it.
#: EPS smoothing (``eps/slopes.py`` → ``MedianWindow``) handles the transients;
#: this handles the steady-state difference between one profile and another.
#:
#: The opposite failure is real too, and is why this is not wider still: a band
#: wide enough to be unreachable is not hysteresis, it is a one-way door, and on
#: this pack the climb back is a charger the X728 is not currently delivering
#: (V13).
RECOVERY_VOLTS = 3.75  # ≈ 48 %

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

    #: Terminal volts, and the only level this policy compares. EPS publishes a
    #: median over a short window as ``voltage_median`` and the raw sample as
    #: ``voltage``; ``reading_from`` prefers the median, because a threshold in
    #: volts is sensitive to load transients in a way a threshold in modelled
    #: percent was not.
    voltage: float
    external_power: bool
    #: Signed millivolts per hour, fitted by EPS over its window. None while
    #: there is too little history to say — its first minutes, and the minutes
    #: after the mains pin changed.
    voltage_rate: float | None = None
    #: Carried for the log line and for nothing else, and None when EPS has not
    #: said. It is derived from ``voltage`` through the pack curve, so it cannot
    #: disagree with the decision — and it is not consulted anyway. Do not add a
    #: threshold on it: that is the arrangement this file spent two revisions
    #: getting out of.
    battery_percent: float | None = None

    @property
    def on_mains(self) -> bool:
        """Whether the satellite is *effectively* on mains.

        Not just the PLD pin. The pin alone would let one failure mode disable
        every protection below: a charger that has stopped charging still reads
        as external power, and a pack that keeps falling would then never reach
        CRITICAL. So a second opinion is needed — but it has to be one that
        measures the pack rather than a model of it.

        **The voltage decides, and it decides alone** (2026-09-04). The
        percentage alone held this job until 2026-09-03, when it was measured on
        the hardware and found to be wrong in the dangerous direction: this gauge
        computes state of charge from a model with no current sense at all, and
        that model was watched drifting down at 8–10 %/h for an hour while the
        satellite sat plugged in with its charge LEDs lit and its terminal
        voltage flat to the millivolt. ``on_mains`` was therefore False on a
        desk, and the descents below — neither of which asks what state it is in
        — were hours away from powering off a satellite that was on mains the
        whole time. That is the exact scenario the comment in ``evaluate`` calls
        actively harmful, arrived at from the opposite side.

        The first fix required **both** slopes to agree, the measured one and the
        modelled one, on the reasoning that a genuinely failed charger moves both
        while a settling model moves only one. That reasoning was sound and the
        arrangement lasted a day: making the percentage a function of the voltage
        (``common/battery.py``) makes its slope a function of the voltage slope,
        so "both agree" became a sentence that could not be false. A fence that
        cannot fail is not a fence, and leaving it in place would have left two
        conditions in the code and one in reality — the kind of gap somebody
        later reasons from. So there is one condition, and it is the measured one.

        What has not changed is the reason for having a second opinion at all: a
        charger that has stopped charging still reads as external power, and the
        pin alone would let that one failure disable every protection below.

        A missing slope falls back to trusting the pin: EPS that has not yet seen
        enough history — its first five minutes, and the five after the pin
        changed — must not cause a plugged-in satellite to power itself off.
        """
        if not self.external_power:
            return False
        falling = self.voltage_rate is not None and self.voltage_rate <= DRAINING_MV_PER_HOUR
        return not falling


def _number(payload: dict[str, Any], key: str) -> float | None:
    """One numeric field, or None if it is absent or not a number.

    ``bool`` is rejected explicitly because it is an ``int`` in Python, and a
    ``True`` arriving where volts belong would otherwise read as 1.0 V and put
    the satellite into CRITICAL.
    """
    raw = payload.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def reading_from(payload: dict[str, Any]) -> PowerReading | None:
    """Extract a reading, or None if the payload carries no usable voltage.

    ``None`` means "no verdict is possible", which is very different from "the
    battery is fine": a gauge that has stopped answering must not read as 0 V and
    power the satellite off, nor as full and keep it running.

    **The required field is the voltage** (2026-09-04, it was the percentage
    before). That is what every threshold compares, so a payload without it
    carries no verdict — while a payload without the percentage is merely a
    payload with nothing to write in the log line.

    The level preferred is ``voltage_median``, EPS' median over a short window,
    falling back to the raw ``voltage`` for its first ticks and for an older EPS
    that publishes only the sample. The fallback is deliberately the raw value
    rather than no verdict: one un-smoothed sample is still a measurement of the
    pack, and the alternative is a satellite with no power protection for the
    first two minutes after every start.

    One spelling per field, not two. Accepting an alias as well would mean a typo
    in a publisher still worked here and stopped working somewhere else, which is
    the kind of divergence that survives review.
    """
    volts = _number(payload, "voltage_median")
    if volts is None:
        volts = _number(payload, "voltage")
    if volts is None:
        return None
    # An older EPS publishes no voltage_rate at all, and a satellite mid-upgrade
    # runs one service ahead of another. Absent reads as "not known yet", which
    # is the same fallback as too little history: the pin is trusted, and one
    # sample cannot condemn a plugged-in pack.
    return PowerReading(
        voltage=volts,
        external_power=bool(payload.get("external_power", False)),
        voltage_rate=_number(payload, "voltage_rate"),
        battery_percent=_number(payload, "battery_percent"),
    )


def evaluate(reading: PowerReading, state: MissionState) -> MissionState | None:
    """The state this reading calls for, or None to stay where we are.

    Ordered from the most severe downwards, so a battery that has fallen through
    two thresholds between two EPS messages lands on the lower one.
    """
    volts = reading.voltage

    if reading.on_mains:
        # On mains there is no power emergency of any kind: the pack cannot die,
        # so there is nothing for CRITICAL to save the SD card from and nothing
        # for LOW_POWER to stretch. Skipping the descents here is not a
        # convenience — CRITICAL on mains is actively harmful. The satellite comes
        # home with a flat pack, is plugged in, reads 3.3 V, powers the host off —
        # and the X728 never brings it back, because mains never left. The normal
        # recovery gesture would brick the unit until someone pulled the plug.
        return MissionState.NOMINAL if state in RECOVERABLE else None

    if volts < CRITICAL_VOLTS:
        return None if state is MissionState.CRITICAL else MissionState.CRITICAL
    if volts < SAFE_VOLTS:
        return None if state in (MissionState.SAFE, MissionState.CRITICAL) else MissionState.SAFE

    # Mains is handled above, in one place, so the descent and the recovery
    # cannot disagree about what "plugged in" means — if they could, the state
    # would flap between them on every EPS message in the band where they differ.
    if state in RECOVERABLE:
        return MissionState.NOMINAL if volts >= RECOVERY_VOLTS else None

    if volts < LOW_POWER_VOLTS and state in LOW_POWER_SOURCES:
        return MissionState.LOW_POWER
    return None
