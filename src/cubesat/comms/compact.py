"""The compact uplink syntax: what a person can actually type on a phone.

**This table is the whole radio vocabulary** since 2026-09-03. Quoted JSON on a
phone keyboard in a field is where commands go to be mistyped, so COMMS accepts
a compact form and canonicalises it into JSON **before** the relay — one
translation point, on the way in. Hand-composed JSON off the air used to be
relayed verbatim beside it and no longer is: an uplink is a line this table
knows or it is not a command, which leaves one parser on the radio side instead
of two with room to disagree. Everything the satellite understands is still
reachable over MQTT, where the dashboard and the CLI publish the JSON directly.
The contract, with the reasoning, is ``docs/concept.md`` → The radio command
contract.

The spelling is a bare verb — ``ping``, ``profile FLIGHT`` — the same lines the
dashboard's Mission Console takes, so there is one command language however the
satellite is reached. The ``!`` prefix is still accepted, and it buys one thing:
**declared intent**. A ``!`` line that does not parse is answered with
``re=? ok=0 err=unknown``, because its sender meant to command and is standing
in a field wondering why nothing happened. A bare line that does not parse is
ordinary mesh chat and is left alone — answering every stray word on a shared
channel would spend the transmission budget on other people's conversations.
The price of the bare spelling is the flip side of the same coin: chat that
happens to be exactly a command line (``ping``, and nothing else on the line)
is a command. On a channel whose members command satellites, that is the right
trade.

The table below names every spelling some service can answer for. It was
deliberately shorter than the agreed vocabulary until 2026-09-01, when
``restart`` got its handler — OBC relays it, HOSTD executes it against the
allowlist — and joined the table. Before that, translating it would have relayed
a command into a bus where nothing picked it up, and silence is exactly what the
``!`` form exists to end.

The query verbs — ``ping``, ``pos``, ``sys``, ``env``, ``mission`` — are
answered by COMMS itself from its caches, immediately and without a relay:
the radio is the thing being asked.

Ordinary mesh chat does not start with ``!``, so a message this table does not
know is still just chat and is never answered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Compact:
    """One translated line: the canonical JSON, the command, and the spelling.

    ``verb`` is the word the operator actually typed — ``beacon``, ``photo``,
    ``pos`` — and it travels with the translation for one reason: the ``re=``
    field of the reply is read on a phone, and a person who typed ``beacon off``
    and got back ``re=set_comms_config`` has to translate the answer back into
    their own vocabulary to believe it (observed on the hardware, 2026-09-02).
    The canonical name is what goes on the bus; the spelling is what goes on the
    air. Filled in by ``translate`` rather than by each builder, so a new verb
    cannot forget it.
    """

    json: str
    command: str
    verb: str = ""


def is_compact(text: str) -> bool:
    # Stripped first: a phone keyboard slips a leading space in front of the
    # ``!`` (bench-found on the very first uplink from a phone, 2026-08-28),
    # and a command that was typed correctly must not become chat over it.
    return text.strip().startswith("!")


def translate(text: str) -> Compact | None:
    """Canonical JSON for a compact line, or None for one nobody wrote.

    Takes the bare spelling and the ``!``-prefixed one alike — the prefix is
    stripped, not required. What None means depends on the caller's channel:
    for a ``!`` line it is a *reply* (``re=? ok=0 err=unknown``), for a bare
    line it means the text was never a command at all — chat, or a spelling the
    radio stopped taking, and either way it is logged and left alone.
    """
    stripped = text.strip()
    words = stripped.removeprefix("!").split()
    if not words:
        return None
    verb, args = words[0].lower(), words[1:]
    builder = _TABLE.get(verb)
    if builder is None:
        return None
    translated = builder(args)
    return None if translated is None else replace(translated, verb=verb)


def _command(name: str, **params: object) -> Compact:
    body: dict[str, object] = {"command": name}
    if params:
        body["params"] = params
    return Compact(json=json.dumps(body), command=name)


def _bare(name: str):
    def build(args: list[str]) -> Compact | None:
        return _command(name) if not args else None

    return build


def _profile(args: list[str]) -> Compact | None:
    if len(args) != 1:
        return None
    # Upper-cased here, not validated: profile names are OBC's to validate
    # against profiles.yaml, and a phone keyboard capitalises on its own terms.
    return _command("set_profile", profile=args[0].upper())


def _restart(args: list[str]) -> Compact | None:
    """``restart adcs`` — one subsystem, by the name the vocabulary uses.

    The name is not checked here. Which services exist is `KNOWN_SERVICES` and
    which units may be touched is HOSTD's allowlist, both on the far side of the
    relay; a copy of either in this file would be a copy that disagrees. What
    this does refuse is the shape — `restart` with no name, or with two.
    """
    if len(args) == 1 and args[0].isalpha():
        return _command("restart_service", service=args[0].lower())
    return None


def _beacon(args: list[str]) -> Compact | None:
    """``beacon on|off`` — start or stop *transmitting*, never receiving.

    Named for what it does. It was ``lora`` until 2026-09-01, and that word said
    the wrong thing: turning it off does not turn the radio off, and the one
    place that distinction has to be unmistakable is the command that makes the
    satellite go quiet — because "quiet but listening" is the way back into a
    satellite in SAFE, and "deaf" is not a state anything here can recover from.

    The parameter it sets was renamed for the same reason on 2026-09-03:
    ``lora_enabled`` said the radio was being switched off, and since replies
    are gated on the profile rather than on this flag, it does not even silence
    every transmission any more. COMMS still accepts the old spelling on the way
    in — see ``_set_config``.
    """
    if args == ["on"]:
        return _command("set_comms_config", beacon_enabled=True)
    if args == ["off"]:
        return _command("set_comms_config", beacon_enabled=False)
    return None


#: verb → builder. A builder returns None when the arguments make no sense,
#: which is answered exactly like an unknown verb: a person mistyped, tell them.
_TABLE = {
    # The queries: answered by COMMS itself with an immediate beacon; never
    # relayed. See CommsService._query_reply for what each one carries.
    "ping": _bare("ping"),
    "pos": _bare("get_position"),
    "sys": _bare("get_system"),
    "env": _bare("get_environment"),
    "mission": _bare("get_mission"),
    # Relayed onto cubesat/command for OBC and PAYLOAD.
    "photo": _bare("take_photo"),
    "recover": _bare("recover"),
    "safe": _bare("safe_mode"),
    "profile": _profile,
    "restart": _restart,
    "beacon": _beacon,
    # The name this verb had until 2026-09-01, kept because it may be in
    # somebody's muscle memory and answering `err=unknown` to a command that
    # worked last week is worse than carrying one extra line. Undocumented on
    # purpose: `beacon` is what the help and the README teach.
    "lora": _beacon,
}
