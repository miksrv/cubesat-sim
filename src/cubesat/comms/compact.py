"""The compact uplink syntax: what a person can actually type on a phone.

The JSON uplink is fine for a ground station and hopeless for a thumb: quoted
JSON on a phone keyboard in a field is where commands go to be mistyped. So
COMMS additionally accepts a compact form and canonicalises it into JSON
**before** the relay — one translation point, on the way in, and the JSON path
stays verbatim, so there is still no re-encoding step that can quietly disagree
with whoever composed the message. The contract, with the reasoning, is
``docs/concept.md`` → The radio command contract.

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

The table below is deliberately **shorter than the agreed vocabulary**: it
names only the spellings some service can actually answer for today.
``!restart`` is in the contract but its handler is not written (ROADMAP R5),
and translating it now would relay a command into a bus where nothing picks it
up — silence, which is exactly what the ``!`` form exists to end. Until the
handler lands, that spelling gets the honest ``re=? ok=0 err=unknown`` reply
like any other line this build does not know.

The query verbs — ``ping``, ``pos``, ``sys``, ``env``, ``mission`` — are
answered by COMMS itself from its caches, immediately and without a relay:
the radio is the thing being asked.

Ordinary mesh chat does not start with ``!``, so a message that is neither a
``!`` line nor JSON is still just chat and is never answered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Compact:
    """One translated line: the canonical JSON and the command it names."""

    json: str
    command: str


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
    line it means the text was never a command — chat, or JSON for the
    verbatim path.
    """
    stripped = text.strip()
    words = stripped.removeprefix("!").split()
    if not words:
        return None
    verb, args = words[0].lower(), words[1:]
    builder = _TABLE.get(verb)
    if builder is None:
        return None
    return builder(args)


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


def _science(args: list[str]) -> Compact | None:
    if args == ["start"]:
        return _command("science_start")
    if args == ["stop"]:
        return _command("science_stop")
    return None


def _timelapse(args: list[str]) -> Compact | None:
    if args == ["stop"]:
        return _command("stop_timelapse")
    if len(args) == 1 and args[0].isdigit():
        return _command("start_timelapse", interval_sec=int(args[0]))
    return None


def _lora(args: list[str]) -> Compact | None:
    if args == ["on"]:
        return _command("set_comms_config", lora_enabled=True)
    if args == ["off"]:
        return _command("set_comms_config", lora_enabled=False)
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
    "science": _science,
    "timelapse": _timelapse,
    "lora": _lora,
}
