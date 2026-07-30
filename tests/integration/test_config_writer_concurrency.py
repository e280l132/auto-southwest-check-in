"""
Tests that concurrent writes to config.json's reservations don't silently lose an update.

Flask runs with threaded=True, and the job manager writes independently of request threads (its
background flight-info caching), so two overlapping "read reservations, add one, write them all
back" cycles could otherwise race: whichever writer's read happened first would still be the one
whose write lands last, discarding the other's change. `create_reservation` sleeps inside its own
`read_reservations` call (monkeypatched below) to force that race window open on every run rather
than relying on timing luck.
"""

import json
import threading
import time
from pathlib import Path

from pytest_mock import MockerFixture

from lib.config import GlobalConfig
from lib.webui import config_writer
from lib.webui.app import create_app


def _write_config(path: Path, reservations: list[dict]) -> None:
    path.write_text(json.dumps({"reservations": reservations}))


def test_concurrent_reservation_creates_dont_lose_an_update(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, [])

    config = GlobalConfig()
    mocker.patch.object(type(config), "config_file_path", property(lambda _self: config_path))

    real_read = config_writer.read_reservations

    def slow_read(path: Path) -> list[dict]:
        # Widen the race window deterministically instead of hoping thread scheduling collides
        time.sleep(0.2)
        return real_read(path)

    mocker.patch.object(config_writer, "read_reservations", side_effect=slow_read)

    app = create_app(config)
    app.config.update(TESTING=True)

    results = {}

    def add_reservation(name: str, confirmation_number: str) -> None:
        with app.test_client() as client:
            results[name] = client.post(
                "/api/reservations",
                data={
                    "confirmationNumber": confirmation_number,
                    "firstName": "Test",
                    "lastName": "User",
                },
            )

    thread_a = threading.Thread(target=add_reservation, args=("a", "AAAAAA"))
    thread_b = threading.Thread(target=add_reservation, args=("b", "BBBBBB"))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    config_writer.read_reservations.side_effect = None  # stop slowing further reads
    saved_confirmations = {r["confirmationNumber"] for r in real_read(config_path)}

    assert saved_confirmations == {"AAAAAA", "BBBBBB"}, (
        "both concurrent creates should be present -- neither write should have clobbered the other"
    )


def test_update_cached_flight_holds_the_same_lock_as_route_handlers(tmp_path: Path) -> None:
    """
    update_cached_flight runs from the job manager's background thread, independently of any
    Flask request. It must serialize against the same lock the route handlers use, or the race
    above just moves to daemon-vs-request-thread instead of request-vs-request.
    """
    config_path = tmp_path / "config.json"
    _write_config(config_path, [{"confirmationNumber": "TEST"}])

    lock_was_held_during_write = []

    real_write = config_writer.write_reservations

    def observing_write(path: Path, reservations: list[dict]) -> None:
        lock_was_held_during_write.append(config_writer.LOCK.locked())
        real_write(path, reservations)

    original_write = config_writer.write_reservations
    config_writer.write_reservations = observing_write
    try:
        config_writer.update_cached_flight(
            config_path,
            "TEST",
            flight_number="123",
            departure_airport_code="LAX",
            destination_airport_code="JFK",
            local_departure_date="2026-08-21",
        )
    finally:
        config_writer.write_reservations = original_write

    assert lock_was_held_during_write == [True]
