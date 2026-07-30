"""
Tests the fallback to Southwest's own reservation lookup.

The mobile view-reservation API now rejects most requests with code 403050700 while the website's
own endpoint answers reliably, so a rejection falls back to letting the site do the lookup. The
payloads differ, so the translation is covered here against a real captured response shape.
"""

import json
from datetime import datetime, timezone
from unittest import mock

import pytest
from pytest_mock import MockerFixture

from lib.checkin_scheduler import RESERVATION_MAX_ATTEMPTS, CheckInScheduler
from lib.config import GlobalConfig
from lib.fare_checker import FareChecker, is_companion_flight
from lib.flight import Flight
from lib.reservation_monitor import ReservationMonitor
from lib.reservation_schema import translate_manage_reservation
from lib.utils import TRANSIENT_ORIGIN_REJECTION, RequestError
from lib.webdriver import WebDriver

# Shaped exactly like a real www manage-reservation 'data' object
WEBSITE_DATA = {
    "id": "CW6KR4",
    "trip_type": "one_way",
    "bounds": [
        {
            "origination_airport_code": "STL",
            "destination_airport_code": "LGA",
            "international": False,
            "segments": [
                {
                    "id": "1",
                    "depart_at": "2026-08-22T16:20:00.000-05:00",
                    "arrive_at": "2026-08-22T19:40:00.000-04:00",
                    "origination_airport_code": "STL",
                    "destination_airport_code": "LGA",
                    "flight_number": "893",
                }
            ],
        }
    ],
    "passengers": [{"name": {"first_name": "BRIAN", "last_name": "FENSTER"}}],
}


def rejection() -> RequestError:
    return RequestError("Forbidden (403)", southwest_code=TRANSIENT_ORIGIN_REJECTION)


def test_translation_produces_what_the_scheduler_needs() -> None:
    info = translate_manage_reservation(WEBSITE_DATA)
    bound = info["bounds"][0]

    assert bound["departureAirport"]["code"] == "STL"
    assert bound["arrivalAirport"]["code"] == "LGA"
    assert bound["flights"] == [{"number": "893"}]
    # depart_at is already local to the departure airport; the offset is re-derived downstream
    assert bound["departureDate"] == "2026-08-22"
    assert bound["departureTime"] == "16:20"
    # A domestic flight must not trigger the passport prompt
    assert bound["arrivalAirport"]["country"] is None


def test_translation_marks_international_flights() -> None:
    data = json.loads(json.dumps(WEBSITE_DATA))
    data["bounds"][0]["international"] = True

    info = translate_manage_reservation(data)

    assert info["bounds"][0]["arrivalAirport"]["country"] is not None


def test_translation_keeps_fare_checks_on_their_skip_path() -> None:
    """
    This payload has no change link. The keys must still exist so the fare checker skips the check
    rather than raising a KeyError.
    """
    info = translate_manage_reservation(WEBSITE_DATA)

    assert info["_links"] == {"change": None, "reaccom": None}


def test_translation_refuses_an_unexpected_payload() -> None:
    """A silently-wrong translation would schedule check-ins for the wrong time."""
    data = json.loads(json.dumps(WEBSITE_DATA))
    data["bounds"][0]["segments"] = []

    with pytest.raises(ValueError):
        translate_manage_reservation(data)


@pytest.fixture(autouse=True)
def _offline(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")
    mocker.patch(
        "pathlib.Path.read_text", return_value=json.dumps({"STL": "America/Chicago"})
    )
    mocker.patch("lib.checkin_handler.Process").return_value.pid = 12345
    mocker.patch("os.kill")
    mocker.patch("os.waitpid")
    mocker.patch("lib.notification_handler.NotificationHandler.new_flights")
    mocker.patch("lib.notification_handler.NotificationHandler.reaccommodated_flights")
    mocker.patch(
        "lib.checkin_scheduler.get_current_time",
        return_value=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    def mock_get_driver(self: WebDriver) -> mock.Mock:
        self.checkin_scheduler.headers = {"X-API-Key": "test_key"}
        self.headers_set = True
        return mocker.patch("lib.webdriver.Driver")

    mocker.patch.object(WebDriver, "_get_driver", mock_get_driver)


def _scheduler() -> "ReservationMonitor":
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "CW6KR4", "firstName": "Brian", "lastName": "Fenster"}]
    )
    return ReservationMonitor(config.reservations[0], lock=None).checkin_scheduler


def test_a_rejected_lookup_falls_back_to_the_website(
    mocker: MockerFixture
) -> None:
    mocker.patch("lib.checkin_scheduler.make_request", side_effect=rejection())
    mock_lookup = mocker.patch.object(WebDriver, "get_reservation", return_value=WEBSITE_DATA)
    mock_notify = mocker.patch(
        "lib.notification_handler.NotificationHandler.failed_reservation_retrieval"
    )

    scheduler = _scheduler()
    scheduler.process_reservations(["CW6KR4"])

    mock_lookup.assert_called_once_with("CW6KR4", "Brian", "Fenster")
    assert len(scheduler.flights) == 1, "the flight should be scheduled from the website lookup"
    assert scheduler.flights[0].flight_number == "893"
    mock_notify.assert_not_called(), "a recovered lookup is not worth reporting as a failure"
    assert scheduler.last_fetch_error is None


