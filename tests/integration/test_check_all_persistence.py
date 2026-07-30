"""
Tests that a failure inside a "check all" job is persisted where the main page actually looks.

_run_single and runner.run_check's other two failure paths all call results_store.save_result on
failure. _run_all's per-reservation except block didn't -- a raised exception only updated the
transient in-memory job dict, which is purged after JOB_TTL and isn't what index() reads. That
failure disappeared from the UI without a trace once the job aged out (or the process restarted).
"""

from unittest import mock

from pytest_mock import MockerFixture

from lib.config import GlobalConfig
from lib.webui.jobs import JobManager


def test_a_failed_reservation_in_check_all_is_saved_to_results_store(
    mocker: MockerFixture,
) -> None:
    config = GlobalConfig()
    config.create_reservation_config(
        [
            {"confirmationNumber": "GOOD", "firstName": "A", "lastName": "B"},
            {"confirmationNumber": "BAD", "firstName": "A", "lastName": "B"},
        ]
    )

    results_store = mock.Mock()
    manager = JobManager(results_store, mocker.Mock(), mocker.Mock())

    good_payload = {"checked_at": "now", "error": None, "transient": False, "flights": []}

    def fake_run_check(reservation_config: mock.Mock, _results_store: mock.Mock) -> dict:
        if reservation_config.confirmation_number == "BAD":
            raise RuntimeError("webdriver blew up")
        return good_payload

    mocker.patch("lib.webui.jobs.runner.run_check", side_effect=fake_run_check)

    job_id = manager._create_job(["GOOD", "BAD"])
    manager._run_all(job_id, config.reservations)

    # Every reservation's outcome should be saved, not just the ones that happened to succeed
    saved_confirmations = {call.args[0] for call in results_store.save_result.call_args_list}
    assert "BAD" in saved_confirmations

    bad_payload = next(
        call.args[1]
        for call in results_store.save_result.call_args_list
        if call.args[0] == "BAD"
    )
    assert bad_payload["error"] == "webdriver blew up"
    # Must match the shape build_check_payload produces elsewhere, not a hand-rolled dict missing
    # keys the UI expects (this was previously missing 'transient')
    assert "transient" in bad_payload
    assert bad_payload["transient"] is False

    job = manager._jobs[job_id]
    assert job["status"] == "done"
    assert job["results"]["BAD"]["error"] == "webdriver blew up"
    assert job["results"]["GOOD"] == good_payload
