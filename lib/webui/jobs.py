"""
Background execution of fare checks triggered from the web UI.

Fare checks drive a real browser session (seleniumbase) and take 30-90 seconds, so they run on
a worker thread while the UI polls for status. A single lock ensures only one webdriver session
runs at a time, since ReservationMonitor/CheckInScheduler were designed for one browser session
per process, not for concurrent use within a process.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..log import get_logger
from . import runner

if TYPE_CHECKING:
    from ..config import ReservationConfig
    from .results_store import ResultsStore

JSON = dict[str, Any]
logger = get_logger(__name__)


class JobManager:
    """In-memory tracker for fare-check jobs triggered from the web UI."""

    def __init__(self, results_store: ResultsStore) -> None:
        self._results_store = results_store
        self._jobs: JSON = {}
        self._jobs_lock = threading.Lock()
        # Ensures only one webdriver session runs at a time, across all jobs.
        self._check_lock = threading.Lock()

    def start_single_check(self, reservation_config: ReservationConfig) -> str:
        job_id = self._create_job([reservation_config.confirmation_number])
        thread = threading.Thread(
            target=self._run_single, args=(job_id, reservation_config), daemon=True
        )
        thread.start()
        return job_id

    def start_check_all(self, reservation_configs: list[ReservationConfig]) -> str:
        confirmation_numbers = [r.confirmation_number for r in reservation_configs]
        job_id = self._create_job(confirmation_numbers)
        thread = threading.Thread(
            target=self._run_all, args=(job_id, reservation_configs), daemon=True
        )
        thread.start()
        return job_id

    def get_job(self, job_id: str) -> JSON | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def _create_job(self, confirmation_numbers: list[str]) -> str:
        job_id = uuid.uuid4().hex
        with self._jobs_lock:
            self._jobs[job_id] = {
                "status": "queued",
                "confirmation_numbers": confirmation_numbers,
                "results": {},
                "error": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
            }
        return job_id

    def _update_job(self, job_id: str, **fields: Any) -> None:
        with self._jobs_lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    def _run_single(self, job_id: str, reservation_config: ReservationConfig) -> None:
        with self._check_lock:
            self._update_job(job_id, status="running")
            try:
                payload = runner.run_check(reservation_config, self._results_store)
                self._update_job(
                    job_id,
                    status="done",
                    results={reservation_config.confirmation_number: payload},
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as err:
                logger.exception(
                    "Web UI job %s failed for %s", job_id, reservation_config.confirmation_number
                )
                self._update_job(
                    job_id,
                    status="error",
                    error=str(err),
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )

    def _run_all(self, job_id: str, reservation_configs: list[ReservationConfig]) -> None:
        with self._check_lock:
            self._update_job(job_id, status="running")
            results: JSON = {}
            error = None
            for reservation_config in reservation_configs:
                try:
                    payload = runner.run_check(reservation_config, self._results_store)
                    results[reservation_config.confirmation_number] = payload
                except Exception as err:
                    logger.exception(
                        "Web UI check-all job %s failed for %s",
                        job_id,
                        reservation_config.confirmation_number,
                    )
                    results[reservation_config.confirmation_number] = {
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "error": str(err),
                        "flights": [],
                    }
                    error = error or str(err)

            self._update_job(
                job_id,
                status="done",
                results=results,
                error=error,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
