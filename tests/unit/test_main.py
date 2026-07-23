import logging

import pytest
from pytest_mock import MockerFixture
from requests_mock.mocker import Mocker as RequestMocker

from lib import main
from lib.config import AccountConfig, GlobalConfig, ReservationConfig
from lib.notification_handler import NotificationHandler
from lib.reservation_monitor import AccountMonitor, ReservationMonitor
from lib.utils import CheckFaresOption


@pytest.fixture(autouse=True)
def mock_config(mocker: MockerFixture) -> None:
    """The config file shouldn't actually be read for these tests"""
    mocker.patch("lib.config.GlobalConfig._read_config")


@pytest.fixture(autouse=True)
def mock_web_ui(mocker: MockerFixture) -> MockerFixture:
    """
    The web UI now starts automatically, so avoid actually building a Flask app/binding a port
    for tests that aren't specifically about the web UI startup itself.
    """
    return mocker.patch("lib.main.start_web_ui_background")


@pytest.fixture
def mock_wait(mocker: MockerFixture) -> MockerFixture:
    """
    set_up_check_in now blocks (waiting for a reload request) after setup, which isn't relevant
    to tests that only care about setup behavior. Not autouse, since the tests exercising
    _wait_for_children_or_reload itself need the real implementation.
    """
    return mocker.patch("lib.main._wait_for_children_or_reload")


def test_get_timezone_fetches_timezone_from_request(requests_mock: RequestMocker) -> None:
    requests_mock.get(main.IP_TIMEZONE_URL, text="Asia/Tokyo")
    assert main.get_timezone() == "Asia/Tokyo"


def test_get_timezone_returns_utc_when_request_fails(requests_mock: RequestMocker) -> None:
    requests_mock.get(main.IP_TIMEZONE_URL, status_code=500)
    assert main.get_timezone() == "UTC"


def test_test_notifications_sends_to_every_url_in_config(mocker: MockerFixture) -> None:
    # Accessing protected methods is just used to not need to provide a full config object
    # to parse

    config = GlobalConfig()
    config.accounts = [AccountConfig()]
    config.reservations = [ReservationConfig()]
    config._create_notification_config([{"url": "url1"}])

    config.accounts[0]._create_notification_config([{"url": "url1"}])
    config.accounts[0]._create_notification_config([{"url": "url2"}])

    config.reservations[0]._create_notification_config([{"url": "url3"}])
    config.reservations[0]._create_notification_config([{"url": "url1"}])

    mock_send_notification = mocker.patch.object(NotificationHandler, "send_notification")

    main.test_notifications(config)

    # Make sure the configs were merged correctly so all of the URLs are only sent one test
    # notification each
    assert len(config.notifications) == 3

    mock_send_notification.assert_called_once()


@pytest.mark.parametrize(("expected", "count"), [("tests", 0), ("test", 1), ("tests", 2)])
def test_pluralize_pluralizes_a_word_if_needed(expected: str, count: int) -> None:
    assert main.pluralize("test", count) == expected


def test_set_up_accounts_starts_all_accounts(mocker: MockerFixture) -> None:
    config = GlobalConfig()
    config.accounts = [AccountConfig(), AccountConfig()]

    mock_account_start = mocker.patch.object(AccountMonitor, "start")
    main.set_up_accounts(config, None)
    assert mock_account_start.call_count == len(config.accounts)


def test_set_up_reservations_starts_all_reservations(mocker: MockerFixture) -> None:
    config = GlobalConfig()
    config.reservations = [ReservationConfig(), ReservationConfig()]

    mock_reservation_start = mocker.patch.object(ReservationMonitor, "start")
    main.set_up_reservations(config, None)
    assert mock_reservation_start.call_count == len(config.reservations)


def test_set_up_check_in_sends_test_notifications_when_flag_passed(mocker: MockerFixture) -> None:
    mock_test_notifications = mocker.patch("lib.main.test_notifications")
    with pytest.raises(SystemExit):
        main.set_up_check_in(["--test-notifications"])
    mock_test_notifications.assert_called_once()


@pytest.mark.parametrize(
    ("arguments", "accounts_len", "reservations_len"),
    [
        ([], 0, 0),
        (["username", "password"], 1, 0),
        (["test", "John", "Doe"], 0, 1),
    ],
)
def test_set_up_check_in_sets_up_account_and_reservation_with_arguments(
    mocker: MockerFixture,
    mock_wait: MockerFixture,
    arguments: list[str],
    accounts_len: int,
    reservations_len: int,
) -> None:
    mock_set_up_accounts = mocker.patch("lib.main.set_up_accounts")
    mock_set_up_reservations = mocker.patch("lib.main.set_up_reservations")

    main.set_up_check_in(arguments)

    assert len(mock_set_up_accounts.call_args[0][0].accounts) == accounts_len
    assert len(mock_set_up_reservations.call_args[0][0].reservations) == reservations_len
    mock_wait.assert_called_once()


