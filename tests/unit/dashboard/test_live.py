"""The in-memory telemetry ring.

Tested apart from the service because what matters here is the bound and the
order, and both are properties of the container rather than of HTTP. The
service's own tests cover the endpoint that reads it.
"""

from __future__ import annotations

from cubesat.dashboard.live import LiveHistory


def rows(history, limit=100):
    return history.records(limit)


def test_the_newest_row_comes_first():
    # The caller's first use of these is the latest row: the host's own CPU, RAM
    # and disk are on no status topic, so this is where a live page reads them.
    history = LiveHistory(10)
    history.offer({"battery": 90})
    history.offer({"battery": 89})
    assert [row["battery"] for row in rows(history)] == [89, 90]


def test_each_row_is_given_an_id_the_interface_can_key_on():
    # A written row has SQLite's autoincrement; a published one has nothing, and
    # the interface uses the id as a list key and to tell two samples apart.
    history = LiveHistory(10)
    history.offer({"battery": 90})
    history.offer({"battery": 89})
    assert [row["id"] for row in rows(history)] == [2, 1]


def test_the_ring_is_bounded_and_drops_the_oldest():
    # The whole point: a demonstration that runs all day must not grow this
    # process. Computed from the capacity rather than a repeated literal.
    capacity = 3
    history = LiveHistory(capacity)
    for value in range(capacity + 2):
        history.offer({"battery": value})

    kept = rows(history)
    assert len(kept) == capacity
    assert [row["battery"] for row in kept] == [4, 3, 2]


def test_ids_keep_counting_after_the_oldest_rows_are_dropped():
    # Monotonic within a session: an id that restarted at 1 would collide with a
    # row a page is already holding.
    history = LiveHistory(2)
    for _ in range(4):
        history.offer({"battery": 1})
    assert [row["id"] for row in rows(history)] == [4, 3]


def test_a_capacity_of_zero_is_treated_as_one_rather_than_crashing():
    # It reaches here from configuration, and a deque(maxlen=0) would silently
    # keep nothing at all — an empty chart with no explanation.
    history = LiveHistory(0)
    history.offer({"battery": 90})
    assert len(rows(history)) == 1


def test_a_limit_is_honoured_and_a_nonsense_one_returns_nothing():
    history = LiveHistory(10)
    history.offer({"battery": 90})
    history.offer({"battery": 89})
    assert len(rows(history, limit=1)) == 1
    assert rows(history, limit=0) == []
    assert rows(history, limit=-5) == []


def test_only_a_non_empty_mapping_is_kept():
    # It arrives from the broker: a malformed payload must not reach a chart as a
    # row of nulls that looks like a measurement.
    history = LiveHistory(10)
    assert history.offer({"battery": 90}) is True
    assert history.offer(None) is False
    assert history.offer("not a row") is False
    assert history.offer([1, 2]) is False
    assert history.offer({}) is False
    assert len(rows(history)) == 1
