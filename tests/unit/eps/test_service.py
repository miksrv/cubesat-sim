import pytest

from cubesat.common import config
from cubesat.common.states import MissionState
from cubesat.common.topics import TOPICS
from cubesat.eps.service import EpsService
from cubesat.hal.interfaces import Power

# The windows, chosen here rather than read from config.yaml so a retuned
# deployment cannot change what these tests prove.
WINDOW_SEC = 100.0
MIN_SPAN_SEC = 50.0
LEVEL_WINDOW_SEC = 100.0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeMonitor:
    def __init__(self, reading=None, answers=True):
        # Like the real X728 gauge: voltage, its own modelled level, the pin —
        # and no rate of its own.
        self.reading = reading or Power(
            voltage=4.044, external_power=True, gauge_percent=79.61, charge_rate=None
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
def eps(service_factory, monkeypatch):
    monkeypatch.setattr(config, "EPS_CHARGE_RATE_WINDOW_SEC", WINDOW_SEC)
    monkeypatch.setattr(config, "EPS_CHARGE_RATE_MIN_SPAN_SEC", MIN_SPAN_SEC)
    monkeypatch.setattr(config, "EPS_LEVEL_WINDOW_SEC", LEVEL_WINDOW_SEC)
    monitor = FakeMonitor()
    clock = Clock()
    service, client = service_factory(EpsService, monitor=monitor, clock=clock)
    return service, client, monitor, clock


def test_a_tick_publishes_the_whole_reading(eps):
    service, client, _, _ = eps
    service.tick()
    payload = client.last(TOPICS["eps_status"])
    assert payload["voltage"] == 4.044
    assert payload["external_power"] is True
    # What the gauge said, reported and not promoted; and what the curve makes
    # of the voltage, which is the percentage everything downstream displays.
    assert payload["gauge_percent"] == 79.61
    assert payload["battery_percent"] == pytest.approx(88.5, abs=0.1)
    # One reading is no history: every rate and estimate is withheld, not
    # invented. The level is not — a median of one sample is that sample.
    assert payload["voltage_median"] == 4.044
    assert payload["charge_rate"] is None
    assert payload["voltage_rate"] is None
    assert payload["time_to_empty_sec"] is None
    assert payload["time_to_full_sec"] is None
    assert payload["timestamp"] > 0


def test_the_published_percentage_follows_the_voltage_and_not_the_gauge(eps):
    # The 2026-09-04 change, from the outside: a gauge claiming 5 % on a pack
    # sitting at 3.85 V publishes the pack's level, and says what the gauge
    # thought in a field named after the gauge.
    service, client, monitor, _ = eps
    monitor.reading = Power(voltage=3.85, external_power=False, gauge_percent=5.0)
    service.tick()
    payload = client.last(TOPICS["eps_status"])
    assert payload["gauge_percent"] == 5.0
    assert payload["battery_percent"] == pytest.approx(62.0, abs=0.1)


def test_both_rates_are_fitted_to_the_one_quantity_the_gauge_measures(eps):
    # The X728's gauge has no rate register (docs/hardware-x728-ups-hat.md), so
    # the −0.208 %/h it used to "report" was a decoded 0xFFFF. Both published
    # rates now come from how the *voltage* moves: 2 mV every 30 s is
    # −240 mV/h, and at 3.848 V the curve's local gradient is 7.143 mV per
    # point, so the same slope reads as −33.6 %/h.
    service, client, monitor, clock = eps
    for i in range(3):
        monitor.reading = Power(voltage=3.850 - 0.002 * i, external_power=False)
        service.tick()
        clock.now += 30
    payload = client.last(TOPICS["eps_status"])
    assert payload["voltage_rate"] == pytest.approx(-240.0)
    assert payload["charge_rate"] == pytest.approx(-33.6, abs=0.1)


def test_a_falling_pack_is_given_a_time_to_empty_and_no_time_to_full(eps):
    service, client, monitor, clock = eps
    for i in range(3):
        monitor.reading = Power(voltage=3.850 - 0.002 * i, external_power=False)
        service.tick()
        clock.now += 30
    payload = client.last(TOPICS["eps_status"])
    # Falling at 33.6 %/h from about 62 %: not quite two hours. The estimate is
    # to the pack's own floor, not to any threshold — EPS knows no thresholds.
    assert payload["time_to_empty_sec"] == pytest.approx(6640, rel=0.05)
    assert payload["time_to_full_sec"] is None


def test_a_charging_pack_is_given_a_time_to_full_and_no_time_to_empty(eps):
    service, client, monitor, clock = eps
    for i in range(3):
        monitor.reading = Power(voltage=3.850 + 0.002 * i, external_power=True)
        service.tick()
        clock.now += 30
    payload = client.last(TOPICS["eps_status"])
    assert payload["time_to_full_sec"] is not None
    assert payload["time_to_empty_sec"] is None


def test_a_gauge_that_measures_its_own_rate_is_believed(eps):
    # The protocol still allows a driver to report a rate; a future gauge with a
    # real CRATE must not have its measurement overwritten by a conversion.
    service, client, monitor, _ = eps
    monitor.reading = Power(voltage=3.85, external_power=True, charge_rate=2.5)
    service.tick()
    assert client.last(TOPICS["eps_status"])["charge_rate"] == 2.5


def test_the_rate_starts_over_when_the_mains_pin_changes(eps):
    # A battery-time slope must not follow the satellite onto mains: the power
    # policy would read "still draining" and refuse to believe the plug for as
    # long as the window is — exactly when a flat pack must not power off.
    service, client, monitor, clock = eps
    for i in range(3):
        monitor.reading = Power(voltage=3.850 - 0.004 * i, external_power=False)
        service.tick()
        clock.now += 30
    assert client.last(TOPICS["eps_status"])["voltage_rate"] < 0
    monitor.reading = Power(voltage=3.842, external_power=True)
    service.tick()
    payload = client.last(TOPICS["eps_status"])
    assert payload["voltage_rate"] is None
    assert payload["charge_rate"] is None
    assert payload["time_to_empty_sec"] is None


def test_the_level_is_a_median_so_one_dip_does_not_descend_a_state(eps):
    # The reason MedianWindow exists. The camera pipeline starting is worth tens
    # of millivolts, and the thresholds are volts 60 mV apart — so a single
    # sample taken mid-capture could put the satellite into SAFE on its own.
    service, client, monitor, clock = eps
    for _ in range(3):
        monitor.reading = Power(voltage=3.700, external_power=False)
        service.tick()
        clock.now += 10
    monitor.reading = Power(voltage=3.400, external_power=False)
    service.tick()
    payload = client.last(TOPICS["eps_status"])
    # The dip is published as the raw sample — it happened — and refused as the
    # level. Both, so that a chart can show it and the policy cannot act on it.
    assert payload["voltage"] == 3.400
    assert payload["voltage_median"] == 3.700


def test_the_level_starts_over_when_the_mains_pin_changes(eps):
    # The plug moving is worth more millivolts than anything else that happens
    # to this pack — 50 of them, measured — so samples from the other side of it
    # describe a different regime and must not be averaged across it.
    service, client, monitor, clock = eps
    for _ in range(3):
        monitor.reading = Power(voltage=3.700, external_power=False)
        service.tick()
        clock.now += 10
    monitor.reading = Power(voltage=3.750, external_power=True)
    service.tick()
    assert client.last(TOPICS["eps_status"])["voltage_median"] == 3.750


def test_status_is_retained_and_sent_at_qos_1(eps):
    # A service that starts late must learn the battery level immediately, and
    # the reading that drives CRITICAL is not one to publish best-effort.
    service, client, _, _ = eps
    service.tick()
    published = client.published[-1]
    assert published.retain is True
    assert published.qos == 1


def test_eps_makes_no_decisions(eps):
    # Thresholds belong to OBC's power policy, in one place. If EPS ever starts
    # publishing a state or a verdict, that split has been broken.
    service, client, monitor, _ = eps
    monitor.reading = Power(voltage=3.25, external_power=False, gauge_percent=5.0)
    service.tick()
    payload = client.last(TOPICS["eps_status"])
    assert set(payload) == {
        "timestamp",
        "voltage",
        "voltage_median",
        "external_power",
        "battery_percent",
        "gauge_percent",
        "charge_rate",
        "voltage_rate",
        "time_to_empty_sec",
        "time_to_full_sec",
    }


def test_it_runs_in_every_profile_so_cadence_has_a_default(eps):
    # HOSTED has no mission and therefore no mission state, and EPS still has
    # to poll: it is the only source of the telemetry that drives CRITICAL.
    service, _, _, _ = eps
    assert service.mission_state is None
    assert service.interval > 0


def test_cadence_slows_in_low_power_and_quickens_in_critical(eps):
    service, client, _, _ = eps
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
    service, client, monitor, _ = eps
    monitor.reading = OSError("bus went away")
    with pytest.raises(OSError):
        service.tick()  # the base class catches this in the run loop
    assert client.payloads(TOPICS["eps_status"]) == []


def test_shutdown_releases_the_gpio_pin(eps):
    service, _, monitor, _ = eps
    service.on_stop()
    assert monitor.closed


def test_shutdown_tolerates_a_monitor_that_cannot_be_closed(service_factory):
    class Bare:
        def probe(self):
            return True

        def read(self):
            return Power(3.7, False)

    service, _ = service_factory(EpsService, monitor=Bare())
    service.on_stop()


def test_the_real_driver_is_used_when_none_is_given(service_factory):
    # The mock HAL is active in tests, so this proves the wiring to the
    # registry rather than the driver itself.
    service, _ = service_factory(EpsService)
    assert service._monitor.probe() is True
    assert 3.0 <= service._monitor.read().voltage <= 4.2