@pytest.mark.usefixtures("mock_wait")
def test_set_up_check_in_starts_ignore_server_when_same_day_smart_configured(
    mocker: MockerFixture,
) -> None:
    def fake_initialize(self: GlobalConfig) -> None:
        account = AccountConfig()
        account.check_fares = CheckFaresOption.SAME_DAY_SMART
        self.accounts = [account]
        self.reservations = []

    mocker.patch.object(GlobalConfig, "initialize", fake_initialize)
    mocker.patch("lib.main.set_up_accounts")
    mocker.patch("lib.main.set_up_reservations")
    mock_start_ignore_server = mocker.patch("lib.main.start_ignore_server")

    main.set_up_check_in([])

    mock_start_ignore_server.assert_called_once()


@pytest.mark.usefixtures("mock_wait")
def test_set_up_check_in_does_not_start_ignore_server_without_same_day_smart(
    mocker: MockerFixture,
) -> None:
    def fake_initialize(self: GlobalConfig) -> None:
        self.accounts = [AccountConfig()]
        self.reservations = []

    mocker.patch.object(GlobalConfig, "initialize", fake_initialize)
    mocker.patch("lib.main.set_up_accounts")
    mocker.patch("lib.main.set_up_reservations")
    mock_start_ignore_server = mocker.patch("lib.main.start_ignore_server")

    main.set_up_check_in([])

    mock_start_ignore_server.assert_not_called()


def test_set_up_check_in_sends_error_message_when_arguments_are_invalid(
    caplog: pytest.CaptureFixture[str],
) -> None:
    arguments = ["1", "2", "3", "4"]

    with pytest.raises(SystemExit):
        main.set_up_check_in(arguments)
    output = caplog.record_tuples[-1]

    assert output[1] == logging.ERROR
    assert "Invalid arguments" in output[2]
    assert "--help" in output[2]


def test_main_sets_up_the_script(mocker: MockerFixture) -> None:
    mock_init_main_logging = mocker.patch("lib.log.init_main_logging")
    mock_set_up_check_in = mocker.patch("lib.main.set_up_check_in")
    mock_get_timezone = mocker.patch("lib.main.get_timezone")

    arguments = ["test", "arguments", "--verbose", "-v"]
    main.main(arguments, "test_version")
    mock_init_main_logging.assert_called_once()

    # Ensure the '--verbose' and '-v' flags are removed
    mock_set_up_check_in.assert_called_once_with(arguments[:2])

    mock_get_timezone.assert_not_called()


def test_main_fetches_timezone_if_docker(mocker: MockerFixture) -> None:
    mocker.patch("lib.log.init_main_logging")
    mocker.patch("lib.main.set_up_check_in")

    mock_get_timezone = mocker.patch("lib.main.get_timezone", return_value="UTC")
    mocker.patch("lib.main.IS_DOCKER", return_value=True)

    main.main([], "test_version")
    mock_get_timezone.assert_called_once()


def test_main_exits_on_keyboard_interrupt(mocker: MockerFixture) -> None:
    mocker.patch("lib.log.init_main_logging")
    mocker.patch.object(main, "set_up_check_in", side_effect=KeyboardInterrupt)

    with pytest.raises(SystemExit):
        main.main([], "test_version")


# ---------------------------------------------------------------------------
# Web UI flags and startup
# ---------------------------------------------------------------------------


def test_extract_web_flags_returns_defaults_when_absent() -> None:
    remaining, web, no_web, port = main._extract_web_flags(["foo", "bar"])
    assert remaining == ["foo", "bar"]
    assert web is False
    assert no_web is False
    assert port is None


def test_extract_web_flags_extracts_web_and_port() -> None:
    remaining, web, no_web, port = main._extract_web_flags(["foo", "--web", "--web-port", "1234"])
    assert remaining == ["foo"]
    assert web is True
    assert no_web is False
    assert port == 1234


def test_extract_web_flags_extracts_no_web() -> None:
    remaining, web, no_web, _port = main._extract_web_flags(["foo", "--no-web"])
    assert remaining == ["foo"]
    assert web is False
    assert no_web is True


def test_extract_web_flags_exits_when_web_port_missing_value() -> None:
    with pytest.raises(SystemExit):
        main._extract_web_flags(["--web-port"])


def test_extract_web_flags_exits_when_web_and_no_web_both_present() -> None:
    with pytest.raises(SystemExit):
        main._extract_web_flags(["--web", "--no-web"])


def test_set_up_check_in_starts_web_ui_by_default(
    mock_web_ui: MockerFixture, mock_wait: MockerFixture
) -> None:
    main.set_up_check_in([])
    mock_web_ui.assert_called_once()
    mock_wait.assert_called_once()


