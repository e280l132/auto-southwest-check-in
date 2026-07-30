"""
Tests that finished web UI jobs are eventually dropped.

The job table is in memory and was never pruned, so every fare check ever triggered stayed there
for the lifetime of the web server.
"""

from datetime import datetime, timedelta, timezone

from pytest_mock import MockerFixture

from lib.webui.jobs import JOB_TTL, JobManager


def _manager(mocker: MockerFixture) -> JobManager:
    return JobManager(mocker.Mock(), mocker.Mock(), mocker.Mock())


def _finish(manager: JobManager, job_id: str, *, age: timedelta) -> None:
    finished_at = datetime.now(timezone.utc) - age
    manager._update_job(job_id, status="done", finished_at=finished_at.isoformat())


def test_finished_jobs_are_dropped_once_stale(mocker: MockerFixture) -> None:
    manager = _manager(mocker)
    job_id = manager._create_job(["TEST"])
    _finish(manager, job_id, age=JOB_TTL + timedelta(minutes=1))

    # Purging happens on access rather than on a timer
    assert manager.get_job(job_id) is None


def test_recently_finished_jobs_are_still_readable(mocker: MockerFixture) -> None:
    manager = _manager(mocker)
    job_id = manager._create_job(["TEST"])
    _finish(manager, job_id, age=timedelta(seconds=5))

    assert manager.get_job(job_id) is not None


def test_unfinished_jobs_are_never_dropped(mocker: MockerFixture) -> None:
    """A slow check must not be purged out from under the page polling it."""
    manager = _manager(mocker)
    job_id = manager._create_job(["TEST"])
    manager._update_job(job_id, status="running")

    # Creating more jobs triggers a purge; the running one has to survive it
    for _ in range(5):
        stale_id = manager._create_job(["OTHER"])
        _finish(manager, stale_id, age=JOB_TTL + timedelta(hours=1))

    manager.get_job(job_id)

    assert manager.get_job(job_id) is not None
    assert len(manager._jobs) == 1, "the stale jobs should have been purged"
