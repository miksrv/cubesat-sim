"""OBC — the On-Board Computer. The head, with no hands.

OBC owns both state machines, decides what the platform should look like, and
publishes that intent. It never touches the host: switching a profile means
``systemctl``, ``nmcli``, the CPU governor and ``poweroff``, all of which need
root, and flight software does not get root. HOSTD executes; OBC decides. The
consequence worth having is that every decision in this file is testable on a
laptop with no privileges and no hardware.

This module is wiring. The decisions live next door and are unit-tested there:

    mission_machine.py   the legal moves between mission states
    profile_machine.py   request a profile, reconcile what HOSTD achieved
    power_policy.py      what a battery percentage means, in one place
    deploy.py            the bring-up self-test
    health.py            who is still alive
    commands.py          parsing what the ground sent

OBC publishes the mission state, so it deliberately does not absorb it: with
``track_mission_state`` left on, the base class would feed OBC's own retained
status back into it and the machine would be reading its own output.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from cubesat.common import profiles as profiles_module
from cubesat.common.profiles import ProfileConfig
from cubesat.common.service import Service
from cubesat.common.states import EndReason, MissionMode, MissionState, Persistence, Profile
from cubesat.common.topics import TOPICS
from cubesat.hal import i2c
from cubesat.obc import commands, deploy, mission_machine, power_policy
from cubesat.obc.health import HealthMonitor
from cubesat.obc.profile_machine import ProfileMachine, ProfileUpdate

#: How long CRITICAL waits for DHS to close its mission before powering off.
#:
#: A bound, not a handshake: a profile with no persistence never started DHS at
#: all, and hanging until a service that was never launched answers would leave
#: the Pi running at under 10 % battery — the exact situation the state exists to
#: get out of before the SD card pays for it.
CRITICAL_FLUSH_GRACE_SEC = 10.0

#: Asked of HOSTD on entering LOW_POWER. The way back out is the active profile's
#: own ``power.governor``, never a second hardcoded name here — FLIGHT already
#: runs `powersave` nominally, and DIAG deliberately runs `performance`.
LOW_POWER_GOVERNOR = "powersave"

#: Which service a status message is evidence for during DEPLOY. Built from the
#: table in deploy.py so the two cannot drift apart.
REPORTER_FOR_TOPIC: dict[str, str] = {
    TOPICS[key]: service for service, key in deploy.REPORT_TOPICS.items()
}


class ObcService(Service):
    name = "obc"
    cadence_key = "obc"
    #: See the module docstring: OBC publishes the mission state.
    track_mission_state = False
    #: The mission services' own status topics are here because DEPLOY's evidence
    #: is a status message, not a heartbeat — see deploy.py. Parsing adcs_status
    #: at 2 Hz costs microseconds on a Pi 4; a bring-up that passes with a dead
    #: sensor costs a demonstration.
    subscriptions = (
        "command",
        "eps_status",
        "host_status",
        "heartbeat",
        "adcs_status",
        "payload_status",
        "dhs_status",
        "comms_status",
    )

    def __init__(
        self,
        profiles: ProfileConfig | None = None,
        *,
        bus: i2c.I2CBus | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._profiles = profiles if profiles is not None else profiles_module.load()
        self._clock = clock
        self._bus = bus
        # Messages arrive on paho's network thread while tick() runs on the main
        # one. Both move the state machine, so every decision below is taken
        # under one lock — two threads racing a transition is how a satellite
        # ends up publishing SAFE and then NOMINAL out of order.
        self._lock = threading.RLock()

        self.mission = mission_machine.MissionMachine(on_change=self._on_state_change)
        # Not `self.profile`: the base class already owns that name for the
        # active Profile enum, and shadowing it with a machine of a different
        # type is a collision waiting for the first line of shared code that
        # reads it. OBC does not absorb its own status, so nothing breaks today —
        # which is exactly why it would go unnoticed.
        self.profile_machine = ProfileMachine(
            self._profiles,
            self._request_apply,
            clock=clock,
            wall_clock=wall_clock,
            log=self.log,
        )
        self.health = HealthMonitor(clock=clock, log=self.log)
        self._deploy: deploy.DeploySelfTest | None = None
        #: Set when SAFE was entered for something charging the battery will not
        #: fix — a ground command, or a subsystem that stopped answering.
        self._fault_latched = False
        #: Last known DHS recording flag; None until DHS says anything.
        self._dhs_recording: bool | None = None
        #: Whether OBC has asked HOSTD to drop the CPU governor for LOW_POWER.
        self._governor_lowered = False
        self._flushed = threading.Event()
        self._flush_thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_start(self) -> None:
        with self._lock:
            # EPS is watched from the outset: it runs in every profile, and it is
            # the only source of the telemetry that drives CRITICAL.
            self.health.watch(())
            # Nothing is self-tested here. DEPLOY is the self-test, and it can
            # only run once a profile has said which hardware this run should
            # have — which OBC learns from the retained host_status, not from a
            # file of its own.
            self.mission.fire(mission_machine.BOOT_COMPLETE)

    def tick(self) -> None:
        with self._lock:
            self._publish_status()
            self.profile_machine.request_default_on_expiry()
            self._reconcile()

    # ── inbound ─────────────────────────────────────────────────────────────

    def on_message(self, topic: str, data: dict[str, Any]) -> None:
        with self._lock:
            if topic == TOPICS["command"]:
                self._on_command(data)
            elif topic == TOPICS["eps_status"]:
                self._on_eps_status(data)
            elif topic == TOPICS["host_status"]:
                self._on_host_status(data)
            elif topic == TOPICS["heartbeat"]:
                self._on_heartbeat(data)
            elif topic == TOPICS["dhs_status"]:
                self._on_dhs_status(data)
            elif topic == TOPICS["adcs_status"]:
                self._on_adcs_status(data)
            self._note_report(topic)
            # Reconciling on every inbound message, not only on the tick: OBC's
            # own cadence is 30 s, and a DEPLOY verdict or a lost subsystem
            # should not wait that long when the evidence has already arrived.
            self._reconcile()

    def _on_command(self, data: dict[str, Any]) -> None:
        command = commands.parse(data)
        if command is None:
            # PAYLOAD's and COMMS' commands share this topic. Not ours, not a
            # problem — see commands.py.
            return
        self.log.info("command %s (request_id=%s)", command.name, command.request_id)
        if command.name == commands.SET_PROFILE:
            self._set_profile(command)
        elif command.name == commands.SCIENCE_START:
            self.mission.fire(mission_machine.SCIENCE_START)
        elif command.name == commands.SCIENCE_STOP:
            self.mission.fire(mission_machine.SCIENCE_STOP)
        elif command.name == commands.SAFE_MODE:
            self._enter_safe("ground command", latch=True)
        elif command.name == commands.RESTART_SERVICE:
            self._restart_service(command)
        else:
            self._recover()

    def _set_profile(self, command: commands.Command) -> None:
        request = commands.profile_request(command)
        if request is None:
            self.log.error("set_profile without a usable profile name: %r", command.params)
            return
        self.profile_machine.request(
            request.profile,
            ttl_minutes=request.ttl_minutes,
            mission_label=request.mission_label,
            request_id=command.request_id,
        )

    def _restart_service(self, command: commands.Command) -> None:
        """Relay a restart to HOSTD, which owns both the privilege and the fence.

        OBC adds no check of its own here, and that is deliberate: which services
        exist is `KNOWN_SERVICES`, which units may be touched is the allowlist,
        and both live on HOSTD's side. A second copy of either would be a second
        thing to keep in step — and the one that gets forgotten is always the one
        that mattered.

        What OBC does add is the privilege boundary itself. `cubesat/host/command`
        is root's inbox and the browser ACL denies it, so a ground client cannot
        ask HOSTD directly; it asks here, on the topic every other command uses.

        And it adds the one thing only OBC knows: that the departure about to
        arrive was asked for. The restarted service says goodbye on its way out,
        `_check_health` read that as a lost subsystem and latched `SAFE` until a
        ground `recover` — which defeated the command's whole purpose, restarting
        one service without taking the dashboard from a room (found on the
        hardware, 2026-09-01). `health.expect_restart` waives that one goodbye for
        one loss grace. It is armed **before** the relay because the goodbye can
        arrive ahead of anything HOSTD says back, which is the race itself.
        """
        request = commands.restart_request(command)
        if request is None:
            self.log.error("restart_service without a service name: %r", command.params)
            return
        self.health.expect_restart(request.service)
        self.log.info(
            "relaying restart of %s to HOSTD (request_id=%s)", request.service, command.request_id
        )
        self.publish(
            "host_command",
            qos=1,
            action="restart_service",
            request_id=command.request_id,
            params={"service": request.service},
        )

    def _on_eps_status(self, data: dict[str, Any]) -> None:
        reading = power_policy.reading_from(data)
        if reading is None:
            self.log.warning("eps_status carries no battery level; no power verdict")
            return
        target = power_policy.evaluate(reading, self.mission.state)
        if target is None:
            return
        self.log.info(
            "battery %.1f%% (external_power=%s) calls for %s",
            reading.battery_percent,
            reading.external_power,
            target.value,
        )
        if target is MissionState.CRITICAL:
            self._enter_critical()
        elif target is MissionState.SAFE:
            # A power SAFE is not latched: it is cured by the very thing the
            # policy is watching, so it recovers on its own once the pack does.
            self._enter_safe("battery below the safe threshold", latch=False)
        elif target is MissionState.LOW_POWER:
            self.mission.fire(mission_machine.ENTER_LOW_POWER)
        else:
            self._recover(automatic=True)

    def _on_host_status(self, data: dict[str, Any]) -> None:
        update = self.profile_machine.observe(data)
        if update is not None:
            self._apply_profile_update(update)

    def _note_report(self, topic: str) -> None:
        """Count a subsystem's own status message towards the bring-up."""
        service = REPORTER_FOR_TOPIC.get(topic)
        if service is not None and self._deploy is not None:
            self._deploy.note_report(service)

    def _on_heartbeat(self, data: dict[str, Any]) -> None:
        # Liveness only. A heartbeat says the process is running, which is not
        # what DEPLOY needs to know — see deploy.py.
        self.health.note(data)

    def _on_dhs_status(self, data: dict[str, Any]) -> None:
        recording = data.get("recording")
        if isinstance(recording, bool):
            self._dhs_recording = recording
            if not recording:
                self._flushed.set()

    def _on_adcs_status(self, data: dict[str, Any]) -> None:
        # ADCS owns the receiver; OBC only reads what ADCS reports about it and
        # never touches the device. A missing fix never fails the bring-up,
        # because indoors there will never be one.
        gnss = data.get("gnss")
        if self._deploy is not None and isinstance(gnss, dict):
            self._deploy.note_gnss(bool(gnss.get("fix")))

    # ── the profile ─────────────────────────────────────────────────────────

    def _request_apply(
        self, profile: Profile, request_id: str | None, ttl_minutes: int | None
    ) -> None:
        self.publish(
            "host_command",
            qos=1,
            action="apply_profile",
            profile=profile.value,
            request_id=request_id,
            ttl_minutes=ttl_minutes,
        )

    def _apply_profile_update(self, update: ProfileUpdate) -> None:
        # Every service derives its poll interval from this, so it has to follow
        # the profile even though OBC's own cadence is state-independent.
        self.cadence_scale = update.spec.power.cadence_scale
        if not update.changed:
            self._publish_status()
            return

        self.health.watch(update.spec.services)
        self._deploy = None
        self._fault_latched = False
        # apply_profile carries the new profile's governor, so there is nothing
        # to restore afterwards — only the record of having lowered it to clear.
        self._governor_lowered = False
        # Every profile change starts from STANDBY, including a change from one
        # active profile to another: DHS closes its mission on the way out, and
        # the new profile's hardware has not been self-tested yet.
        self.mission.fire(mission_machine.STAND_DOWN)
        self._publish_status()

        if update.active and update.matches_request:
            self._begin_deploy(update)
        elif update.active:
            self.log.error(
                "not deploying into %s: it is not the profile OBC asked for",
                update.achieved.value,
            )

    def _begin_deploy(self, update: ProfileUpdate) -> None:
        check = deploy.DeploySelfTest(update.spec, bus=self._bus, clock=self._clock)
        if not self.mission.fire(mission_machine.BEGIN_DEPLOY):
            return
        self._deploy = check
        check.begin()
        self._reconcile()

    # ── periodic reconciliation ─────────────────────────────────────────────

    def _reconcile(self) -> None:
        self._check_deploy()
        self._check_health()

    def _check_deploy(self) -> None:
        check = self._deploy
        if check is None or self.mission.state is not MissionState.DEPLOY:
            return
        outcome = check.evaluate()
        if outcome is deploy.Outcome.PENDING:
            return
        check.log_gnss()
        self._deploy = None
        if outcome is deploy.Outcome.PASSED:
            self.mission.fire(mission_machine.DEPLOY_COMPLETE)
            return
        for reason in check.failures:
            self.log.error("DEPLOY failed: %s", reason)
        self._enter_safe("deploy self-test failed", latch=True)

    def _check_health(self) -> None:
        if self.mission.state in (MissionState.BOOT, MissionState.SAFE, MissionState.CRITICAL):
            return
        if self.profile_machine.settling:
            # HOSTD is stopping and starting units on OBC's own request right
            # now: a goodbye arriving mid-switch is the switch, not a fault.
            # The first hardware run latched SAFE on exactly this — ADCS's last
            # will beat host_status by four seconds (2026-08-28).
            return
        lost = self.health.lost()
        if lost:
            self._enter_safe(f"subsystem(s) lost: {', '.join(lost)}", latch=True)

    # ── descent and recovery ────────────────────────────────────────────────

    def _enter_safe(self, reason: str, *, latch: bool) -> None:
        if self.mission.fire(mission_machine.ENTER_SAFE):
            self.log.warning("entering SAFE: %s", reason)
        # Latch even if the transition was refused (we may already be in SAFE
        # for a milder reason): the fault is real either way, and forgetting it
        # is how a charging battery quietly clears a lost subsystem.
        self._fault_latched = self._fault_latched or latch

    def _recover(self, *, automatic: bool = False) -> None:
        if automatic and self._fault_latched:
            # The battery is fine, but what put us here was not the battery. A
            # ground `recover` is the only thing that clears that.
            self.log.info("power recovery withheld: SAFE is latched by a fault")
            return
        self._fault_latched = False
        spec = self.profile_machine.spec
        if spec is not None and spec.mission is not MissionMode.ACTIVE:
            # Recovered inside a profile that asks for no mission is idle, not
            # flying: a HOSTED satellite brought out of SAFE must land in
            # STANDBY — NOMINAL there would beacon every minute on a desk that
            # asked for silence (bench-found 2026-08-28, the first `recover`
            # ever sent to the hardware).
            self.mission.fire(mission_machine.STAND_DOWN)
            return
        self.mission.fire(mission_machine.RECOVER)

    def _enter_critical(self) -> None:
        if not self.mission.fire(mission_machine.ENTER_CRITICAL):
            return
        self._flushed.clear()
        # In its own thread: the poweroff must not wait for OBC's 30 s tick, and
        # the grace must not block the message thread that is going to deliver
        # the very dhs_status it is waiting for.
        self._flush_thread = threading.Thread(
            target=self._flush_then_poweroff, name="critical-flush", daemon=True
        )
        self._flush_thread.start()

    def _flush_then_poweroff(self) -> None:
        if self._awaiting_flush():
            if self._flushed.wait(CRITICAL_FLUSH_GRACE_SEC):
                self.log.info("DHS closed its mission; powering off")
            else:
                self.log.warning(
                    "DHS did not report recording:false within %.0fs; powering off anyway",
                    CRITICAL_FLUSH_GRACE_SEC,
                )
        # No new command for this: DHS closes its mission on seeing CRITICAL in
        # the retained obc_status. State-driven is the point of that topic.
        self.publish(
            "host_command",
            qos=1,
            action="poweroff",
            params={"reason": EndReason.BATTERY_CRITICAL.value},
        )

    def _awaiting_flush(self) -> bool:
        spec = self.profile_machine.spec
        if spec is None or "dhs" not in spec.services:
            # A profile that never started a recorder has nothing to flush.
            return False
        return self._dhs_recording is not False

    # ── outbound status ─────────────────────────────────────────────────────

    def _on_state_change(self, _previous: MissionState, current: MissionState) -> None:
        self._publish_status()
        self._reconcile_governor(current)

    def _reconcile_governor(self, current: MissionState) -> None:
        """Ask HOSTD for the CPU governor this state wants.

        Tracked as a flag rather than keyed on the previous state, because the
        way out of LOW_POWER is not always direct: a battery that keeps falling
        goes LOW_POWER -> SAFE, and the recovery from there lands in NOMINAL
        having never passed back through LOW_POWER.
        """
        if current is MissionState.LOW_POWER and not self._governor_lowered:
            self._governor_lowered = True
            self._request_governor(LOW_POWER_GOVERNOR)
        elif current is MissionState.NOMINAL and self._governor_lowered:
            self._governor_lowered = False
            spec = self.profile_machine.spec
            if spec is not None:
                self._request_governor(spec.power.governor)

    def _request_governor(self, governor: str) -> None:
        self.publish(
            "host_command", qos=1, action="set_governor", params={"governor": governor}
        )

    def _publish_status(self) -> None:
        """Publish the retained status: one message, everything a subsystem needs.

        ``persistence`` and ``mission_label`` are here so DHS can open a mission
        from this alone, and ``cadence_scale`` so every service can derive its own
        poll interval — no second channel, no request/response at startup.

        Before HOSTD's retained ``host_status`` has been seen, the profile is
        genuinely unknown and is published as null with persistence ``none``.
        Naming the default profile here instead would be a guess, and a guess
        that happens to permit writing telemetry rows against the wrong profile.

        ``subsystems`` is OBC's own health verdict, published so the ground can
        tell "off because the profile never started it" from "expected and
        silent" — without it a dashboard has to guess, and both guesses are
        wrong somewhere: FAIL on a service HOSTED never launched, or a grey
        shrug on a service that died. ``lost`` is empty while a profile switch
        is settling, for the same reason _check_health suppresses the verdict
        then: a goodbye arriving mid-switch is the switch, not a fault.
        """
        spec = self.profile_machine.spec
        self.publish(
            "obc_status",
            qos=1,
            status=self.mission.state.value,
            profile=spec.name.value if spec else None,
            cadence_scale=spec.power.cadence_scale if spec else 1.0,
            persistence=(spec.persistence if spec else Persistence.NONE).value,
            mission_label=self.profile_machine.label,
            subsystems={
                "watched": sorted(self.health.watched),
                "lost": [] if self.profile_machine.settling else list(self.health.lost()),
            },
        )