@pytest.mark.usefixtures("mock_wait")
def test_set_up_check_in_skips_web_ui_with_no_web_flag(
    mocker: MockerFixture, mock_web_ui: MockerFixture
) -> None:
    mocker.patch("lib.main.set_up_accounts")
    mocker.patch("lib.main.set_up_reservations")

    main.set_up_check_in(["--no-web"])

    mock_web_ui.assert_not_called()


def test_set_up_check_in_web_only_skips_monitoring(
    mocker: MockerFixture, mock_web_ui: MockerFixture, mock_wait: MockerFixture
) -> None:
    mock_set_up_accounts = mocker.patch("lib.main.set_up_accounts")
    mock_set_up_reservations = mocker.patch("lib.main.set_up_reservations")

    main.set_up_check_in(["--web"])

    mock_web_ui.assert_called_once()
    mock_wait.assert_called_once()
    mock_set_up_accounts.assert_not_called()
    mock_set_up_reservations.assert_not_called()


@pytest.mark.usefixtures("mock_wait")
def test_set_up_check_in_reloads_config_when_requested(mocker: MockerFixture) -> None:
    mock_set_up_accounts = mocker.patch("lib.main.set_up_accounts")
    mock_set_up_reservations = mocker.patch("lib.main.set_up_reservations")
    # Reload requested once, then not requested again on the next check (so the loop exits)
    mocker.patch("lib.main.app_control.reload_requested", side_effect=[True, False])
    mock_clear = mocker.patch("lib.main.app_control.clear_reload_request")
    mock_stop = mocker.patch("lib.main.app_control.stop_monitoring_processes")

    main.set_up_check_in([])

    mock_clear.assert_called_once()
    mock_stop.assert_called_once()
    # Once for the initial setup, once more for the reload
    assert mock_set_up_accounts.call_count == 2
    assert mock_set_up_reservations.call_count == 2


@pytest.mark.usefixtures("mock_wait")
def test_set_up_check_in_does_not_reload_when_not_requested(mocker: MockerFixture) -> None:
    mocker.patch("lib.main.set_up_accounts")
    mocker.patch("lib.main.set_up_reservations")
    mocker.patch("lib.main.app_control.reload_requested", return_value=False)
    mock_stop = mocker.patch("lib.main.app_control.stop_monitoring_processes")

    main.set_up_check_in([])

    mock_stop.assert_not_called()


def test_set_up_check_in_web_only_clears_reload_request_without_reloading(
    mocker: MockerFixture, mock_web_ui: MockerFixture, mock_wait: MockerFixture
) -> None:
    mocker.patch("lib.main.app_control.reload_requested", side_effect=[True, False])
    mock_clear = mocker.patch("lib.main.app_control.clear_reload_request")

    main.set_up_check_in(["--web"])

    mock_web_ui.assert_called_once()
    mock_clear.assert_called_once()
    assert mock_wait.call_count == 2


# ---------------------------------------------------------------------------
# _wait_for_children_or_reload
# ---------------------------------------------------------------------------


def test_wait_for_children_or_reload_joins_children_until_reload(mocker: MockerFixture) -> None:
    mock_child = mocker.Mock()
    mocker.patch("multiprocessing.active_children", return_value=[mock_child])
    # Reload requested only after the first children batch is joined
    mocker.patch("lib.main.app_control.reload_requested", side_effect=[False, True])

    main._wait_for_children_or_reload()

    mock_child.join.assert_called_once_with(timeout=1)


def test_wait_for_children_or_reload_sleeps_when_no_children(mocker: MockerFixture) -> None:
    mocker.patch("multiprocessing.active_children", return_value=[])
    mocker.patch("lib.main.app_control.reload_requested", side_effect=[False, False, True])
    mock_sleep = mocker.patch("lib.main.time.sleep")

    main._wait_for_children_or_reload()

    assert mock_sleep.call_count == 2


# ---------------------------------------------------------------------------
# _apply_cli_overrides
# ---------------------------------------------------------------------------


def test_apply_cli_overrides_adds_account_for_two_arguments() -> None:
    config = GlobalConfig()
    main._apply_cli_overrides(config, ["username", "password"])
    assert len(config.accounts) == 1


def test_apply_cli_overrides_adds_reservation_for_three_arguments() -> None:
    config = GlobalConfig()
    main._apply_cli_overrides(config, ["conf", "First", "Last"])
    assert len(config.reservations) == 1


def test_apply_cli_overrides_does_nothing_for_no_arguments() -> None:
    config = GlobalConfig()
    main._apply_cli_overrides(config, [])
    assert config.accounts == []
    assert config.reservations == []


def test_apply_cli_overrides_exits_for_too_many_arguments() -> None:
    config = GlobalConfig()
    with pytest.raises(SystemExit):
        main._apply_cli_overrides(config, ["1", "2", "3", "4"])