def test_the_website_is_not_used_for_a_real_reservation_error(
    mocker: MockerFixture
) -> None:
    """A cancelled or mistyped reservation is a real answer, so don't go asking the website too."""
    # 400620389 == SouthwestErrorCode.RESERVATION_NOT_FOUND
    mocker.patch(
        "lib.checkin_scheduler.make_request",
        side_effect=RequestError("Reservation not found", southwest_code=400620389),
    )
    mock_lookup = mocker.patch.object(WebDriver, "get_reservation")
    mocker.patch("lib.notification_handler.NotificationHandler.failed_reservation_retrieval")

    scheduler = _scheduler()
    scheduler.process_reservations(["CW6KR4"])

    mock_lookup.assert_not_called()


def test_the_original_error_is_reported_when_the_website_also_fails(
    mocker: MockerFixture
) -> None:
    mocker.patch("lib.checkin_scheduler.make_request", side_effect=rejection())
    mocker.patch.object(WebDriver, "get_reservation", side_effect=Exception("driver blew up"))
    mock_notify = mocker.patch(
        "lib.notification_handler.NotificationHandler.failed_reservation_retrieval"
    )

    scheduler = _scheduler()
    scheduler.process_reservations(["CW6KR4"])

    mock_notify.assert_called_once()
    reported = mock_notify.call_args[0][0]
    assert reported.southwest_code == TRANSIENT_ORIGIN_REJECTION
    assert len(scheduler.flights) == 0


def test_the_mobile_api_is_not_retried_into_the_ground_first(
    mocker: MockerFixture
) -> None:
    """The website answers reliably, so don't exhaust the endpoint that mostly says no first."""
    mock_request = mocker.patch("lib.checkin_scheduler.make_request", side_effect=rejection())
    mocker.patch.object(WebDriver, "get_reservation", return_value=WEBSITE_DATA)

    scheduler = _scheduler()
    scheduler.process_reservations(["CW6KR4"])

    assert mock_request.call_args.kwargs["max_attempts"] == RESERVATION_MAX_ATTEMPTS
    assert RESERVATION_MAX_ATTEMPTS < 20


def test_public_search_uses_the_right_paid_fare(mocker: MockerFixture) -> None:
    """
    Which figure was paid depends on how the reservation was booked, so the public search has to
    compare against the matching one or every result is wrong by the difference between them.
    """
    config = GlobalConfig()
    config.create_reservation_config(
        [
            {
                "confirmationNumber": "CW6KR4",
                "firstName": "Brian",
                "lastName": "Fenster",
                "originalFarePoints": 13500,
                "companionFarePoints": 4200,
            }
        ]
    )
    monitor = ReservationMonitor(config.reservations[0], lock=None, send_external=False)
    checker = FareChecker(monitor)
    delegate = mocker.patch.object(checker, "_check_companion_fare_via_webdriver")
    flight = mocker.Mock()

    mocker.patch.object(FareChecker, "_is_companion_flight", return_value=False)
    checker._check_fare_via_public_search(flight)
    assert delegate.call_args[0][1] == 13500

    mocker.patch.object(FareChecker, "_is_companion_flight", return_value=True)
    checker._check_fare_via_public_search(flight)
    assert delegate.call_args[0][1] == 4200


def test_a_companion_reservation_is_recognised_from_the_website_payload() -> None:
    """
    The website states the companion outright while the mobile payload only implies it in a grey
    box message. Losing it here silently prices the flight against the wrong paid fare.
    """
    data = json.loads(json.dumps(WEBSITE_DATA))
    data["associated_reservations"] = [{"id": "CW3WCW", "type": "COMPANION"}]

    info = translate_manage_reservation(data)
    assert info["hasCompanion"] is True

    flight = Flight(info["bounds"][0], info, "CW6KR4")
    assert is_companion_flight(flight) is True


def test_a_solo_reservation_is_not_treated_as_a_companion_one() -> None:
    info = translate_manage_reservation(WEBSITE_DATA)

    assert info["hasCompanion"] is False


def test_a_malformed_bound_is_skipped_not_fatal(mocker: MockerFixture) -> None:
    """
    Before this fix, one bad bound (an unexpected shape from Southwest, or a translated reservation
    missing a field) raised uncaught out of _get_flights and killed the entire monitor process --
    taking down every other flight on the reservation with it.
    """
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "TEST", "firstName": "Berkant", "lastName": "Marika"}]
    )
    monitor = ReservationMonitor(config.reservations[0], lock=None, send_external=False)
    scheduler: CheckInScheduler = monitor.checkin_scheduler

    good_bound = translate_manage_reservation(WEBSITE_DATA)["bounds"][0]
    broken_bound = {"origination_airport_code": "STL"}  # missing everything else

    mocker.patch.object(
        scheduler,
        "_get_reservation_info",
        return_value=({"bounds": [broken_bound, good_bound], "_links": {}}, True),
    )
    mocker.patch(
        "lib.checkin_scheduler.get_current_time",
        return_value=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    flights, is_authoritative = scheduler._get_flights("TEST")

    assert is_authoritative is True
    assert len(flights) == 1
    assert flights[0].flight_number == "893"
