"""Tests the alert-once-then-only-on-improvement dedupe policy in FareWatchState"""

from pathlib import Path

import pytest

from lib.fare_watch_state import FareWatchState, flight_key


@pytest.fixture
def state(tmp_path: Path) -> FareWatchState:
    return FareWatchState(filepath=tmp_path / "fare_watch_state.json")


def test_alerts_the_first_time_a_flight_qualifies(state: FareWatchState) -> None:
    key = flight_key("2099-01-01", "100")
    assert state.should_alert("watch1", key, 6000) is True


def test_does_not_realert_at_the_same_price(state: FareWatchState) -> None:
    key = flight_key("2099-01-01", "100")
    state.record_alert("watch1", key, 6000)

    assert state.should_alert("watch1", key, 6000) is False


def test_does_not_realert_on_a_price_increase(state: FareWatchState) -> None:
    key = flight_key("2099-01-01", "100")
    state.record_alert("watch1", key, 6000)

    assert state.should_alert("watch1", key, 7000) is False


def test_realerts_when_price_drops_further(state: FareWatchState) -> None:
    key = flight_key("2099-01-01", "100")
    state.record_alert("watch1", key, 6000)

    assert state.should_alert("watch1", key, 5000) is True


def test_state_is_scoped_per_watch(state: FareWatchState) -> None:
    key = flight_key("2099-01-01", "100")
    state.record_alert("watch1", key, 6000)

    assert state.should_alert("watch2", key, 6000) is True


def test_prune_drops_watches_no_longer_in_config(state: FareWatchState) -> None:
    key = flight_key("2099-01-01", "100")
    state.record_alert("watch1", key, 6000)
    state.record_alert("watch2", key, 6000)

    state.prune({"watch1"})

    assert state.should_alert("watch1", key, 6000) is False
    assert state.should_alert("watch2", key, 6000) is True


def test_state_persists_across_instances(tmp_path: Path) -> None:
    filepath = tmp_path / "fare_watch_state.json"
    key = flight_key("2099-01-01", "100")

    FareWatchState(filepath=filepath).record_alert("watch1", key, 6000)

    reloaded = FareWatchState(filepath=filepath)
    assert reloaded.should_alert("watch1", key, 6000) is False
