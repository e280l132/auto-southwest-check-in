import threading
import time
from collections.abc import Callable
from typing import Any

from pytest_mock import MockerFixture

from lib.webui.jobs import JobManager

JSON = dict[str, Any]


def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


class FakeReservationConfig:
    def __init__(self, confirmation_number: str) -> None:
        self.confirmation_number = confirmation_number


class TestJobManagerSingleCheck:
    def test_start_single_check_reports_done_with_result(self, mocker: MockerFixture) -> None:
        mocker.patch(
            "lib.webui.jobs.runner.run_check", return_value={"checked_at": "now", "flights": []}
        )
        manager = JobManager(results_store=mocker.MagicMock())

        job_id = manager.start_single_check(FakeReservationConfig("ABCDEF"))
        _wait_for(lambda: manager.get_job(job_id)["status"] == "done")

        job = manager.get_job(job_id)
        assert job["results"]["ABCDEF"] == {"checked_at": "now", "flights": []}
        assert job["error"] is None

    def test_start_single_check_reports_error_on_exception(self, mocker: MockerFixture) -> None:
        mocker.patch("lib.webui.jobs.runner.run_check", side_effect=RuntimeError("boom"))
        manager = JobManager(results_store=mocker.MagicMock())

        job_id = manager.start_single_check(FakeReservationConfig("ABCDEF"))
        _wait_for(lambda: manager.get_job(job_id)["status"] == "error")

        assert "boom" in manager.get_job(job_id)["error"]

    def test_get_job_returns_none_for_unknown_id(self, mocker: MockerFixture) -> None:
        manager = JobManager(results_store=mocker.MagicMock())
        assert manager.get_job("does-not-exist") is None


class TestJobManagerCheckAll:
    def test_start_check_all_runs_every_reservation(self, mocker: MockerFixture) -> None:
        def fake_run_check(config: Any, _store: Any) -> JSON:
            return {"checked_at": "now", "flights": [], "conf": config.confirmation_number}

        mocker.patch("lib.webui.jobs.runner.run_check", side_effect=fake_run_check)
        manager = JobManager(results_store=mocker.MagicMock())
        configs = [FakeReservationConfig("ABCDEF"), FakeReservationConfig("GHIJKL")]

        job_id = manager.start_check_all(configs)
        _wait_for(lambda: manager.get_job(job_id)["status"] == "done")

        job = manager.get_job(job_id)
        assert set(job["results"]) == {"ABCDEF", "GHIJKL"}

    def test_start_check_all_continues_after_one_reservation_errors(
        self, mocker: MockerFixture
    ) -> None:
        def fake_run_check(config: Any, _store: Any) -> JSON:
            if config.confirmation_number == "ABCDEF":
                raise RuntimeError("boom")
            return {"checked_at": "now", "flights": []}

        mocker.patch("lib.webui.jobs.runner.run_check", side_effect=fake_run_check)
        manager = JobManager(results_store=mocker.MagicMock())
        configs = [FakeReservationConfig("ABCDEF"), FakeReservationConfig("GHIJKL")]

        job_id = manager.start_check_all(configs)
        _wait_for(lambda: manager.get_job(job_id)["status"] == "done")

        job = manager.get_job(job_id)
        assert job["error"] is not None
        assert job["results"]["ABCDEF"]["error"] == "boom"
        assert job["results"]["GHIJKL"]["flights"] == []


class TestJobManagerSerialization:
    def test_checks_do_not_run_concurrently(self, mocker: MockerFixture) -> None:
        concurrent_count = 0
        max_concurrent = 0
        lock = threading.Lock()

        def fake_run_check(_config: Any, _store: Any) -> JSON:
            nonlocal concurrent_count, max_concurrent
            with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            time.sleep(0.05)
            with lock:
                concurrent_count -= 1
            return {"checked_at": "now", "flights": []}

        mocker.patch("lib.webui.jobs.runner.run_check", side_effect=fake_run_check)
        manager = JobManager(results_store=mocker.MagicMock())

        job_id_1 = manager.start_single_check(FakeReservationConfig("ABCDEF"))
        job_id_2 = manager.start_single_check(FakeReservationConfig("GHIJKL"))

        _wait_for(lambda: manager.get_job(job_id_1)["status"] == "done")
        _wait_for(lambda: manager.get_job(job_id_2)["status"] == "done")

        assert max_concurrent == 1
