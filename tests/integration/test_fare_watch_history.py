"""Tests the price-trend annotations (delta, lowest ever seen) attached to fare watch rows"""

from pathlib import Path

import pytest

from lib.fare_watch_history import MAX_HISTORY_ENTRIES, FareWatchHistory


@pytest.fixture
def history(tmp_path: Path) -> FareWatchHistory:
    return FareWatchHistory(filepath=tmp_path / "fare_watch_history.json")


def make_rows(points: int | None, flight: str = "100") -> list[dict]:
    return [{"displayNumber": flight, "points": points, "_historyKey": f"2099-01-01:{flight}"}]


def check(history: FareWatchHistory, points: int, checked_at: str = "2099-01-01T00:00:00") -> dict:
    """Run one annotate-then-record cycle, as add_price_history does, and return the row."""
    rows = make_rows(points)
    history.annotate("watch1", rows)
    history.record("watch1", rows, checked_at)
    return rows[0]


def test_first_check_has_no_previous_price(history: FareWatchHistory) -> None:
    row = check(history, 18000)

    assert row["previousPoints"] is None
    assert row["delta"] is None
    # With nothing recorded yet, today's price is the lowest ever seen
    assert row["lowestEver"] == 18000


def test_delta_compares_against_the_previous_check(history: FareWatchHistory) -> None:
    check(history, 18000)
    row = check(history, 15500)

    assert row["previousPoints"] == 18000
    assert row["delta"] == -2500


def test_delta_is_positive_when_the_price_rises(history: FareWatchHistory) -> None:
    check(history, 15500)
    row = check(history, 18000)

    assert row["delta"] == 2500


def test_lowest_ever_only_ratchets_down(history: FareWatchHistory) -> None:
    check(history, 18000)
    check(history, 15500)
    row = check(history, 21000)

    assert row["lowestEver"] == 15500


def test_history_is_capped(history: FareWatchHistory) -> None:
    for i in range(MAX_HISTORY_ENTRIES + 10):
        check(history, 10000 + i, checked_at=f"2099-01-01T00:{i:02d}:00")

    stored = history._load()["watch1"]["2099-01-01:100"]
    assert len(stored["history"]) == MAX_HISTORY_ENTRIES
    # The oldest entries are the ones dropped
    assert stored["history"][-1][1] == 10000 + MAX_HISTORY_ENTRIES + 9


def test_unpriced_flights_are_not_recorded(history: FareWatchHistory) -> None:
    rows = make_rows(None)
    history.annotate("watch1", rows)
    history.record("watch1", rows, "2099-01-01T00:00:00")

    assert history._load() == {"watch1": {}}
    assert rows[0]["lowestEver"] is None


def test_history_is_scoped_per_flight(history: FareWatchHistory) -> None:
    check(history, 18000)

    other = make_rows(9000, flight="200")
    history.annotate("watch1", other)

    assert other[0]["previousPoints"] is None


def test_prune_drops_watches_no_longer_in_config(history: FareWatchHistory) -> None:
    check(history, 18000)

    history.prune({"watch2"})

    assert history._load() == {}


def test_history_persists_across_instances(tmp_path: Path) -> None:
    filepath = tmp_path / "fare_watch_history.json"
    check(FareWatchHistory(filepath=filepath), 18000)

    row = check(FareWatchHistory(filepath=filepath), 15500)
    assert row["previousPoints"] == 18000
