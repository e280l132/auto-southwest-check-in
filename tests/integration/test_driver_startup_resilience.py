"""
Tests that a browser startup failure degrades to a handled DriverTimeoutError instead of crashing
the whole monitor process.

Found by a live run against real reservations: seleniumbase's undetected-chromedriver layer has a
known internal flakiness bug (AttributeError from remove_cdc_props_as_needed() hitting a None
list) that killed a monitor process outright -- uncaught, at driver.get(MOBILE_LOGIN_URL) inside
_get_driver(). It has nothing to do with Southwest; any browser-startup failure needed the same
treatment, since every caller of _get_driver() already handles DriverTimeoutError.
"""

from unittest import mock

import pytest
from pytest_mock import MockerFixture

from lib.config import GlobalConfig
from lib.reservation_monitor import ReservationMonitor
from lib.utils import DriverTimeoutError
from lib.webdriver import WebDriver


@pytest.fixture(autouse=True)
def _offline(mocker: MockerFixture) -> None:
    mocker.patch("lib.webdriver.IS_DOCKER", False)


def test_driver_construction_failure_becomes_a_driver_timeout_error(mocker: MockerFixture) -> None:
    wd = WebDriver(mocker.Mock())
    mocker.patch(
        "lib.webdriver.Driver",
        side_effect=AttributeError("'NoneType' object has no len()"),
    )

    with pytest.raises(DriverTimeoutError):
        wd._get_driver()


def test_login_page_load_failure_becomes_a_driver_timeout_error_and_quits_the_browser(
    mocker: MockerFixture,
) -> None:
    """
    This is the exact failure observed live: Driver() succeeds, but driver.get(...) --
    seleniumbase's uc_special_open_if_cf -> remove_cdc_props_as_needed() -- throws.
    """
    wd = WebDriver(mocker.Mock())
    fake_driver = mock.Mock()
    fake_driver.caps = {"browserVersion": "test"}
    fake_driver.get.side_effect = AttributeError("'NoneType' object has no len()")
    mocker.patch("lib.webdriver.Driver", return_value=fake_driver)
    mock_quit = mocker.patch.object(WebDriver, "_quit_driver")

    with pytest.raises(DriverTimeoutError):
        wd._get_driver()

    mock_quit.assert_called_once_with(fake_driver)


def test_a_transient_browser_startup_failure_does_not_kill_the_monitor_process(
    mocker: MockerFixture,
) -> None:
    """
    End-to-end through the actual code path that crashed live: ReservationMonitor._check() ->
    refresh_headers() -> set_headers() -> _get_driver(). Before this fix, this raised uncaught out
    of _check() and killed the whole process; DriverTimeoutError is the one exception _check()
    already knows how to handle (skip this cycle, try again next interval).
    """
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "TEST", "firstName": "A", "lastName": "B"}]
    )
    monitor = ReservationMonitor(config.reservations[0], lock=None, send_external=False)

    mocker.patch(
        "lib.webdriver.Driver",
        side_effect=AttributeError("'NoneType' object has no len()"),
    )
    mock_notify = mocker.patch(
        "lib.notification_handler.NotificationHandler.timeout_during_retrieval"
    )

    # Must not raise -- this is the assertion that matters
    should_exit = monitor._check()

    assert should_exit is False
    mock_notify.assert_called_once()
