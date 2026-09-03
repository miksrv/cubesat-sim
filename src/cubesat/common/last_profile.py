"""The ``last-profile`` file: what the previous run was doing, as evidence.

Written by HOSTD after every profile application and read by exactly two
processes — HOSTD itself, once at start, and ``cubesat status``. OBC never opens
it: what it needs arrives on ``host_status``, because a service that runs
unprivileged should not be reading root's bookkeeping off the card.

**It answers *what*, never *whether*.** Restoring a profile because a file says
so is the trap ``docs/concept.md`` argues out at length: a satellite that hit
``CRITICAL`` on a trip and is plugged in at a desk hours later must come up on
the home network with SSH reachable. What makes reading it safe is that the
decision to resume is taken from a measurement — no mains at boot — and this
file only names the profile that measurement is allowed to restore. See
``obc/resume.py``.

**The format is JSON, and a bare profile name is still accepted.** Until
2026-09-03 the file held one line: ``FLIGHT``. A satellite upgraded in the field
has that file on its card, and the first thing the new build does with it is
read it — so the old spelling parses, with every other field absent. Writing is
always JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PreviousRun:
    """What the file says about the run before this one."""

    #: The profile name as written. Deliberately a string and not a ``Profile``:
    #: a file naming a profile this build no longer defines must parse and be
    #: refused by the caller, not raise while being read.
    profile: str | None = None
    #: Wall clock, when the file was last written.
    written_at: float | None = None
    #: The absolute moment that profile was due to expire, if it had a TTL. This
    #: is what lets a resumed trip serve out the remainder of its strap instead
    #: of starting a fresh one.
    ttl_expires_at: float | None = None
    #: The label the mission was running under, so a trip interrupted by a reset
    #: reads as one journey in two parts.
    mission_label: str | None = None
    #: How many resumes have been taken in a row without a session living long
    #: enough to count as a flight. The boot-loop fence.
    resume_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "written_at": self.written_at,
            "ttl_expires_at": self.ttl_expires_at,
            "mission_label": self.mission_label,
            "resume_count": self.resume_count,
        }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse(text: str) -> PreviousRun | None:
    """Read the file's contents. ``None`` when it says nothing usable.

    Every field is validated separately and a bad one is dropped rather than
    failing the parse: this file is written on the way out of a run that may
    have been cut short mid-write, and half of it is still worth more than none
    of it.
    """
    text = text.strip()
    if not text:
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        # The pre-2026-09-03 spelling: the profile name and nothing else.
        return PreviousRun(profile=text.splitlines()[0].strip() or None)
    if not isinstance(raw, dict):
        return None
    count = raw.get("resume_count")
    return PreviousRun(
        profile=_text(raw.get("profile")),
        written_at=_number(raw.get("written_at")),
        ttl_expires_at=_number(raw.get("ttl_expires_at")),
        mission_label=_text(raw.get("mission_label")),
        resume_count=int(count) if isinstance(count, int) and not isinstance(count, bool) else 0,
    )


def read(path: Path) -> PreviousRun | None:
    """Read the file at ``path``. ``None`` if it is missing or unreadable."""
    try:
        return parse(path.read_text())
    except OSError:
        return None


def write(path: Path, previous: PreviousRun) -> None:
    """Write the file. Raises ``OSError``, which the caller reports and survives."""
    path.write_text(json.dumps(previous.as_dict(), sort_keys=True) + "\n")
