import pytest

from cubesat.common.states import MissionState
from cubesat.common.topics import TOPICS
from cubesat.eps.service import EpsService
from cubesat.hal.interfaces import Power


class FakeMonitor:
    def __init__(self, reading=None, answers=True):
        self.reading = reading or Power(
            battery_percent=79.61, voltage=4.044, external_power=True, charge_rate=-0.208
        )
        self.answers = answers
        self.reads = 0
        self.closed = False

    def probe(self):
        return self.answers

    def read(self):
        self.reads += 1
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading

    def close(self):
        self.closed = True


@pytest.fixture
def eps(service_factory):
    monitor = FakeMonitor()
    service, client = service_factory(EpsService, monitor=monitor)
    return service, client, monitor


def test_a_tick_publishes_the_whole_reading(eps):
    service, client, _ = eps
    service.tick()
    payload = client.last(TOPICS["eps_status"])
    assert payload["battery_percent"] == 79.61
    assert payload["voltage"] == 4.044
    assert payload["external_power"] is True
    assert payload["charge_rate"] == -0.208
    assert payload["timestamp"] > 0


def test_status_is_retained_and_sent_at_qos_1(eps):
    # A service that starts late must learn the battery level immediately, and
    # the reading that drives CRITICAL is not one to publish best-effort.
    service, client, _ = eps
    service.tick()
    published = client.published[-1]
    assert published.retain is True
    assert published.qos == 1


def test_eps_makes_no_decisions(eps):
    # Thresholds belong to OBC's power policy, in one place. If EPS ever starts
    # publishing a state or a verdict, that split has been broken.
    service, client, monitor = eps
    monitor.reading = Power(battery_percent=5.0, voltage=3.25, external_power=False)
    service.tick()
    payload = client.last(TOPICS["eps_status"])
    assert set(payload) == {
        "timestamp", "battery_percent", "voltage", "external_power", "charge_rate"
    }


def test_it_runs_in_every_profile_so_cadence_has_a_default(eps):
    # HOSTED has no mission and therefore no mission state, and EPS still has
    # to poll: it is the only source of the telemetry that drives CRITICAL.
    service, _, _ = eps
    assert service.mission_state is None
    assert service.interval > 0


def test_cadence_slows_in_low_power_and_quickens_in_critical(eps):
    service, client, _ = eps
    client.deliver(TOPICS["obc_status"], {"status": MissionState.NOMINAL.value})
    nominal = service.interval
    client.deliver(TOPICS["obc_status"], {"status": MissionState.LOW_POWER.value})
    assert service.interval > nominal
    client.deliver(TOPICS["obc_status"], {"status": MissionState.CRITICAL.value})
    # About to decide whether to power the host off: watch closely.
    assert service.interval < nominal


def test_a_silent_gauge_is_reported_but_keeps_the_service_up(service_factory, caplog):
    # Staying up means OBC sees silence on eps_status, which it can act on,
    # rather than a vanished process that took its heartbeat with it.
    monitor = FakeMonitor(answers=False)
    service, _ = service_factory(EpsService, monitor=monitor)
    with caplog.at_level("ERROR"):
        service.on_start()
    assert "not answering" in caplog.text
    assert service.running


def test_a_failing_read_does_not_publish_a_half_reading(eps):
    service, client, monitor = eps
    monitor.reading = OSError("bus went away")
    with pytest.raises(OSError):
        service.tick()  # the base class catches this in the run loop
    assert client.payloads(TOPICS["eps_status"]) == []


def test_shutdown_releases_the_gpio_pin(eps):
    service, _, monitor = eps
    service.on_stop()
    assert monitor.closed


def test_shutdown_tolerates_a_monitor_that_cannot_be_closed(service_factory):
    class Bare:
        def probe(self):
            return True

        def read(self):
            return Power(50.0, 3.7, False)

    service, _ = service_factory(EpsService, monitor=Bare())
    service.on_stop()


def test_the_real_driver_is_used_when_none_is_given(service_factory):
    # The mock HAL is active in tests, so this proves the wiring to the
    # registry rather than the driver itself.
    service, _ = service_factory(EpsService)
    assert service._monitor.probe() is True
    assert 0 <= service._monitor.read().battery_percent <= 100
