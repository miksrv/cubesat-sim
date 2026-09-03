import pytest

from cubesat.common import profiles
from cubesat.common.states import Profile
from cubesat.obc.profile_machine import ProfileMachine


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance_minutes(self, minutes):
        self.now += minutes * 60.0


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def applied():
    return []


@pytest.fixture
def machine(clock, applied):
    return ProfileMachine(
        profiles.load(),
        lambda profile, request_id, ttl, label, resume: applied.append(
            (profile, request_id, ttl, label, resume)
        ),
        clock=clock,
        wall_clock=clock,
    )


def host_status(profile, requested=None, ttl_minutes=None, now=0.0, **extra):
    """A host_status payload as HOSTD publishes it, retained.

    ``ttl_expires_at`` is an absolute timestamp computed by HOSTD, not a duration
    computed here: HOSTD holds the applied profile, so it owns the deadline, and
    OBC reading it back is what lets an expiry survive an OBC restart.
    """
    payload = {
        "timestamp": 1.0,
        "profile": profile,
        "profile_requested": requested if requested is not None else profile,
        "ttl_expires_at": None if ttl_minutes is None else now + ttl_minutes * 60.0,
        **extra,
    }
    return payload


# ── requesting ───────────────────────────────────────────────────────────────


def test_a_valid_profile_is_translated_into_an_apply_profile_action(machine, applied):
    assert machine.request("EXPO", request_id="req_010") is True
    assert applied == [(Profile.EXPO, "req_010", None, None, False)]


def test_an_unknown_profile_never_reaches_hostd(machine, applied, caplog):
    # HOSTD has no decision logic at all — it is the hands. A profile that does
    # not exist is a decision, and decisions are refused here.
    with caplog.at_level("ERROR"):
        assert machine.request("MARS") is False
    assert applied == []
    assert "unknown profile" in caplog.text


def test_a_profile_missing_from_the_yaml_is_refused_too(clock, applied):
    trimmed = profiles.load()
    del trimmed.profiles[Profile.DIAG]
    machine = ProfileMachine(
        trimmed,
        lambda profile, rid, ttl, label, resume: applied.append((profile, rid)),
        clock=clock,
    )
    assert machine.request(Profile.DIAG) is False


# ── reconciling against what HOSTD achieved ──────────────────────────────────


def test_the_achieved_profile_is_learned_only_from_host_status(machine):
    # OBC writes no file and restores nothing: every boot starts in HOSTED, which
    # is what makes a power cycle a recovery path from any profile.
    assert machine.achieved is None
    update = machine.observe(host_status("EXPO"))
    assert update.achieved is Profile.EXPO
    # The spec that comes with it is the achieved profile's own, asserted by
    # identity rather than by one of its values: which knobs EXPO sets is the
    # profile file's business and legitimately changes (its persistence did, on
    # 2026-09-01), while "the spec follows the achieved profile" is this
    # machine's business and must not.
    assert machine.spec.name is Profile.EXPO


def test_a_profile_nobody_asked_for_is_adopted_as_the_truth(machine):
    # `systemctl restart cubesat@obc` mid-demo: HOSTD holds the applied profile,
    # so whatever it reports is what the platform is, and adopting it is how OBC
    # recovers without disturbing the access point.
    update = machine.observe(host_status("EXPO"))
    assert update.matches_request is True
    assert update.changed is True
    assert update.active is True


def test_a_mismatch_is_not_treated_as_an_application(machine, caplog):
    machine.request("FLIGHT")
    with caplog.at_level("ERROR"):
        update = machine.observe(host_status("HOSTED", requested="HOSTED"))
    assert update.matches_request is False
    assert "requested FLIGHT" in caplog.text
    # Still recorded as the truth about the platform — it just is not an answer
    # to our request, so the caller must not advance to DEPLOY on it.
    assert machine.achieved is Profile.HOSTED


def test_a_partial_application_is_reported_by_hostd_and_logged(machine, caplog):
    # The AP failed to come up. profile versus profile_requested is exactly what
    # makes that debuggable instead of mysterious.
    machine.request("EXPO")
    with caplog.at_level("ERROR"):
        machine.observe(host_status("HOSTED", requested="EXPO"))
    assert "applied HOSTED only partially" in caplog.text


