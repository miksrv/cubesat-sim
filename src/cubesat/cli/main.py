"""The ``cubesat`` command: the operator's side of the control plane.

A thin MQTT client. It publishes the same ground commands the dashboard and a
LoRa uplink publish, onto the same topic, and reads the same retained statuses —
so there is exactly one code path into a profile switch and this is not a second
one. It needs no privileges: HOSTD holds the root and hears the request from OBC.

Why it exists at all, given that `mosquitto_pub` can publish the identical JSON:
on the satellite this is the only interface that is always available. The
dashboard does not run in every profile and does not run in ``FLIGHT`` at all,
which is the profile whose state one most wants to check afterwards over SSH.

Exit codes, because this ends up in scripts: ``0`` the thing was done or the
question answered, ``1`` the satellite did not answer or answered badly, ``2``
the command line itself was wrong. A partial profile switch is ``1`` — it says
what applied, and it is not a success.
"""

from __future__ import annotations

import argparse
import re
import sys

from cubesat.cli.commands import mission as mission_cmd
from cubesat.cli.commands import profile as profile_cmd
from cubesat.cli.commands import status as status_cmd
from cubesat.cli.session import BrokerUnavailable, Session
from cubesat.common import profiles as profiles_module
from cubesat.common.profiles import KNOWN_SERVICES, ProfileError

#: `8h`, `45m`, or a bare number of minutes. Accepted in the units a person
#: thinks in — "the walk takes an hour, give it two" — and converted here,
#: because `ttl_minutes` is what the wire carries.
_TTL = re.compile(r"^(\d+)([hm]?)$", re.IGNORECASE)


def _ttl_minutes(text: str) -> int:
    match = _TTL.match(text.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"expected 8h, 45m or a number of minutes, got {text!r}")
    value, unit = int(match.group(1)), match.group(2).lower()
    minutes = value * 60 if unit == "h" else value
    if minutes <= 0:
        raise argparse.ArgumentTypeError("a TTL of zero would expire immediately")
    return minutes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cubesat",
        description="Operate the satellite: profiles, health, recorded missions.",
    )
    sub = parser.add_subparsers(dest="command")

    profile = sub.add_parser("profile", help="show the active profile, or switch to one")
    profile.add_argument("name", nargs="?", help="profile to apply (case-insensitive)")
    profile.add_argument(
        "--ttl",
        type=_ttl_minutes,
        metavar="8h",
        help="override the profile's own TTL; without it the profile's default applies",
    )
    profile.add_argument(
        "--mission",
        metavar="LABEL",
        help="label the mission this profile records; defaults to its start time",
    )

    sub.add_parser("status", help="mission state, power, radio, recorder, host")

    missions = sub.add_parser("mission", help="the trips recorded on the card")
    missions_sub = missions.add_subparsers(dest="mission_command")
    listing = missions_sub.add_parser("list", help="every mission, newest first")
    listing.add_argument(
        "--all", action="store_true", help="do not stop at the most recent few"
    )

    # `lora` is the name this had until 2026-09-01, kept as an alias because it
    # may be in somebody's shell history. argparse renders it as `beacon (lora)`,
    # which is the right emphasis: the word to learn first, and the old one
    # visible enough that nobody wonders whether it still works. Renamed because
    # `lora off` said the wrong thing — it never turned the radio off, and that
    # distinction is the way back into a satellite in SAFE.
    beacon = sub.add_parser(
        "beacon",
        aliases=["lora"],
        help="start or stop transmitting, inside what the profile permits",
    )
    beacon.add_argument("state", choices=("on", "off"))

    restart = sub.add_parser(
        "restart",
        help="restart one mission service through HOSTD, not around it",
    )
    restart.add_argument("service", help=f"one of: {', '.join(sorted(KNOWN_SERVICES))}")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 2

    # The archive is on this disk; everything else is a conversation with the
    # broker. Separated here so `mission list` works with no broker at all —
    # which is the case on a satellite whose mosquitto has fallen over and whose
    # last trip is the thing being investigated.
    if args.command == "mission":
        if args.mission_command != "list":
            # `list` is the only verb here so far. Said as a usage line rather
            # than by re-entering argparse, which would exit instead of
            # returning and take the exit code with it.
            return _print(2, ["usage: cubesat mission list [--all]"])
        code, lines = mission_cmd.listing(limit=0 if args.all else mission_cmd.DEFAULT_LIMIT)
        return _print(code, lines)

    # A typo is answered from this disk before anything is connected to: being
    # told "no such profile" — or "no such service" — should not require a
    # reachable satellite.
    if args.command == "restart" and args.service not in KNOWN_SERVICES:
        return _print(
            2, [f"Unknown service {args.service!r}. One of: {', '.join(sorted(KNOWN_SERVICES))}"]
        )
    wanted = None
    if args.command == "profile" and args.name is not None:
        try:
            config = profiles_module.load()
        except ProfileError as exc:
            return _print(1, [f"cannot read the profile definitions: {exc}"])
        wanted, complaints = profile_cmd.resolve(config, args.name)
        if wanted is None:
            return _print(2, complaints)

    try:
        with Session() as session:
            if args.command == "profile":
                if wanted is None:
                    code, lines = profile_cmd.show(session)
                else:
                    code, lines = profile_cmd.switch(
                        session,
                        wanted,
                        ttl_minutes=args.ttl,
                        mission_label=args.mission,
                    )
            elif args.command == "status":
                code, lines = status_cmd.show(session)
            elif args.command == "restart":
                code, lines = _restart(session, args.service)
            else:
                code, lines = _beacon(session, args.state)
    except BrokerUnavailable as exc:
        return _print(1, [str(exc), "Is mosquitto running? `systemctl status mosquitto`"])

    return _print(code, lines)


