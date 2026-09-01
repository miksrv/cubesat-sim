"""HOSTD — the hands. Root, and no opinions.

Everything that decides runs unprivileged in OBC. HOSTD consumes a fixed
vocabulary of three actions, executes them in a fixed order, and reports what it
actually achieved. There is no policy in this file: no thresholds, no state
machine, no notion of what a battery percentage or an expired TTL means. If
something here starts looking like a decision, it belongs next door.

    apply_profile   the whole sequence: stop, network, start, governor, note, report
    set_governor    the governor only, for LOW_POWER without a profile change
    poweroff        requested by OBC on CRITICAL, and by nothing else

Three properties this file is responsible for:

* **``profile`` is what was achieved; ``profile_requested`` is what was asked
  for.** They differ when a profile applies partially, and that difference is
  the whole debugging story of a failed switch. A step that failed leaves
  ``profile`` reporting the last profile that fully applied — never the one that
  did not.
* **Applying the active profile again changes nothing.** Units already active
  are left alone; a restart in the middle of a demonstration would be a bug with
  an audience.
* **The last profile is written, never read back as instruction.** Every boot
  applies ``default_profile``. The satellite that hit ``CRITICAL`` on a trip and
  is plugged in at a desk hours later must come up on the home network with SSH
  reachable — see ``docs/concept.md``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from cubesat.common import config
from cubesat.common import profiles as profiles_module
from cubesat.common.profiles import (
    KNOWN_SERVICES,
    NetworkSpec,
    ProfileConfig,
    ProfileError,
    ProfileSpec,
)
from cubesat.common.service import Service
from cubesat.common.states import NetworkMode, Profile
from cubesat.hostd import executor as executor_module
from cubesat.hostd.allowlist import DASHBOARD_UNIT, Allowlist, Refused, unit_for
from cubesat.hostd.control_socket import ControlSocket
from cubesat.hostd.executor import ACTIVE, INACTIVE, Executor, ExecutorError, HostActions
from cubesat.hostd.network import UNKNOWN_MODE, Network, NetworkState

#: The whole vocabulary. Anything else is logged, reported, and not acted on.
APPLY_PROFILE = "apply_profile"
SET_GOVERNOR = "set_governor"
POWEROFF = "poweroff"
#: Restart one mission service, by name. Added 2026-09-01 (ROADMAP R5).
#:
#: It is deliberately the narrowest of the four: the allowlist already bounds
#: which units exist at all, ``DENIED_UNITS`` already keeps `cubesat@obc`,
#: `mosquitto`, `cubesat-hostd` and `NetworkManager` out of reach, and this
#: action adds nothing to either. What it does add is a *reason to run one*
#: without applying a whole profile — re-applying a profile to restart one
#: service would also stop and start everything else the profile names, which in
#: `EXPO` means taking the dashboard away from a room full of people.
RESTART_SERVICE = "restart_service"


class _Outcome:
    """Accumulates what went wrong while an action ran.

    Two severities, because they mean different things to OBC: ``failed`` says
    the platform is not what was asked for, so the achieved profile must not
    claim success; ``noted`` says something went wrong that did not change the
    platform — writing the informational ``last-profile`` file, say.
    """

    def __init__(self, log: logging.Logger) -> None:
        self._log = log
        self.errors: list[str] = []
        self.achieved = True

    def failed(self, message: str) -> None:
        self._log.error(message)
        self.errors.append(message)
        self.achieved = False

    def noted(self, message: str) -> None:
        self._log.warning(message)
        self.errors.append(message)


class HostdService(Service):
    name = "hostd"
    #: No periodic work of its own: HOSTD acts on request and is silent in
    #: between. The retained ``host_status`` is what a late subscriber reads,
    #: and republishing it on a timer would say nothing new.
    cadence_key = None
    #: HOSTD does not care what the mission is doing, and must not start caring.
    #: The moment it reads ``obc_status``, the privilege split has a hole in it.
    track_mission_state = False
    subscriptions = ("host_command",)

    def __init__(
        self,
        profiles: ProfileConfig | None = None,
        executor: Executor | None = None,
        *,
        socket_path: Path | None = config.HOSTD_SOCKET,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._profiles = profiles if profiles is not None else profiles_module.load()
        self._allowlist = Allowlist.from_profiles(self._profiles)
        # Selected before anything else can be attempted, so an unprivileged
        # start fails here rather than after reporting a profile as applied.
        self._executor = executor if executor is not None else executor_module.select_executor()
        self._host = HostActions(self._executor, self._allowlist, log=self.log)
        self._network = Network(self._executor, self._allowlist, log=self.log)
        #: Wall clock: the TTL deadline crosses a process boundary as an absolute
        #: timestamp, and the DS1307 RTC keeps that honest with no network.
        self._clock = clock
        # Actions arrive on paho's thread and on the socket thread. One at a
        # time, always: two profiles applying at once would interleave
        # systemctl calls and report a platform that never existed.
        self._lock = threading.RLock()
        self._socket = (
            ControlSocket(socket_path, self.handle, log=self.log)
            if socket_path is not None
            else None
        )

        # Everything below is exactly what host_status reports.
        self._achieved: Profile | None = None
        self._requested: Any = None
        self._network_spec: NetworkSpec | None = None
        self._network_state = NetworkState(mode=UNKNOWN_MODE)
        self._governor: str | None = None
        self._unit_states: dict[str, str] = {}
        self._errors: tuple[str, ...] = ()
        self._ttl_expires_at: float | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_start(self) -> None:
        with self._lock:
            # Said at WARNING, and again here rather than only where the
            # executor was chosen: mistaking the mock for the real one is the
            # worst confusion this service can present, and the startup lines
            # are what an operator actually reads.
            self.log.warning("host executor: %s", type(self._executor).__name__)
            self.log.info("unit allowlist: %s", ", ".join(sorted(self._allowlist.permitted)))
            self.log.info("units a profile may ask for: %s",
                          ", ".join(sorted(self._allowlist.profile_units)))
            self._log_previous_profile()
            if self._socket is not None:
                self._socket.start()
            # The default profile, never the previous one. This is the decision
            # argued out in docs/concept.md: a boot means the situation is no
            # longer known, and HOSTED is the only safe assumption after an
            # unattended gap.
            self.handle({"action": APPLY_PROFILE, "profile": self._profiles.default.value})

    def on_stop(self) -> None:
        if self._socket is not None:
            self._socket.stop()

    def on_message(self, _topic: str, data: dict[str, Any]) -> None:
        # host_command is the only subscription and mission state is not
        # tracked, so anything arriving here is an action for us.
        self.handle(data)

    def _log_previous_profile(self) -> None:
        """Read ``last-profile`` for the log, and for nothing else.

        "What was it doing before it died?" deserves an answer; "what should it
        do now?" is answered by ``default_profile`` alone.
        """
        try:
            previous = config.LAST_PROFILE_FILE.read_text().strip()
        except OSError:
            previous = ""
        self.log.info(
            "previous run left profile %s; not restoring it — applying %s",
            previous or "(unrecorded)",
            self._profiles.default.value,
        )

    # ── the one entry point ─────────────────────────────────────────────────

    def handle(self, action: dict[str, Any]) -> dict[str, Any]:
        """Validate and execute one action object. Used by MQTT and the socket.

        One path, so the break-glass socket cannot be a way around a check —
        and so there is one place to read when asking what HOSTD can be told.
        """
        with self._lock:
            name = action.get("action")
            if name == APPLY_PROFILE:
                return self._apply_profile(action)
            if name == SET_GOVERNOR:
                return self._set_governor(action)
            if name == RESTART_SERVICE:
                return self._restart_service(action)
            if name == POWEROFF:
                return self._poweroff(action)
            self.log.error("unknown action %r; nothing was done", name)
            self._errors = (f"unknown action {name!r}",)
            self._publish()
            return self._result(ok=False)

    # ── apply_profile ───────────────────────────────────────────────────────

    def _apply_profile(self, action: dict[str, Any]) -> dict[str, Any]:
        requested = _field(action, "profile")
        try:
            spec = self._profiles.get(requested)
        except (ProfileError, TypeError) as exc:
            # Refused whole: nothing is stopped, started or reconfigured. An
            # unknown profile is not a reason to leave the platform half-way
            # between two known ones.
            self.log.error("refusing apply_profile: %s", exc)
            self._requested = requested
            self._errors = (str(exc),)
            self._publish()
            return self._result(ok=False)

        self.log.info(
            "applying profile %s (request_id=%s)", spec.name.value, action.get("request_id")
        )
        self._requested = spec.name.value
        out = _Outcome(self.log)
        wanted = self._wanted_units(spec)

        # 1. Stop first. The units being stopped hold the I2C bus, the camera and
        #    the radio that the incoming ones are about to want.
        for unit in sorted(self._allowlist.profile_units - wanted):
            self._stop_unit(unit, out)

        # 2. Then the network, before the services that will use it.
        self._switch_network(spec, out)

        # 3. Then what the profile asks for.
        for unit in sorted(wanted):
            self._start_unit(unit, out)

        # 4. The governor last: it is the one step that cannot fail in a way
        #    that changes what is running.
        self._apply_governor(spec.power.governor, out)

        # 5. Note it, for the log and for `cubesat status`. Not instruction.
        self._note_profile(spec, out)

        self._ttl_expires_at = self._deadline_for(spec, action)
        self._unit_states = self._collect_unit_states()
        self._errors = tuple(out.errors)
        if out.achieved:
            self._achieved = spec.name
        else:
            # Deliberately left at the last profile that fully applied: OBC
            # refuses to deploy into a profile it did not get, and a `profile`
            # field that claimed success would take that guard away.
            self.log.error(
                "profile %s applied only partially; reporting %s as achieved",
                spec.name.value,
                self._achieved.value if self._achieved else "nothing",
            )
        self._publish()
        return self._result(ok=out.achieved)

    def _wanted_units(self, spec: ProfileSpec) -> frozenset[str]:
        """Every unit this profile asks to be running.

        The complement within the allowlist is exactly what must stop, which is
        why a foreign unit the profile does not name is stopped rather than left
        as it was found. There is no verb to interpret here: ``start``/``stop``
        were resolved into a list of units when the profile was loaded, so this
        method reads the same whether a profile named two units or all of them.

        The final filter is not belt-and-braces. A registry entry the allowlist
        refused — one naming a ``cubesat@`` unit, say — is still in the spec,
        and must not become something a profile can ask HOSTD to start.
        """
        units = {unit_for(service) for service in spec.services}
        if spec.dashboard:
            units.add(DASHBOARD_UNIT)
        units |= set(spec.external_units)
        return frozenset(unit for unit in units if unit in self._allowlist.profile_units)

    def _start_unit(self, unit: str, out: _Outcome) -> None:
        try:
            if self._host.unit_state(unit) == ACTIVE:
                # Idempotence, and the reason it matters: re-applying EXPO in
                # the middle of a demonstration must not restart the service
                # driving the screen.
                self.log.debug("%s is already active", unit)
                return
            self._host.start(unit)
        except (Refused, ExecutorError) as exc:
            # Reported, and the remaining steps still taken: a profile that got
            # its network but not one unit is a state worth reaching and seeing.
            out.failed(f"start {unit}: {exc}")

    def _stop_unit(self, unit: str, out: _Outcome) -> None:
        try:
            if self._host.unit_state(unit) == INACTIVE:
                return
            self._host.stop(unit)
        except (Refused, ExecutorError) as exc:
            out.failed(f"stop {unit}: {exc}")

    def _switch_network(self, spec: ProfileSpec, out: _Outcome) -> None:
        if spec.network == self._network_spec and not self._network_state.errors:
            # The same radio configuration is already up and applied cleanly.
            # Re-running nmcli would drop the access point — and every phone
            # connected to it — for a few seconds, for nothing.
            self.log.info("network is already %s", spec.network.mode.value)
            clients = (
                self._network.client_count() if spec.network.mode is NetworkMode.AP else None
            )
            self._network_state = replace(self._network_state, clients=clients)
            return
        state = self._network.apply(spec.network)
        self._network_spec = spec.network
        self._network_state = state
        for error in state.errors:
            out.failed(f"network: {error}")

    def _apply_governor(self, governor: str, out: _Outcome) -> None:
        try:
            self._host.set_governor(governor)
        except ExecutorError as exc:
            out.failed(f"governor: {exc}")
            return
        self._governor = governor

    def _note_profile(self, spec: ProfileSpec, out: _Outcome) -> None:
        try:
            config.LAST_PROFILE_FILE.write_text(f"{spec.name.value}\n")
        except OSError as exc:
            # Reported but not fatal: the file is information, and losing it
            # does not make the platform any less the profile it now is.
            out.noted(f"could not record the profile in {config.LAST_PROFILE_FILE}: {exc}")

    def _deadline_for(self, spec: ProfileSpec, action: dict[str, Any]) -> float | None:
        """The absolute moment this profile expires, or ``None``.

        Published, not acted on: what an expiry *means* is OBC's decision. It
        lives here because HOSTD holds the applied profile, so an OBC restart
        can recover the deadline from the retained message instead of forgetting
        it — a ``FLIGHT`` profile whose safety net evaporates because OBC was
        restarted is the failure this closes.

        The command's own ``ttl_minutes`` wins, so a ground command can shorten
        or lengthen a profile's default; absent one, the profile's own value
        applies, which is what makes ``FLIGHT`` expire whoever asked for it.
        """
        raw = action.get("ttl_minutes")
        minutes: float | None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            minutes = float(raw)
        else:
            minutes = spec.ttl_minutes
        if not minutes:
            return None
        deadline = self._clock() + minutes * 60.0
        self.log.info("profile %s expires in %g minute(s)", spec.name.value, minutes)
        return deadline

    def _collect_unit_states(self) -> dict[str, str]:
        """Re-read every permitted unit, so the report is observation, not intent.

        Reporting is the last step and must not be the one that throws: by here
        the platform has already been changed, and an exception would lose the
        record of it.
        """
        states: dict[str, str] = {}
        for unit in sorted(self._allowlist.profile_units):
            try:
                states[unit] = self._host.unit_state(unit)
            except (Refused, ExecutorError) as exc:
                self.log.error("could not read the state of %s: %s", unit, exc)
                states[unit] = executor_module.UNKNOWN
        return states

    # ── set_governor ────────────────────────────────────────────────────────

    def _set_governor(self, action: dict[str, Any]) -> dict[str, Any]:
        governor = _field(action, "governor")
        out = _Outcome(self.log)
        if not isinstance(governor, str):
            out.failed(f"set_governor without a governor name: {action!r}")
        else:
            self._apply_governor(governor, out)
        self._errors = tuple(out.errors)
        # The achieved profile is untouched: LOW_POWER lowers the governor
        # inside the profile it is already in, and the way back out is that
        # profile's own value.
        self._publish()
        return self._result(ok=out.achieved)

    # ── restart_service ─────────────────────────────────────────────────────

    def _restart_service(self, action: dict[str, Any]) -> dict[str, Any]:
        """Restart one mission service. The name is a service, never a unit.

        ``restart_service {"service": "adcs"}`` — the vocabulary on the bus talks
        about subsystems, and this is the one place a subsystem name becomes a
        systemd unit. That mapping is ``unit_for``, the same function the profile
        path uses, so there is no second spelling of a unit name anywhere.

        A unit name arriving here directly is refused rather than obeyed: it
        would be a ground client reaching past the vocabulary into systemd, and
        the answer to "which units exist" belongs to the allowlist.
        """
        service = _field(action, "service")
        out = _Outcome(self.log)
        if not isinstance(service, str) or not service:
            out.failed(f"restart_service without a service name: {action!r}")
        elif service not in KNOWN_SERVICES:
            # Refused against the model rather than against a string pattern:
            # `KNOWN_SERVICES` is what a profile may name, so this command can
            # reach exactly what a profile can and nothing more.
            out.failed(
                f"restart_service: unknown service {service!r} "
                f"(one of: {', '.join(sorted(KNOWN_SERVICES))})"
            )
        else:
            unit = unit_for(service)
            try:
                self._host.restart(unit)
                self.log.info("restarted %s on request (request_id=%s)",
                              unit, action.get("request_id"))
            except (Refused, ExecutorError) as exc:
                # Refused covers the units that are denied outright — nothing in
                # KNOWN_SERVICES is, today, but the check stays because the deny
                # list is the property and this is not the place to restate it.
                out.failed(f"restart {unit}: {exc}")
            self._unit_states = self._collect_unit_states()
        self._errors = tuple(out.errors)
        # The achieved profile is untouched: a restart is inside the profile, not
        # a change of it.
        self._publish()
        return self._result(ok=out.achieved)

    # ── poweroff ────────────────────────────────────────────────────────────

    def _poweroff(self, action: dict[str, Any]) -> dict[str, Any]:
        reason = _field(action, "reason")
        out = _Outcome(self.log)
        try:
            self._host.poweroff(str(reason) if reason is not None else "unspecified")
        except ExecutorError as exc:
            out.failed(f"poweroff: {exc}")
        self._errors = tuple(out.errors)
        # Published after the request: `systemctl poweroff` returns immediately
        # and the shutdown takes seconds, so this still reaches the broker — and
        # if it did not, the retained status would claim a healthy platform that
        # is in fact off.
        self._publish()
        return self._result(ok=out.achieved)

    # ── reporting ───────────────────────────────────────────────────────────

    def on_connected(self) -> None:
        """Republish the retained host status after every connect.

        HOSTD publishes only when it has done something, so a broker restart
        would leave `host_status` absent until the next profile change — and OBC
        learns the active profile from exactly that retained message. It would
        sit in STANDBY under a fully applied profile with no way to find out.

        Nothing is published before the first profile has been applied. The
        connect callback arrives ahead of ``on_start``, and an empty status is
        not neutral: OBC reads a null profile as "HOSTD has not applied any
        profile" and says so at ERROR. Publishing one on every boot would
        announce a fault that is about to resolve itself a moment later, which
        is how a real one stops being noticed.
        """
        with self._lock:
            if self._achieved is None:
                return
            self._publish()

    def _publish(self) -> None:
        self.publish(
            "host_status",
            qos=1,
            profile=self._achieved.value if self._achieved is not None else None,
            profile_requested=self._requested,
            network=self._network_state.as_dict(),
            units=dict(self._unit_states),
            governor=self._governor,
            errors=list(self._errors),
            ttl_expires_at=self._ttl_expires_at,
        )

    def _result(self, *, ok: bool) -> dict[str, Any]:
        """The socket's answer. The same facts as ``host_status``, in a reply."""
        return {
            "ok": ok,
            "profile": self._achieved.value if self._achieved is not None else None,
            "profile_requested": self._requested,
            "errors": list(self._errors),
        }


def _field(action: dict[str, Any], name: str) -> Any:
    """Read ``name`` from ``params``, or from the top level.

    OBC sends ``profile`` at the top level and ``governor``/``reason`` inside
    ``params`` (see ``docs/concept.md``). Accepting either spelling for either
    field is tolerance about shape, not a second vocabulary: the action names,
    the validation and the effects are all still the ones above.
    """
    params = action.get("params")
    if isinstance(params, dict) and name in params:
        return params[name]
    return action.get(name)
