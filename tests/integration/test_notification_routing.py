"""
Tests who gets told about a failed reservation retrieval, and how loudly.

Southwest intermittently rejects valid lookups with code 403050700. That is routine and self-heals,
so it should not page the user every time — while a genuinely broken reservation still should.
Separately, a check the user runs from the web UI should report on their screen, not to their inbox.

These tests drive `make_request` directly rather than mocking HTTP, because a single retrieval
"cycle" is 20 retries deep and a per-response mock would be consumed inside one cycle.
"""

import json
from datetime import datetime, timezone
from unittest import mock

import pytest
from pytest_mock import MockerFixture

from lib.checkin_scheduler import TRANSIENT_FAILURE_ALERT_THRESHOLD
from lib.config import GlobalConfig
from lib.reservation_monitor import ReservationMonitor
from lib.utils import TRANSIENT_ORIGIN_REJECTION, NotificationLevel, RequestError
from lib.webdriver import WebDriver
from lib.webui import runner

# 400620389 == SouthwestErrorCode.RESERVATION_NOT_FOUND
RESERVATION_NOT_FOUND = 400620389

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


def rejection() -> RequestError:
    return RequestError("Forbidden (403)", southwest_code=TRANSIENT_ORIGIN_REJECTION)


def not_found() -> RequestError:
    return RequestError("Reservation not found", southwest_code=RESERVATION_NOT_FOUND)


@pytest.fixture(autouse=True)
def _fast_and_offline(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")
    mocker.patch(
        "pathlib.Path.read_text", return_value=json.dumps({"LAX": "America/Los_Angeles"})
    )
    mocker.patch("lib.checkin_handler.Process").return_value.pid = 12345
    mocker.patch("os.kill")
    mocker.patch("os.waitpid")

    def mock_get_driver(self: WebDriver) -> mock.Mock:
        self.checkin_scheduler.headers = {"X-API-Key": "test_key"}
        self.headers_set = True
        return mocker.patch("lib.webdriver.Driver")

    mocker.patch.object(WebDriver, "_get_driver", mock_get_driver)


def _reservation_config(healthchecks_url: str | None = None) -> GlobalConfig:
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "TEST", "firstName": "Berkant", "lastName": "Marika"}]
    )
    if healthchecks_url:
        config.reservations[0].healthchecks_url = healthchecks_url
    return config


def test_transient_rejection_stays_quiet_until_it_keeps_happening(mocker: MockerFixture) -> None:
    config = _reservation_config()
    mock_notify = mocker.patch("lib.notification_handler.NotificationHandler.send_notification")
    mocker.patch("lib.checkin_scheduler.make_request", side_effect=rejection())

    scheduler = ReservationMonitor(config.reservations[0], lock=None).checkin_scheduler

    levels = []
    for _ in range(TRANSIENT_FAILURE_ALERT_THRESHOLD):
        scheduler.process_reservations(["TEST"])
        levels.append(mock_notify.call_args[0][1])

    quiet_cycles = TRANSIENT_FAILURE_ALERT_THRESHOLD - 1
    assert levels[:-1] == [NotificationLevel.NOTICE] * quiet_cycles, (
        "an isolated transient rejection should not page the user"
    )
    assert levels[-1] == NotificationLevel.ERROR, (
        "failing this many cycles running is worth knowing about"
    )


def test_a_success_resets_the_escalation(mocker: MockerFixture) -> None:
    config = _reservation_config()
    mock_notify = mocker.patch("lib.notification_handler.NotificationHandler.send_notification")
    mocker.patch(
        "lib.checkin_scheduler.get_current_time",
        return_value=datetime(2020, 10, 5, tzinfo=timezone.utc),
    )
    mocker.patch("lib.notification_handler.NotificationHandler.new_flights")
    mocker.patch("lib.notification_handler.NotificationHandler.reaccommodated_flights")
    mock_request = mocker.patch("lib.checkin_scheduler.make_request")

    scheduler = ReservationMonitor(config.reservations[0], lock=None).checkin_scheduler

    mock_request.side_effect = rejection()
    scheduler.process_reservations(["TEST"])
    scheduler.process_reservations(["TEST"])
    assert scheduler.transient_failures["TEST"] == 2

    mock_request.side_effect = None
    mock_request.return_value = RESERVATION
    scheduler.process_reservations(["TEST"])
    assert "TEST" not in scheduler.transient_failures, "a success should clear the streak"

    mock_request.side_effect = rejection()
    scheduler.process_reservations(["TEST"])

    assert scheduler.transient_failures["TEST"] == 1
    assert mock_notify.call_args[0][1] == NotificationLevel.NOTICE


def test_a_broken_reservation_still_reports_immediately(mocker: MockerFixture) -> None:
    """A reservation that does not exist will never come back, so it should not be held back."""
    config = _reservation_config()
    mock_notify = mocker.patch("lib.notification_handler.NotificationHandler.send_notification")
    mocker.patch("lib.checkin_scheduler.make_request", side_effect=not_found())

    scheduler = ReservationMonitor(config.reservations[0], lock=None).checkin_scheduler
    scheduler.process_reservations(["TEST"])

    assert mock_notify.call_args[0][1] == NotificationLevel.ERROR


def test_web_initiated_check_sends_nothing_externally(mocker: MockerFixture) -> None:
    """The user is watching the page, so a manual check must not push to Apprise or Healthchecks."""
    config = _reservation_config(healthchecks_url="https://hc.example.com/ping")
    mock_apprise = mocker.patch("apprise.Apprise")
    # Patch the module reference inside notification_handler only. Patching requests.post itself
    # would also break the HTTP layer every other test depends on.
    mock_requests = mocker.patch("lib.notification_handler.requests")
    mocker.patch("lib.checkin_scheduler.make_request", side_effect=rejection())

    payload = runner.run_check(config.reservations[0], mocker.Mock())

    mock_apprise.assert_not_called()
    mock_requests.post.assert_not_called()
    assert payload["error"], "the failure should still be reported back to the page"
    assert payload["transient"] is True


def test_daemon_check_still_notifies(mocker: MockerFixture) -> None:
    """The suppression must be specific to the web UI, not a global mute."""
    config = _reservation_config()
    mock_notify = mocker.patch("lib.notification_handler.NotificationHandler.send_notification")
    mocker.patch("lib.checkin_scheduler.make_request", side_effect=not_found())

    scheduler = ReservationMonitor(config.reservations[0], lock=None).checkin_scheduler
    scheduler.process_reservations(["TEST"])

    mock_notify.assert_called_once()


def test_apprise_is_reached_when_notifications_are_enabled(mocker: MockerFixture) -> None:
    """Guards the suppression flag: with it on, the daemon really does hit Apprise."""
    config = GlobalConfig()
    config.create_reservation_config(
        [
            {
                "confirmationNumber": "TEST",
                "firstName": "Berkant",
                "lastName": "Marika",
                "notifications": [{"url": "test://url"}],
            }
        ]
    )
    mock_apprise = mocker.patch("apprise.Apprise")
    mocker.patch("lib.checkin_scheduler.make_request", side_effect=not_found())

    scheduler = ReservationMonitor(config.reservations[0], lock=None).checkin_scheduler
    scheduler.process_reservations(["TEST"])

    mock_apprise.assert_called_once_with("test://url")
