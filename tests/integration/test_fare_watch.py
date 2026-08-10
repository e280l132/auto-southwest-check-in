"""Tests FareWatchChecker's card filtering, cheapest-fare selection, and threshold comparison"""

import pytest
from pytest_mock import MockerFixture

from lib.config import FareWatchConfig, GlobalConfig
from lib.fare_watch import FareWatchChecker
from lib.reservation_monitor import ReservationMonitor
from lib.utils import DriverTimeoutError, RequestError

SEARCH_RESPONSE_URL_CARDS = [
    {
        "flightNumbers": ["100"],
        "departureTime": "08:00",
        "filterTags": ["NONSTOP"],
        "segments": [{"destinationAirportCode": "MCO"}],
        "fareProducts": {
            "ADULT": {
                "WGA": {"fare": {"totalFare": {"value": "7,500", "currencyCode": "POINTS"}}},
                "ANYTIME": {"fare": {"totalFare": {"value": "12,000", "currencyCode": "POINTS"}}},
            }
        },
    },
    {
        "flightNumbers": ["200", "201"],
        "departureTime": "14:30",
        "filterTags": [],
        "segments": [
            {"destinationAirportCode": "BWI"},
            {"destinationAirportCode": "MCO"},
        ],
        "fareProducts": {
            "ADULT": {
                "WGA": {"fare": {"totalFare": {"value": "6,000", "currencyCode": "POINTS"}}},
            }
        },
    },
]


def make_watch(**overrides: object) -> FareWatchConfig:
    watch_json = {
        "origin": "LGA",
        "destination": "MCO",
        "date": "2099-01-01",
        "maxPoints": 8000,
        **overrides,
    }
    watch = FareWatchConfig()
    watch.create(watch_json)
    return watch


@pytest.fixture
def monitor() -> ReservationMonitor:
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "TEST", "firstName": "Berkant", "lastName": "Marika"}]
    )
    return ReservationMonitor(config.reservations[0])


def _search_response(cards: list) -> dict:
    return {"data": {"searchResults": {"airProducts": [{"details": cards}]}}}


def test_hit_when_cheapest_fare_is_at_or_below_threshold(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = _search_response(
        SEARCH_RESPONSE_URL_CARDS
    )

    watch = make_watch(maxPoints=8000)
    result = FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert result.status == "hit"
    assert result.lowest_points == 6000
    hits = [row for row in result.rows if row["isHit"]]
    assert {row["displayNumber"] for row in hits} == {"100", "200/201"}


def test_no_hit_when_every_fare_is_above_threshold(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = _search_response(
        SEARCH_RESPONSE_URL_CARDS
    )

    watch = make_watch(maxPoints=1000)
    result = FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert result.status == "no_hit"
    assert all(not row["isHit"] for row in result.rows)


def test_nonstop_only_filters_out_connecting_flights(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = _search_response(
        SEARCH_RESPONSE_URL_CARDS
    )

    watch = make_watch(maxPoints=8000, nonstopOnly=True)
    result = FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert [row["displayNumber"] for row in result.rows] == ["100"]


def test_flight_numbers_select_what_alerts_without_hiding_the_rest(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    """
    A selection is an alert filter, not a search filter: the board must keep showing every flight
    so the user can change their mind without re-running a check.
    """
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = _search_response(
        SEARCH_RESPONSE_URL_CARDS
    )

    watch = make_watch(maxPoints=8000, flightNumbers=["200"])
    result = FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert [row["displayNumber"] for row in result.rows] == ["100", "200/201"]

    tracked = {row["displayNumber"]: row["isTracked"] for row in result.rows}
    assert tracked == {"100": False, "200/201": True}

    # Flight 100 is under the threshold but unselected, so it must not count as a hit
    hits = {row["displayNumber"] for row in result.rows if row["isHit"]}
    assert hits == {"200/201"}


def test_no_selection_means_every_flight_is_tracked(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = _search_response(
        SEARCH_RESPONSE_URL_CARDS
    )

    result = FareWatchChecker(monitor).check(make_watch(maxPoints=8000), "2099-01-01T00:00:00")

    assert all(row["isTracked"] for row in result.rows)


def test_every_fare_product_is_kept_per_flight(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    """The board needs each class's own price, not just the cheapest one."""
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = _search_response(
        SEARCH_RESPONSE_URL_CARDS
    )

    result = FareWatchChecker(monitor).check(make_watch(), "2099-01-01T00:00:00")

    by_flight = {row["displayNumber"]: row for row in result.rows}
    assert by_flight["100"]["fares"] == {"WGA": 7500, "ANYTIME": 12000}
    assert by_flight["200/201"]["fares"] == {"WGA": 6000}


def test_fares_omit_products_that_are_not_priced_in_points(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    card = {
        "flightNumbers": ["100"],
        "departureTime": "08:00",
        "filterTags": ["NONSTOP"],
        "segments": [{"destinationAirportCode": "MCO"}],
        "fareProducts": {
            "ADULT": {
                "WGA": {"fare": {"totalFare": {"value": "7,500", "currencyCode": "POINTS"}}},
                "CASHONLY": {"fare": {"totalFare": {"value": "199.00", "currencyCode": "USD"}}},
                "BROKEN": {},
            }
        },
    }
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = _search_response([card])

    result = FareWatchChecker(monitor).check(make_watch(), "2099-01-01T00:00:00")

    assert result.rows[0]["fares"] == {"WGA": 7500}


def test_fare_types_restricts_which_products_are_considered(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    """Without a filter, the cheapest of WGA/ANYTIME on card 100 (7,500) applies. Restricting to
    ANYTIME only should raise that flight's price to 12,000 and drop it below the threshold."""
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = _search_response(
        [SEARCH_RESPONSE_URL_CARDS[0]]
    )

    watch = make_watch(maxPoints=8000, fareTypes=["ANYTIME"])
    result = FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert result.rows[0]["points"] == 12000
    assert result.status == "no_hit"


def test_unavailable_when_response_has_no_recognizable_cards(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.return_value = {"data": {}}

    watch = make_watch()
    result = FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert result.status == "unavailable"


def test_transient_search_failure_is_reported_as_transient(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.side_effect = RequestError(
        "Forbidden (403)", '{"code": 403050700}'
    )

    watch = make_watch()
    result = FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert result.status == "error"
    assert result.transient is True


def test_driver_timeout_is_reported_as_error(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    mocker.patch(
        "lib.webdriver.WebDriver"
    ).return_value.get_public_flight_prices.side_effect = DriverTimeoutError("timed out")

    watch = make_watch()
    result = FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert result.status == "error"
    assert result.message == "Webdriver timeout"


def test_retries_with_a_fresh_browser_session_on_transient_failure(
    mocker: MockerFixture, monitor: ReservationMonitor
) -> None:
    mock_webdriver = mocker.patch("lib.webdriver.WebDriver")
    mock_webdriver.return_value.get_public_flight_prices.side_effect = RequestError(
        "Forbidden (403)", '{"code": 403050700}'
    )

    watch = make_watch()
    FareWatchChecker(monitor).check(watch, "2099-01-01T00:00:00")

    assert mock_webdriver.call_count == 3
