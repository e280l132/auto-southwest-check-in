from pathlib import Path

import pytest

from lib.webui.results_store import ResultsStore


@pytest.fixture
def tmp_results_file(tmp_path: Path) -> Path:
    return tmp_path / "webui_last_results.json"


@pytest.fixture
def store(tmp_results_file: Path) -> ResultsStore:
    return ResultsStore(filepath=tmp_results_file)


class TestResultsStore:
    def test_get_result_returns_none_when_not_saved(self, store: ResultsStore) -> None:
        assert store.get_result("ABCDEF") is None

    def test_save_and_get_result_round_trips(self, store: ResultsStore) -> None:
        payload = {"checked_at": "now", "flights": [{"status": "lower_fare"}]}
        store.save_result("ABCDEF", payload)

        assert store.get_result("ABCDEF") == payload

    def test_save_result_overwrites_previous_value(self, store: ResultsStore) -> None:
        store.save_result("ABCDEF", {"checked_at": "first"})
        store.save_result("ABCDEF", {"checked_at": "second"})

        assert store.get_result("ABCDEF")["checked_at"] == "second"

    def test_get_all_returns_every_confirmation(self, store: ResultsStore) -> None:
        store.save_result("ABCDEF", {"checked_at": "a"})
        store.save_result("GHIJKL", {"checked_at": "b"})

        all_results = store.get_all()
        assert set(all_results) == {"ABCDEF", "GHIJKL"}

    def test_clear_all_removes_every_result(self, store: ResultsStore) -> None:
        store.save_result("ABCDEF", {"checked_at": "a"})
        store.save_result("GHIJKL", {"checked_at": "b"})

        store.clear_all()

        assert store.get_all() == {}

    def test_clear_all_is_a_no_op_when_already_empty(self, store: ResultsStore) -> None:
        store.clear_all()
        assert store.get_all() == {}

    def test_tolerates_corrupt_file(self, tmp_results_file: Path) -> None:
        tmp_results_file.write_text("not valid json{{{")
        store = ResultsStore(filepath=tmp_results_file)

        assert store.get_all() == {}
        # Should still be able to save after a corrupt read
        store.save_result("ABCDEF", {"checked_at": "now"})
        assert store.get_result("ABCDEF") == {"checked_at": "now"}