def test_hostd_errors_are_surfaced(machine, caplog):
    with caplog.at_level("ERROR"):
        machine.observe(host_status("DEMO", errors=["cubesat@comms.service failed"]))
    assert "cubesat@comms.service failed" in caplog.text


def test_an_empty_error_list_says_nothing(machine, caplog):
    with caplog.at_level("ERROR"):
        machine.observe(host_status("DEMO", errors=[]))
    assert caplog.text == ""


def test_the_same_profile_again_is_not_a_change(machine):
    machine.observe(host_status("DEMO"))
    assert machine.observe(host_status("DEMO")).changed is False


def test_a_standby_profile_is_not_active(machine):
    assert machine.observe(host_status("HOSTED")).active is False
    assert machine.observe(host_status("MAINTENANCE")).active is False


@pytest.mark.parametrize("payload", [{}, {"profile": None}])
def test_a_host_status_with_no_profile_adopts_nothing_but_is_not_silent(
    machine, payload, caplog
):
    # No profile means nothing to adopt and nothing will advance. Returning None
    # is right; being quiet about it is not, because then a satellite parked in
    # STANDBY has no visible reason for being there.
    with caplog.at_level("ERROR"):
        assert machine.observe(payload) is None
    assert "has not applied any profile" in caplog.text
    assert machine.achieved is None


@pytest.mark.parametrize("value", ["MARS", 7, ""])
def test_a_profile_this_build_does_not_know_is_not_adopted(machine, value, caplog):
    # A newer HOSTD must not be able to put OBC into a profile it has no
    # definition for; it would have no service list to deploy against.
    with caplog.at_level("ERROR"):
        assert machine.observe(host_status(value)) is None
    assert machine.achieved is None


# ── the mission label ────────────────────────────────────────────────────────


def test_the_label_from_the_command_becomes_the_active_label(machine):
    machine.request("FLIGHT", mission_label="walk to work")
    machine.observe(host_status("FLIGHT"))
    assert machine.label == "walk to work"


def test_relabelling_the_same_profile_updates_the_label(machine):
    # A label is a name for the recording, not its identity. The profile did not
    # change, so DHS keeps the mission it already has and only the name moves;
    # treating a rename as a mission boundary would split one walk into two.
    machine.request("FLIGHT", mission_label="walk to work")
    update = machine.observe(host_status("FLIGHT"))
    machine.request("FLIGHT", mission_label="walk home")
    update = machine.observe(host_status("FLIGHT"))
    assert machine.label == "walk home"
    assert update.changed is False


def test_a_republished_host_status_does_not_clear_the_label(machine):
    machine.request("EXPO", mission_label="school visit")
    machine.observe(host_status("EXPO"))
    machine.observe(host_status("EXPO"))
    assert machine.label == "school visit"


def test_a_new_profile_without_a_label_clears_the_old_one(machine):
    machine.request("FLIGHT", mission_label="walk to work")
    machine.observe(host_status("FLIGHT"))
    machine.request("DEMO")
    machine.observe(host_status("DEMO"))
    assert machine.label is None


# ── the TTL: OBC decides what expiry means, HOSTD holds the deadline ─────────


def test_the_profile_ttl_travels_with_the_request(machine, applied):
    # FLIGHT is sized to a working day, and that duration comes from the profile
    # definition — but it is sent to HOSTD rather than timed here, so that HOSTD
    # can turn it into an absolute deadline and publish it retained.
    machine.request("FLIGHT")
    assert applied == [(Profile.FLIGHT, None, 600, None, False)]


def test_a_command_can_override_the_profile_s_own_ttl(machine, applied):
    machine.request("FLIGHT", ttl_minutes=5)
    assert applied == [(Profile.FLIGHT, None, 5, None, False)]


def test_the_deadline_comes_from_hostd_not_from_a_local_timer(machine, clock):
    # Deciding that a profile has been on long enough is OBC's call; knowing when
    # it started is HOSTD's, because HOSTD is what holds the applied profile.
    # Reading the deadline back is what makes an expiry survive an OBC restart:
    # a FLIGHT profile whose safety net evaporates on a restart is the failure
    # this arrangement closes.
    machine.request("FLIGHT")
    machine.observe(host_status("FLIGHT", ttl_minutes=600, now=clock.now))
    assert machine.expired() is False
    clock.advance_minutes(600)
    assert machine.expired() is True


