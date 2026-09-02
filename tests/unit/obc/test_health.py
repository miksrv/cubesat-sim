import pytest

from cubesat.obc.health import HealthMonitor


class Clock:
    """A hand-cranked monotonic clock, so a 30 s timeout costs no wall time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def monitor(clock):
    # 10 s heartbeats, 3 misses — the committed defaults, spelled out so the
    # arithmetic in these tests is visible.
    return HealthMonitor(clock=clock, interval=10.0, threshold=3)


def beat(monitor, *services, alive=True):
    for service in services:
        monitor.note({"service": service, "alive": alive, "timestamp": 0})


def keep_alive(monitor, clock, seconds, *services):
    """Advance the clock in heartbeat-sized steps, beating for ``services``."""
    for _ in range(int(seconds // 10)):
        clock.advance(10.0)
        beat(monitor, *services)


def test_eps_is_watched_even_with_no_profile_services(monitor):
    # EPS runs in every profile and is outside profile control: it is the only
    # source of the telemetry that drives CRITICAL.
    monitor.watch(())
    assert monitor.watched == {"eps"}


def test_the_watch_list_follows_the_active_profile(monitor):
    monitor.watch(["adcs", "payload", "dhs", "comms"])
    assert monitor.watched == {"eps", "adcs", "payload", "dhs", "comms"}


def test_a_service_the_profile_never_started_is_not_watched(monitor, clock):
    # Otherwise HOSTED — which runs OBC and EPS and nothing else — would put a
    # perfectly healthy satellite in SAFE for not running the things it was told
    # not to run.
    monitor.watch(())
    keep_alive(monitor, clock, 600, "eps")
    beat(monitor, "adcs", "payload", "dhs", "comms", alive=False)
    assert monitor.lost() == ()


def test_silence_past_three_intervals_is_a_loss(monitor, clock):
    monitor.watch(["adcs"])
    beat(monitor, "adcs", "eps")
    clock.advance(29.0)
    assert monitor.lost() == ()
    clock.advance(2.0)
    assert monitor.lost() == ("adcs", "eps")


def test_a_heartbeat_keeps_a_slow_subsystem_alive(monitor, clock):
    # A subsystem polling every 300 s in LOW_POWER must not be declared lost for
    # doing exactly what it was told; the heartbeat runs on its own interval.
    monitor.watch(["payload"])
    keep_alive(monitor, clock, 400, "payload", "eps")
    assert monitor.lost() == ()


def test_a_goodbye_is_acted_on_immediately(monitor):
    # The MQTT last will, or a clean shutdown. There is nothing to be gained by
    # waiting out a timeout for a service that already said it is gone.
    monitor.watch(["comms"])
    beat(monitor, "comms", alive=False)
    assert monitor.lost() == ("comms",)


def test_a_restarted_service_is_healthy_again(monitor, clock):
    monitor.watch(["dhs"])
    beat(monitor, "dhs", alive=False)
    clock.advance(1.0)
    beat(monitor, "dhs")
    assert monitor.lost() == ()


def test_a_departure_obc_asked_for_is_not_a_loss(monitor, clock):
    # `cubesat restart comms` reaches the service as a SIGTERM, and it says
    # goodbye on the way out. Reading that as a fault latched SAFE on the
    # hardware (2026-09-01) and defeated the command's whole purpose.
    monitor.watch(["comms"])
    monitor.expect_restart("comms")
    beat(monitor, "comms", alive=False)
    assert monitor.lost() == ()

    clock.advance(2.0)
    beat(monitor, "comms")
    assert monitor.lost() == ()


def test_a_service_that_never_comes_back_is_lost_on_the_usual_schedule(monitor, clock):
    # The waiver postpones the protection; it must not switch it off. Nothing in
    # `lost()` knows about restarts — the silence clock restarted with the
    # request, so the service goes stale exactly one grace later.
    monitor.watch(["comms"])
    monitor.expect_restart("comms")
    beat(monitor, "comms", alive=False)

    keep_alive(monitor, clock, monitor.grace - 10.0, "eps")
    assert monitor.lost() == ()
    clock.advance(11.0)
    beat(monitor, "eps")
    assert monitor.lost() == ("comms",)


def test_the_waiver_covers_one_restart_and_not_the_next_departure(monitor):
    # The service came back, so the waiver is spent. A goodbye after that is an
    # ordinary fault again — otherwise one restart would buy a silent death.
    monitor.watch(["dhs"])
    monitor.expect_restart("dhs")
    beat(monitor, "dhs", alive=False)
    beat(monitor, "dhs")

    beat(monitor, "dhs", alive=False)
    assert monitor.lost() == ("dhs",)


def test_a_goodbye_after_the_window_closes_is_a_fault_again(monitor, clock):
    # A restart slower than a service is allowed to be silent is not a restart.
    # Silence alone would have caught this one too; what the expiry buys is that
    # the waiver is not left lying around to forgive some later death.
    monitor.watch(["adcs"])
    monitor.expect_restart("adcs")
    keep_alive(monitor, clock, monitor.grace + 10.0, "eps")
    beat(monitor, "adcs", alive=False)
    assert monitor.lost() == ("adcs",)

    beat(monitor, "adcs")
    beat(monitor, "adcs", alive=False)
    assert monitor.lost() == ("adcs",)


def test_restarting_a_service_the_profile_does_not_run_waives_nothing(monitor):
    # HOSTED watches EPS alone. There is no departure to forgive, and the
    # expectation must not be recorded for something nobody is watching.
    monitor.watch(())
    monitor.expect_restart("adcs")
    monitor.watch(["adcs"])
    beat(monitor, "adcs", alive=False)
    assert monitor.lost() == ("adcs",)


def test_an_expectation_is_forgotten_when_the_profile_stops_asking_for_it(monitor):
    monitor.watch(["comms"])
    monitor.expect_restart("comms")
    monitor.watch(())
    monitor.watch(["comms"])
    beat(monitor, "comms", alive=False)
    assert monitor.lost() == ("comms",)


def test_a_second_request_extends_the_window(monitor, clock):
    # Two restarts in a row: the second one starts its own window rather than
    # inheriting what is left of the first.
    monitor.watch(["payload"])
    monitor.expect_restart("payload")
    keep_alive(monitor, clock, monitor.grace - 10.0, "eps")
    monitor.expect_restart("payload")
    keep_alive(monitor, clock, monitor.grace - 10.0, "eps")
    beat(monitor, "payload", alive=False)
    assert monitor.lost() == ()


def test_a_pending_departure_is_cleared_by_the_request(monitor):
    # The goodbye can arrive before OBC gets to arm anything — a race it cannot
    # win on ordering alone, so arming also forgives a departure already noted.
    monitor.watch(["comms"])
    beat(monitor, "comms", alive=False)
    assert monitor.lost() == ("comms",)
    monitor.expect_restart("comms")
    assert monitor.lost() == ()


def test_a_departure_is_logged_once_however_many_wills_arrive(monitor, caplog):
    monitor.watch(["adcs"])
    with caplog.at_level("WARNING"):
        beat(monitor, "adcs", alive=False)
        beat(monitor, "adcs", alive=False)
    assert caplog.text.count("adcs announced it is gone") == 1


def test_a_new_service_gets_the_full_grace_before_its_first_beat(monitor, clock):
    # systemd has only just started it. Counting from some earlier zero would
    # mean every profile switch declared its own new subsystems lost.
    clock.advance(500)
    monitor.watch(["adcs"])
    assert monitor.lost() == ()
    clock.advance(31.0)
    assert "adcs" in monitor.lost()


def test_switching_profiles_keeps_what_the_new_one_still_uses(monitor, clock):
    monitor.watch(["adcs", "payload"])
    keep_alive(monitor, clock, 30, "adcs", "payload", "eps")
    clock.advance(31.0)
    monitor.watch(["adcs"])
    # ADCS was already overdue and stays overdue; PAYLOAD is simply no longer
    # anybody's problem.
    assert monitor.lost() == ("adcs", "eps")


def test_a_departure_is_forgotten_when_the_profile_stops_asking_for_it(monitor):
    monitor.watch(["comms"])
    beat(monitor, "comms", alive=False)
    monitor.watch(())
    assert "comms" not in monitor.lost()


def test_losses_are_reported_in_a_stable_order(monitor, clock):
    monitor.watch(["payload", "adcs"])
    clock.advance(31.0)
    assert monitor.lost() == ("adcs", "eps", "payload")


@pytest.mark.parametrize("payload", [{}, {"service": None}, {"service": 7}, {"service": "hostd"}])
def test_a_heartbeat_for_something_unwatched_or_unnamed_is_ignored(monitor, payload):
    monitor.watch(["adcs"])
    monitor.note(payload)
    assert monitor.lost() == ()


def test_the_defaults_come_from_the_committed_config(clock):
    from cubesat.common import config

    monitor = HealthMonitor(clock=clock)
    assert monitor.grace == config.HEARTBEAT_INTERVAL_SEC * config.HEARTBEAT_MISS_THRESHOLD
