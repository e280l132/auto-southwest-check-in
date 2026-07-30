"""
Tests that a failed reservation lookup does not tear down check-ins that are already scheduled.

Southwest intermittently rejects otherwise-valid reservation lookups with code 403050700. A failed
lookup yields no flights, which used to be indistinguishable from a reservation whose flights are
gone, so a single bad response would cancel every scheduled check-in and stop the monitor.
"""

import json
from datetime import datetime, timezone
from multiprocessing import Lock
from unittest import mock

from pytest_mock import MockerFixture
from requests_mock.mocker import Mocker as RequestMocker

from lib.checkin_scheduler import VIEW_RESERVATION_URL
from lib.config import GlobalConfig
from lib.reservation_monitor import ReservationMonitor
from lib.utils import BASE_URL, TRANSIENT_ORIGIN_REJECTION
from lib.webdriver import WebDriver

TEST_RESERVATION_URL = BASE_URL + VIEW_RESERVATION_URL + "TEST"

ALL_HEADERS = {
    "User-Agent": "test_agent",
    "X-API-Key": "test_key",
    "X-Channel-ID": "test_channel_id",
    "EE30zvQLWf-a": "test_a",
}

RESERVATION = {
    "viewReservationViewPage": {
        "bounds": [
            {
                "arrivalAirport": {"name": "test_inbound", "country": None},
                "departureAirport": {"code": "LAX", "name": "test_outbound"},
                "departureDate": "2020-10-13",
                "departureTime": "14:40",
                "flights": [{"number": "WN100"}],
            },
        ],
        "_links": {"reaccom": None},
    }
}


def _set_up(mocker: MockerFixture) -> GlobalConfig:
    mocker.patch(
        "pathlib.Path.read_text", return_value=json.dumps({"LAX": "America/Los_Angeles"})
    )
    mocker.patch("lib.checkin_handler.Process").return_value.pid = 12345
    mocker.patch("lib.notification_handler.NotificationHandler.new_flights")
    mocker.patch("lib.notification_handler.NotificationHandler.reaccommodated_flights")
    mocker.patch("lib.fare_checker.FareChecker.check_flight_price")
    mocker.patch("os.kill")
    mocker.patch("os.waitpid")
    # The retry backoff deliberately spans minutes in production; don't actually wait for it
    mocker.patch("time.sleep")

    def mock_get_driver(self: WebDriver) -> mock.Mock:
        self.checkin_scheduler.headers = self._get_needed_headers(ALL_HEADERS)
        self.headers_set = True
        return mocker.patch("lib.webdriver.Driver")

    mocker.patch.object(WebDriver, "_get_driver", mock_get_driver)

    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "TEST", "firstName": "Berkant", "lastName": "Marika"}]
    )
    return config


def test_transient_rejection_keeps_scheduled_check_ins(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    config = _set_up(mocker)

    current_utc_time = datetime(2020, 10, 5, 18, 29, tzinfo=timezone.utc)
    mocker.patch("lib.reservation_monitor.get_current_time", return_value=current_utc_time)
    mocker.patch("lib.checkin_scheduler.get_current_time", return_value=current_utc_time)

    mock_failed_notification = mocker.patch(
        "lib.notification_handler.NotificationHandler.failed_reservation_retrieval"
    )

    # First cycle schedules the flight, second cycle is rejected by Southwest
    requests_mock.post(
        TEST_RESERVATION_URL,
        [
            {"json": RESERVATION, "status_code": 200},
            {"json": {"code": TRANSIENT_ORIGIN_REJECTION}, "status_code": 403},
        ],
    )

    monitor = ReservationMonitor(config.reservations[0], Lock())
    scheduler = monitor.checkin_scheduler

    scheduler.refresh_headers()
    scheduler.process_reservations(["TEST"])
    assert len(scheduler.flights) == 1, "flight should be scheduled after a successful lookup"

    scheduler.process_reservations(["TEST"])

    # The rejection says nothing about the reservation, so the check-in must survive it
    assert len(scheduler.flights) == 1, "a transient rejection must not cancel a scheduled check-in"
    assert len(scheduler.checkin_handlers) == 1
    mock_failed_notification.assert_called_once()


def test_departed_flights_are_still_removed(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    """The failure path that legitimately clears flights must keep working."""
    config = _set_up(mocker)

    current_utc_time = datetime(2020, 10, 5, 18, 29, tzinfo=timezone.utc)
    mocker.patch("lib.reservation_monitor.get_current_time", return_value=current_utc_time)
    mocker.patch("lib.checkin_scheduler.get_current_time", return_value=current_utc_time)

    requests_mock.post(
        TEST_RESERVATION_URL,
        [
            {"json": RESERVATION, "status_code": 200},
            # 400520413 == SouthwestErrorCode.FLIGHT_IN_PAST
            {"json": {"code": 400520413}, "status_code": 400},
        ],
    )

    monitor = ReservationMonitor(config.reservations[0], Lock())
    scheduler = monitor.checkin_scheduler

    scheduler.refresh_headers()
    scheduler.process_reservations(["TEST"])
    assert len(scheduler.flights) == 1

    scheduler.process_reservations(["TEST"])

    assert len(scheduler.flights) == 0, "departed flights should still be removed"
    assert len(scheduler.checkin_handlers) == 0
