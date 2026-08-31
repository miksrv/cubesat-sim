"""The LoRa beacon: one line, one complete observation, never truncated.

A Meshtastic message carries at most 240 bytes. A full telemetry packet — the
one COMMS POSTs to the cloud and DHS writes to SQLite — runs to several hundred.
That left two options, and this module is the answer to which one was taken.

**A compact beacon, not chunked telemetry.** Three reasons, in the order they
mattered:

* **One message is one complete observation.** Chunking makes a lost fragment
  void a whole packet, and over a link with no reliable delivery guarantee the
  common case is losing one fragment. Three messages that must all arrive are
  strictly worse than one that stands alone.
* **Airtime is the scarce resource.** LoRa is slow and duty-cycle limited, and
  three messages per cycle cost three times as much of it — on a shared mesh
  other people are also using.
* **The radio's job here is "alive, and where".** The full record is in DHS, on
  the satellite, and it gets collected when the satellite is back on a network.
  The beacon is not a downlink of the archive; it is proof of life with a
  position attached.

**The format is a single line of ``key=value`` pairs**, and that is an
operational property rather than decoration: it is readable in the Meshtastic
phone app, so the satellite can be checked from a phone with no ground station
at all::

    CSAT t=1741863600 st=NOMINAL pr=FLIGHT b=78.2 v=3.94 ep=0 lat=55.7558 \
lon=37.6173 alt=156 sat=23 m=42

    t    unix seconds        st   mission state       pr   platform profile
    b    battery percent     v    pack volts          ep   external power, 1/0
    lat  degrees north       lon  degrees east        alt  metres
    sat  satellites in the fix                        m    mission id
    down present, and only ever ``1``, on the going-down beacon

**``down=1`` is the last message a satellite sends.** COMMS transmits it once on
entering ``CRITICAL``, the only state permitted to power the host off. Without
it, a satellite that shut itself down at 8 % battery leaves a silence
indistinguishable from a crash, a dead radio, or somebody walking out of range;
with it, the ground has a recorded event — where it was, what the battery was
doing, and that it switched itself off on purpose. It is spelled out rather than
abbreviated because it appears once in the life of a mission and has to be
unmistakable to a person reading a phone, not a key they have to look up. There
is no ``down=0``: absent means routine, exactly as with every other field here.

A reader takes the fields it recognises and **ignores the keys it does not**, so
the format can grow without breaking a ground station that has not been updated.
Nothing is positional beyond the ``CSAT`` prefix, which is there so a person can
pick the line out of mesh chat.

Two rules keep it honest.

**It is never truncated.** The pre-rewrite driver silently cut every payload to
28 bytes and transmitted the remainder; removing that bug is why this phase
exists. When a line will not fit, whole fields are dropped — in the documented
order below — and what goes out is still a valid line. A field is either present
and true or absent; there is no third state where it is present and mangled.

**Absent values are omitted, not sent as zero.** A satellite with no GNSS fix
must not report ``lat=0 lon=0``: that is a real place in the Gulf of Guinea,
several hundred kilometres from anywhere, and this project has already been
bitten by it once — see the "tidy zeros" note in ``hal/rpi/tel0157.py``. An
absent field is unambiguous; a zeroed one is a confident lie.

The drop order
--------------

What a person needs from a satellite they cannot see, worst case, is that it is
**alive**, what **state** it is in, what its **battery** is doing and **where**
it is. Everything else is a luxury, and luxuries go first::

    m → sat → alt → ep → v → pr → (lat, lon) → b → st

Read backwards, that is the priority list: ``CSAT t=…`` can never be dropped at
all, then the mission state, then the battery, then the position, then the
profile. ``lat`` and ``lon`` leave together — half a coordinate is not a
degraded position, it is a meaningless one.

``down`` is not in that list either, and never goes: it is the entire reason the
message it appears on was sent, and a going-down beacon that dropped it would be
an ordinary beacon that happened to be the last one — which is precisely the
ambiguity it exists to remove.

The ceiling is ``MAX_RADIO_MESSAGE_BYTES``, taken from beside the ``Radio``
protocol rather than restated here: a limit this module stays under and a driver
enforces must be one number, or it will eventually be two.

With real values the line is around 100 bytes and the worst plausible case is
about 120, so dropping is unreachable in normal operation. It is implemented and
tested anyway, because "unreachable" is what the 28-byte truncation was, too.

**Position is only reported from a live fix.** ADCS keeps publishing the last
known position with ``fix: false`` when the signal is gone, which is right for
telemetry that carries a timestamp — but there is no room in 240 bytes for the
age of a fix, and a coordinate with no age attached is indistinguishable from a
current one. PAYLOAD's photo sidecar makes the same call for the same reason. In
practice the cost is nil: ADCS publishes every 0.5 s in NOMINAL against COMMS'
30 s, so a fix that exists is never stale here.
"""

from __future__ import annotations

import math
from typing import Any

from cubesat.hal.interfaces import MAX_RADIO_MESSAGE_BYTES

#: The magic word a person looks for in a list of mesh messages, matching the
#: node's Meshtastic short name.
PREFIX = "CSAT"

#: Which fields go, and in what order, when the line will not fit. Grouped
#: because ``lat`` without ``lon`` is not a position. See "The drop order".
DROP_ORDER: tuple[tuple[str, ...], ...] = (
    ("m",),
    ("sat",),
    ("alt",),
    ("ep",),
    ("v",),
    ("pr",),
    ("lat", "lon"),
    ("b",),
    ("st",),
)

