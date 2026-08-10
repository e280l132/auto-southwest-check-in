"""
Runs every enabled fare watch on a fixed interval, in its own process alongside the account and
reservation monitors. Mirrors ReservationMonitor's _monitor/_smart_sleep loop shape (see
lib/reservation_monitor.py), but there is only ever one FareWatchMonitor process for every watch
in the config, since watches share the same webdriver lock anyway and are typically checked far
less often than reservations.
"""

from __future__ import annotations

import multiprocessing
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .checkin_scheduler import CheckInScheduler
from .fare_watch import FareWatchChecker, FareWatchResult
from .fare_watch_state import FareWatchState, flight_key
from .log import get_logger
from .notification_handler import NotificationHandler
from .reservation_monitor import escalation_level
from .utils import get_current_time

if TYPE_CHECKING:
    from .config import FareWatchConfig, GlobalConfig

logger = get_logger(__name__)

# Statuses that count as a failed cycle for the consecutive-failure escalation policy.
FAILURE_STATUSES = ("error", "unavailable")


class FareWatchMonitor:
    def __init__(self, config: GlobalConfig, lock: multiprocessing.Lock) -> None:
        self.config = config
        self.lock = lock
        # Only the background daemon runs this monitor, so notifications always go out externally
        # (there is no on-demand/UI equivalent that needs send_external=False here, unlike
        # ReservationMonitor).
        self.notification_handler = NotificationHandler(self, send_external=True)
        # CheckInScheduler is reused purely as the WebDriver's config/notification sink -- fare
        # watches never schedule check-ins.
        self.checkin_scheduler = CheckInScheduler(self)
        self.state = FareWatchState()

        # Consecutive failures per watch id. This process is long-lived, so this survives across
        # retrieval cycles without needing to be persisted.
        self._failures: dict[str, int] = {}

    def get_display_name(self) -> str:
        return "Fare Watches"

    def start(self) -> None:
        """Start fare watch monitoring in a separate process, like ReservationMonitor.start."""
        process = multiprocessing.Process(target=self.monitor)
        process.start()

    def monitor(self) -> None:
        try:
            self._monitor()
        except KeyboardInterrupt:
            # Add a small delay so the MainThread's message prints first
            time.sleep(0.05)

    def _monitor(self) -> None:
        """Continuously check every enabled fare watch every X hours (fare_watch_interval)."""
        while True:
            time_before = get_current_time()

            logger.debug("Acquiring lock for fare watches...")
            with self.lock:
                logger.debug("Lock acquired")
                self._check_all()

            logger.debug("Lock released")
            self._smart_sleep(time_before)

    def _check_all(self) -> None:
        checker = FareWatchChecker(self)
        checked_at = datetime.now(timezone.utc).isoformat()

        for watch in self.config.fare_watches:
            if not watch.enabled:
                continue

            result = checker.check(watch, checked_at)

            if result.status in FAILURE_STATUSES:
                self._handle_failure(watch, result)
                continue

            self._failures[watch.id] = 0

            if result.status == "hit":
                self._handle_hit(watch, result)

    def _handle_hit(self, watch: FareWatchConfig, result: FareWatchResult) -> None:
        """
        Alert only for flights that either haven't been alerted on before, or have dropped in
        price since the last alert -- never every cycle a flight stays qualified.
        """
        new_hits = []
        for row in result.rows:
            if not row["isHit"]:
                continue

            key = flight_key(watch.date, row["displayNumber"])
            if self.state.should_alert(watch.id, key, row["points"]):
                new_hits.append(row)

        if not new_hits:
            logger.debug(
                "Fare watch %s: qualifying flight(s) already alerted at this price or lower",
                watch.id,
            )
            return

        self.notification_handler.fare_watch_hit(watch, new_hits)
        for row in new_hits:
            key = flight_key(watch.date, row["displayNumber"])
            self.state.record_alert(watch.id, key, row["points"])

    def _handle_failure(self, watch: FareWatchConfig, result: FareWatchResult) -> None:
        failures = self._failures.get(watch.id, 0) + 1
        self._failures[watch.id] = failures
        level = escalation_level(failures)
        logger.warning(
            "Fare watch %s failed (%d consecutive): %s", watch.id, failures, result.message
        )
        self.notification_handler.fare_watch_failure(watch, result.message, level)

    def _smart_sleep(self, previous_time: Any) -> None:
        """Same wall-clock cadence trick as ReservationMonitor._smart_sleep."""
        current_time = get_current_time()
        time_taken = (current_time - previous_time).total_seconds()
        sleep_time = max(self.config.fare_watch_interval - time_taken, 0)
        logger.debug("Sleeping for %d seconds", sleep_time)
        time.sleep(sleep_time)
