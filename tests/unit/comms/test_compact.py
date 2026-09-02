"""The compact uplink syntax: a phone keyboard's path to the satellite."""

import json

import pytest

from cubesat.comms import compact


def canonical(text):
    translated = compact.translate(text)
    assert translated is not None, f"{text!r} should translate"
    return json.loads(translated.json), translated.command


# ── the agreed table ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!ping", {"command": "ping"}),
        ("!pos", {"command": "get_position"}),
        ("!sys", {"command": "get_system"}),
        ("!env", {"command": "get_environment"}),
        ("!mission", {"command": "get_mission"}),
        ("!photo", {"command": "take_photo"}),
        ("!recover", {"command": "recover"}),
        ("!safe", {"command": "safe_mode"}),
        ("!profile FLIGHT", {"command": "set_profile", "params": {"profile": "FLIGHT"}}),
        ("!beacon on", {"command": "set_comms_config", "params": {"lora_enabled": True}}),
        ("!beacon off", {"command": "set_comms_config", "params": {"lora_enabled": False}}),
    ],
)
def test_the_contract_table_translates_to_canonical_json(text, expected):
    body, command = canonical(text)
    assert body == expected
    assert command == expected["command"]


def test_a_phone_keyboard_case_is_forgiven():
    # Phones capitalise the first letter on their own terms, and the profile
    # name is upper-cased rather than validated: names are OBC's to validate.
    body, _command = canonical("!Profile demo")
    assert body == {"command": "set_profile", "params": {"profile": "DEMO"}}


def test_a_phone_keyboard_leading_space_is_forgiven():
    # The very first uplink ever sent from a phone arrived as
    # ' !profile HOSTED' — the keyboard slipped a space in — and was dropped
    # as chat. A command that was typed correctly must not die over that.
    assert compact.is_compact(" !profile HOSTED")
    body, _command = canonical(" !profile HOSTED ")
    assert body == {"command": "set_profile", "params": {"profile": "HOSTED"}}


# ── what is answered rather than guessed ─────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "!",  # nothing at all
        "!launch",  # a verb nobody wrote
        "!profile",  # a profile command with no profile
        "!profile DEMO EXPO",  # or with two
        "!science faster",  # an argument the verb does not take
        "!timelapse 30",  # the verb itself is gone: a mission photographs itself
        "!beacon maybe",
        "!ping now",  # a bare verb given an argument
        "!restart",  # which service?
        "!restart adcs payload",  # or two of them
        "!restart cubesat@adcs.service",  # a unit name: the vocabulary names services
    ],
)
def test_a_line_nobody_wrote_is_a_reply_not_a_shrug(text):
    assert compact.translate(text) is None


def test_ordinary_chat_is_not_compact():
    assert not compact.is_compact("anyone out there?")
    assert compact.is_compact("!ping")


# ── the bare spelling ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ping", {"command": "ping"}),
        ("profile FLIGHT", {"command": "set_profile", "params": {"profile": "FLIGHT"}}),
        ("beacon off", {"command": "set_comms_config", "params": {"lora_enabled": False}}),
    ],
)
def test_the_bare_spelling_is_the_same_language(text, expected):
    # One command language however the satellite is reached: the bare verb the
    # dashboard console takes works over the radio too, `!` optional.
    body, _command = canonical(text)
    assert body == expected


def test_restart_names_a_subsystem_and_not_a_systemd_unit():
    """`restart adcs` — the vocabulary talks about subsystems.

    The translation into `cubesat@adcs.service` happens once, inside HOSTD, next
    to the allowlist that bounds it. A ground client able to name a unit would be
    reaching past the vocabulary into systemd, so a unit name does not parse here
    at all — it is answered `err=unknown` like any other line.
    """
    assert compact.translate("restart adcs") == compact.translate("!restart adcs")
    assert canonical("restart dhs") == (
        {"command": "restart_service", "params": {"service": "dhs"}},
        "restart_service",
    )


def test_the_verb_this_one_replaced_still_works():
    """`lora on|off` was renamed to `beacon` on 2026-09-01 — the old word said
    the wrong thing, because turning it off never turned the radio off. It is
    still accepted, undocumented: answering `err=unknown` to a command that
    worked last week is worse than one extra line in the table."""
    from cubesat.comms.compact import translate as _translate

    assert _translate("lora off") == _translate("beacon off")
    assert _translate("!lora on") == _translate("!beacon on")


@pytest.mark.parametrize(
    "text",
    [
        "anyone out there?",  # ordinary chat
        "photo of the pad looks great",  # a known verb inside somebody's sentence
        "ping me when you land",  # likewise
        "",  # nothing at all
    ],
)
def test_chat_that_is_not_exactly_a_command_translates_to_nothing(text):
    # For a bare line the caller treats None as chat and stays silent — only a
    # `!` line declared intent and earns the err=unknown reply.
    assert compact.translate(text) is None
