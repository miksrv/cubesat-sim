from dataclasses import replace

import pytest

from cubesat.adcs.service import AdcsService
from cubesat.common.states import MissionState
from cubesat.common.topics import TOPICS
from cubesat.hal.interfaces import Attitude, Calibration, Position, Quaternion, Vector3

#: The bench numbers from the two hardware documents, so the payload assertions
#: below are the same values the assembled satellite actually produced.
BENCH_ATTITUDE = Attitude(
    roll=27.1875,
    pitch=7.0625,
    yaw=0.0,
    quaternion=Quaternion(w=0.96997, x=-0.05701, y=-0.23602, z=0.0),
    accel_g=Vector3(x=0.4477, y=-0.102, z=0.8433),
    gyro_dps=Vector3(x=-0.125, y=-0.3125, z=-0.1875),
    temperature=31.0,
    calibration=Calibration(sys=3, gyro=3, accel=3, mag=3),
)
BENCH_POSITION = Position(
    lat=37.676896, lon=-121.876561, alt=116.59, speed=0.0, fix=True, satellites=23
)


def _uncalibrated(mag: int = 1) -> Attitude:
    """What the driver hands over while the magnetometer is not there yet: no
    heading, and the calibration that explains why."""
    return replace(
        BENCH_ATTITUDE, yaw=None, calibration=replace(BENCH_ATTITUDE.calibration, mag=mag)
    )


class FakeImu:
    def __init__(self, reading=BENCH_ATTITUDE, answers=True):
        self.reading = reading
        self.answers = answers

    def probe(self):
        return self.answers

    def read(self):
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


class FakeGnss(FakeImu):
    def __init__(self, reading=BENCH_POSITION, answers=True):
        super().__init__(reading, answers)


@pytest.fixture
def adcs(service_factory):
    imu, gnss = FakeImu(), FakeGnss()
    service, client = service_factory(AdcsService, imu=imu, gnss=gnss)
    return service, client, imu, gnss


def test_a_tick_publishes_orientation_and_position_in_one_message(adcs):
    service, client, _, _ = adcs
    service.tick()
    payload = client.last(TOPICS["adcs_status"])
    assert payload["roll"] == 27.1875
    assert payload["pitch"] == 7.0625
    assert payload["quaternion"] == {"w": 0.96997, "x": -0.05701, "y": -0.23602, "z": 0.0}
    assert payload["imu_temp"] == 31.0
    assert payload["accel_g"] == {"x": 0.4477, "y": -0.102, "z": 0.8433}
    assert payload["gyro_dps"] == {"x": -0.125, "y": -0.3125, "z": -0.1875}
    assert payload["gnss"]["lat"] == 37.676896
    assert payload["gnss"]["lon"] == -121.876561
    assert payload["gnss"]["satellites"] == 23
    assert payload["timestamp"] > 0


def test_the_payload_has_exactly_the_documented_shape(adcs):
    service, client, _, _ = adcs
    service.tick()
    assert set(client.last(TOPICS["adcs_status"])) == {
        "timestamp", "roll", "pitch", "yaw", "quaternion", "calib_status",
        "imu_temp", "accel_g", "gyro_dps", "gnss",
    }


def test_a_withheld_heading_reaches_the_payload_as_null_beside_its_calibration(adcs):
    # Whether a heading exists is the driver's judgement — its tests own that
    # rule. What matters here is that the null survives the whole way out,
    # rather than becoming a 0.0 in the JSON, and that calib_status travels with
    # it: it is the only thing that explains the null to a consumer.
    service, client, imu, _ = adcs
    imu.reading = _uncalibrated(mag=1)
    service.tick()
    payload = client.last(TOPICS["adcs_status"])
    assert payload["yaw"] is None
    assert payload["calib_status"] == {"sys": 3, "gyro": 3, "accel": 3, "mag": 1}
    # And the rest of the attitude is untouched by it.
    assert (payload["roll"], payload["pitch"]) == (27.1875, 7.0625)


