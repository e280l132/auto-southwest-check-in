import logging
from collections.abc import Callable
from typing import Any

import pytest
from pytest_mock import MockerFixture

from lib import fare_checker
from lib.config import ReservationConfig
from lib.fare_checker import FareChecker
from lib.flight import Flight
from lib.notification_handler import NotificationHandler
from lib.reservation_monitor import ReservationMonitor
from lib.utils import CheckFaresOption, DriverTimeoutError, FlightChangeError

JSON = dict[str, Any]


@pytest.fixture(autouse=True)
def mock_sleep(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")


@pytest.fixture
def test_flight(mocker: MockerFixture) -> Flight:
    mocker.patch.object(Flight, "_set_flight_time")
    flight_info = {
        "departureAirport": {"name": None},
        "arrivalAirport": {"name": None, "country": None},
        "departureTime": None,
        "flights": [{"number": "WN100"}],
    }

    reservation_info = {"bounds": [flight_info]}
    return Flight(flight_info, reservation_info, "")


@pytest.fixture
def companion_flight(mocker: MockerFixture) -> Flight:
    """A flight with a matching bound that includes departure date and fare type."""
    from datetime import datetime, timezone

    mocker.patch.object(Flight, "_set_flight_time")
    flight_info = {
        "departureAirport": {"name": None, "code": "LAX"},
        "arrivalAirport": {"name": None, "country": None, "code": "MIA"},
        "departureTime": None,
        "flights": [{"number": "WN100"}],
    }
    reservation_info = {
        "bounds": [
            {
                "flights": [{"number": "WN100"}],
                "departureDate": "2025-12-01",
                "fareProductDetails": {"fareProductId": "WANNA_GET_AWAY"},
            }
        ]
    }
    flight = Flight(flight_info, reservation_info, "ABCDEF")
    # Set a real local departure time so _check_all_alternate_fares can format the date
    flight._local_departure_time = datetime(2025, 12, 1, 14, 40, tzinfo=timezone.utc)
    return flight


class TestFareChecker:
    @pytest.fixture(autouse=True)
    def _set_up_checker(self) -> None:
        self.checker = FareChecker(ReservationMonitor(ReservationConfig()))

    def test_check_flight_price_sends_notification_on_lower_fares(
        self, mocker: MockerFixture, test_flight: Flight
    ) -> None:
        flight_price = {"amount": -10, "currencyCode": "USD"}
        mocker.patch.object(FareChecker, "_get_flight_price", return_value=flight_price)
        mock_lower_fare_notification = mocker.patch.object(NotificationHandler, "lower_fare")

        self.checker.check_flight_price(test_flight)

        mock_lower_fare_notification.assert_called_once()

    # -1 dollar fares are a false positive and are treated as a higher fare
    @pytest.mark.parametrize("amount", [10, 0, -1])
    def test_check_flight_price_does_not_send_notifications_when_fares_are_higher(
        self, mocker: MockerFixture, amount: int, test_flight: Flight
    ) -> None:
        flight_price = {"amount": amount, "currencyCode": "USD"}
        mocker.patch.object(FareChecker, "_get_flight_price", return_value=flight_price)
        mock_lower_fare_notification = mocker.patch.object(NotificationHandler, "lower_fare")

        self.checker.check_flight_price(test_flight)
        mock_lower_fare_notification.assert_not_called()

    def test_get_flight_price_gets_flight_price_matching_current_flight(
        self, mocker: MockerFixture, test_flight: Flight
    ) -> None:
        flights = [
            {"flightNumbers": "99"},
            {"flightNumbers": "100", "fares": ["fare_one", "fare_two"]},
        ]
        mocker.patch.object(
            FareChecker, "_get_matching_flights", return_value=(flights, "test_fare")
        )
        mock_get_matching_fare = mocker.patch.object(
            FareChecker, "_get_matching_fare", return_value={"amount": -300, "currencyCode": "PTS"}
        )

        price = self.checker._get_flight_price(test_flight)

        assert price == {"amount": -300, "currencyCode": "PTS"}
        mock_get_matching_fare.assert_called_once_with(["fare_one", "fare_two"], "test_fare")

    @pytest.mark.parametrize("bound", ["outbound", "inbound"])
    def test_get_matching_flights_retrieves_correct_bound_page(
        self, mocker: MockerFixture, test_flight: Flight, bound: str
    ) -> None:
        change_flight_page = {"_links": {"changeShopping": {"href": "test_link"}}}
        fare_type_bounds = [
            {"fareProductDetails": {"fareProductId": "outbound_fare"}},
            {"fareProductDetails": {"fareProductId": "inbound_fare"}},
        ]
        mocker.patch.object(
            FareChecker,
            "_get_change_flight_page",
            return_value=(change_flight_page, fare_type_bounds),
        )

        search_query = {"outbound": {"isChangeBound": False}}
        search_query.update({bound: {"isChangeBound": True}})
        mocker.patch.object(FareChecker, "_get_search_query", return_value=search_query)

        response = {"changeShoppingPage": {"flights": {f"{bound}Page": {"cards": "test_cards"}}}}
        mocker.patch("lib.fare_checker.make_request", return_value=response)

        matching_flights, fare_type = self.checker._get_matching_flights(test_flight)

        assert matching_flights == "test_cards"
        assert fare_type == bound + "_fare"

    def test_get_change_flight_page_raises_exception_when_bound_not_matched(
        self, mocker: MockerFixture, test_flight: Flight
    ) -> None:
        change_flight_page = {"_links": {"changeShopping": {"href": "test_link"}}}
        fare_type_bounds = [
            {"fareProductDetails": {"fareProductId": "outbound_fare"}},
            {"fareProductDetails": {"fareProductId": "inbound_fare"}},
        ]
        mocker.patch.object(
            FareChecker,
            "_get_change_flight_page",
            return_value=(change_flight_page, fare_type_bounds),
        )

        # Set both bounds to be false which could happen when the flight number doesn't match those
        # on the reservation, indicating a formatting change on Southwest's end
        search_query = {"outbound": {"isChangeBound": False}, "inbound": {"isChangeBound": False}}
        mocker.patch.object(FareChecker, "_get_search_query", return_value=search_query)

        mocker.patch("lib.fare_checker.make_request")

        with pytest.raises(ValueError):
            self.checker._get_matching_flights(test_flight)

    def test_get_change_flight_page_retrieves_change_flight_page(
        self, mocker: MockerFixture
    ) -> None:
        res_info = {
            "bounds": ["bound_one", "bound_two"],
            "_links": {"change": {"href": "test_link", "query": "query_body"}, "reaccom": None},
        }
        flight_page = {"changeFlightPage": "test_page"}
        mock_make_request = mocker.patch("lib.fare_checker.make_request", return_value=flight_page)

        change_flight_page, fare_type_bounds = self.checker._get_change_flight_page(res_info)

        assert change_flight_page == "test_page"
        assert fare_type_bounds == ["bound_one", "bound_two"]

        call_args = mock_make_request.call_args[0]
        assert call_args[1] == fare_checker.BOOKING_URL + "test_link"
        assert call_args[3] == "query_body"

    def test_get_change_flight_page_raises_exception_when_flight_is_reaccommodated(self) -> None:
        reservation_info = {
            "greyBoxMessage": None,
            "bounds": ["bound_one", "bound_two"],
            "_links": {"change": None, "reaccom": {"href": "test_link"}},
        }

        with pytest.raises(FlightChangeError) as err:
            self.checker._get_change_flight_page(reservation_info)

        assert "reaccommodated" in str(err.value).lower()

    def test_get_change_flight_page_raises_exception_when_flight_cannot_be_changed(self) -> None:
        reservation_info = {
            "greyBoxMessage": None,
            "bounds": ["bound_one", "bound_two"],
            "_links": {"change": None, "reaccom": None},
        }

        with pytest.raises(FlightChangeError) as err:
            self.checker._get_change_flight_page(reservation_info)

        assert "cannot be changed" in str(err.value).lower()

    def test_get_search_query_returns_the_correct_query_for_one_way(
        self, test_flight: Flight
    ) -> None:
        bound_one = {
            "originalDate": "1/1",
            "toAirportCode": "LAX",
            "fromAirportCode": "MIA",
            "flight": "100",
        }
        flight_page = {
            "boundSelections": [bound_one],
            "_links": {"changeShopping": {"body": [{"boundReference": "bound_1"}]}},
        }

        search_query = self.checker._get_search_query(flight_page, test_flight)

        assert len(search_query) == 1
        assert search_query.get("outbound") == {
            "boundReference": "bound_1",
            "date": "1/1",
            "destination-airport": "LAX",
            "origin-airport": "MIA",
            "isChangeBound": True,
        }

    def test_get_search_query_returns_the_correct_query_for_round_trip(
        self, test_flight: Flight
    ) -> None:
        bound_one = {
            "originalDate": "1/1",
            "toAirportCode": "LAX",
            "fromAirportCode": "MIA",
            "flight": "99",
        }
        bound_two = {
            "originalDate": "1/2",
            "toAirportCode": "MIA",
            "fromAirportCode": "LAX",
            "flight": "100",
        }
        flight_page = {
            "boundSelections": [bound_one, bound_two],
            "_links": {
                "changeShopping": {
                    "body": [{"boundReference": "bound_1"}, {"boundReference": "bound_2"}]
                }
            },
        }

        search_query = self.checker._get_search_query(flight_page, test_flight)

        assert len(search_query) == 2
        assert search_query.get("outbound") == {
            "boundReference": "bound_1",
            "date": "1/1",
            "destination-airport": "LAX",
            "origin-airport": "MIA",
            "isChangeBound": False,
        }
        assert search_query.get("inbound") == {
            "boundReference": "bound_2",
            "date": "1/2",
            "destination-airport": "MIA",
            "origin-airport": "LAX",
            "isChangeBound": True,
        }

    def test_check_for_companion_raises_exception_when_a_companion_is_detected(self) -> None:
        reservation_info = {
            "greyBoxMessage": {
                "body": (
                    "In order to change or cancel, you must first cancel the associated "
                    "companion reservation."
                )
            }
        }

        with pytest.raises(FlightChangeError):
            self.checker._check_for_companion(reservation_info)

    @pytest.mark.parametrize(
        "reservation",
        [
            {"greyBoxMessage": None},
            {"greyBoxMessage": {}},
            {"greyBoxMessage": {"body": None}},
            {"greyBoxMessage": {"body": ""}},
        ],
    )
    def test_check_for_companion_passes_when_no_companion_exists(self, reservation: JSON) -> None:
        # An exception will be thrown if the test does not pass
        self.checker._check_for_companion(reservation)

    def test_get_lowest_fare_returns_lowest_matching_fare(
        self, mocker: MockerFixture, test_flight: Flight
    ) -> None:
        self.checker.filter = fare_checker.any_flight_filter

        flights = [{"fares": "fare1"}, {"fares": "fare2"}, {"fares": "fare3"}]
        fares = [
            {"amount": 3000, "currencyCode": "PTS"},
            {"amount": -2000, "currencyCode": "PTS"},
            {"amount": -1000, "currencyCode": "PTS"},
        ]
        mocker.patch.object(FareChecker, "_get_matching_fare", side_effect=fares)

        assert self.checker._get_lowest_fare(test_flight, flights, "test_fare") == fares[1]

    def test_get_lowest_fare_returns_matching_fare_when_only_one_flight(
        self, mocker: MockerFixture, test_flight: Flight
    ) -> None:
        self.checker.filter = fare_checker.same_flight_filter

        flights = [
            {"fares": "fare1", "flightNumbers": "100"},
            {"fares": "fare2", "flightNumbers": "101"},
        ]

        fares = [{"amount": 3000, "currencyCode": "PTS"}, {"amount": -2000, "currencyCode": "PTS"}]
        # Only should be called once, so should only return the first fare
        mocker.patch.object(FareChecker, "_get_matching_fare", side_effect=fares)

        assert self.checker._get_lowest_fare(test_flight, flights, "test_fare") == fares[0]

    # An empty list of flights should never be returned from Southwest, but test just in case
    @pytest.mark.parametrize("flights", [[], [{"fares": "fare1"}]])
    def test_get_lowest_fare_returns_zero_when_no_matching_fares(
        self, mocker: MockerFixture, test_flight: Flight, flights: list[JSON]
    ) -> None:
        self.checker.filter = fare_checker.any_flight_filter
        mocker.patch.object(FareChecker, "_get_matching_fare", return_value=None)

        assert self.checker._get_lowest_fare(test_flight, flights, "test_fare") == {
            "amount": 0,
            "currencyCode": "USD",
        }

    def test_get_matching_fare_returns_the_correct_fare(self) -> None:
        fares = [
            {
                "_meta": {"fareProductId": "wrong_fare"},
                "priceDifference": {"amount": "10,000", "currencyCode": "PTS"},
            },
            {
                "_meta": {"fareProductId": "right_fare"},
                "priceDifference": {"amount": "3,000", "sign": "-", "currencyCode": "PTS"},
            },
        ]
        fare_price = self.checker._get_matching_fare(fares, "right_fare")
        assert fare_price == {"amount": -3000, "currencyCode": "PTS"}

    @pytest.mark.parametrize("fares", [None, [], [{"_meta": {"fareProductId": "right_fare"}}]])
    def test_get_matching_fare_returns_nothing_when_price_is_not_available(
        self, fares: list[JSON]
    ) -> None:
        assert self.checker._get_matching_fare(fares, "right_fare") is None

    # --- _bound_matches_flight ---

    def test_bound_matches_flight_returns_true_when_number_matches(
        self, test_flight: Flight
    ) -> None:
        bound = {"flights": [{"number": "WN100"}]}
        assert self.checker._bound_matches_flight(bound, test_flight) is True

    def test_bound_matches_flight_returns_false_when_number_differs(
        self, test_flight: Flight
    ) -> None:
        bound = {"flights": [{"number": "WN200"}]}
        assert self.checker._bound_matches_flight(bound, test_flight) is False

    def test_bound_matches_flight_handles_multi_segment_flight(
        self, mocker: MockerFixture
    ) -> None:
        mocker.patch.object(Flight, "_set_flight_time")
        flight_info = {
            "departureAirport": {"name": None},
            "arrivalAirport": {"name": None, "country": None},
            "departureTime": None,
            "flights": [{"number": "WN100"}, {"number": "WN200"}],
        }
        flight = Flight(flight_info, {"bounds": []}, "")
        bound = {"flights": [{"number": "WN100"}, {"number": "WN200"}]}
        assert self.checker._bound_matches_flight(bound, flight) is True

    # --- _extract_cards_from_search_response ---

    def test_extract_cards_returns_details_list(self) -> None:
        details = [{"card": "data"}]
        response = {"data": {"searchResults": {"airProducts": [{"details": details}]}}}
        assert self.checker._extract_cards_from_search_response(response) == details

    def test_extract_cards_returns_none_on_missing_key(self) -> None:
        assert self.checker._extract_cards_from_search_response({"wrong": "key"}) is None

    def test_extract_cards_returns_none_on_empty_air_products(self) -> None:
        response = {"data": {"searchResults": {"airProducts": []}}}
        assert self.checker._extract_cards_from_search_response(response) is None

    def test_extract_cards_returns_none_on_wrong_type(self) -> None:
        assert self.checker._extract_cards_from_search_response(None) is None

    # --- _get_lowest_points_from_cards ---

    def _make_card(self, fare_type: str, value: str, currency: str = "POINTS") -> JSON:
        return {
            "fareProducts": {
                "ADULT": {
                    fare_type: {"fare": {"totalFare": {"value": value, "currencyCode": currency}}}
                }
            }
        }

    def test_get_lowest_points_returns_lowest_across_cards(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.any_flight_filter
        cards = [
            self._make_card("WANNA_GET_AWAY", "15000"),
            self._make_card("WANNA_GET_AWAY", "12000"),
        ]
        assert self.checker._get_lowest_points_from_cards(cards, "WANNA_GET_AWAY", test_flight) == 12000

    def test_get_lowest_points_returns_none_when_no_filter_match(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.same_flight_filter
        cards = [
            {
                "flightNumbers": ["999"],
                **self._make_card("WANNA_GET_AWAY", "12000"),
            }
        ]
        assert self.checker._get_lowest_points_from_cards(cards, "WANNA_GET_AWAY", test_flight) is None

    def test_get_lowest_points_skips_non_points_currency(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.any_flight_filter
        cards = [self._make_card("WANNA_GET_AWAY", "150", currency="USD")]
        assert self.checker._get_lowest_points_from_cards(cards, "WANNA_GET_AWAY", test_flight) is None

    def test_get_lowest_points_returns_none_when_fare_type_missing(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.any_flight_filter
        cards = [{"fareProducts": {"ADULT": {}}}]
        assert self.checker._get_lowest_points_from_cards(cards, "WANNA_GET_AWAY", test_flight) is None

    # --- _public_search_filter ---

    def test_public_search_filter_same_flight_matches(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.same_flight_filter
        assert self.checker._public_search_filter({"flightNumbers": ["100"]}, test_flight) is True

    def test_public_search_filter_same_flight_no_match(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.same_flight_filter
        assert self.checker._public_search_filter({"flightNumbers": ["999"]}, test_flight) is False

    def test_public_search_filter_nonstop_matches(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.nonstop_flight_filter
        assert self.checker._public_search_filter({"filterTags": ["NONSTOP"]}, test_flight) is True

    def test_public_search_filter_nonstop_no_match(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.nonstop_flight_filter
        assert self.checker._public_search_filter({"filterTags": ["CONNECTING"]}, test_flight) is False

    def test_public_search_filter_any_flight_always_true(self, test_flight: Flight) -> None:
        self.checker.filter = fare_checker.any_flight_filter
        assert self.checker._public_search_filter({"flightNumbers": ["999"]}, test_flight) is True

    # --- _log_companion_unavailable ---

    def test_log_companion_unavailable_with_points_mentions_paid_fare(
        self, test_flight: Flight, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="lib.fare_checker"):
            self.checker._log_companion_unavailable(test_flight, 12500, reason="test reason")
        assert "12,500" in caplog.text
        assert "test reason" in caplog.text

    def test_log_companion_unavailable_without_points_mentions_config(
        self, test_flight: Flight, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="lib.fare_checker"):
            self.checker._log_companion_unavailable(test_flight, None)
        assert "companionFarePoints" in caplog.text

    # --- _check_companion_fare_via_webdriver ---

    def test_check_companion_fare_via_webdriver_no_bound_calls_unavailable(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mocker.patch.object(self.checker, "_bound_matches_flight", return_value=False)
        mock_log_unavailable = mocker.patch.object(self.checker, "_log_companion_unavailable")

        self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        mock_log_unavailable.assert_called_once()

    def test_check_companion_fare_via_webdriver_retries_on_timeout(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.side_effect = [DriverTimeoutError("timeout"), {}]
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mocker.patch.object(self.checker, "_extract_cards_from_search_response", return_value=None)
        mock_log_unavailable = mocker.patch.object(self.checker, "_log_companion_unavailable")

        self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        assert mock_wd.get_public_flight_prices.call_count == 2
        mock_log_unavailable.assert_called_once()

    def test_check_companion_fare_via_webdriver_timeout_both_calls_unavailable(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.side_effect = DriverTimeoutError("timeout")
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mock_log_unavailable = mocker.patch.object(self.checker, "_log_companion_unavailable")

        self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        assert mock_wd.get_public_flight_prices.call_count == 2
        mock_log_unavailable.assert_called_once()

    def test_check_companion_fare_via_webdriver_unexpected_error_no_retry(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.side_effect = ValueError("unexpected")
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mock_log_unavailable = mocker.patch.object(self.checker, "_log_companion_unavailable")

        self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        assert mock_wd.get_public_flight_prices.call_count == 1
        mock_log_unavailable.assert_called_once()

    def test_check_companion_fare_via_webdriver_no_cards_calls_unavailable(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.return_value = {}
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mocker.patch.object(self.checker, "_extract_cards_from_search_response", return_value=None)
        mock_log_unavailable = mocker.patch.object(self.checker, "_log_companion_unavailable")

        self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        mock_log_unavailable.assert_called_once()

    def test_check_companion_fare_via_webdriver_fare_type_not_found_returns(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        # First bound_matches call (departure date) → True, second (fare type) → False
        mocker.patch.object(
            self.checker, "_bound_matches_flight", side_effect=[True, False]
        )
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.return_value = {}
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mocker.patch.object(self.checker, "_extract_cards_from_search_response", return_value=[{}])
        mock_lower_fare = mocker.patch.object(NotificationHandler, "lower_fare")

        self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        mock_lower_fare.assert_not_called()

    def test_check_companion_fare_via_webdriver_sends_notification_on_lower_fare(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.return_value = {}
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mocker.patch.object(self.checker, "_extract_cards_from_search_response", return_value=[{}])
        mocker.patch.object(self.checker, "_get_lowest_points_from_cards", return_value=10000)
        mock_lower_fare = mocker.patch.object(NotificationHandler, "lower_fare")

        # companion_fare_points=12500, lowest=10000 → difference=-2500 → lower fare
        self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        mock_lower_fare.assert_called_once()

    def test_check_companion_fare_via_webdriver_logs_no_lower_fare(
        self, mocker: MockerFixture, companion_flight: Flight, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.return_value = {}
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mocker.patch.object(self.checker, "_extract_cards_from_search_response", return_value=[{}])
        mocker.patch.object(self.checker, "_get_lowest_points_from_cards", return_value=13000)
        mock_lower_fare = mocker.patch.object(NotificationHandler, "lower_fare")

        # companion_fare_points=12500, lowest=13000 → difference=+500 → no lower fare
        with caplog.at_level(logging.INFO, logger="lib.fare_checker"):
            self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        mock_lower_fare.assert_not_called()
        assert "no lower fare" in caplog.text

    def test_check_companion_fare_via_webdriver_logs_price_when_no_config(
        self, mocker: MockerFixture, companion_flight: Flight, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.return_value = {}
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mocker.patch.object(self.checker, "_extract_cards_from_search_response", return_value=[{}])
        mocker.patch.object(self.checker, "_get_lowest_points_from_cards", return_value=12500)

        with caplog.at_level(logging.INFO, logger="lib.fare_checker"):
            self.checker._check_companion_fare_via_webdriver(companion_flight, None)

        assert "12,500" in caplog.text
        assert "companionFarePoints" in caplog.text

    # --- _check_companion_fare_via_webdriver: same_day_smart dispatch ---

    def test_check_companion_fare_via_webdriver_same_day_smart_delegates_to_alternate_fares(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        self.checker.reservation_monitor.config.check_fares = CheckFaresOption.SAME_DAY_SMART
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.return_value = {}
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mocker.patch.object(self.checker, "_extract_cards_from_search_response", return_value=[{}])
        mocker.patch.object(self.checker, "_get_lowest_points_from_cards", return_value=10000)
        mock_alt = mocker.patch.object(self.checker, "_check_companion_alternate_fares")

        self.checker._check_companion_fare_via_webdriver(companion_flight, 12500)

        mock_alt.assert_called_once()

    def test_check_companion_fare_via_webdriver_same_day_smart_no_fare_points_logs(
        self, mocker: MockerFixture, companion_flight: Flight, caplog: pytest.LogCaptureFixture
    ) -> None:
        self.checker.reservation_monitor.config.check_fares = CheckFaresOption.SAME_DAY_SMART
        mock_wd = mocker.MagicMock()
        mock_wd.get_public_flight_prices.return_value = {}
        mocker.patch("lib.webdriver.WebDriver", return_value=mock_wd)
        mocker.patch.object(self.checker, "_extract_cards_from_search_response", return_value=[{}])
        mocker.patch.object(self.checker, "_get_lowest_points_from_cards", return_value=10000)
        mock_alt = mocker.patch.object(self.checker, "_check_companion_alternate_fares")
        mock_lower_fare = mocker.patch.object(NotificationHandler, "lower_fare")

        with caplog.at_level(logging.INFO, logger="lib.fare_checker"):
            self.checker._check_companion_fare_via_webdriver(companion_flight, None)

        mock_alt.assert_not_called()
        mock_lower_fare.assert_not_called()
        assert "companionFarePoints" in caplog.text

    # --- _check_companion_alternate_fares ---

    @staticmethod
    def _make_public_card(
        flight_nums: list[str],
        price_pts: int,
        fare_type: str = "WANNA_GET_AWAY",
        nonstop: bool = True,
        dep_time: str = "08:00",
    ) -> dict:
        return {
            "flightNumbers": flight_nums,
            "filterTags": ["NONSTOP"] if nonstop else [],
            "departureTime": dep_time,
            "stopDescription": "Nonstop" if nonstop else "1 Stop, LAX",
            "fareProducts": {
                "ADULT": {
                    fare_type: {
                        "fare": {
                            "totalFare": {"value": str(price_pts), "currencyCode": "POINTS"}
                        }
                    }
                }
            },
        }

    def test_check_companion_alternate_fares_sends_notification_for_cheaper_flight(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mock_ignore.is_ignored.return_value = False
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mock_alt_fares = mocker.patch.object(NotificationHandler, "alternate_fares")
        self.checker.reservation_monitor.config.ignore_server_port = 8765

        cards = [self._make_public_card(["200"], 10000)]  # 10000 < 12500 → savings -2500
        self.checker._check_companion_alternate_fares(
            companion_flight, cards, "WANNA_GET_AWAY", 12500, "2025-12-01"
        )

        mock_alt_fares.assert_called_once()
        alts = mock_alt_fares.call_args[0][1]
        assert len(alts) == 1
        assert alts[0]["savings"]["amount"] == -2500

    def test_check_companion_alternate_fares_day_ignored_skips(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = True
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mock_alt_fares = mocker.patch.object(NotificationHandler, "alternate_fares")

        self.checker._check_companion_alternate_fares(
            companion_flight, [], "WANNA_GET_AWAY", 12500, "2025-12-01"
        )

        mock_alt_fares.assert_not_called()

    def test_check_companion_alternate_fares_no_cheaper_flights_no_notification(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mock_alt_fares = mocker.patch.object(NotificationHandler, "alternate_fares")

        # Price equal to companion_fare_points → not cheaper
        cards = [self._make_public_card(["200"], 12500)]
        self.checker._check_companion_alternate_fares(
            companion_flight, cards, "WANNA_GET_AWAY", 12500, "2025-12-01"
        )

        mock_alt_fares.assert_not_called()

    def test_check_companion_alternate_fares_nonstop_filter_excludes_connecting(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        """companion_flight is nonstop → only nonstop alternatives should appear."""
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mock_ignore.is_ignored.return_value = False
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mock_alt_fares = mocker.patch.object(NotificationHandler, "alternate_fares")
        self.checker.reservation_monitor.config.ignore_server_port = 8765

        cards = [
            self._make_public_card(["200"], 10000, nonstop=True),
            self._make_public_card(["300"], 8000, nonstop=False),  # connecting — filtered out
        ]
        self.checker._check_companion_alternate_fares(
            companion_flight, cards, "WANNA_GET_AWAY", 12500, "2025-12-01"
        )

        alts = mock_alt_fares.call_args[0][1]
        assert len(alts) == 1
        assert alts[0]["displayNumber"] == "200"

    def test_check_companion_alternate_fares_excludes_current_flight(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        """The current flight (100) should not appear in alternatives."""
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mock_ignore.is_ignored.return_value = False
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mock_alt_fares = mocker.patch.object(NotificationHandler, "alternate_fares")
        self.checker.reservation_monitor.config.ignore_server_port = 8765

        cards = [
            self._make_public_card(["100"], 10000),  # current flight — excluded
            self._make_public_card(["200"], 10000),  # alternate — included
        ]
        self.checker._check_companion_alternate_fares(
            companion_flight, cards, "WANNA_GET_AWAY", 12500, "2025-12-01"
        )

        alts = mock_alt_fares.call_args[0][1]
        assert len(alts) == 1
        assert alts[0]["displayNumber"] == "200"

    def test_check_companion_alternate_fares_all_ignored_no_notification(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mock_ignore.is_ignored.return_value = True  # all ignored
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mock_alt_fares = mocker.patch.object(NotificationHandler, "alternate_fares")

        cards = [self._make_public_card(["200"], 10000)]
        self.checker._check_companion_alternate_fares(
            companion_flight, cards, "WANNA_GET_AWAY", 12500, "2025-12-01"
        )

        mock_alt_fares.assert_not_called()

    # --- _is_nonstop ---

    def test_is_nonstop_returns_true_for_single_segment_flight(
        self, test_flight: Flight
    ) -> None:
        # test_flight has flight_number "100" (no zero-width slash)
        assert self.checker._is_nonstop(test_flight) is True

    def test_is_nonstop_returns_false_for_connecting_flight(
        self, mocker: MockerFixture
    ) -> None:
        mocker.patch.object(Flight, "_set_flight_time")
        flight_info = {
            "departureAirport": {"name": None},
            "arrivalAirport": {"name": None, "country": None},
            "departureTime": None,
            "flights": [{"number": "WN100"}, {"number": "WN200"}],
        }
        connecting_flight = Flight(flight_info, {"bounds": []}, "")
        assert self.checker._is_nonstop(connecting_flight) is False

    # --- _get_all_cheaper_flights ---

    def test_get_all_cheaper_flights_returns_sorted_cheaper_options(
        self, mocker: MockerFixture, test_flight: Flight
    ) -> None:
        cards = [
            {
                "flightNumbers": "200",
                "departureTime": "08:00",
                "stopDescription": "Nonstop",
                "fares": [
                    {
                        "_meta": {"fareProductId": "WGA"},
                        "priceDifference": {"amount": "1,500", "sign": "-", "currencyCode": "PTS"},
                    }
                ],
            },
            {
                "flightNumbers": "300",
                "departureTime": "10:00",
                "stopDescription": "Nonstop",
                "fares": [
                    {
                        "_meta": {"fareProductId": "WGA"},
                        "priceDifference": {"amount": "3,000", "sign": "-", "currencyCode": "PTS"},
                    }
                ],
            },
        ]
        mocker.patch.object(
            FareChecker, "_get_matching_flights", return_value=(cards, "WGA")
        )

        results = self.checker._get_all_cheaper_flights(test_flight)

        assert len(results) == 2
        # Sorted biggest savings first (most negative amount first)
        assert results[0]["savings"]["amount"] == -3000
        assert results[1]["savings"]["amount"] == -1500

    def test_get_all_cheaper_flights_nonstop_flight_only_includes_nonstop(
        self, mocker: MockerFixture, test_flight: Flight
    ) -> None:
        """Nonstop current flight should exclude connecting alternatives."""
        cards = [
            {
                "flightNumbers": "200",
                "departureTime": "08:00",
                "stopDescription": "Nonstop",
                "fares": [
                    {
                        "_meta": {"fareProductId": "WGA"},
                        "priceDifference": {"amount": "2,000", "sign": "-", "currencyCode": "PTS"},
                    }
                ],
            },
            {
                "flightNumbers": "300",
                "departureTime": "10:00",
                "stopDescription": "1 Stop, LAX",
                "fares": [
                    {
                        "_meta": {"fareProductId": "WGA"},
                        "priceDifference": {"amount": "3,000", "sign": "-", "currencyCode": "PTS"},
                    }
                ],
            },
        ]
        mocker.patch.object(
            FareChecker, "_get_matching_flights", return_value=(cards, "WGA")
        )

        results = self.checker._get_all_cheaper_flights(test_flight)

        assert len(results) == 1
        assert results[0]["flightNumbers"] == "200"

    def test_get_all_cheaper_flights_connecting_flight_includes_any(
        self, mocker: MockerFixture
    ) -> None:
        """Connecting current flight should include both nonstop and connecting alternatives."""
        mocker.patch.object(Flight, "_set_flight_time")
        flight_info = {
            "departureAirport": {"name": None},
            "arrivalAirport": {"name": None, "country": None},
            "departureTime": None,
            "flights": [{"number": "WN100"}, {"number": "WN200"}],
        }
        connecting_flight = Flight(flight_info, {"bounds": []}, "")

        cards = [
            {
                "flightNumbers": "300",
                "departureTime": "08:00",
                "stopDescription": "Nonstop",
                "fares": [
                    {
                        "_meta": {"fareProductId": "WGA"},
                        "priceDifference": {"amount": "1,000", "sign": "-", "currencyCode": "PTS"},
                    }
                ],
            },
            {
                "flightNumbers": "400",
                "departureTime": "12:00",
                "stopDescription": "1 Stop, PHX",
                "fares": [
                    {
                        "_meta": {"fareProductId": "WGA"},
                        "priceDifference": {"amount": "2,000", "sign": "-", "currencyCode": "PTS"},
                    }
                ],
            },
        ]
        mocker.patch.object(
            FareChecker, "_get_matching_flights", return_value=(cards, "WGA")
        )

        results = self.checker._get_all_cheaper_flights(connecting_flight)

        assert len(results) == 2

    def test_get_all_cheaper_flights_excludes_false_positive_minus_one(
        self, mocker: MockerFixture, test_flight: Flight
    ) -> None:
        cards = [
            {
                "flightNumbers": "200",
                "departureTime": "08:00",
                "stopDescription": "Nonstop",
                "fares": [
                    {
                        "_meta": {"fareProductId": "WGA"},
                        "priceDifference": {"amount": "1", "sign": "-", "currencyCode": "USD"},
                    }
                ],
            }
        ]
        mocker.patch.object(
            FareChecker, "_get_matching_flights", return_value=(cards, "WGA")
        )

        results = self.checker._get_all_cheaper_flights(test_flight)
        assert results == []

    # --- _check_all_alternate_fares ---

    def test_check_all_alternate_fares_day_ignored_skips_check(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = True
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mock_get_all = mocker.patch.object(self.checker, "_get_all_cheaper_flights")

        self.checker._check_all_alternate_fares(companion_flight)

        mock_get_all.assert_not_called()

    def test_check_all_alternate_fares_no_cheaper_flights_no_notification(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mocker.patch.object(self.checker, "_get_all_cheaper_flights", return_value=[])
        mock_alternate_fares = mocker.patch.object(NotificationHandler, "alternate_fares")

        self.checker._check_all_alternate_fares(companion_flight)

        mock_alternate_fares.assert_not_called()

    def test_check_all_alternate_fares_all_ignored_no_notification(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mock_ignore.is_ignored.return_value = True
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        alternatives = [{"flightNumbers": "200", "savings": {"amount": -2000, "currencyCode": "PTS"}}]
        mocker.patch.object(self.checker, "_get_all_cheaper_flights", return_value=alternatives)
        mock_alternate_fares = mocker.patch.object(NotificationHandler, "alternate_fares")

        self.checker._check_all_alternate_fares(companion_flight)

        mock_alternate_fares.assert_not_called()

    def test_check_all_alternate_fares_visible_alternatives_sends_notification(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mock_ignore.is_ignored.return_value = False
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        alternatives = [{"flightNumbers": "200", "savings": {"amount": -2000, "currencyCode": "PTS"}}]
        mocker.patch.object(self.checker, "_get_all_cheaper_flights", return_value=alternatives)
        mock_alternate_fares = mocker.patch.object(NotificationHandler, "alternate_fares")
        self.checker.reservation_monitor.config.ignore_server_port = 8765

        self.checker._check_all_alternate_fares(companion_flight)

        mock_alternate_fares.assert_called_once()

    def test_check_all_alternate_fares_flight_change_error_handled(
        self, mocker: MockerFixture, companion_flight: Flight
    ) -> None:
        mock_ignore = mocker.MagicMock()
        mock_ignore.is_day_ignored.return_value = False
        mocker.patch("lib.ignore_manager.IgnoreManager", return_value=mock_ignore)
        mocker.patch.object(
            self.checker, "_get_all_cheaper_flights", side_effect=FlightChangeError("blocked")
        )
        mocker.patch.object(self.checker, "_is_companion_flight", return_value=False)
        mock_alternate_fares = mocker.patch.object(NotificationHandler, "alternate_fares")

        # Should not raise
        self.checker._check_all_alternate_fares(companion_flight)

        mock_alternate_fares.assert_not_called()


@pytest.mark.parametrize(
    ("option", "expected_filter"),
    [
        (CheckFaresOption.SAME_FLIGHT, fare_checker.same_flight_filter),
        (CheckFaresOption.SAME_DAY_NONSTOP, fare_checker.nonstop_flight_filter),
        (CheckFaresOption.SAME_DAY, fare_checker.any_flight_filter),
        (CheckFaresOption.SAME_DAY_SMART, fare_checker.any_flight_filter),
    ],
)
def test_get_fare_check_filter_returns_the_corresponding_filter(
    option: CheckFaresOption, expected_filter: Callable[[Flight, JSON], bool]
) -> None:
    assert fare_checker.get_fare_check_filter(option) == expected_filter


def test_get_fare_check_filter_raises_exception_when_option_does_not_match() -> None:
    with pytest.raises(ValueError):
        fare_checker.get_fare_check_filter("wrong_option")


@pytest.mark.parametrize(
    ("flight", "filter_out"), [({"flightNumbers": "100"}, True), ({"flightNumbers": "101"}, False)]
)
def test_same_flight_filter(flight: JSON, filter_out: bool, test_flight: Flight) -> None:
    assert fare_checker.same_flight_filter(test_flight, flight) == filter_out


def test_any_flight_filter(test_flight: Flight) -> None:
    assert fare_checker.any_flight_filter(test_flight, {"flightNumbers": "101"})


@pytest.mark.parametrize(
    ("flight", "filter_out"),
    [({"stopDescription": "1 Stop, LAX"}, False), ({"stopDescription": "Nonstop"}, True)],
)
def test_nonstop_flight_filter(flight: JSON, filter_out: bool) -> None:
    assert fare_checker.nonstop_flight_filter(test_flight, flight) == filter_out
