"""``last-profile``: the file HOSTD writes and two readers read.

It is written on the way out of a run that may be cut short at any moment, so
every test here is about tolerating what a half-written or older file can say.
"""

from __future__ import annotations

import json

import pytest

from cubesat.common import last_profile


@pytest.fixture
def path(tmp_path):
    return tmp_path / "last-profile"


def test_a_round_trip_keeps_every_field(path):
    written = last_profile.PreviousRun(
        profile="FLIGHT",
        written_at=1_788_000_000.0,
        ttl_expires_at=1_788_003_600.0,
        mission_label="walk to work",
        resume_count=2,
    )
    last_profile.write(path, written)
    assert last_profile.read(path) == written


def test_the_pre_2026_09_03_spelling_still_parses(path):
    # A satellite upgraded in the field has this file on its card, and the first
    # thing the new build does is read it.
    path.write_text("FLIGHT\n")
    previous = last_profile.read(path)
    assert previous.profile == "FLIGHT"
    assert previous.resume_count == 0
    assert previous.ttl_expires_at is None


def test_a_missing_file_is_not_an_error(path):
    assert last_profile.read(path) is None


def test_an_empty_file_says_nothing(path):
    path.write_text("   \n")
    assert last_profile.read(path) is None


def test_a_json_document_that_is_not_an_object_says_nothing(path):
    path.write_text("[1, 2, 3]")
    assert last_profile.read(path) is None


def test_a_field_of_the_wrong_type_is_dropped_and_the_rest_survives(path):
    # Half of this file is worth more than none of it: the profile name is what
    # the resume rule needs, and a mangled TTL must not take it down with it.
    path.write_text(
        json.dumps(
            {
                "profile": "FLIGHT",
                "written_at": "yesterday",
                "ttl_expires_at": True,
                "mission_label": 7,
                "resume_count": "many",
            }
        )
    )
    previous = last_profile.read(path)
    assert previous.profile == "FLIGHT"
    assert previous.written_at is None
    assert previous.ttl_expires_at is None
    assert previous.mission_label is None
    assert previous.resume_count == 0


def test_an_unreadable_file_reads_as_nothing(tmp_path):
    # A directory where the file should be: what HOSTD does about it is report
    # and carry on, so this must not raise.
    assert last_profile.read(tmp_path) is None


def test_a_profile_name_is_never_coerced_into_an_enum(path):
    # A file naming a profile this build no longer defines has to parse; whether
    # it means anything is the caller's decision — see obc/resume.py.
    path.write_text(json.dumps({"profile": "SCIENCE"}))
    assert last_profile.read(path).profile == "SCIENCE"
