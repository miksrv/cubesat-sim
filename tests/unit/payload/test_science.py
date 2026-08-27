import logging

import pytest

from cubesat.hal.interfaces import Environment
from cubesat.payload import science

#: The verified indoor reading from docs/hardware-sen0501-environmental-sensor.md,
#: as the driver hands it over: an unresolved UV index beside the raw count it
#: was withheld from.
BENCH_READING = Environment(
    temperature=27.96,
    humidity=41.21,
    pressure=1000.0,
    light=388.96,
    uv_index=None,
    uv_raw=14,
)


class FakeSensor:
    def __init__(self, reading=BENCH_READING, answers=True):
        self.reading = reading
        self.answers = answers

    def probe(self):
        return self.answers

    def read(self):
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


@pytest.fixture
def log():
    return logging.getLogger("payload.test")


def test_a_reading_becomes_the_documented_payload():
    assert science.payload_from(BENCH_READING) == {
        "temperature": 27.96,
        "humidity": 41.21,
        "pressure": 1000.0,
        "light": 388.96,
        "uv_index": None,
        "uv_raw": 14,
    }


def test_the_raw_uv_count_travels_beside_the_withheld_index():
    # The same reasoning as calib_status beside a null yaw: the raw count is the
    # only thing that tells a consumer "the revision is unknown" from "the
    # sensor was not read", and it means the question can be settled later from
    # data already recorded.
    payload = science.payload_from(BENCH_READING)
    assert payload["uv_index"] is None
    assert payload["uv_raw"] == 14


def test_a_resolved_index_is_passed_through_unchanged():
    # Whether an index exists is the driver's judgement, not this module's.
    reading = Environment(20.0, 40.0, 1013.0, 100.0, uv_index=1.43, uv_raw=400)
    assert science.payload_from(reading)["uv_index"] == 1.43


def test_no_altitude_is_derived_from_the_pressure():
    # The vendor's elevation uses a hard-coded 1015.0 hPa reference and the
    # register is whole hectopascals — 8 m per bit. Altitude belongs to the GNSS
    # receiver, which measures it.
    payload = science.payload_from(BENCH_READING)
    assert "altitude" not in payload
    assert "elevation" not in payload
    assert payload["pressure"] == 1000.0


def test_the_payload_has_exactly_the_documented_shape():
    assert set(science.payload_from(BENCH_READING)) == {
        "temperature", "humidity", "pressure", "light", "uv_index", "uv_raw",
    }


def test_a_reading_is_returned_when_the_sensor_answers(log):
    assert science.read(FakeSensor(), log) is BENCH_READING


def test_a_failed_read_becomes_none_rather_than_an_exception(log, caplog):
    # PAYLOAD still owns a camera and still has to keep its heartbeat going. One
    # dead device degrades the payload; it does not take the subsystem off the
    # bus.
    with caplog.at_level("ERROR"):
        assert science.read(FakeSensor(OSError("bus went away")), log) is None
    assert "SEN0501 read failed" in caplog.text


def test_any_exception_at_all_is_caught(log, caplog):
    # A driver may raise anything; whatever it was, the camera still works.
    with caplog.at_level("ERROR"):
        assert science.read(FakeSensor(ValueError("nonsense register")), log) is None
