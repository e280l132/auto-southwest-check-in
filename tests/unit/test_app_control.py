import pytest
from pytest_mock import MockerFixture

from lib import app_control


@pytest.fixture(autouse=True)
def reset_reload_event() -> None:
    """The reload event is module-level state; make sure it doesn't leak between tests."""
    app_control._reload_event.clear()
    yield
    app_control._reload_event.clear()


def test_reload_requested_is_false_by_default() -> None:
    assert app_control.reload_requested() is False


def test_request_reload_sets_the_event() -> None:
    app_control.request_reload()
    assert app_control.reload_requested() is True


def test_clear_reload_request_clears_the_event() -> None:
    app_control.request_reload()
    app_control.clear_reload_request()
    assert app_control.reload_requested() is False


def test_request_reload_signals_active_children(mocker: MockerFixture) -> None:
    child = mocker.Mock(pid=1234)
    mocker.patch("multiprocessing.active_children", return_value=[child])
    mock_kill = mocker.patch("os.kill")

    app_control.request_reload()

    mock_kill.assert_called_once_with(1234, mocker.ANY)


def test_request_reload_ignores_already_exited_children(mocker: MockerFixture) -> None:
    child = mocker.Mock(pid=1234)
    mocker.patch("multiprocessing.active_children", return_value=[child])
    mocker.patch("os.kill", side_effect=ProcessLookupError)

    # Should not raise
    app_control.request_reload()
    assert app_control.reload_requested() is True


def test_request_reload_can_be_called_repeatedly(mocker: MockerFixture) -> None:
    child = mocker.Mock(pid=1234)
    mocker.patch("multiprocessing.active_children", return_value=[child])
    mock_kill = mocker.patch("os.kill")

    app_control.request_reload()
    app_control.request_reload()

    assert app_control.reload_requested() is True
    assert mock_kill.call_count == 2


def test_stop_monitoring_processes_joins_and_terminates_stragglers(mocker: MockerFixture) -> None:
    stuck_child = mocker.Mock(pid=42)
    stuck_child.is_alive.return_value = True
    mocker.patch("multiprocessing.active_children", return_value=[stuck_child])

    app_control.stop_monitoring_processes(timeout=1)

    stuck_child.join.assert_any_call(1)
    stuck_child.terminate.assert_called_once()


def test_stop_monitoring_processes_does_not_terminate_children_that_exit(
    mocker: MockerFixture,
) -> None:
    exited_child = mocker.Mock()
    exited_child.is_alive.return_value = False
    mocker.patch("multiprocessing.active_children", return_value=[exited_child])

    app_control.stop_monitoring_processes(timeout=1)

    exited_child.terminate.assert_not_called()
