"""
Tests the airport-timezone cache added to lib.flight.

The timezone file is static and was being re-read and re-parsed from disk on every single Flight
construction. Caching it needs to actually cache (prove the file is read at most once across many
Flight constructions) without leaking stale data between tests, which mock Path.read_text with
different timezone maps -- the autouse fixture in conftest.py is what's expected to keep these
isolated.
"""

import json

from pytest_mock import MockerFixture

from lib.flight import Flight, _load_airport_timezones, clear_airport_timezone_cache

FLIGHT_INFO = {
    "departureAirport": {"name": "Los Angeles", "code": "LAX"},
    "arrivalAirport": {"name": "test_inbound", "code": "SYD", "country": None},
    "departureDate": "2026-08-21",
    "departureTime": "08:00",
    "flights": [{"number": "WN100"}],
}


def test_the_timezone_file_is_read_at_most_once_across_many_flights(
    mocker: MockerFixture,
) -> None:
    mock_read_text = mocker.patch(
        "pathlib.Path.read_text", return_value=json.dumps({"LAX": "America/Los_Angeles"})
    )

    for _ in range(5):
        Flight(dict(FLIGHT_INFO), {}, "TEST")

    assert mock_read_text.call_count == 1


def test_the_cache_does_not_leak_stale_data_between_different_mocked_files(
    mocker: MockerFixture,
) -> None:
    """
    This is what the autouse fixture in conftest.py exists to prevent: if the cache from a
    previous test survived, a new airport code that only the OLD test's data knew about would
    resolve, and a code only THIS test's data knows about would wrongly KeyError or vice versa.
    """
    mocker.patch(
        "pathlib.Path.read_text", return_value=json.dumps({"LAX": "America/Los_Angeles"})
    )
    assert _load_airport_timezones() == {"LAX": "America/Los_Angeles"}

    # Simulate moving to a "new test" without relying on conftest.py's fixture firing mid-test
    clear_airport_timezone_cache()
    mocker.patch(
        "pathlib.Path.read_text", return_value=json.dumps({"SYD": "Australia/Sydney"})
    )

    assert _load_airport_timezones() == {"SYD": "Australia/Sydney"}
