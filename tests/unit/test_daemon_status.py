import os
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from lib import daemon_status


@pytest.fixture
def pid_file(tmp_path: Path) -> Path:
    return tmp_path / "daemon.pid"


class TestWritePidFile:
    def test_writes_the_current_pid(self, pid_file: Path) -> None:
        daemon_status.write_pid_file(pid_file)
        assert pid_file.read_text() == str(os.getpid())

    def test_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        # A directory in place of the file makes the write fail
        unwritable = tmp_path / "daemon.pid"
        unwritable.mkdir()

        daemon_status.write_pid_file(unwritable)


class TestRemovePidFile:
    def test_removes_a_file_owned_by_this_process(self, pid_file: Path) -> None:
        daemon_status.write_pid_file(pid_file)
        daemon_status.remove_pid_file(pid_file)

        assert not pid_file.exists()

    def test_leaves_a_file_owned_by_another_process(self, pid_file: Path) -> None:
        """A child process inheriting the atexit handler must not delete the daemon's PID file."""
        pid_file.write_text(str(os.getpid() + 1))

        daemon_status.remove_pid_file(pid_file)

        assert pid_file.exists()

    def test_missing_file_does_not_raise(self, pid_file: Path) -> None:
        daemon_status.remove_pid_file(pid_file)

    def test_unlink_failure_does_not_raise(self, pid_file: Path, mocker: MockerFixture) -> None:
        daemon_status.write_pid_file(pid_file)
        mocker.patch.object(Path, "unlink", side_effect=OSError("boom"))

        daemon_status.remove_pid_file(pid_file)


class TestGetRunningPid:
    def test_returns_none_when_the_file_is_missing(self, pid_file: Path) -> None:
        assert daemon_status.get_running_pid(pid_file) is None

    def test_returns_the_pid_of_a_live_process(self, pid_file: Path) -> None:
        daemon_status.write_pid_file(pid_file)
        assert daemon_status.get_running_pid(pid_file) == os.getpid()

    def test_returns_none_for_a_stale_pid(self, pid_file: Path, mocker: MockerFixture) -> None:
        pid_file.write_text("4242")
        mocker.patch("os.kill", side_effect=ProcessLookupError)

        assert daemon_status.get_running_pid(pid_file) is None

    def test_returns_none_for_an_unexpected_os_error(
        self, pid_file: Path, mocker: MockerFixture
    ) -> None:
        pid_file.write_text("4242")
        mocker.patch("os.kill", side_effect=OSError("boom"))

        assert daemon_status.get_running_pid(pid_file) is None

    def test_returns_the_pid_when_owned_by_another_user(
        self, pid_file: Path, mocker: MockerFixture
    ) -> None:
        pid_file.write_text("4242")
        mocker.patch("os.kill", side_effect=PermissionError)

        assert daemon_status.get_running_pid(pid_file) == 4242

    @pytest.mark.parametrize("contents", ["", "not-a-pid", "0", "-1"])
    def test_returns_none_for_unusable_contents(self, pid_file: Path, contents: str) -> None:
        pid_file.write_text(contents)

        assert daemon_status.get_running_pid(pid_file) is None

    def test_is_running_reflects_get_running_pid(self, pid_file: Path) -> None:
        assert daemon_status.is_running(pid_file) is False

        daemon_status.write_pid_file(pid_file)
        assert daemon_status.is_running(pid_file) is True
