import pytest

from cubesat.common import config, profiles
from cubesat.common.states import Profile
from cubesat.obc import deploy
from cubesat.obc.deploy import DeploySelfTest, Outcome


class Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeBus:
    """A bus where a chosen set of addresses answers and nothing else does."""

    def __init__(self, present=(0x20, 0x22, 0x28, 0x36)):
        self.answers = set(present)
        self.probed = []

    def present(self, address):
        self.probed.append(address)
        return address in self.answers


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def spec_for():
    config_ = profiles.load()
    return config_.get


def check(spec, bus, clock, **kwargs):
    return DeploySelfTest(spec, bus=bus, clock=clock, **kwargs)


def satisfy(test, *services):
    for service in services:
        test.note_report(service)


# ── the bus sweep ────────────────────────────────────────────────────────────


def test_the_sweep_covers_exactly_what_the_profile_asked_for(spec_for, clock):
    bus = FakeBus()
    test = check(spec_for(Profile.EXPO), bus, clock)
    test.begin()
    # EPS's gauge plus ADCS's two devices plus PAYLOAD's sensor. DHS and COMMS
    # own nothing on the bus.
    assert sorted(bus.probed) == [0x20, 0x22, 0x28, 0x36]


def test_a_profile_with_no_mission_services_still_sweeps_the_fuel_gauge(spec_for, clock):
    # EPS runs in every profile: a satellite that cannot see its own battery
    # cannot protect its filesystem.
    bus = FakeBus()
    test = check(spec_for(Profile.HOSTED), bus, clock)
    test.begin()
    assert bus.probed == [0x36]


def test_a_profile_that_runs_no_payload_is_not_failed_for_its_sensor(spec_for, clock):
    # The sweep follows the profile's service list, not the whole address map, so
    # a profile with no mission services passes on the fuel gauge alone.
    # MAINTENANCE is the one such profile left: even HOSTED runs COMMS now.
    bus = FakeBus(present=(0x36,))
    test = check(spec_for(Profile.MAINTENANCE), bus, clock)
    test.begin()
    assert test.evaluate() is Outcome.PASSED


def test_eps_is_swept_but_not_waited_for(spec_for, clock):
    # EPS has been up since boot and is in no profile's service list; the 0x36
    # probe already proves the gauge answers, and its cadence during DEPLOY is
    # longer than the window. Its liveness is the health monitor's business.
    test = check(spec_for(Profile.DEMO), FakeBus(), clock)
    test.begin()
    assert 0x36 in test.expected_addresses
    assert "eps" not in test.awaited_services


def test_a_silent_address_fails_the_bring_up(spec_for, clock, caplog):
    bus = FakeBus(present=(0x36, 0x22, 0x20))  # the BNO055 is not answering
    test = check(spec_for(Profile.EXPO), bus, clock)
    with caplog.at_level("ERROR"):
        test.begin()
    assert test.evaluate() is Outcome.FAILED
    assert "0x28" in caplog.text
    assert "BNO055 orientation at 0x28 did not answer" in test.failures


def test_a_missing_address_fails_even_once_everyone_has_reported(spec_for, clock):
    bus = FakeBus(present=(0x36,))
    test = check(spec_for(Profile.DEMO), bus, clock)
    test.begin()
    satisfy(test, "adcs", "payload", "dhs", "comms")
    assert test.evaluate() is Outcome.FAILED


def test_there_is_no_verdict_before_the_sweep_has_run(spec_for, clock):
    test = check(spec_for(Profile.HOSTED), FakeBus(), clock)
    assert test.evaluate() is Outcome.PENDING


def test_the_rtc_is_never_probed():
    # 0x68 is kernel-owned and shows as UU in a bus scan. Probing it from user
    # space is how a working clock gets broken, and DEPLOY has no business there.
    assert 0x68 not in deploy.DEVICE_NAMES
    assert all(0x68 not in addrs for addrs in deploy.SERVICE_ADDRESSES.values())


def test_with_no_bus_the_sweep_is_skipped_rather_than_failed(spec_for, clock, monkeypatch):
    # Under CUBESAT_MOCK_HARDWARE the whole stack runs on a laptop. Every
    # address would read as absent there, which would put a healthy simulation
    # in SAFE.
    monkeypatch.setattr(config, "MOCK_HARDWARE", True)
    test = DeploySelfTest(spec_for(Profile.DEMO), clock=clock)
    test.begin()
    assert test.missing_addresses == []


def test_on_real_hardware_the_shared_bus_is_used(spec_for, monkeypatch):
    # One handle per process; the advisory lock inside it is what actually keeps
    # four processes off each other's transactions.
    from cubesat.hal import i2c

    monkeypatch.setattr(config, "MOCK_HARDWARE", False)
    assert deploy.default_bus() is i2c.shared_bus()