#: Never dropped, and every key not in ``DROP_ORDER`` must be here — a test
#: pins that, so a field added without a decision about its priority fails
#: rather than silently becoming un-droppable. Without ``t`` the line says
#: nothing at all, and a beacon that says nothing is worse than no beacon
#: because it still costs the airtime; without ``down`` the going-down beacon
#: is indistinguishable from any other. ``re``, ``ok`` and ``err`` are the
#: reply contract (docs/concept.md → The radio command contract): an ack that
#: dropped the name of the command it acknowledges is an ordinary beacon that
#: cost the airtime and answered nothing.
CORE_KEYS = ("t", "down", "re", "ok", "err")


def build(
    *,
    now: float,
    state: str | None = None,
    profile: str | None = None,
    eps: dict[str, Any] | None = None,
    adcs: dict[str, Any] | None = None,
    mission_id: Any = None,
    going_down: bool = False,
    reply: dict[str, str] | None = None,
    limit: int = MAX_RADIO_MESSAGE_BYTES,
) -> str:
    """Assemble one beacon line from whatever the caches actually hold.

    Every argument is optional and every one of them may be missing, half
    populated or the wrong type: these come off MQTT, from subsystems that may
    not have reported yet or may have reported a null because a device did not
    answer. A field that cannot be rendered is left out, and the line is still
    valid.
    """
    fields: dict[str, str] = {}
    _put(fields, "t", _integer(now))
    _put(fields, "st", state)
    if going_down:
        # Third, so it reads as a headline rather than as a footnote after ten
        # fields of telemetry: this is the one line where what happened matters
        # more than what was measured.
        _put(fields, "down", "1")
    if reply:
        # The ack and query fields, right after the headline slot and before
        # the telemetry: ``re=`` is what makes this transmission an answer, and
        # a person on a phone reads left to right. Every reply field is core by
        # construction — never dropped, and never overwritten by the routine
        # telemetry below (``_put`` keeps the first writer; a ``!pos`` answer's
        # ``lat=`` with its honest age must not be replaced or dropped in
        # favour of the schedule's version of the same key). When the line will
        # not fit, the routine telemetry gives way instead.
        for key, value in reply.items():
            _put(fields, key, value)
    _put(fields, "pr", profile)

    if isinstance(eps, dict):
        _put(fields, "b", _decimal(eps.get("battery_percent"), 1))
        _put(fields, "v", _decimal(eps.get("voltage"), 2))
        external = eps.get("external_power")
        _put(fields, "ep", None if external is None else ("1" if external else "0"))

    gnss = adcs.get("gnss") if isinstance(adcs, dict) else None
    # Only a live fix. See the note at the end of the module docstring.
    if isinstance(gnss, dict) and gnss.get("fix"):
        _put(fields, "lat", _decimal(gnss.get("lat"), 4))
        _put(fields, "lon", _decimal(gnss.get("lon"), 4))
        _put(fields, "alt", _decimal(gnss.get("alt"), 0))
        _put(fields, "sat", _integer(gnss.get("satellites")))

    _put(fields, "m", _integer(mission_id))
    return _fit(fields, limit, protected=frozenset(reply or ()))


def _put(fields: dict[str, str], key: str, value: Any) -> None:
    """Add one field, unless there is nothing honest to put in it.

    Whitespace disqualifies a value outright. Every field here renders to a
    number or an enum name so it cannot legitimately contain a space, and one
    that did would split the line into fields nobody wrote — the same class of
    fault as truncation, arriving by a different door.
    """
    if value is None or key in fields:
        # First writer wins: reply fields land before the routine telemetry,
        # and a query's answer must not be overwritten by the schedule's
        # version of the same key.
        return
    text = str(value)
    if not text or any(character.isspace() for character in text):
        return
    fields[key] = text


def _decimal(value: Any, digits: int) -> str | None:
    """Render a measurement, or None if there is not one.

    Booleans are excluded explicitly: ``True`` is an ``int`` in Python, and a
    battery that reported ``True`` would come out as ``1.0`` — a plausible
    reading of a satellite with a flat pack. Infinities and NaN are excluded for
    the same reason, since ``f"{nan:.1f}"`` is the perfectly printable ``nan``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    if digits == 0:
        return str(round(value))
    return f"{value:.{digits}f}"


def _integer(value: Any) -> str | None:
    """Render a count, an id or a whole-second timestamp, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return None if not math.isfinite(value) else str(int(value))
    # DHS reports the mission id as an integer and PAYLOAD stringifies it; both
    # spellings reach here, and neither should become a different number.
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return value
    return None


def _render(fields: dict[str, str]) -> str:
    return " ".join([PREFIX, *(f"{key}={value}" for key, value in fields.items())])


def _size(line: str) -> int:
    """Bytes on the air, not characters: the limit is a byte limit."""
    return len(line.encode("utf-8"))


def _fit(fields: dict[str, str], limit: int, *, protected: frozenset[str] = frozenset()) -> str:
    """Drop whole fields until the line fits. Never cut one in half.

    ``protected`` names the reply fields: a query's answer may share a key with
    the routine telemetry (``!pos`` answers with ``lat=``), and the drop order
    must sacrifice the schedule's furniture, never the parcel that was asked
    for.

    If even the core does not fit, the core goes out anyway. A limit below
    ``CSAT t=…`` is a programming error rather than a situation to degrade
    gracefully into, and truncating the timestamp would produce a line that
    parses cleanly and states the wrong time — the exact failure this module is
    written to make impossible.
    """
    line = _render(fields)
    if _size(line) <= limit:
        return line
    for group in DROP_ORDER:
        for key in group:
            if key not in protected:
                fields.pop(key, None)
        line = _render(fields)
        if _size(line) <= limit:
            return line
    return line
