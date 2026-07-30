"""Tests various functionalities with the arguments that can be passed into the script"""

import json
import logging
from collections.abc import Iterator

import pytest
from pytest_mock import MockerFixture

import southwest
from lib import main


@pytest.fixture(autouse=True)
def logger(mocker: MockerFixture) -> Iterator[logging.Logger]:
    logger = logging.getLogger("lib")
    # Make sure no file system changes are done
    mocker.patch("pathlib.Path.mkdir")
    # Make sure logs aren't written to a file
    mock_file_handler = mocker.patch("logging.handlers.RotatingFileHandler")
    mock_file_handler.return_value.level = logging.DEBUG

    yield logger

    logger.handlers = []  # Clean up after each test


@pytest.fixture(autouse=True)
def mock_read_config(mocker: MockerFixture) -> None:
    # Don't ever read the actual config file. Will be mocked within the test
    # if a certain config needs to be used
    mocker.patch("pathlib.Path.read_text", side_effect=FileNotFoundError)


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_is_printed(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        southwest.init([flag])

    assert southwest.__version__ in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_is_printed(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        southwest.init([flag])

    output = capsys.readouterr().out
    assert southwest.__version__ in output
    assert southwest.__doc__ in output


def test_notifications_are_tested(mocker: MockerFixture) -> None:
    config = {
        "notifications": [{"url": "test_global_1"}, {"url": "test_global_2"}],
        "accounts": [
            {
                "username": "test_user",
                "password": "test_pass",
                "notifications": [{"url": "test_account_1"}],
            },
        ],
        "reservations": [
            {
                "confirmationNumber": "TEST",
                "firstName": "Mererid",
                "lastName": "Marian",
                "notifications": [{"url": "test_global_1"}, {"url": "test_reservation_1"}],
            },
        ],
    }

    mocker.patch("pathlib.Path.read_text", return_value=json.dumps(config))

    mock_apprise = mocker.patch("apprise.Apprise")

    with pytest.raises(SystemExit):
        main.main(["--test-notifications"], "test_version")

    assert mock_apprise.call_count == 4
    assert mock_apprise.return_value.notify.call_count == 4

    # Ensure all URLs are sent one notification
    expected_urls = ["test_global_1", "test_global_2", "test_account_1", "test_reservation_1"]
    for call in mock_apprise.call_args_list:
        assert call[0][0] in expected_urls
        expected_urls.remove(call[0][0])


@pytest.mark.parametrize("verbose_flag", ["-v", "--verbose"])
def test_account_from_command_line_with_verbose(
    mocker: MockerFixture, verbose_flag: str, logger: logging.Logger
) -> None:
    mock_process = mocker.patch("multiprocessing.Process").return_value
    # The monitoring process is still running on the first poll and gone on the second, which is
    # what lets the wait loop finish
    mocker.patch("multiprocessing.active_children", side_effect=[[mock_process], []])
    mock_web_ui = mocker.patch("lib.main.start_web_ui_background")

    # '--no-web' keeps the test from binding a real port, and means the script exits once
    # monitoring is done rather than staying up to serve the web UI
    args = ["test_user", "test_pass", verbose_flag, "--no-web"]
    # sys.argv is used instead of the args passed in to the log module (it also would have
    # southwest.py prepended to it in real use)
    mocker.patch("sys.argv", ["test_file", *args])

    main.main(args, "test_version")

    mock_process.start.assert_called_once()
    mock_process.join.assert_called_once()
    mock_web_ui.assert_not_called()

    assert logger.handlers[1].level == logging.DEBUG


def test_reservation_from_command_line_without_verbose(
    mocker: MockerFixture, logger: logging.Logger
) -> None:
    mock_process = mocker.patch("multiprocessing.Process").return_value
    mocker.patch("multiprocessing.active_children", side_effect=[[mock_process], []])
    mocker.patch("lib.main.start_web_ui_background")

    args = ["TEST", "Charli", "Silvester", "--no-web"]
    # sys.argv is used instead of the args passed in to the log module (it also would have
    # southwest.py prepended to it in real use)
    mocker.patch("sys.argv", ["test_file", *args])

    main.main(args, "test_version")

    mock_process.start.assert_called_once()
    mock_process.join.assert_called_once()

    assert logger.handlers[1].level == logging.INFO


def test_accounts_and_reservations_from_config(mocker: MockerFixture) -> None:
    config = {
        "accounts": [{"username": "test_user", "password": "test_pass"}],
        "reservations": [
            {"confirmationNumber": "TEST", "firstName": "Nana", "lastName": "Linus"},
        ],
    }
    mocker.patch("pathlib.Path.read_text", return_value=json.dumps(config))

    mock_process = mocker.patch("multiprocessing.Process").return_value
    mocker.patch(
        "multiprocessing.active_children", side_effect=[[mock_process, mock_process], []]
    )
    mocker.patch("lib.main.start_web_ui_background")

    main.main(["--no-web"], "test_version")

    assert mock_process.start.call_count == 2
    assert mock_process.join.call_count == 2


def test_error_on_invalid_arguments(mocker: MockerFixture) -> None:
    # The web UI starts before the arguments are validated, so it has to be stubbed out or the
    # test binds a real port
    mocker.patch("lib.main.start_web_ui_background")

    with pytest.raises(SystemExit):
        main.main(["most", "definitely", "invalid", "arguments"], "test_version")


def test_waiting_returns_when_idle_and_web_ui_is_disabled(mocker: MockerFixture) -> None:
    """Without a web UI to serve, an idle script should exit instead of polling forever."""
    mocker.patch("multiprocessing.active_children", return_value=[])
    mock_sleep = mocker.patch("lib.main.time.sleep")

    main._wait_for_children_or_reload(keep_alive_when_idle=False)

    mock_sleep.assert_not_called()


def test_waiting_stays_alive_when_idle_to_serve_the_web_ui(mocker: MockerFixture) -> None:
    """With the web UI up there is still something to serve, so the script keeps waiting."""
    mocker.patch("multiprocessing.active_children", return_value=[])
    # Break out of the otherwise-infinite wait on the second poll
    mocker.patch("lib.main.app_control.reload_requested", side_effect=[False, False, True])
    mock_sleep = mocker.patch("lib.main.time.sleep")

    main._wait_for_children_or_reload(keep_alive_when_idle=True)

    mock_sleep.assert_called()
