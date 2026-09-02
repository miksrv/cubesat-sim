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
SAFE_MODE = "safe_mode"
RECOVER = "recover"
RESTART_SERVICE = "restart_service"

#: The commands OBC answers for: the ones that are mission decisions.
#:
#: ``restart_service`` is here rather than in HOSTD's own subscription for the
#: reason the whole privilege split exists: `cubesat/host/command` is root's
#: inbox and the browser ACL denies it outright, so a ground client must not be
#: able to publish there. It asks OBC, which decides whether to ask HOSTD.
HANDLED = frozenset(
    {SET_PROFILE, SAFE_MODE, RECOVER, RESTART_SERVICE}
)


@dataclass(frozen=True)
class Command:
    name: str
    request_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RestartRequest:
    """Which subsystem to restart. A service name, never a systemd unit.

    The vocabulary on the bus talks about subsystems, and the translation into a
    unit happens once, inside HOSTD, next to the allowlist that bounds it. A
    ground client that could name a unit would be reaching past the vocabulary
    into systemd.
    """

    service: str


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


def restart_request(command: Command) -> RestartRequest | None:
    """Pull a ``restart_service`` request out of its params, or None if unusable.

    Whether the name is a service this satellite has is HOSTD's answer, next to
    the allowlist — this only establishes that there is a name at all.
    """
    service = command.params.get("service")
    if not isinstance(service, str) or not service:
        return None
    return RestartRequest(service=service)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None
