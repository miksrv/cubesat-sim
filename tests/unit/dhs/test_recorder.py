"""Row assembly and the write, against a real database file.

The payloads below are the ones the README documents, verbatim, because the
column set exists to hold exactly them and a row assembled from an invented
payload proves only that the assembler agrees with the test.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from cubesat.common import config
from cubesat.common.metrics import SystemMetrics
from cubesat.dhs import schema
from cubesat.dhs.recorder import (
    AttitudeBuffer,
    RadioBuffer,
    Recorder,
    build_attitude,
    build_radio_event,
    build_row,
)

LOG = logging.getLogger("test-dhs")

MISSION_INSERT = (
    "INSERT INTO missions (id, profile, started_at) VALUES (?, ?, '2026-08-24T07:00:00Z')"
)

EPS = {
    "timestamp": 1741863600.0,
    "battery_percent": 87.5,
    "voltage": 4.123,
    "external_power": True,
    "charge_rate": -0.208,
}

ADCS = {
    "timestamp": 1741863600.0,
    "roll": 1.23,
    "pitch": -0.45,
    "yaw": 178.9,
    "quaternion": {"w": 0.999, "x": 0.01, "y": 0.02, "z": 0.03},
    "calib_status": {"sys": 3, "gyro": 3, "accel": 3, "mag": 2},
    "imu_temp": 34.5,
    "accel_g": {"x": 0.01, "y": 0.02, "z": 0.99},
    "gyro_dps": {"x": 0.1, "y": -0.2, "z": 0.05},
    "gnss": {
        "lat": 55.7558,
        "lon": 37.6173,
        "alt": 156.2,
        "speed": 0.4,
        "fix": True,
        "satellites": 23,
    },
}

SCIENCE = {
    "timestamp": 1741863600.0,
    "temperature": 23.4,
    "humidity": 45.2,
    "pressure": 1013.0,
    "light": 412.0,
    "uv_index": None,
    "uv_raw": 14,
}

METRICS = SystemMetrics(
    cpu_percent=12.5,
    ram_percent=31.0,
    swap_percent=0.0,
    disk_percent=44.2,
    uptime_seconds=3600.0,
    cpu_temperature=48.3,
)


@pytest.fixture
def conn(tmp_path):
    connection = schema.connect(tmp_path / "comms.db", LOG)
    connection.execute(MISSION_INSERT, (42, "FLIGHT"))
    yield connection
    connection.close()


@pytest.fixture
def recorder(conn):
    return Recorder(conn, LOG)


def row(**overrides):
    fields = {
        "mission_id": 42,
        "profile": "FLIGHT",
        "obc_state": "NOMINAL",
        "eps": EPS,
        "adcs": ADCS,
        "science": SCIENCE,
        "metrics": METRICS,
        "timestamp": 1741863600.0,
    }
    fields.update(overrides)
    return build_row(**fields)


# ── assembly ────────────────────────────────────────────────────────────────


def test_the_documented_payloads_flatten_into_the_documented_columns():
    assembled = row()

    assert assembled["timestamp"] == "2025-03-13T11:00:00Z"
    assert assembled["mission_id"] == 42
    assert assembled["profile"] == "FLIGHT"
    assert assembled["obc_state"] == "NOMINAL"
    assert (assembled["battery"], assembled["voltage"]) == (87.5, 4.123)
    assert (assembled["roll"], assembled["pitch"], assembled["yaw"]) == (1.23, -0.45, 178.9)
    assert assembled["quat_w"] == 0.999
    assert (assembled["accel_x"], assembled["gyro_y"]) == (0.01, -0.2)
    assert (assembled["lat"], assembled["lon"]) == (55.7558, 37.6173)
    assert (assembled["alt"], assembled["speed"], assembled["satellites"]) == (156.2, 0.4, 23)
    assert (assembled["temperature"], assembled["pressure"]) == (23.4, 1013.0)
    assert (assembled["cpu_percent"], assembled["cpu_temperature"]) == (12.5, 48.3)


def test_the_charge_rate_is_a_column_because_the_policy_decides_on_it(conn, recorder):
    # Promoted out of raw_json at schema version 6. Every other EPS field is a
    # reading; this one is the quantity power_policy compares against
    # DRAINING_PERCENT_PER_HOUR, so it is what explains a descent that happened
    # or one that did not. A black box that keeps the readings but not the
    # number under the decision makes the analyst rebuild the decision by hand.
    recorder.write(row())
    assert conn.execute("SELECT charge_rate FROM telemetry").fetchone()["charge_rate"] == -0.208


def test_a_missing_charge_rate_is_null_rather_than_zero():
    # EPS publishes no rate until it holds charge_rate_min_span_sec of history,
    # and none again for that long after the mains pin changes. Null there is a
    # measurement of ignorance; zero would read as a pack holding steady.
    assert row(eps={"battery_percent": 80.0, "voltage": 3.9})["charge_rate"] is None


def test_position_has_real_columns_because_flight_exists_to_record_a_track():
    # The pre-rewrite schema kept GNSS only inside raw_json, which meant every
    # chart and every export had to parse JSON per row to draw a map.
    assembled = row()
    for column in ("lat", "lon", "alt", "speed", "fix", "satellites"):
        assert assembled[column] is not None


def test_booleans_are_stored_as_zero_and_one(conn, recorder):
    # SQLite has no boolean type, and "true" in one column with 1 in another is
    # how a chart ends up filtering on a string.
    recorder.write(row())
    stored = conn.execute("SELECT external_power, fix FROM telemetry").fetchone()
    assert (stored["external_power"], stored["fix"]) == (1, 1)


def test_a_subsystem_that_has_said_nothing_leaves_nulls_not_missing_columns():
    # A missing key and a null mean the same thing to a consumer and different
    # things to a writer, so every column is present on every row.
    assembled = row(eps=None, adcs=None, science=None)

    assert set(assembled) == set(schema.TELEMETRY_COLUMNS)
    assert assembled["battery"] is None
    assert assembled["lat"] is None
    assert assembled["fix"] is None
    assert assembled["calib_status"] is None
    # The host's own health is DHS's to collect, so it survives every silence.
    assert assembled["cpu_percent"] == 12.5


def test_a_nulled_out_adcs_payload_is_not_treated_as_malformed():
    # ADCS publishes a fixed key set with explicit nulls where a device did not
    # answer, so this is the normal shape of a row taken while the IMU was
    # silent — not a message to warn about.
    assembled = row(adcs={"roll": None, "quaternion": None, "gnss": None, "calib_status": None})
    assert assembled["quat_w"] is None
    assert assembled["lat"] is None


def test_a_value_that_is_not_a_number_is_recorded_as_unknown():
    # A garbled uplink or a driver bug should leave a null in a chart, not a
    # string in a REAL column that every later query has to defend against.
    assembled = row(eps={"battery_percent": "eighty", "voltage": True, "external_power": 1})
    assert assembled["battery"] is None
    # True passes isinstance(..., int); a boolean where a reading was expected
    # is not a reading of 1.0.
    assert assembled["voltage"] is None
    # And the flag column only accepts an actual boolean.
    assert assembled["external_power"] is None


def test_raw_json_keeps_everything_no_column_holds():
    # A column exists because something charts it. The raw copy exists so that
    # today's decision about what is worth charting cannot destroy a field
    # measured on a walk last spring.
    raw = json.loads(row()["raw_json"])

    assert raw["eps"]["charge_rate"] == -0.208
    assert raw["payload"]["uv_raw"] == 14
    assert raw["adcs"]["calib_status"] == {"sys": 3, "gyro": 3, "accel": 3, "mag": 2}
    # Each source's own timestamp, which is what makes the age of any field
    # recoverable afterwards — the reason nothing here judges a value stale.
    assert raw["adcs"]["timestamp"] == 1741863600.0
    assert raw["context"] == {"mission_id": 42, "profile": "FLIGHT", "obc_state": "NOMINAL"}


def test_the_calibration_status_travels_as_json_because_it_explains_a_null_yaw():
    assert json.loads(row()["calib_status"])["mag"] == 2


# ── writing ─────────────────────────────────────────────────────────────────


def test_a_written_row_lands_in_the_table(conn, recorder):
    assert recorder.write(row()) is True
    assert recorder.written == 1
    assert recorder.count() == 1
    assert conn.execute("SELECT mission_id FROM telemetry").fetchone()["mission_id"] == 42


def test_a_failed_write_is_survived_and_counted_rather_than_raised(conn, recorder, caplog):
    # A full card, a locked database, a corrupt page: the recorder logs it and
    # stays alive. A service that exits on one bad write takes the rest of the
    # trip's track with it.
    with caplog.at_level(logging.ERROR):
        landed = recorder.write(row(mission_id=999))

    assert landed is False
    assert recorder.failed == 1
    assert recorder.written == 0
    assert "the mission stays open" in caplog.text


def test_the_recorder_keeps_working_after_a_failed_write(conn, recorder):
    # The next row is attempted as though nothing happened, which is the whole
    # point: one bad write must cost one row and not the rest of the mission.
    recorder.write(row(mission_id=999))
    assert recorder.write(row()) is True
    assert (recorder.written, recorder.failed) == (1, 1)


def test_a_write_against_a_closed_database_is_survived_too(conn, recorder, caplog):
    conn.close()
    with caplog.at_level(logging.ERROR):
        assert recorder.write(row()) is False
    assert recorder.failed == 1


def test_counting_the_table_counts_every_mission_s_rows(conn, recorder):
    conn.execute(MISSION_INSERT, (43, "DEMO"))
    recorder.write(row())
    recorder.write(row(mission_id=43))
    assert recorder.count() == 2


def test_an_unwritable_row_never_leaves_a_half_written_one_behind(conn, recorder):
    # Straight through the connection, to prove the constraint the recorder is
    # swallowing is a real one and not an accident of the test data.
    with pytest.raises(sqlite3.IntegrityError), schema.transaction(conn) as tx:
        tx.execute("INSERT INTO telemetry (timestamp, mission_id) VALUES ('x', 999)")
    assert recorder.count() == 0


# ── attitude ────────────────────────────────────────────────────────────────


def buffer(min_interval=1.0, capacity=10):
    return AttitudeBuffer(min_interval, capacity, LOG)


def sample(t, *, mission_id=42):
    return {
        "mission_id": mission_id,
        "t": t,
        "quat_w": 0.999, "quat_x": 0.01, "quat_y": 0.02, "quat_z": 0.03,
        "gyro_x": 0.1, "gyro_y": -0.2, "gyro_z": 0.05,
    }


def test_a_sample_is_built_from_the_documented_adcs_payload():
    built = build_attitude(ADCS, mission_id=42, now=0.0)
    assert built == {
        "mission_id": 42,
        # The payload's own timestamp, not `now`: this is when the IMU was read,
        # and at 2 Hz the difference between the two is most of the interval.
        "t": ADCS["timestamp"],
        "quat_w": 0.999, "quat_x": 0.01, "quat_y": 0.02, "quat_z": 0.03,
        "gyro_x": 0.1, "gyro_y": -0.2, "gyro_z": 0.05,
    }


def test_a_payload_with_no_timestamp_is_stamped_on_arrival_instead():
    without = {key: value for key, value in ADCS.items() if key != "timestamp"}
    assert build_attitude(without, mission_id=42, now=1741863999.0)["t"] == 1741863999.0


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"timestamp": 1.0},
        # The normal shape of a message taken while the IMU was silent: ADCS
        # publishes its fixed key set with explicit nulls rather than dropping
        # the message, because the gap is the signal for the *position* half.
        {"timestamp": 1.0, "quaternion": None, "gyro_dps": None},
        {"timestamp": 1.0, "quaternion": {"w": None}, "gyro_dps": {"x": None}},
    ],
)
def test_nothing_is_recorded_when_there_is_no_orientation_to_record(payload):
    # Nine nulls on a chart look exactly like a satellite that was not moving.
    assert build_attitude(payload, mission_id=42, now=0.0) is None


def test_a_sample_survives_a_gnss_only_message():
    # ADCS publishes when *either* half answered, so a message with a fix and no
    # orientation is normal — and the reverse must still record.
    position_only = {"timestamp": 1.0, "gnss": {"lat": 55.7, "lon": 37.6}}
    assert build_attitude(position_only, mission_id=42, now=0.0) is None


def test_samples_inside_the_floor_are_dropped_before_they_cost_anything():
    # The decimation is what keeps DIAG — where ADCS runs at 10 Hz — from
    # writing ten rows a second to the card. Measured against the last sample
    # kept, not the last one offered.
    buf = buffer(min_interval=1.0)
    assert buf.offer(sample(100.0)) is True
    assert buf.offer(sample(100.4)) is False
    assert buf.offer(sample(100.9)) is False
    assert buf.offer(sample(101.0)) is True
    assert [s["t"] for s in buf.drain()] == [100.0, 101.0]
    assert buf.decimated == 2


def test_the_floor_is_computed_from_the_configured_interval_and_not_a_literal():
    interval = config.DHS_ATTITUDE_MIN_INTERVAL_SEC
    buf = buffer(min_interval=interval)
    buf.offer(sample(0.0))
    assert buf.offer(sample(interval / 2)) is False
    assert buf.offer(sample(interval)) is True


def test_a_clock_that_went_backwards_does_not_wedge_the_buffer_shut(caplog):
    # An NTP step landing on a satellite that has just found a network. Without
    # this the buffer would refuse everything until wall time caught up, which
    # for a large step is the rest of the trip.
    buf = buffer(min_interval=1.0)
    buf.offer(sample(1_000_000.0))
    with caplog.at_level(logging.INFO):
        assert buf.offer(sample(500.0)) is True
    assert "went backwards" in caplog.text
    assert buf.offer(sample(501.0)) is True


def test_the_buffer_is_bounded_and_drops_the_oldest():
    # A failing card must not turn a recorder that survives into a process that
    # grows. The recent samples are the ones a viewer is about to ask for.
    buf = buffer(min_interval=0.0, capacity=3)
    for t in range(6):
        buf.offer(sample(float(t)))
    assert [s["t"] for s in buf.drain()] == [3.0, 4.0, 5.0]
    assert buf.overflowed == 3


def test_draining_leaves_the_buffer_empty():
    buf = buffer()
    buf.offer(sample(1.0))
    assert len(buf) == 1
    buf.drain()
    assert len(buf) == 0 and buf.drain() == []


def test_a_restored_batch_keeps_its_order_in_front_of_what_arrived_since():
    buf = buffer(min_interval=0.0)
    held = [sample(1.0), sample(2.0)]
    buf.offer(sample(3.0))
    buf.restore(held)
    assert [s["t"] for s in buf.drain()] == [1.0, 2.0, 3.0]


def test_a_batch_is_written_in_one_transaction(conn, recorder):
    assert recorder.write_attitude([sample(1.0), sample(2.0), sample(3.0)]) is True
    assert recorder.attitude_written == 3
    stored = conn.execute("SELECT t, quat_w, gyro_z FROM attitude ORDER BY t").fetchall()
    assert [row["t"] for row in stored] == [1.0, 2.0, 3.0]
    assert stored[0]["quat_w"] == 0.999 and stored[0]["gyro_z"] == 0.05


def test_an_empty_batch_is_a_success_that_touches_nothing(recorder):
    assert recorder.write_attitude([]) is True
    assert recorder.attitude_written == 0


def test_a_batch_that_fails_lands_none_of_it(conn, recorder, caplog):
    # All or nothing: a partial batch would leave the caller unable to say which
    # samples to put back, and a gap in the middle of a replay is worse than one
    # at the end.
    batch = [sample(1.0), sample(2.0, mission_id=999), sample(3.0)]
    with caplog.at_level(logging.ERROR):
        assert recorder.write_attitude(batch) is False
    assert conn.execute("SELECT COUNT(*) AS n FROM attitude").fetchone()["n"] == 0
    assert recorder.failed == 1
    assert "3 sample(s) held" in caplog.text


def test_attitude_is_counted_apart_from_telemetry_rows(conn, recorder):
    recorder.write(row())
    recorder.write_attitude([sample(1.0), sample(2.0)])
    assert (recorder.written, recorder.attitude_written) == (1, 2)
    # count() is still the telemetry table: one row, not three.
    assert recorder.count() == 1


# ── the radio buffer and its writes ───────────────────────────────────────────


def radio_row(t, *, mission_id=42, direction="rx", **overrides):
    event = {
        "mission_id": mission_id,
        "t": t,
        "direction": direction,
        "kind": None,
        "text": "!pos",
        "bytes": 4,
        "sender": "!e2f1a4c8",
        "snr": 6.25,
        "rssi": -96.0,
        "hops": 0,
        "sent": None,
    }
    event.update(overrides)
    return event


def test_the_radio_buffer_takes_everything_and_never_decimates():
    # Attitude is a continuous signal where a sample stands for its neighbours;
    # a radio session is discrete events, and the one packet dropped would be
    # the uplink somebody is trying to find in the log.
    buf = RadioBuffer(capacity=10)
    for t in (1000.0, 1000.1, 1000.2):
        assert buf.offer(radio_row(t)) is True
    assert len(buf) == 3
    assert buf.offer(None) is False


def test_the_radio_buffer_is_bounded_and_drops_the_oldest():
    buf = RadioBuffer(capacity=2)
    for t in (1.0, 2.0, 3.0):
        buf.offer(radio_row(t))
    assert buf.overflowed == 1
    assert [e["t"] for e in buf.drain()] == [2.0, 3.0]


def test_a_restored_radio_batch_keeps_its_order():
    buf = RadioBuffer(capacity=10)
    buf.offer(radio_row(3.0))
    buf.restore([radio_row(1.0), radio_row(2.0)])
    assert [e["t"] for e in buf.drain()] == [1.0, 2.0, 3.0]


def test_radio_events_are_written_in_one_batch(conn, recorder):
    batch = [radio_row(1.0), radio_row(2.0, direction="tx", kind="beacon", sent=1)]
    assert recorder.write_radio(batch) is True
    assert recorder.radio_written == 2
    stored = conn.execute("SELECT * FROM radio_log ORDER BY t").fetchall()
    assert [row["direction"] for row in stored] == ["rx", "tx"]
    assert stored[0]["snr"] == 6.25
    assert stored[1]["kind"] == "beacon" and stored[1]["sent"] == 1


def test_an_empty_radio_batch_is_a_success_that_touches_nothing(recorder):
    assert recorder.write_radio([]) is True
    assert recorder.radio_written == 0


def test_a_radio_batch_that_fails_lands_none_of_it(conn, recorder, caplog):
    batch = [radio_row(1.0), radio_row(2.0, mission_id=999), radio_row(3.0)]
    with caplog.at_level(logging.ERROR):
        assert recorder.write_radio(batch) is False
    assert conn.execute("SELECT COUNT(*) AS n FROM radio_log").fetchone()["n"] == 0
    assert recorder.failed == 1
    assert "3 event(s) held" in caplog.text


def test_build_radio_event_refuses_what_cannot_name_a_direction():
    assert build_radio_event(None, mission_id=42, now=1000.0) is None
    assert (
        build_radio_event({"timestamp": 1.0, "text": "?"}, mission_id=42, now=1000.0)
        is None
    )
    assert (
        build_radio_event(
            {"timestamp": 1.0, "direction": "sideways"}, mission_id=42, now=1000.0
        )
        is None
    )


def test_build_radio_event_falls_back_to_now_only_when_the_event_carries_no_time():
    event = build_radio_event({"direction": "rx"}, mission_id=42, now=1234.0)
    assert event is not None and event["t"] == 1234.0
    # And everything unsaid is null, never substituted.
    assert event["snr"] is None and event["sent"] is None and event["text"] is None