def test_a_calibrated_heading_is_published_unchanged(adcs):
    service, client, _, _ = adcs
    service.tick()
    assert client.last(TOPICS["adcs_status"])["yaw"] == 0.0


def test_a_dead_receiver_does_not_cost_us_the_attitude(adcs):
    service, client, _, gnss = adcs
    gnss.reading = OSError("bus went away")
    service.tick()
    payload = client.last(TOPICS["adcs_status"])
    assert payload["roll"] == 27.1875
    assert payload["gnss"] is None


def test_a_dead_imu_does_not_cost_us_the_position(adcs):
    service, client, imu, _ = adcs
    imu.reading = OSError("bus went away")
    service.tick()
    payload = client.last(TOPICS["adcs_status"])
    assert payload["gnss"]["lat"] == 37.676896
    assert payload["roll"] is None
    assert payload["calib_status"] is None
    assert payload["yaw"] is None


def test_nothing_is_published_when_neither_device_can_be_read(adcs, caplog):
    # An empty payload would tell OBC's DEPLOY that the hardware answered when
    # none of it did. The gap on adcs_status is the honest signal.
    service, client, imu, gnss = adcs
    imu.reading = OSError("bus went away")
    gnss.reading = OSError("bus went away")
    with caplog.at_level("ERROR"):
        service.tick()
    assert client.payloads(TOPICS["adcs_status"]) == []
    assert "neither" in caplog.text


def test_a_fixless_receiver_still_publishes_a_gnss_object(adcs):
    # OBC reads gnss.fix to log the best-effort fix wait during DEPLOY; a
    # missing key and a false fix must not look the same to it.
    service, client, _, gnss = adcs
    gnss.reading = Position(None, None, None, None, fix=False, satellites=4)
    service.tick()
    assert client.last(TOPICS["adcs_status"])["gnss"] == {
        "lat": None, "lon": None, "alt": None, "speed": None, "fix": False, "satellites": 4
    }


def test_the_status_is_not_retained(adcs):
    # Attitude ages in half a second. A late subscriber wants the next reading,
    # not the one from before the last power cycle.
    service, client, _, _ = adcs
    service.tick()
    assert client.published[-1].retain is False


def test_on_start_says_which_device_answered(adcs, caplog):
    service, _, _, gnss = adcs
    gnss.answers = False
    with caplog.at_level("INFO"):
        service.on_start()
    assert "BNO055 orientation answered" in caplog.text
    assert "TEL0157 GNSS is not answering" in caplog.text


def test_a_silent_device_keeps_the_service_up(service_factory, caplog):
    # A vanished process takes its heartbeat with it, and OBC then cannot tell a
    # broken sensor from a broken service.
    service, _ = service_factory(
        AdcsService, imu=FakeImu(answers=False), gnss=FakeGnss(answers=False)
    )
    with caplog.at_level("ERROR"):
        service.on_start()
    assert service.running


def test_the_cadence_follows_the_mission_state(adcs):
    service, client, _, _ = adcs
    client.deliver(TOPICS["obc_status"], {"status": MissionState.NOMINAL.value})
    nominal = service.interval
    assert nominal == 0.5  # 2 Hz
    client.deliver(TOPICS["obc_status"], {"status": MissionState.LOW_POWER.value})
    assert service.interval > nominal
    client.deliver(TOPICS["obc_status"], {"status": MissionState.DEPLOY.value})
    # DEPLOY has to report in well inside OBC's bring-up window.
    assert service.interval == 0.5


def test_the_real_drivers_are_used_when_none_are_given(service_factory):
    # The mock HAL is active in tests, so this proves the wiring to the registry
    # rather than the drivers themselves.
    service, _ = service_factory(AdcsService)
    assert service._imu.probe() is True
    assert service._gnss.probe() is True
    assert service._imu.read().calibration.mag >= 0


def test_the_service_never_touches_the_bus_itself(adcs):
    # Each driver takes the advisory lock for its own reads and releases it
    # before the other device is touched. A service holding the lock across both
    # would keep EPS off a 10 kHz bus for the length of a GNSS block.
    service, _, _, _ = adcs
    assert not any("bus" in name for name in vars(service))
