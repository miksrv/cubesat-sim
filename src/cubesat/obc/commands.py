"""Parsing ground commands off ``cubesat/command``.

All commands share one topic and the ``command`` field selects the handler, so
OBC sees PAYLOAD's and COMMS' commands too. Those are **ignored silently**: they
are not errors, they are simply not addressed to us, and logging a warning for
each one would make every photo request look like a fault.

What is not tolerated is a payload taking OBC down. The same command arrives
over MQTT and over LoRa, so anything that can be typed by hand, garbled by a
radio or left over from an older ground client eventually shows up here.
Everything below returns ``None`` rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SET_PROFILE = "set_profile"
SCIENCE_START = "science_start"
SCIENCE_STOP = "science_stop"
SAFE_MODE = "safe_mode"
RECOVER = "recover"

#: The commands OBC answers for: the ones that are mission decisions.
HANDLED = frozenset({SET_PROFILE, SCIENCE_START, SCIENCE_STOP, SAFE_MODE, RECOVER})


@dataclass(frozen=True)
class Command:
    name: str
    request_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileRequest:
    profile: str
    ttl_minutes: int | None = None
    mission_label: str | None = None


def parse(payload: dict[str, Any]) -> Command | None:
    """Return the command if OBC handles it, else None."""
    name = payload.get("command")
    if not isinstance(name, str) or name not in HANDLED:
        return None
    raw_params = payload.get("params")
    request_id = payload.get("request_id")
    return Command(
        name=name,
        request_id=request_id if isinstance(request_id, str) else None,
        params=raw_params if isinstance(raw_params, dict) else {},
    )


def profile_request(command: Command) -> ProfileRequest | None:
    """Pull a ``set_profile`` request out of its params, or None if unusable.

    The profile name is validated against ``profiles.yaml`` later, by the profile
    machine — this only establishes that there is a string to validate. A TTL
    that is not a positive integer is dropped rather than refused: losing an
    expiry is a smaller problem than refusing the profile change that was
    probably someone's way back out of ``FLIGHT``.
    """
    profile = command.params.get("profile")
    if not isinstance(profile, str) or not profile:
        return None
    label = command.params.get("mission_label")
    return ProfileRequest(
        profile=profile,
        ttl_minutes=_positive_int(command.params.get("ttl_minutes")),
        mission_label=label if isinstance(label, str) and label else None,
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None
