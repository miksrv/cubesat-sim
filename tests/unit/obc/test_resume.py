"""The rule that decides whether a reset gets to end a trip.

Everything here is condition-by-condition: each of the five refuses a case that
would otherwise be indistinguishable from a trip worth resuming, and each one is
the thing the satellite would get wrong if it were dropped.
"""

from __future__ import annotations

from cubesat.common.states import Profile
from cubesat.obc import resume

NOW = 1_788_000_000.0
HOUR = 3600.0

MAX_RESUMES = 3


def previous(profile="FLIGHT", **fields):
    """What HOSTD publishes about the run before this boot."""
    return {
        "profile": profile,
        "written_at": NOW - HOUR,
        "ttl_expires_at": NOW + HOUR,
        "mission_label": "walk to work",
        "resume_count": 0,
        **fields,
    }


def weigh(previous_run, *, now=NOW, max_consecutive=MAX_RESUMES):
    return resume.candidate_from(previous_run, now=now, max_consecutive=max_consecutive)


# ── what may be resumed at all ───────────────────────────────────────────────


def test_an_interrupted_flight_is_a_candidate():
    verdict = weigh(previous())
    assert verdict.resumed is False  # nothing is resumed before the mains is read
    assert verdict.previous == "FLIGHT"
    assert verdict.candidate is not None
    assert verdict.candidate.profile is Profile.FLIGHT
    assert verdict.candidate.mission_label == "walk to work"
    assert verdict.candidate.ttl_minutes == 60.0


def test_only_flight_resumes_itself():
    # EXPO on battery with nobody present is pointless and DEMO is a desk, so
    # neither is worth coming back up without SSH for.
    for profile in ("EXPO", "DEMO", "DIAG", "HOSTED", "MAINTENANCE"):
        verdict = weigh(previous(profile))
        assert verdict.candidate is None
        assert verdict.reason == resume.NOT_RESUMABLE
        # And nothing is said on the radio about an ordinary reboot.
        assert verdict.previous is None


def test_a_profile_this_build_does_not_know_is_refused_rather_than_raised():
    # A file written by a build with a profile since removed. Reading it must
    # not be able to take the satellite down with it.
    verdict = weigh(previous("SCIENCE"))
    assert verdict.reason == resume.NOT_RESUMABLE


def test_no_previous_run_at_all_is_not_an_error():
    for missing in (None, {}, "FLIGHT", []):
        verdict = weigh(missing)
        assert verdict.resumed is False
        assert verdict.candidate is None


# ── the TTL from before the reset ────────────────────────────────────────────


def test_the_remaining_ttl_is_what_survives_the_reset():
    # Ten minutes into a 600-minute strap: what comes back is the remainder, not
    # a fresh full term. A trip that resets four times must not thereby become
    # unbounded.
    verdict = weigh(previous(ttl_expires_at=NOW + 590 * 60.0))
    assert verdict.candidate.ttl_minutes == 590.0


def test_a_trip_whose_strap_already_ran_out_does_not_restart():
    verdict = weigh(previous(ttl_expires_at=NOW - 1.0))
    assert verdict.candidate is None
    assert verdict.reason == resume.TTL_EXPIRED
    # Said on the radio: this is a refusal somebody may be waiting on.
    assert verdict.previous == "FLIGHT"


def test_a_profile_that_had_no_ttl_resumes_without_one():
    for absent in (None, "soon", True):
        verdict = weigh(previous(ttl_expires_at=absent))
        assert verdict.candidate is not None
        assert verdict.candidate.ttl_minutes is None


# ── the boot-loop fence ──────────────────────────────────────────────────────


def test_resumes_in_a_row_are_allowed_up_to_the_fence():
    # Deliberately not one: a parachute opening is a burst of jolts, and a burst
    # must not exhaust the budget the descent needs.
    for count in range(MAX_RESUMES):
        assert weigh(previous(resume_count=count)).candidate is not None


def test_the_fence_stops_a_boot_loop():
    verdict = weigh(previous(resume_count=MAX_RESUMES))
    assert verdict.candidate is None
    assert verdict.reason == resume.BOOT_LOOP
    assert verdict.previous == "FLIGHT"


def test_a_nonsense_count_reads_as_none_taken():
    # The file is written on the way out of a run that may have been cut short.
    for junk in ("many", None, True, 1.5):
        assert weigh(previous(resume_count=junk)).candidate is not None


def test_a_label_that_is_not_a_name_is_dropped_rather_than_carried():
    for junk in ("", None, 7, {"$ne": None}):
        assert weigh(previous(mission_label=junk)).candidate.mission_label is None
