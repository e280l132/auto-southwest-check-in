import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from lib.ignore_manager import IgnoreManager

FUTURE_DATE = (date.today() + timedelta(days=30)).isoformat()
FUTURE_DATE_2 = (date.today() + timedelta(days=31)).isoformat()


@pytest.fixture
def tmp_ignore_file(tmp_path: Path) -> Path:
    return tmp_path / "ignored_flights.json"


@pytest.fixture
def manager(tmp_ignore_file: Path) -> IgnoreManager:
    return IgnoreManager(filepath=tmp_ignore_file)


class TestIgnoreManager:
    def test_ignore_flight_is_reflected_by_is_ignored(self, manager: IgnoreManager) -> None:
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")
        assert manager.is_ignored("ABCDEF", FUTURE_DATE, "100")

    def test_is_ignored_returns_false_when_nothing_matches(self, manager: IgnoreManager) -> None:
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")
        assert not manager.is_ignored("ABCDEF", FUTURE_DATE, "200")
        assert not manager.is_ignored("XXXXXX", FUTURE_DATE, "100")
        assert not manager.is_ignored("ABCDEF", FUTURE_DATE_2, "100")

    def test_ignore_flight_does_not_duplicate(self, manager: IgnoreManager) -> None:
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")
        data = json.loads(manager._filepath.read_text())
        assert len(data["specific"]) == 1

    def test_ignore_all_day_makes_is_day_ignored_true(self, manager: IgnoreManager) -> None:
        manager.ignore_all_day("ABCDEF", FUTURE_DATE)
        assert manager.is_day_ignored("ABCDEF", FUTURE_DATE)

    def test_ignore_all_day_makes_is_ignored_true_for_any_flight(
        self, manager: IgnoreManager
    ) -> None:
        manager.ignore_all_day("ABCDEF", FUTURE_DATE)
        assert manager.is_ignored("ABCDEF", FUTURE_DATE, "100")
        assert manager.is_ignored("ABCDEF", FUTURE_DATE, "999")

    def test_is_day_ignored_returns_false_when_not_set(self, manager: IgnoreManager) -> None:
        assert not manager.is_day_ignored("ABCDEF", FUTURE_DATE)

    def test_cleanup_removes_past_entries(self, manager: IgnoreManager) -> None:
        past_date = (date.today() - timedelta(days=1)).isoformat()
        future_date = (date.today() + timedelta(days=10)).isoformat()

        manager.ignore_flight("ABCDEF", past_date, "100")
        manager.ignore_flight("ABCDEF", future_date, "200")
        manager.ignore_all_day("ABCDEF", past_date)
        manager.ignore_all_day("ABCDEF", future_date)

        data = json.loads(manager._filepath.read_text())
        assert all(e["date"] >= date.today().isoformat() for e in data["specific"])
        assert all(e["date"] >= date.today().isoformat() for e in data["all_day"])

    def test_cleanup_keeps_today_and_future_entries(self, manager: IgnoreManager) -> None:
        today = date.today().isoformat()
        future = (date.today() + timedelta(days=5)).isoformat()

        manager.ignore_flight("ABCDEF", today, "100")
        manager.ignore_flight("ABCDEF", future, "200")

        assert manager.is_ignored("ABCDEF", today, "100")
        assert manager.is_ignored("ABCDEF", future, "200")

    def test_ignore_all_day_does_not_duplicate(self, manager: IgnoreManager) -> None:
        manager.ignore_all_day("ABCDEF", FUTURE_DATE)
        manager.ignore_all_day("ABCDEF", FUTURE_DATE)
        data = json.loads(manager._filepath.read_text())
        assert len(data["all_day"]) == 1

    def test_load_returns_empty_when_json_is_not_a_dict(self, tmp_ignore_file: Path) -> None:
        tmp_ignore_file.write_text("[]")
        manager = IgnoreManager(filepath=tmp_ignore_file)
        # Should not raise; a non-dict JSON payload is treated as empty data
        assert not manager.is_ignored("ABCDEF", "2025-12-01", "100")

    def test_save_removes_directory_at_filepath(self, tmp_ignore_file: Path) -> None:
        tmp_ignore_file.mkdir()
        manager = IgnoreManager(filepath=tmp_ignore_file)
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")
        assert tmp_ignore_file.is_file()
        assert manager.is_ignored("ABCDEF", FUTURE_DATE, "100")

    def test_save_logs_error_on_oserror(
        self, manager: IgnoreManager, mocker: MockerFixture
    ) -> None:
        mocker.patch.object(Path, "write_text", side_effect=OSError("disk full"))
        # Should not raise even though the write fails
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")

    def test_load_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        manager = IgnoreManager(filepath=tmp_path / "nonexistent.json")
        assert not manager.is_ignored("ABCDEF", "2025-12-01", "100")

    def test_load_recovers_from_corrupt_file(self, tmp_ignore_file: Path) -> None:
        tmp_ignore_file.write_text("not valid json{{{")
        manager = IgnoreManager(filepath=tmp_ignore_file)
        # Should not raise; just treat as empty
        assert not manager.is_ignored("ABCDEF", "2025-12-01", "100")

    def test_persists_across_instances(self, tmp_ignore_file: Path) -> None:
        """Data written by one instance should be visible to a new instance."""
        m1 = IgnoreManager(filepath=tmp_ignore_file)
        m1.ignore_flight("ABCDEF", FUTURE_DATE, "100")

        m2 = IgnoreManager(filepath=tmp_ignore_file)
        assert m2.is_ignored("ABCDEF", FUTURE_DATE, "100")

    def test_cleanup_confirmations_removes_inactive(self, manager: IgnoreManager) -> None:
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")
        manager.ignore_all_day("ABCDEF", FUTURE_DATE)
        manager.ignore_flight("ZZZZZZ", FUTURE_DATE, "200")
        manager.ignore_all_day("ZZZZZZ", FUTURE_DATE)

        manager.cleanup_confirmations({"ZZZZZZ"})

        assert not manager.is_ignored("ABCDEF", FUTURE_DATE, "100")
        assert not manager.is_day_ignored("ABCDEF", FUTURE_DATE)
        assert manager.is_ignored("ZZZZZZ", FUTURE_DATE, "200")
        assert manager.is_day_ignored("ZZZZZZ", FUTURE_DATE)

    def test_cleanup_confirmations_no_op_when_all_active(self, manager: IgnoreManager) -> None:
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")
        manager.cleanup_confirmations({"ABCDEF", "ZZZZZZ"})
        assert manager.is_ignored("ABCDEF", FUTURE_DATE, "100")

    def test_cleanup_confirmations_no_write_when_nothing_removed(
        self, manager: IgnoreManager, tmp_ignore_file: Path
    ) -> None:
        manager.ignore_flight("ABCDEF", FUTURE_DATE, "100")
        mtime_before = tmp_ignore_file.stat().st_mtime
        manager.cleanup_confirmations({"ABCDEF"})
        assert tmp_ignore_file.stat().st_mtime == mtime_before
