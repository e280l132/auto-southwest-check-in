"""
Executes a fare check for one reservation by reusing the existing monitoring code, without
going through CheckInScheduler.process_reservations (which would schedule real check-in
processes via CheckInHandler). This is the only place the web UI touches the webdriver/fare
check machinery directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..log import get_logger
from ..reservation_monitor import ReservationMonitor
from ..utils import DriverTimeoutError
from . import service

if TYPE_CHECKING:
    from ..config import ReservationConfig
    from .results_store import ResultsStore
    from .service import JSON

logger = get_logger(__name__)


def run_check(reservation_config: ReservationConfig, results_store: ResultsStore) -> JSON:
    """
    Run a single fare check for one reservation and persist the result. Returns the same
    payload that gets stored, so the caller (a background job) can report it immediately.
    """
    confirmation_number = reservation_config.confirmation_number
    checked_at = datetime.now(timezone.utc).isoformat()

    # The user is watching this check run in their browser, so the results belong on the page
    # rather than in their inbox. Only the daemon pushes notifications.
    monitor = ReservationMonitor(reservation_config, lock=None, send_notifications=False)

    try:
        monitor.checkin_scheduler.refresh_headers()
    except DriverTimeoutError as err:
        logger.error("Web UI fare check for %s: header refresh timed out", confirmation_number)
        payload = service.build_check_payload(
            reservation_config,
            [],
            checked_at=checked_at,
            error=f"Timed out refreshing session: {err}",
        )
        results_store.save_result(confirmation_number, payload)
        return payload
    except Exception as err:
        logger.exception("Web UI fare check for %s: header refresh failed", confirmation_number)
        payload = service.build_check_payload(
            reservation_config,
            [],
            checked_at=checked_at,
            error=f"Failed to refresh session: {err}",
        )
        results_store.save_result(confirmation_number, payload)
        return payload

    flights = monitor.checkin_scheduler.fetch_flights(confirmation_number)

    if not flights:
        error = (
            monitor.checkin_scheduler.last_fetch_error
            or "No upcoming flights found for this reservation"
        )
        payload = service.build_check_payload(
            reservation_config,
            [],
            checked_at=checked_at,
            error=error,
            transient=monitor.checkin_scheduler.last_fetch_error_is_transient,
        )
        results_store.save_result(confirmation_number, payload)
        return payload

    results = monitor.check_fares_for_flights(flights)

    payload = service.build_check_payload(reservation_config, results, checked_at=checked_at)
    results_store.save_result(confirmation_number, payload)
    return payload
