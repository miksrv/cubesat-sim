"""The platform profile: request it, then learn what actually happened.

Deliberately **not** a ``transitions`` machine. There is no transition table
here to write down — this is a request/reconcile loop with two participants, and
forcing it into a state machine would hide the one thing that matters about it:
what OBC asked for and what HOSTD achieved are separate facts, and the second is
the only one that is true.

Three decisions this module exists to protect:

* **OBC writes no file and restores nothing.** The active profile is learned
  only from the retained ``host_status``. Every boot therefore starts in
  ``HOSTED``, which is what makes a power cycle a recovery path from any profile
  — a satellite that hit ``CRITICAL`` on a trip and is plugged in at a desk
  hours later must not come back up with Wi-Fi off and no SSH. HOSTD does write
  ``last-profile``, as information; nothing reads it to decide anything.
* **A mismatch is not an application.** If HOSTD reports a profile other than
  the one requested, that is logged and left visible, and the mission machine is
  not advanced to ``DEPLOY``. Pretending otherwise turns a debuggable failure
  into a mystery.
* **OBC owns the TTL timer.** The expiry is a decision — "this profile has been
  on long enough, go back to something with SSH" — and decisions live here.
  HOSTD only executes the resulting ``apply_profile``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cubesat.common.profiles import ProfileConfig, ProfileError, ProfileSpec
from cubesat.common.states import MissionMode, Profile
from cubesat.obc import resume as resume_rule

#: How long after a profile request the platform is considered to be settling —
#: units stopping and starting on OBC's own instruction. Matches the
#: heartbeat-loss window (HEARTBEAT_INTERVAL_SEC * HEARTBEAT_MISS_THRESHOLD):
#: a switch that takes longer than a service is allowed to be silent is no
#: longer a switch, and the health monitor should get its say back.
SETTLE_GRACE_SEC = 30.0


@dataclass(frozen=True)
class ProfileUpdate:
    """What a ``host_status`` message told us about the platform."""

    achieved: Profile
    spec: ProfileSpec
    #: The achieved profile differs from the one we had previously observed.
    changed: bool
    #: The achieved profile is the one OBC asked for — or OBC never asked, which
    #: is the case after ``systemctl restart cubesat@obc`` mid-demo: whatever
    #: HOSTD reports then *is* the truth, and adopting it is how OBC recovers the
    #: running profile without disturbing the access point.
    matches_request: bool

    @property
    def active(self) -> bool:
        """Whether this profile asks for a mission at all."""
        return self.spec.mission is MissionMode.ACTIVE


class ProfileMachine:
    """Requests profiles, reconciles them against HOSTD, and times them out."""

    def __init__(
        self,
        config: ProfileConfig,
        apply: Callable[[Profile, str | None, float | None, str | None, bool], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        log: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._apply = apply
        self._clock = clock
        #: TTL deadlines cross a process boundary as absolute timestamps, so
        #: they are compared against the wall clock, not the monotonic one.
        self._wall_clock = wall_clock
        self.log = log or logging.getLogger("obc.profile")

        self.achieved: Profile | None = None
        self.spec: ProfileSpec | None = None
        #: Operator-supplied name for the mission this profile will record.
        self.label: str | None = None
        #: Why this profile is active: a command, or the satellite putting
        #: itself back where a reset found it. Published on ``obc_status`` and
        #: recorded in the mission row — see ``resume.py``.
        self.start_reason: str = resume_rule.START_COMMAND

        self._requested: Profile | None = None
        self._requested_label: str | None = None
        self._requested_ttl: float | None = None
        self._requested_resume = False
        self._requested_at: float | None = None
        self._deadline: float | None = None

    @property
    def default(self) -> Profile:
        return self._config.default

    @property
    def settling(self) -> bool:
        """A profile request is in flight and recent: HOSTD is rearranging units.

        While this is true, a subsystem's goodbye is the profile change itself —
        HOSTD stops units on OBC's own request — and must not be read as a
        fault (the first hardware run latched SAFE on exactly that, 2026-08-28).
        Bounded in time so a request HOSTD never answers cannot suppress the
        health monitor forever: after the window, silence is silence again.
        """
        if self._requested is None or self._requested_at is None:
            return False
        return self._clock() - self._requested_at < SETTLE_GRACE_SEC

    @property
    def deadline(self) -> float | None:
        return self._deadline

    # ── requesting ──────────────────────────────────────────────────────────

    def request(
        self,
        profile: Profile | str,
        *,
        ttl_minutes: float | None = None,
        mission_label: str | None = None,
        request_id: str | None = None,
        resume: bool = False,
    ) -> bool:
        """Validate a profile and ask HOSTD for it. Returns whether it was sent.

        Validation happens here and not in HOSTD because HOSTD has no decision
        logic at all — it is the hands, and a profile that does not exist is a
        decision to refuse.

        ``resume`` says this request is the satellite putting itself back where
        a reset found it (W11). It travels to HOSTD, which counts consecutive
        resumes in ``last-profile``, and it becomes this session's
        ``start_reason`` — so the mission row records why it started rather than
        leaving a trip in two halves looking like two trips.
        """
        try:
            spec = self._config.get(profile)
        except ProfileError as exc:
            self.log.error("refusing profile request: %s", exc)
            return False

        self._requested = spec.name
        self._requested_label = mission_label
        self._requested_ttl = ttl_minutes if ttl_minutes is not None else spec.ttl_minutes
        self._requested_resume = resume
        self._requested_at = self._clock()
        self.log.info(
            "requesting profile %s (ttl=%s, label=%r%s)",
            spec.name.value,
            self._requested_ttl,
            mission_label,
            ", resuming" if resume else "",
        )
        # The TTL travels with the request: HOSTD turns it into an absolute
        # deadline and publishes that, so the expiry outlives an OBC restart.
        # The label travels too, because HOSTD writes it into `last-profile` and
        # that is what lets a resumed trip keep the name it was given.
        self._apply(spec.name, request_id, self._requested_ttl, mission_label, resume)
        return True

    # ── reconciling ─────────────────────────────────────────────────────────

    def observe(self, payload: dict) -> ProfileUpdate | None:
        """Absorb a ``host_status`` message. None if it says nothing usable.

        ``profile`` is the achieved state and ``profile_requested`` the intended
        one; HOSTD reports both because a profile can apply *partially* — an AP
        that never came up, a unit that refused to start.
        """
        raw = payload.get("profile")
        if raw is None:
            # HOSTD reports a null profile when nothing has ever fully applied —
            # a boot-time partial failure. There is no profile to adopt, but the
            # errors it carries are the only explanation anyone will get for a
            # satellite sitting in STANDBY, so they must not be swallowed along
            # with the unusable name.
            self._log_host_trouble(payload, None)
            return None
        try:
            achieved = Profile(raw)
            spec = self._config.get(achieved)
        except (ValueError, ProfileError):
            self.log.error("host_status reports a profile we do not know: %r", raw)
            return None

        self._log_host_trouble(payload, achieved)

        matches = self._requested is None or achieved is self._requested
        if not matches:
            # Left visible and *not* treated as an application: the mission
            # machine must not advance to DEPLOY on a platform that is not the
            # one it was promised.
            self.log.error(
                "profile mismatch: requested %s, HOSTD achieved %s",
                self._requested.value if self._requested else "?",
                achieved.value,
            )

        changed = achieved is not self.achieved
        if changed:
            self.log.info("active profile is now %s", achieved.value)
        self.achieved = achieved
        self.spec = spec

        if matches:
            # Only on a change, or in answer to a request of our own. HOSTD may
            # republish the retained status for its own reasons, and taking those
            # as answers would push a TTL out forever and clear a label nobody
            # asked to clear.
            #
            # Re-applying the *same* profile with a new label therefore updates
            # the label — deliberately. A label is a name for the recording, not
            # its identity: the profile did not change, so DHS keeps the mission
            # it already has and only the name on obc/status moves. Treating a
            # rename as a mission boundary would split one walk into two.
            if changed or self._requested is not None:
                self.label = self._requested_label
                self.start_reason = (
                    resume_rule.START_RESUME
                    if self._requested_resume
                    else resume_rule.START_COMMAND
                )
                self._arm_ttl(spec, payload)
            self._requested = None
            self._requested_label = None
            self._requested_ttl = None
            self._requested_resume = False
            self._requested_at = None

        return ProfileUpdate(
            achieved=achieved, spec=spec, changed=changed, matches_request=matches
        )

    def _log_host_trouble(self, payload: dict[str, Any], achieved: Profile | None) -> None:
        """Say out loud whatever HOSTD could not do.

        ``achieved`` is None when nothing has ever fully applied, which is the
        boot-time failure case: there is no profile to adopt and nothing will
        advance, so this log line is the whole diagnosis.
        """
        # profile_requested is the one field that is *not* guaranteed to name a
        # real profile: on a refusal it carries what was asked for verbatim, so
        # that an operator can see it. It reaches here from a ground command,
        # which may have arrived over the radio, so it is logged with %r —
        # never coerced to a Profile, and never able to forge a log line with an
        # embedded newline.
        intended = payload.get("profile_requested")
        name = achieved.value if achieved is not None else "nothing"
        if intended is not None and intended != name:
            self.log.error("HOSTD applied %s only partially: it was asked for %r", name, intended)
        elif achieved is None:
            self.log.error("HOSTD has not applied any profile")
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            self.log.error("HOSTD reported errors applying %r: %s", intended or name, errors)

    # ── expiry ──────────────────────────────────────────────────────────────

    def _arm_ttl(self, spec: ProfileSpec, payload: dict[str, Any]) -> None:
        """Adopt the expiry HOSTD published, rather than timing it here.

        The deadline is data, not a decision: HOSTD holds the applied profile, so
        it computes the absolute moment and publishes it retained, and OBC reads
        it back. Timing it locally instead would mean a `systemctl restart
        cubesat@obc` mid-flight silently discards the safety net that a `FLIGHT`
        profile is relying on — the profile survives the restart because it comes
        from the retained message, and its expiry has to survive the same way.

        Wall clock, not monotonic, because that is what crosses a process
        boundary. The DS1307 RTC on the UPS HAT keeps it honest with no network.
        """
        raw = payload.get("ttl_expires_at")
        # The default profile is where an expiry sends us, so an expiry on it
        # would be a loop with nowhere to land.
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or spec.name is self.default:
            self._deadline = None
            return
        self._deadline = float(raw)
        remaining = max(0.0, self._deadline - self._wall_clock())
        self.log.info(
            "profile %s expires in %d minute(s)", spec.name.value, round(remaining / 60.0)
        )

    def expired(self) -> bool:
        """Whether the active profile has outlived its TTL."""
        return self._deadline is not None and self._wall_clock() >= self._deadline

    def request_default_on_expiry(self) -> bool:
        """If the TTL has run out, ask for the default profile.

        Called from OBC's tick. The timer is disarmed before the request goes
        out, so a HOSTD that never answers produces one request rather than one
        per tick for the rest of the flight.
        """
        if not self.expired():
            return False
        self._deadline = None
        self.log.warning(
            "profile %s expired; falling back to %s",
            self.achieved.value if self.achieved else "?",
            self.default.value,
        )
        return self.request(self.default)
