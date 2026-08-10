"""
Executes a fare watch check triggered from the web UI, mirroring runner.py's role for
reservations. Uses FareWatchChecker directly rather than FareWatchMonitor, since the UI runs one
check at a time on demand and never sends notifications -- the results are already on screen.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..checkin_scheduler import CheckInScheduler
from ..fare_watch import FareWatchChecker
from ..log import get_logger
from ..notification_handler import NotificationHandler
from . import service

if TYPE_CHECKING:
    from ..config import FareWatchConfig, GlobalConfig
    from .results_store import ResultsStore
    from .service import JSON

logger = get_logger(__name__)


class _WatchCheckContext:
    """
    Stands in for ReservationMonitor as far as CheckInScheduler/WebDriver are concerned: it only
    needs .config (for browser_path and notifications) and .notification_handler. send_external
    is always False here since the UI is the one watching the result.
    """

    def __init__(self, config: GlobalConfig) -> None:
        self.config = config
        self.notification_handler = NotificationHandler(self, send_external=False)
        self.checkin_scheduler = CheckInScheduler(self)

    def get_display_name(self) -> str:
        return "Fare Watches"


def run_watch_check(
    watch: FareWatchConfig, config: GlobalConfig, results_store: ResultsStore
) -> JSON:
    """
    Run a single fare watch check and persist the result. Returns the same payload that gets
    stored, so the caller (a background job) can report it immediately.
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    context = _WatchCheckContext(config)
    checker = FareWatchChecker(context)

    result = checker.check(watch, checked_at)
    payload = service.build_watch_payload(result)
    results_store.save_result(watch.id, payload)
    return payload
