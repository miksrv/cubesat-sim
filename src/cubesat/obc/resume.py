"""Whether an unexpected reset should put the satellite back where it was.

`FLIGHT` is the one profile that resumes itself, and it resumes on physical
evidence rather than on a stored command. The reasoning is in
`docs/concept.md` → Open questions; the short version is that a reset mid-trip
— a brownout, a watchdog bite, the jolt of a parachute opening — otherwise ends
the recording silently, in the profile that has no dashboard, no Wi-Fi and
nobody looking at it.

**The measurement is what decides.** Not the file: "there is no mains at boot"
means the satellite is demonstrably not on a desk, and it is what keeps the
dangerous case safe by construction — a satellite brought home flat and plugged
in *has* mains, so it comes up in `HOSTED` with SSH exactly as it always has.
The file only names which profile that measurement is allowed to restore.

Five conditions, all of which must hold. Each is here because it refuses a case
that would otherwise be indistinguishable from a trip:

1. **HOSTD is still holding the profile it applied at boot** (`boot`). This is
   what tells a real boot from a ``systemctl restart cubesat@obc``: a human who
   has already asked for a profile has said what they want, and this must not
   argue with them.
2. **The previous run was in a resumable profile** — `FLIGHT` alone. `EXPO` on
   battery with no operator is pointless, `DIAG` lives on a desk with mains, and
   the rest do not record. Kept as a set rather than as a comparison so the
   answer to "which profiles resume themselves" is one readable line.
3. **No mains** (`external_power` false), from EPS' own reading of the X728 PLD
   pin. Withheld rather than assumed: with no `eps_status` at all there is no
   resume, because a missing measurement is not a measurement of no mains — and
   an EPS that failed to start is itself a reason to stay reachable.
4. **The TTL from before the reset is still in the future.** A trip whose strap
   had already run out does not restart, and a resumed one serves out the
   remainder rather than a fresh full term.
5. **Fewer than `max_consecutive` resumes in a row.** The boot-loop fence, and
   it is a lifetime rather than a counter: the count clears once a resumed
   session has lived `settle_sec`, so three means three *short* lives in a row.
   Deliberately not one — a parachute opening is a burst of jolts, and a burst
   must not exhaust the budget the descent needs.

Every refusal carries a reason, and the reason is transmitted (`comms/service.py`
→ `_boot_beacon`). A satellite that silently declines to resume is
indistinguishable from one that never woke up, which is the same silence the ack
work closed on 2026-09-03.
"""

from __future__ import annotations

from dataclasses import dataclass

from cubesat.common.states import Profile

#: The profiles a reset may put the satellite back into. See condition 2.
RESUMABLE: frozenset[Profile] = frozenset({Profile.FLIGHT})

#: What ``obc_status.mission_start_reason`` carries, and what DHS writes into
#: ``missions.start_reason``. Two values, and the pair is the point: a mission
#: that began as a resume is half of a trip whose other half is already in the
#: archive.
START_COMMAND = "command"
START_RESUME = "resume"

#: Why a resume was not taken. Transmitted, so they are short enough for a
#: 240-byte line and plain enough to read on a phone.
MAINS = "mains"
TTL_EXPIRED = "ttl"
BOOT_LOOP = "loop"
NO_EVIDENCE = "noeps"
NOT_RESUMABLE = "profile"


@dataclass(frozen=True)
class Candidate:
    """A previous run that might be worth resuming, once the mains is read."""

    profile: Profile
    #: Remaining TTL in minutes, or None when that profile had no strap.
    ttl_minutes: float | None
    mission_label: str | None
    resume_count: int


@dataclass(frozen=True)
class Verdict:
    """The decision, and why.

    ``previous`` is the interrupted profile whenever there was one worth
    considering — set even on a refusal, because it is what makes the refusal
    worth transmitting. When it is None nothing resumable was interrupted, and
    the satellite stays quiet: a desk reboot in ``HOSTED`` is not news, and news
    is what airtime on a shared mesh is for.
    """

    resumed: bool
    reason: str | None = None
    candidate: Candidate | None = None
    previous: str | None = None


def candidate_from(
    previous: dict | None,
    *,
    now: float,
    max_consecutive: int,
) -> Verdict:
    """Weigh everything that can be known before EPS has said anything.

    Returns a refusal with its reason, or a verdict whose ``candidate`` is what
    a subsequent mains reading may confirm. It never returns ``resumed=True``:
    conditions 1–2, 4 and 5 can all be settled from the retained ``host_status``,
    but condition 3 is a measurement and has to be waited for.
    """
    if not isinstance(previous, dict):
        return Verdict(resumed=False, reason=NOT_RESUMABLE)
    try:
        profile = Profile(previous.get("profile"))
    except ValueError:
        # A profile name this build no longer defines, or none at all. Refused
        # here rather than raised: an unrecognised name is exactly as good a
        # reason not to resume as a recognised unresumable one.
        return Verdict(resumed=False, reason=NOT_RESUMABLE)
    if profile not in RESUMABLE:
        return Verdict(resumed=False, reason=NOT_RESUMABLE)

    # From here the interrupted profile is one this satellite would resume, so
    # every exit below carries it and is therefore worth saying out loud.
    name = profile.value
    count = previous.get("resume_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    if count >= max_consecutive:
        return Verdict(resumed=False, reason=BOOT_LOOP, previous=name)

    deadline = previous.get("ttl_expires_at")
    ttl_minutes: float | None = None
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        remaining = float(deadline) - now
        if remaining <= 0.0:
            return Verdict(resumed=False, reason=TTL_EXPIRED, previous=name)
        ttl_minutes = remaining / 60.0

    label = previous.get("mission_label")
    return Verdict(
        resumed=False,
        previous=name,
        candidate=Candidate(
            profile=profile,
            ttl_minutes=ttl_minutes,
            mission_label=label if isinstance(label, str) and label else None,
            resume_count=count,
        ),
    )
