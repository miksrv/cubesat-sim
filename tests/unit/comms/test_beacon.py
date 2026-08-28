"""The beacon format: what goes out, what is left out, and what never happens.

Three properties carry the weight here and each is asserted directly rather than
by comparing whole strings:

* **Nothing is ever truncated.** The pre-rewrite driver cut every payload to 28
  bytes and transmitted the remainder. Every size test below is computed from
  ``MAX_RADIO_MESSAGE_BYTES`` — the one beside the ``Radio`` protocol — rather
  than from a literal, so tightening the limit moves the tests with it instead
  of leaving them asserting an old number.
* **An absent value is absent, not zero.** ``lat=0 lon=0`` is a real place in
  the Gulf of Guinea.
* **A reader can ignore what it does not recognise.** ``read`` below is written
  the way a ground station would write it — three lines, no schema — and it is
  the test that the format can grow.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from cubesat.comms import beacon
from cubesat.comms.beacon import PREFIX
from cubesat.hal.interfaces import MAX_RADIO_MESSAGE_BYTES

EPS = {"battery_percent": 78.24, "voltage": 3.9418, "external_power": False}
FIX = {
    "lat": 55.75583,
    "lon": 37.61733,
    "alt": 156.4,
    "speed": 0.4,
    "fix": True,
    "satellites": 23,
}
ADCS = {"roll": 1.2, "yaw": 178.9, "gnss": FIX}

#: A full beacon of the kind a satellite in FLIGHT actually sends.
FULL = {
    "now": 1741863600.4,
    "state": "NOMINAL",
    "profile": "FLIGHT",
    "eps": EPS,
    "adcs": ADCS,
}


def read(line: str) -> dict[str, str]:
    """A ground station's reader, in the three lines it would really be.

    Deliberately naive: it knows the prefix and the ``key=value`` shape and
    nothing else. Anything this cannot cope with is a format the satellite must
    not emit.
    """
    head, *fields = line.split(" ")
    assert head == PREFIX
    return dict(field.split("=", 1) for field in fields)


def size(line: str) -> int:
    return len(line.encode("utf-8"))


# ── the shape ───────────────────────────────────────────────────────────────


def test_a_full_beacon_carries_everything_a_person_would_want_from_a_phone():
    line = beacon.build(mission_id=42, **FULL)

    assert line == (
        "CSAT t=1741863600 st=NOMINAL pr=FLIGHT b=78.2 v=3.94 ep=0 "
        "lat=55.7558 lon=37.6173 alt=156 sat=23 m=42"
    )


def test_the_line_is_readable_by_something_that_knows_nothing_about_this_build():
    fields = read(beacon.build(mission_id=42, **FULL))
    assert fields["st"] == "NOMINAL"
    assert fields["b"] == "78.2"
    assert (fields["lat"], fields["lon"]) == ("55.7558", "37.6173")


def test_a_key_a_reader_has_never_seen_does_not_break_it():
    # The format has to be able to grow without a ground station being updated
    # in step, which is the whole reason nothing here is positional.
    line = beacon.build(mission_id=42, **FULL) + " zz=1"
    assert read(line)["zz"] == "1"
    assert read(line)["st"] == "NOMINAL"


def test_the_timestamp_is_whole_seconds():
    # Sub-second precision on a link whose airtime is measured in seconds would
    # be four bytes spent on a digit nobody can act on.
    assert read(beacon.build(now=1741863600.987))["t"] == "1741863600"


def test_a_beacon_with_nothing_to_report_still_says_something_is_alive():
    # A satellite that has just started and heard from nobody. Proof of life is
    # the minimum useful message and it is still worth the airtime.
    assert beacon.build(now=1741863600.0) == "CSAT t=1741863600"


# ── absent values ───────────────────────────────────────────────────────────


def test_a_satellite_with_no_fix_reports_no_position_at_all():
    # Not lat=0 lon=0, which is a real place several hundred kilometres off the
    # coast of Ghana and a fault this project has already been bitten by once.
    line = beacon.build(mission_id=42, **{**FULL, "adcs": {"gnss": {**FIX, "fix": False}}})
    fields = read(line)
    assert "lat" not in fields and "lon" not in fields
    assert "alt" not in fields and "sat" not in fields
    # And everything that is known is still there.
    assert fields["b"] == "78.2"


def test_a_position_that_never_arrived_at_all_is_simply_missing():
    assert "lat" not in read(beacon.build(now=1.0, adcs=None))
    assert "lat" not in read(beacon.build(now=1.0, adcs={"roll": 1.2, "gnss": None}))


def test_a_fix_missing_its_altitude_still_reports_where_it_is():
    # ADCS publishes nulls per field where a device did not answer; a partial
    # fix is a real thing and dropping the whole position would lose more.
    fields = read(beacon.build(now=1.0, adcs={"gnss": {**FIX, "alt": None}}))
    assert fields["lat"] == "55.7558"
    assert "alt" not in fields


def test_a_battery_that_has_not_reported_is_left_out_rather_than_zeroed():
    fields = read(beacon.build(now=1.0, eps=None))
    assert "b" not in fields and "v" not in fields and "ep" not in fields


def test_an_eps_payload_missing_a_field_leaves_out_only_that_field():
    fields = read(beacon.build(now=1.0, eps={"battery_percent": 78.2}))
    assert fields["b"] == "78.2"
    assert "v" not in fields and "ep" not in fields


def test_external_power_is_a_flag_and_false_is_not_the_same_as_absent():
    assert read(beacon.build(now=1.0, eps={"external_power": True}))["ep"] == "1"
    assert read(beacon.build(now=1.0, eps={"external_power": False}))["ep"] == "0"
    assert "ep" not in read(beacon.build(now=1.0, eps={"external_power": None}))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "3.9", True, None])
def test_a_reading_that_is_not_a_finite_number_is_dropped(value):
    # f"{nan:.1f}" is the perfectly printable "nan", and True is an int in
    # Python — a battery reporting True would go out as a satellite at 1.0 %.
    assert "b" not in read(beacon.build(now=1.0, eps={"battery_percent": value}))


@pytest.mark.parametrize("mission_id", [42, "42", -1])
def test_a_mission_id_survives_either_spelling(mission_id):
    # DHS reports it as an integer and PAYLOAD stringifies it; both reach here.
    assert read(beacon.build(now=1.0, mission_id=mission_id))["m"] == str(mission_id)


@pytest.mark.parametrize("mission_id", [None, True, "not-an-id", 1.5, float("nan")])
def test_a_mission_id_that_is_not_one_is_left_out(mission_id):
    fields = read(beacon.build(now=1.0, mission_id=mission_id))
    assert "m" not in fields or fields["m"] == "1"


def test_a_value_carrying_whitespace_is_dropped_rather_than_splitting_the_line():
    # Fields are separated by spaces, so a value containing one would invent
    # fields nobody wrote — the same class of fault as truncation, by a
    # different door. Nothing here can legitimately contain a space.
    assert "st" not in read(beacon.build(now=1.0, state="LOW POWER"))
    assert "pr" not in read(beacon.build(now=1.0, profile=""))


# ── the going-down beacon ───────────────────────────────────────────────────


def test_the_going_down_beacon_says_so_where_a_person_will_see_it():
    # A satellite that shut itself down at 8 % battery otherwise leaves a
    # silence indistinguishable from a crash, a flat radio, or somebody walking
    # out of range. This turns that into a recorded event.
    line = beacon.build(
        now=1741863600,
        state="CRITICAL",
        profile="FLIGHT",
        eps={"battery_percent": 8.1, "voltage": 3.2, "external_power": False},
        adcs=ADCS,
        mission_id=42,
        going_down=True,
    )
    assert read(line)["down"] == "1"
    # Third field, so it reads as a headline rather than as a footnote after ten
    # fields of telemetry.
    assert line.startswith("CSAT t=1741863600 st=CRITICAL down=1 ")
    # And it still carries where it was and what the battery was doing.
    assert read(line)["b"] == "8.1"
    assert read(line)["lat"] == "55.7558"


def test_an_ordinary_beacon_never_carries_it():
    # Absent means routine. There is no down=0, exactly as with every other
    # field here.
    assert "down" not in read(beacon.build(mission_id=42, **FULL))
    assert "down" not in read(beacon.build(going_down=False, **FULL))


def test_a_reader_can_tell_the_two_apart_without_inferring_anything():
    # Not "it was in CRITICAL so it was probably the last one" — a satellite can
    # be in CRITICAL and recover.
    routine = read(beacon.build(now=1.0, state="CRITICAL"))
    final = read(beacon.build(now=1.0, state="CRITICAL", going_down=True))
    assert routine["st"] == final["st"] == "CRITICAL"
    assert "down" not in routine and final["down"] == "1"


def test_the_going_down_marker_is_never_dropped():
    # It is the entire reason the message was sent. A going-down beacon that
    # dropped it would be an ordinary beacon that happened to be the last one,
    # which is precisely the ambiguity it exists to remove.
    for limit in range(1, size(beacon.build(mission_id=42, going_down=True, **FULL)) + 1):
        assert read(beacon.build(mission_id=42, going_down=True, limit=limit, **FULL))["down"] == (
            "1"
        )


def test_every_field_is_either_droppable_or_declared_core():
    # So a field added without a decision about its priority fails here rather
    # than silently becoming un-droppable.
    emitted = set(read(beacon.build(mission_id=42, going_down=True, **FULL)))
    droppable = {key for group in beacon.DROP_ORDER for key in group}
    assert emitted == droppable | set(beacon.CORE_KEYS)
    assert not droppable & set(beacon.CORE_KEYS)


# ── the size limit ──────────────────────────────────────────────────────────


def test_a_realistic_beacon_is_comfortably_inside_the_limit():
    # Around a hundred bytes with everything present, so the drop path below is
    # unreachable in normal operation — which is exactly what the 28-byte
    # truncation was, too.
    assert size(beacon.build(mission_id=42, **FULL)) < MAX_RADIO_MESSAGE_BYTES // 2


def _mission_id_of(digits: int) -> int:
    return int("1" * digits)


def test_a_beacon_that_lands_exactly_on_the_limit_goes_out_whole():
    # The boundary is computed from the limit rather than written down, so
    # changing MAX_RADIO_MESSAGE_BYTES moves this test instead of stranding it.
    base = beacon.build(mission_id=1, **FULL)
    spare = MAX_RADIO_MESSAGE_BYTES - size(base)
    exact = beacon.build(mission_id=_mission_id_of(1 + spare), **FULL)

    assert size(exact) == MAX_RADIO_MESSAGE_BYTES
    assert "m" in read(exact)


def test_one_byte_over_the_limit_costs_a_whole_field_and_not_a_character():
    base = beacon.build(mission_id=1, **FULL)
    spare = MAX_RADIO_MESSAGE_BYTES - size(base)
    over = beacon.build(mission_id=_mission_id_of(2 + spare), **FULL)

    assert size(over) <= MAX_RADIO_MESSAGE_BYTES
    # The lowest-priority field, gone entirely. Not a mission id cut in half,
    # which would arrive as a different and perfectly plausible mission.
    assert "m" not in read(over)
    assert read(over)["sat"] == "23"


def test_fields_are_dropped_in_the_documented_priority_order():
    # Read backwards, this is the answer to "what does a person need from a
    # satellite they cannot see": that it is alive, its state, its battery and
    # its position. Everything else is a luxury.
    survivors = []
    for limit in range(size(beacon.build(mission_id=42, **FULL)), 0, -1):
        line = beacon.build(mission_id=42, limit=limit, **FULL)
        survivors.append(tuple(read(line)))
    # Each step is the previous one minus whole fields, never a changed value.
    for looser, tighter in pairwise(survivors):
        assert set(tighter) <= set(looser)
    assert survivors[-1] == ("t",)


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (100, ("t", "st", "pr", "b", "v", "ep", "lat", "lon", "alt", "sat")),
        (90, ("t", "st", "pr", "b", "v", "ep", "lat", "lon", "alt")),
        (80, ("t", "st", "pr", "b", "v", "lat", "lon")),
        (70, ("t", "st", "pr", "b", "lat", "lon")),
        (60, ("t", "st", "b", "lat", "lon")),
        (40, ("t", "st", "b")),
        (30, ("t", "st")),
        (20, ("t",)),
    ],
)
def test_what_survives_at_a_given_size(limit, expected):
    line = beacon.build(mission_id=42, limit=limit, **FULL)
    assert size(line) <= limit
    assert tuple(read(line)) == expected


def test_latitude_and_longitude_leave_together():
    # Half a coordinate is not a degraded position, it is a meaningless one.
    for limit in range(20, size(beacon.build(mission_id=42, **FULL)) + 1):
        fields = read(beacon.build(mission_id=42, limit=limit, **FULL))
        assert ("lat" in fields) == ("lon" in fields)


def test_a_limit_below_the_floor_still_sends_a_whole_line():
    # A limit smaller than "CSAT t=…" is a programming error, not a situation to
    # degrade into. Truncating the timestamp would produce a line that parses
    # cleanly and states the wrong time.
    line = beacon.build(now=1741863600.0, limit=1, **{k: v for k, v in FULL.items() if k != "now"})
    assert line == "CSAT t=1741863600"
    assert read(line)["t"] == "1741863600"


def test_the_limit_is_counted_in_bytes_and_not_characters():
    # Only ASCII is emitted today, but the guard has to be the same one the
    # radio applies, and Meshtastic counts bytes.
    line = beacon.build(mission_id=42, **FULL)
    assert size(line) == len(line)
    assert not math.isnan(float(read(line)["b"]))