def _restart(session: Session, service: str) -> tuple[int, list[str]]:
    """Restart one mission service — through HOSTD rather than around it.

    ``sudo systemctl restart cubesat@adcs`` does the same thing and needs no
    broker, so why this exists: going through HOSTD means the action is logged
    beside every other thing root did, checked against the allowlist, and
    reflected on ``host_status`` like a profile switch. A restart done behind
    HOSTD's back is one that does not appear in the account of what happened.

    The name is checked in ``main`` before the broker is contacted, for the same
    reason a profile typo is: the answer is on this disk.
    """
    session.subscribe("host_status")
    session.send("restart_service", service=service)
    answer = session.await_message(
        "host_status", lambda payload: isinstance(payload.get("units"), dict), timeout=20.0
    )
    if answer is None:
        return 1, [
            f"No answer about {service}. The command was published; OBC or HOSTD did not report.",
            "Check `journalctl -u cubesat-hostd -u cubesat@obc`.",
        ]
    errors = answer.get("errors") or []
    if errors:
        return 1, [f"⚠ {error}" for error in errors]
    state = (answer.get("units") or {}).get(f"cubesat@{service}.service")
    return 0, [f"{service} restarted (now {state or 'unknown'})."]


def _beacon(session: Session, state: str) -> tuple[int, list[str]]:
    """Ask for the beacon on or off — the same command the radio and the
    dashboard console send, and bounded by the same envelope.

    The profile decides whether the radio runs at all, so this cannot widen
    anything: in ``MAINTENANCE`` it is refused by COMMS and the status below says
    so rather than this tool pretending otherwise.
    """
    session.subscribe("comms_status")
    wanted = state == "on"
    session.send("set_comms_config", lora_enabled=wanted)
    answer = session.await_message(
        "comms_status", lambda payload: payload.get("lora_enabled") is wanted, timeout=10.0
    )
    if answer is not None:
        return 0, [f"Beacon {state}."]
    # No confirmation: either COMMS is not running, or the profile forbids the
    # radio and refused. Both are visible in what it last published.
    seen = session.collect("comms_status", window=1.0).get("comms_status")
    if seen is None:
        return 1, ["COMMS published no status — is `cubesat@comms` running in this profile?"]
    if not seen.get("lora_listening"):
        return 1, ["This profile does not use the radio, so the beacon cannot be turned on."]
    return 1, ["COMMS did not confirm the change."]


def _print(code: int, lines: list[str]) -> int:
    stream = sys.stdout if code == 0 else sys.stderr
    for line in lines:
        stream.write(f"{line}\n")
    return code
