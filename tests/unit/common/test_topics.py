import json

from cubesat.common.topics import RETAINED, TOPICS, envelope


def test_every_topic_lives_under_the_cubesat_prefix():
    assert all(t.startswith("cubesat/") for t in TOPICS.values())


def test_topic_strings_are_unique():
    # Two keys pointing at one topic would make one of them silently dead.
    assert len(set(TOPICS.values())) == len(TOPICS)


def test_retained_set_only_names_real_topics():
    assert set(TOPICS.values()) >= RETAINED


def test_envelope_stamps_a_timestamp_and_keeps_fields():
    data = json.loads(envelope(status="NOMINAL", battery=42))
    assert data["status"] == "NOMINAL"
    assert data["battery"] == 42
    assert isinstance(data["timestamp"], float)


def test_envelope_lets_a_caller_override_the_timestamp():
    data = json.loads(envelope(timestamp=1.0))
    assert data["timestamp"] == 1.0