# ── waiting for the subsystems to report ─────────────────────────────────────


def test_it_waits_for_every_service_the_profile_started(spec_for, clock):
    test = check(spec_for(Profile.DEMO), FakeBus(), clock)
    test.begin()
    assert test.silent == ("adcs", "comms", "dhs", "payload")
    assert test.evaluate() is Outcome.PENDING


def test_the_last_report_completes_the_bring_up(spec_for, clock):
    # DIAG runs all four services: the radio's presence is part of the bench
    # evidence, so bring-up is not complete until COMMS reports in too.
    test = check(spec_for(Profile.DIAG), FakeBus(), clock)
    test.begin()
    satisfy(test, "adcs", "payload", "dhs")
    assert test.evaluate() is Outcome.PENDING
    satisfy(test, "comms")
    assert test.evaluate() is Outcome.PASSED


def test_a_service_that_never_reports_fails_when_the_timeout_runs_out(spec_for, clock):
    test = check(spec_for(Profile.DEMO), FakeBus(), clock)
    test.begin()
    satisfy(test, "adcs", "payload", "dhs")
    clock.advance(deploy.DEPLOY_TIMEOUT_SEC - 1)
    assert test.evaluate() is Outcome.PENDING
    clock.advance(2)
    assert test.evaluate() is Outcome.FAILED
    assert test.failures == ("comms never reported",)


def test_a_report_from_something_this_profile_never_started_is_ignored(spec_for, clock):
    # DIAG has no comms. A radio reporting in anyway must not count for anything,
    # and must certainly not be missed when it stops.
    test = check(spec_for(Profile.DIAG), FakeBus(), clock)
    test.begin()
    satisfy(test, "comms", "dashboard")
    assert test.silent == ("adcs", "dhs", "payload")


def test_a_repeated_report_is_logged_once(spec_for, clock, caplog):
    test = check(spec_for(Profile.DEMO), FakeBus(), clock)
    test.begin()
    with caplog.at_level("INFO"):
        satisfy(test, "adcs", "adcs", "adcs")
    assert caplog.text.count("adcs reported in") == 1


def test_the_timeout_lands_before_the_heartbeat_loss_window(spec_for):
    # Both fire on a subsystem that never came up, and "the bring-up self-test
    # failed" is the more informative of the two reasons — so it has to land
    # first by construction, not by the order OBC happens to evaluate them in.
    from cubesat.common import config

    assert deploy.DEPLOY_TIMEOUT_SEC < (
        config.HEARTBEAT_INTERVAL_SEC * config.HEARTBEAT_MISS_THRESHOLD
    )


def test_every_awaited_service_has_a_status_topic_to_be_heard_on():
    # A heartbeat will not do: every service in this project logs a silent device
    # and stays up, so a heartbeat proves the process started and nothing about
    # the sensor. If a service is awaited, it must have a status topic.
    from cubesat.common.profiles import KNOWN_SERVICES
    from cubesat.common.topics import TOPICS

    assert set(deploy.REPORT_TOPICS) == set(KNOWN_SERVICES)
    assert all(key in TOPICS for key in deploy.REPORT_TOPICS.values())


# ── the GNSS fix, which is not a check ───────────────────────────────────────


def test_an_indoor_run_with_no_fix_still_passes(spec_for, clock, caplog):
    # DEMO and EXPO run indoors, where a fix never arrives. Failing on it would
    # send every indoor demonstration to SAFE.
    test = check(spec_for(Profile.EXPO), FakeBus(), clock)
    test.begin()
    satisfy(test, "adcs", "payload", "dhs", "comms")
    test.note_gnss(False)
    assert test.evaluate() is Outcome.PASSED
    with caplog.at_level("INFO"):
        test.log_gnss()
    assert "no GNSS fix yet" in caplog.text


def test_a_fix_that_never_arrived_at_all_is_reported_the_same_way(spec_for, clock, caplog):
    test = check(spec_for(Profile.EXPO), FakeBus(), clock)
    test.begin()
    assert test.gnss_fix is None
    with caplog.at_level("INFO"):
        test.log_gnss()
    assert "no GNSS fix yet" in caplog.text


def test_a_fix_is_noted_when_there_is_one(spec_for, clock, caplog):
    test = check(spec_for(Profile.FLIGHT), FakeBus(), clock)
    test.begin()
    test.note_gnss(True)
    with caplog.at_level("INFO"):
        test.log_gnss()
    assert "GNSS has a fix" in caplog.text
