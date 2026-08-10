"""
Tests that the fare watch board reflects the user's *current* selection.

The board is rendered from the last check's stored result, but the selection lives in config.json
and can change between checks. Without re-deriving it at render time, ticking flights and saving
would appear to do nothing until the next check ran.
"""

from pathlib import Path

import pytest

from lib.config import GlobalConfig
from lib.webui import service
from lib.webui.results_store import ResultsStore

WATCH = {
    "id": "w1",
    "origin": "LGA",
    "destination": "STL",
    "date": "2099-11-28",
    "maxPoints": 20000,
}


def make_config(**overrides: object) -> GlobalConfig:
    config = GlobalConfig()
    config.create_fare_watch_config([{**WATCH, **overrides}])
    return config


@pytest.fixture
def store(tmp_path: Path) -> ResultsStore:
    """A results store holding one check where every flight was tracked and priced."""
    store = ResultsStore(filepath=tmp_path / "watch_results.json")
    store.save_result(
        "w1",
        {
            "checked_at": "2099-01-01T00:00:00",
            "status": "hit",
            "message": "",
            "transient": False,
            "lowest_points": 15500,
            "fare_classes": [{"id": "WGA", "label": "Wanna Get Away"}],
            "rows": [
                _row("3200", {"WGA": 15500, "ANY": 28000}, 15500),
                _row("2992", {"WGA": 19000, "ANY": 52000}, 19000),
            ],
        },
    )
    return store


def _row(flight_number: str, fares: dict, points: int) -> dict:
    return {
        "flight_number": flight_number,
        "departure_time": "06:20",
        "stop_description": "Nonstop",
        "is_nonstop": True,
        "fares": fares,
        "points": points,
        "is_tracked": True,
        "is_hit": True,
        "previous_points": None,
        "delta": None,
        "lowest_ever": points,
    }


def test_selecting_flights_updates_the_board_without_a_new_check(store: ResultsStore) -> None:
    views = service.list_watches(make_config(flightNumbers=["3200"]), store)
    rows = {row["flight_number"]: row for row in views[0]["last_check"]["rows"]}

    assert rows["3200"]["is_tracked"] is True
    assert rows["2992"]["is_tracked"] is False
    # Still under the threshold, but no longer selected, so it must not read as a hit
    assert rows["2992"]["is_hit"] is False


def test_selecting_a_fare_class_reprices_the_board(store: ResultsStore) -> None:
    views = service.list_watches(make_config(fareTypes=["ANY"]), store)
    rows = {row["flight_number"]: row for row in views[0]["last_check"]["rows"]}

    assert rows["3200"]["points"] == 28000
    # 28,000 is above the 20,000 threshold, so the watch is no longer hitting
    assert rows["3200"]["is_hit"] is False
    assert views[0]["last_check"]["status"] == "no_hit"


def test_no_selection_leaves_every_flight_tracked(store: ResultsStore) -> None:
    views = service.list_watches(make_config(), store)

    assert all(row["is_tracked"] for row in views[0]["last_check"]["rows"])
    assert views[0]["last_check"]["status"] == "hit"


def test_a_failed_check_keeps_its_status(tmp_path: Path) -> None:
    """Re-deriving hits must not turn an error into a 'no_hit' result."""
    store = ResultsStore(filepath=tmp_path / "watch_results.json")
    store.save_result(
        "w1",
        {
            "checked_at": "2099-01-01T00:00:00",
            "status": "error",
            "message": "Webdriver timeout",
            "transient": False,
            "lowest_points": None,
            "fare_classes": [],
            "rows": [],
        },
    )

    views = service.list_watches(make_config(), store)

    assert views[0]["last_check"]["status"] == "error"
