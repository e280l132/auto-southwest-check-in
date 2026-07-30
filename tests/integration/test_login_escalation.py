"""
Tests that a persistently broken account login/header-refresh eventually gets loud.

Before this fix, `timeout_during_retrieval` and `too_many_requests_during_login` always notified at
NOTICE, which is below the default filter level. A webdriver that fails every single cycle forever
(not just an isolated blip) would never surface to the user — only the reservation-lookup path had
this "escalate after repeats" behavior. This applies the same policy to account login and to a
reservation monitor's header-refresh timeout.
"""

from unittest import mock

import pytest
from pytest_mock import MockerFixture

from lib.checkin_scheduler import TRANSIENT_FAILURE_ALERT_THRESHOLD
from lib.config import GlobalConfig
from lib.reservation_monitor import AccountMonitor, ReservationMonitor
from lib.utils import DriverTimeoutError, LoginError, NotificationLevel


@pytest.fixture(autouse=True)
def _fast(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")


def test_account_login_timeout_stays_quiet_until_it_keeps_happening(mocker: MockerFixture) -> None:
    config = GlobalConfig()
    config.create_account_config([{"username": "test_user", "password": "test_pass"}])
    monitor = AccountMonitor(config.accounts[0], mock.Mock())

    mocker.patch("lib.reservation_monitor.WebDriver").return_value.get_reservations.side_effect = (
        DriverTimeoutError("timed out")
    )
    mock_notify = mocker.patch(
        "lib.notification_handler.NotificationHandler.timeout_during_retrieval"
    )

    levels = []
    for _ in range(TRANSIENT_FAILURE_ALERT_THRESHOLD):
        monitor._get_reservations(max_retries=0)
        levels.append(mock_notify.call_args[0][1])

    quiet_cycles = TRANSIENT_FAILURE_ALERT_THRESHOLD - 1
    assert levels[:-1] == [NotificationLevel.NOTICE] * quiet_cycles
    assert levels[-1] == NotificationLevel.ERROR


def test_account_login_success_resets_the_escalation(mocker: MockerFixture) -> None:
    config = GlobalConfig()
    config.create_account_config([{"username": "test_user", "password": "test_pass"}])
    monitor = AccountMonitor(config.accounts[0], mock.Mock())

    mock_webdriver = mocker.patch("lib.reservation_monitor.WebDriver").return_value
    mock_notify = mocker.patch(
        "lib.notification_handler.NotificationHandler.timeout_during_retrieval"
    )

    mock_webdriver.get_reservations.side_effect = DriverTimeoutError("timed out")
    monitor._get_reservations(max_retries=0)
    monitor._get_reservations(max_retries=0)
    assert monitor.login_retrieval_failures == 2

    mock_webdriver.get_reservations.side_effect = None
    mock_webdriver.get_reservations.return_value = []
    monitor._get_reservations(max_retries=0)
    assert monitor.login_retrieval_failures == 0

    mock_webdriver.get_reservations.side_effect = DriverTimeoutError("timed out")
    monitor._get_reservations(max_retries=0)
    assert monitor.login_retrieval_failures == 1
    assert mock_notify.call_args[0][1] == NotificationLevel.NOTICE


def test_too_many_requests_shares_the_same_escalation_as_timeouts(mocker: MockerFixture) -> None:
    """Both failure modes mean the same thing -- the account currently can't be checked."""
    config = GlobalConfig()
    config.create_account_config([{"username": "test_user", "password": "test_pass"}])
    monitor = AccountMonitor(config.accounts[0], mock.Mock())

    mock_webdriver = mocker.patch("lib.reservation_monitor.WebDriver").return_value
    mock_notify = mocker.patch(
        "lib.notification_handler.NotificationHandler.too_many_requests_during_login"
    )
    mock_webdriver.get_reservations.side_effect = LoginError("throttled", status_code=429)

    for _ in range(TRANSIENT_FAILURE_ALERT_THRESHOLD - 1):
        monitor._get_reservations(max_retries=0)
        assert mock_notify.call_args[0][0] == NotificationLevel.NOTICE

    monitor._get_reservations(max_retries=0)

    assert monitor.login_retrieval_failures == TRANSIENT_FAILURE_ALERT_THRESHOLD
    assert mock_notify.call_args[0][0] == NotificationLevel.ERROR


def test_reservation_header_refresh_timeout_also_escalates(mocker: MockerFixture) -> None:
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "TEST", "firstName": "Berkant", "lastName": "Marika"}]
    )
    monitor = ReservationMonitor(config.reservations[0], lock=None)

    mocker.patch.object(
        monitor.checkin_scheduler, "refresh_headers", side_effect=DriverTimeoutError("timed out")
    )
    mock_notify = mocker.patch(
        "lib.notification_handler.NotificationHandler.timeout_during_retrieval"
    )

    levels = []
    for _ in range(TRANSIENT_FAILURE_ALERT_THRESHOLD):
        monitor._check()
        levels.append(mock_notify.call_args[0][1])

    quiet_cycles = TRANSIENT_FAILURE_ALERT_THRESHOLD - 1
    assert levels[:-1] == [NotificationLevel.NOTICE] * quiet_cycles
    assert levels[-1] == NotificationLevel.ERROR
