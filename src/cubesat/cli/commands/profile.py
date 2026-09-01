"""``cubesat profile`` — read the platform's state, or change it.

Both halves go through the same MQTT command every other ground client uses, so
there is one code path into a profile switch and this tool is not a second one.
It needs no privileges: HOSTD holds the root, and it hears about the request from
OBC, never from here.

**A switch is reported against what was achieved, not against what was asked
for.** HOSTD publishes both, and the difference is the whole debugging story of a
failed switch — a profile can apply partially, leaving some units started and a
network mode unchanged. So this waits for a ``host_status`` that names the
request and then says which of the three things happened: applied, applied in
part (with the errors HOSTD reported), or nothing arrived at all.
"""

from __future__ import annotations

from typing import Any

from cubesat.cli.session import Session
from cubesat.common.profiles import ProfileConfig
from cubesat.common.states import Profile

#: The topics a plain `cubesat profile` reads. All retained, so subscribing is
#: the whole query.
SNAPSHOT = ("host_status", "obc_status", "dhs_status")


def show(session: Session) -> tuple[int, list[str]]:
    """What the platform is now: profile, mission state, mission."""
    seen = session.collect(*SNAPSHOT)
    host = seen.get("host_status")
    obc = seen.get("obc_status")
    dhs = seen.get("dhs_status")

    if host is None and obc is None:
        # Both are retained and both are published by services that run in every
        # profile, so silence here is not "idle" — it is nobody home.
        return 1, ["Nothing has published a status. Is the satellite's software running?"]

    lines = [f"Profile:       {_profile_line(host)}"]
    if obc is not None:
        lines.append(f"Mission state: {obc.get('status') or '-'}")
        lines.append(f"Cadence:       ×{obc.get('cadence_scale') or 1}")
    lines.append(f"Recording:     {_recording_line(dhs)}")
    if host is not None and host.get("ttl_expires_at"):
        lines.append(f"TTL:           returns to the default profile at {host['ttl_expires_at']}")
    errors = (host or {}).get("errors") or []
    lines.extend(f"⚠ HOSTD: {error}" for error in errors)
    return 0, lines


def resolve(config: ProfileConfig, name: str) -> tuple[Profile | None, list[str]]:
    """Turn a typed name into a profile, or explain why it is not one.

    Separate from ``switch`` and called **before** the broker is contacted: a
    typo is answered from this disk, instantly, and does not need a satellite to
    be reachable to be told it is a typo. The same check exists in OBC, which is
    the authority — this one only saves a round trip that would end in a timeout.
    """
    try:
        wanted = Profile(name.upper())
    except ValueError:
        known = ", ".join(profile.value for profile in Profile)
        return None, [f"Unknown profile {name!r}. One of: {known}"]
    if wanted not in config.profiles:
        # A profile the enum knows but this deployment's file does not.
        return None, [f"Profile {wanted.value} is not defined in this deployment's profiles.yaml"]
    return wanted, []


def switch(
    session: Session,
    wanted: Profile,
    *,
    ttl_minutes: int | None = None,
    mission_label: str | None = None,
) -> tuple[int, list[str]]:
    """Ask for a profile and report what HOSTD achieved."""
    # Subscribed before publishing: HOSTD can finish applying a profile faster
    # than a subscription is set up, and a confirmation that arrives before we
    # are listening is one we would wait out to the timeout.
    session.subscribe("host_status")
    params: dict[str, Any] = {"profile": wanted.value}
    if ttl_minutes is not None:
        params["ttl_minutes"] = ttl_minutes
    if mission_label is not None:
        params["mission_label"] = mission_label
    request_id = session.send("set_profile", **params)

    answer = session.await_message(
        "host_status", lambda payload: payload.get("profile_requested") == wanted.value
    )
    if answer is None:
        return 1, [
            f"No answer from the satellite ({request_id}).",
            "The command was published; OBC or HOSTD did not report back.",
            "Check `journalctl -u cubesat-hostd -u cubesat@obc`.",
        ]

    achieved = answer.get("profile")
    errors = answer.get("errors") or []
    if achieved == wanted.value and not errors:
        lines = [f"{wanted.value} applied."]
        if answer.get("ttl_expires_at"):
            lines.append(f"Returns to the default profile at {answer['ttl_expires_at']}.")
        if mission_label is not None:
            lines.append(f"The mission will be labelled {mission_label!r}.")
        return 0, lines

    # Partial: some steps failed. `profile` reports the last profile that fully
    # applied, which is exactly the thing worth printing.
    lines = [
        f"{wanted.value} applied only in part — the platform reports {achieved or 'nothing'}.",
        *(f"⚠ {error}" for error in errors),
        "Read `journalctl -u cubesat-hostd` for what it refused and why.",
    ]
    return 1, lines


def _profile_line(host: dict[str, Any] | None) -> str:
    if host is None:
        return "unknown (HOSTD has published nothing)"
    achieved = host.get("profile") or "none"
    requested = host.get("profile_requested")
    if requested and requested != achieved:
        return f"{achieved} (a switch to {requested} did not fully apply)"
    return str(achieved)


def _recording_line(dhs: dict[str, Any] | None) -> str:
    if dhs is None:
        return "no (DHS is not running in this profile)"
    mission = dhs.get("mission")
    if not dhs.get("recording") or not isinstance(mission, dict):
        return "no"
    label = mission.get("label") or "unlabelled"
    return f"mission {mission.get('id')} — {label} ({mission.get('rows') or 0} rows)"