def test_a_restarted_obc_adopts_the_deadline_already_in_flight(machine, clock):
    # No request of its own: this is the retained host_status arriving on
    # subscribe after `systemctl restart cubesat@obc`. Ten minutes of the TTL are
    # already gone, and the deadline has to reflect that rather than restarting.
    clock.now = 1_000_000.0
    machine.observe(host_status("FLIGHT", ttl_minutes=600, now=clock.now - 590 * 60.0))
    assert machine.expired() is False
    clock.advance_minutes(11)
    assert machine.expired() is True


def test_a_nonsense_deadline_is_ignored(machine, clock):
    machine.request("FLIGHT")
    machine.observe(host_status("FLIGHT", ttl_expires_at="soon"))
    clock.advance_minutes(10_000)
    assert machine.expired() is False


def test_the_default_profile_never_expires(machine, clock):
    # It is where an expiry sends us; giving it one of its own would be a loop
    # with nowhere to land — so the deadline is dropped even if HOSTD publishes one.
    machine.request("HOSTED", ttl_minutes=1)
    machine.observe(host_status("HOSTED", ttl_minutes=1, now=clock.now))
    clock.advance_minutes(120)
    assert machine.expired() is False
    assert machine.deadline is None


def test_a_profile_with_no_ttl_runs_until_told_otherwise(machine, clock):
    machine.request("EXPO")
    machine.observe(host_status("EXPO"))
    clock.advance_minutes(10_000)
    assert machine.expired() is False


def test_expiry_asks_for_the_default_profile(machine, clock, applied, caplog):
    machine.request("DIAG")
    machine.observe(host_status("DIAG", ttl_minutes=120, now=clock.now))
    applied.clear()
    clock.advance_minutes(120)
    with caplog.at_level("WARNING"):
        assert machine.request_default_on_expiry() is True
    assert applied == [(Profile.HOSTED, None, None, None, False)]
    assert "expired" in caplog.text


def test_a_live_profile_is_left_alone(machine, applied):
    machine.request("DIAG")
    machine.observe(host_status("DIAG"))
    applied.clear()
    assert machine.request_default_on_expiry() is False
    assert applied == []


def test_expiry_asks_once_even_if_hostd_never_answers(machine, clock, applied):
    # Otherwise a HOSTD that has died produces one apply_profile per tick for the
    # rest of the flight.
    machine.request("DIAG")
    machine.observe(host_status("DIAG"))
    clock.advance_minutes(200)
    machine.request_default_on_expiry()
    applied.clear()
    assert machine.request_default_on_expiry() is False
    assert applied == []


def test_a_republished_host_status_does_not_push_the_expiry_out(machine, clock):
    machine.request("DIAG")
    machine.observe(host_status("DIAG"))
    deadline = machine.deadline
    clock.advance_minutes(60)
    machine.observe(host_status("DIAG"))
    assert machine.deadline == deadline


def test_a_boot_time_failure_is_diagnosed_even_with_no_profile_to_adopt(machine, caplog):
    # HOSTD reports a null profile when nothing has ever fully applied. There is
    # nothing for OBC to adopt, and it will sit in STANDBY — so this log line is
    # the entire explanation an operator gets, and swallowing it with the
    # unusable profile name would leave a silent satellite and no reason why.
    with caplog.at_level("ERROR"):
        machine.observe(
            {
                "timestamp": 1.0,
                "profile": None,
                "profile_requested": "EXPO",
                "errors": ["nmcli device wifi hotspot: exit 4"],
            }
        )
    assert "asked for 'EXPO'" in caplog.text
    assert "hotspot" in caplog.text
    assert machine.achieved is None


def test_a_status_with_neither_profile_nor_errors_still_says_something(machine, caplog):
    with caplog.at_level("ERROR"):
        machine.observe({"timestamp": 1.0, "profile": None})
    assert "has not applied any profile" in caplog.text


def test_a_hostile_requested_name_cannot_forge_a_log_line(machine, caplog):
    # profile_requested is the one field HOSTD does not guarantee to be a real
    # profile, and it originates in a ground command that may have arrived over
    # the radio. It must reach the log as data, not as extra lines.
    with caplog.at_level("ERROR"):
        machine.observe(
            {
                "profile": None,
                "profile_requested": "EXPO\nERROR all systems nominal",
                "errors": ["nope"],
            }
        )
    assert "\\nERROR" in caplog.text
    assert machine.achieved is None
