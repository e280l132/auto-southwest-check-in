"""
Background execution of fare checks triggered from the web UI.

Fare checks drive a real browser session (seleniumbase) and take 30-90 seconds, so they run on
a worker thread while the UI polls for status. A single lock ensures only one webdriver session
runs at a time, since ReservationMonitor/CheckInScheduler were designed for one browser session
per process, not for concurrent use within a process.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..log import get_logger
from . import config_writer, runner, service

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ..config import ReservationConfig
    from .results_store import ResultsStore

JSON = dict[str, Any]

# How long a finished job stays readable before it is dropped. The page only polls a job until it
# finishes, so this just needs to outlive a poll in flight. Without it, jobs accumulate for the
# lifetime of the web server.
JOB_TTL = timedelta(minutes=30)

logger = get_logger(__name__)


class JobManager:
    """In-memory tracker for fare-check jobs triggered from the web UI."""

    def __init__(
        self,
        results_store: ResultsStore,
        config_path: Path,
        reload_config: Callable[[], None],
    ) -> None:
        self._results_store = results_store
        self._config_path = config_path
        self._reload_config = reload_config
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
            self._purge_expired()
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def _purge_expired(self) -> None:
        """
        Drop finished jobs that are past their TTL. Callers must hold _jobs_lock. Unfinished jobs
        are always kept, however long they run, so a slow check is never lost out from under the
        page polling it.
        """
        cutoff = datetime.now(timezone.utc) - JOB_TTL
        expired = []
        for job_id, job in self._jobs.items():
            finished_at = job.get("finished_at")
            if finished_at and datetime.fromisoformat(finished_at) < cutoff:
                expired.append(job_id)

        for job_id in expired:
            del self._jobs[job_id]

        if expired:
            logger.debug("Purged %d finished web UI job(s)", len(expired))

    def _create_job(self, confirmation_numbers: list[str]) -> str:
        job_id = uuid.uuid4().hex
        with self._jobs_lock:
            self._purge_expired()
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

    def _cache_flight_info(self, confirmation_number: str, payload: JSON) -> None:
        """
        Persist the checked flight's route/number/date to config.json (not its fare), so the
        page shows it on future loads without needing another check. Best-effort: a failure here
        must never fail the check itself.
        """
        flights = payload.get("flights") or []
        if not flights:
            return

        flight = flights[0]
        try:
            changed = config_writer.update_cached_flight(
                self._config_path,
                confirmation_number,
                flight_number=flight["flight_number"],
                departure_airport_code=flight["departure_airport_code"],
                destination_airport_code=flight["destination_airport_code"],
                local_departure_date=flight["local_departure_date"],
            )
            if changed:
                self._reload_config()
        except Exception:
            logger.exception("Could not cache flight info for %s", confirmation_number)

    def _run_single(self, job_id: str, reservation_config: ReservationConfig) -> None:
        queued_at = time.monotonic()
        with self._check_lock:
            wait_time = time.monotonic() - queued_at
            if wait_time > 1:
                logger.info(
                    "Job %s waited %.1fs for the webdriver lock (another check was running)",
                    job_id,
                    wait_time,
                )
            self._update_job(job_id, status="running")
            run_start = time.monotonic()
            try:
                payload = runner.run_check(reservation_config, self._results_store)
                self._cache_flight_info(reservation_config.confirmation_number, payload)
                self._update_job(
                    job_id,
                    status="done",
                    results={reservation_config.confirmation_number: payload},
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                logger.info("Job %s finished in %.1fs", job_id, time.monotonic() - run_start)
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
        queued_at = time.monotonic()
        with self._check_lock:
            wait_time = time.monotonic() - queued_at
            if wait_time > 1:
                logger.info(
                    "Job %s waited %.1fs for the webdriver lock (another check was running)",
                    job_id,
                    wait_time,
                )
            self._update_job(job_id, status="running")
            run_start = time.monotonic()
            results: JSON = {}
            error = None
            for reservation_config in reservation_configs:
                reservation_start = time.monotonic()
                try:
                    payload = runner.run_check(reservation_config, self._results_store)
                    self._cache_flight_info(reservation_config.confirmation_number, payload)
                    results[reservation_config.confirmation_number] = payload
                    logger.info(
                        "Check-all job %s: %s finished in %.1fs",
                        job_id,
                        reservation_config.confirmation_number,
                        time.monotonic() - reservation_start,
                    )
                except Exception as err:
                    logger.exception(
                        "Web UI check-all job %s failed for %s",
                        job_id,
                        reservation_config.confirmation_number,
                    )
                    # Build this through the same helper runner.run_check's own failure paths use,
                    # so it doesn't drift from the canonical payload shape (it was previously
                    # missing the 'transient' key), and persist it to results_store like every
                    # other failure path -- otherwise this failure only lives in the job dict,
                    # which is purged after JOB_TTL and isn't what the main page reads.
                    payload = service.build_check_payload(
                        reservation_config,
                        [],
                        checked_at=datetime.now(timezone.utc).isoformat(),
                        error=str(err),
                    )
                    self._results_store.save_result(
                        reservation_config.confirmation_number, payload
                    )
                    results[reservation_config.confirmation_number] = payload
                    error = error or str(err)

            self._update_job(
                job_id,
                status="done",
                results=results,
                error=error,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            logger.info(
                "Check-all job %s finished all %d reservation(s) in %.1fs",
                job_id,
                len(reservation_configs),
                time.monotonic() - run_start,
            )
