from __future__ import annotations

import multiprocessing
import sys
import time
from typing import TYPE_CHECKING, Any

from .checkin_scheduler import CheckInScheduler
from .fare_checker import FareChecker
from .log import get_logger
from .notification_handler import NotificationHandler
from .utils import (
    CheckFaresOption,
    DriverTimeoutError,
    FlightChangeError,
    LoginError,
    RequestError,
    get_current_time,
)
from .webdriver import WebDriver

if TYPE_CHECKING:
    from datetime import datetime

    from .config import AccountConfig, ReservationConfig
    from .fare_check_result import FareCheckResult
    from .flight import Flight

TOO_MANY_REQUESTS_CODE = 429
INTERNAL_SERVER_ERROR_CODE = 500

RETRY_WAIT_SECONDS = 10

logger = get_logger(__name__)


class ReservationMonitor:
    """
    A high-level class responsible for monitoring one or more reservations for
    check-ins, flight changes or cancellations, and lower flight fares.
    """

    def __init__(
        self,
        config: AccountConfig | ReservationConfig,
        lock: multiprocessing.Lock | None = None,
    ) -> None:
        self.first_name = config.first_name
        self.last_name = config.last_name

        self.config = config
        self.lock = lock
        self.notification_handler = NotificationHandler(self)
        self.checkin_scheduler = CheckInScheduler(self)

    def start(self) -> None:
        """Start each reservation monitor in a separate process to run them in parallel"""
        process = multiprocessing.Process(target=self.monitor)
        process.start()

    def monitor(self) -> None:
        try:
            self._monitor()
        except KeyboardInterrupt:
            # Add a small delay so the MainThread's message prints first
            time.sleep(0.05)
            # Lock so all processes are stopped sequentially
            with self.lock:
                self._stop_monitoring()

    def _monitor(self) -> None:
        """Continuously performs checks every X hours (the retrieval interval)"""
        while True:
            time_before = get_current_time()

            # Acquire a lock to prevent concurrency issues with the webdriver
            logger.debug("Acquiring lock...")
            with self.lock:
                logger.debug("Lock acquired")

                should_exit = self._check()
                if should_exit:
                    logger.debug("Stopping monitoring")
                    break

                if self.config.retrieval_interval <= 0:
                    logger.debug("Monitoring is disabled as retrieval interval is 0")
                    break

            logger.debug("Lock released")
            self._smart_sleep(time_before)

    def _check(self) -> bool:
        """
        Check for reservation changes and lower fares. Returns true if future checks should not be
        performed (e.g. no more flights are scheduled to check in).
        """
        reservation = {"record_locator": self.config.confirmation_number}

        # Ensure there are valid headers
        try:
            self.checkin_scheduler.refresh_headers()
        except DriverTimeoutError:
            logger.warning("Timeout while refreshing headers. Skipping reservation retrieval")
            self.notification_handler.timeout_during_retrieval("reservation")
            return False

        # Schedule the reservations every time in case a flight is changed or cancelled
        self._schedule_reservations([reservation])

        if len(self.checkin_scheduler.flights) <= 0:
            logger.debug("No more flights are scheduled for check-in. Exiting...")
            return True

        self._check_flight_fares()
        return False

    def _schedule_reservations(self, reservations: list[dict[str, Any]]) -> None:
        logger.debug("Scheduling flight check-ins for %d reservations", len(reservations))
        confirmation_numbers = [reservation["record_locator"] for reservation in reservations]
        self.checkin_scheduler.process_reservations(confirmation_numbers)

    def _check_flight_fares(self) -> None:
        if self.config.check_fares == CheckFaresOption.NO:
            return

        flights = self.checkin_scheduler.flights
        self.check_fares_for_flights(flights)

    def check_fares_for_flights(self, flights: list[Flight]) -> list[FareCheckResult]:
        """
        Check fares for the given flights, reusing the same error handling and Healthchecks
        pings as the scheduled daemon check. Shared by both the daemon loop (_check_flight_fares)
        and the web UI so both go through identical fare-check logic.
        """
        logger.debug("Checking fares for %d flights", len(flights))

        fare_checker = FareChecker(self)
        results = []
        for flight in flights:
            # If a fare check fails, don't completely exit. Just print the error
            # and continue
            try:
                results.append(fare_checker.check_flight_price(flight))
                self.notification_handler.healthchecks_success(
                    f"Successful fare check,\nconfirmation number = {flight.confirmation_number}"
                )
            except RequestError as err:
                logger.error("Requesting error during fare check. %s. Skipping...", err)
                self.notification_handler.healthchecks_fail(
                    f"Failed fare check,\nconfirmation number = {flight.confirmation_number}"
                )
                results.append(self._make_error_result(flight, str(err)))
            except FlightChangeError as err:
                logger.info(
                    "Skipping fare check for flight %s: %s", flight.confirmation_number, err
                )
                self.notification_handler.healthchecks_success(
                    f"Successful fare check,\nconfirmation number = {flight.confirmation_number}"
                )
                results.append(self._make_skipped_result(flight, str(err)))
            except Exception as err:
                logger.exception("Unexpected error during fare check: %s", repr(err))
                self.notification_handler.healthchecks_fail(
                    f"Failed fare check,\nconfirmation number = {flight.confirmation_number}"
                )
                results.append(self._make_error_result(flight, str(err)))

        return results

    def _make_error_result(
        self, flight: Flight, message: str, status: str = "error"
    ) -> FareCheckResult:
        """
        Build a result describing a failed or skipped fare check.

        This runs inside the exception handlers of check_fares_for_flights, so it must not be able
        to raise under any circumstances: an exception here would replace the original error and
        propagate out through _check() and _monitor(), ending this reservation's monitoring
        entirely. Every field is therefore read defensively — the result is only ever used for
        display, so degraded values beat taking the monitor down.
        """
        from .fare_check_result import FareCheckResult  # noqa: PLC0415 (avoids circular import)

        try:
            local_departure_date, display_time = flight.get_safe_display_fields()
        except Exception:
            local_departure_date, display_time = "", ""

        return FareCheckResult(
            confirmation_number=getattr(flight, "confirmation_number", ""),
            flight_number=getattr(flight, "flight_number", ""),
            departure_airport_code=getattr(flight, "departure_airport_code", ""),
            destination_airport_code=getattr(flight, "destination_airport_code", ""),
            local_departure_date=local_departure_date,
            display_time=display_time,
            is_companion=False,
            status=status,
            message=message,
        )

    def _make_skipped_result(self, flight: Flight, message: str) -> FareCheckResult:
        return self._make_error_result(flight, message, status="skipped")

    def _smart_sleep(self, previous_time: datetime) -> None:
        """
        Account for the time it took to do recurring tasks so the sleep interval
        is the exact time provided in the configuration file.
        """
        current_time = get_current_time()
        time_taken = (current_time - previous_time).total_seconds()
        sleep_time = max(self.config.retrieval_interval - time_taken, 0)
        logger.debug("Sleeping for %d seconds", sleep_time)
        time.sleep(sleep_time)

    def get_display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def _stop_checkins(self) -> None:
        """
        Stops all check-ins for a monitor. This is called when Ctrl-C is pressed. The
        flight information is not logged because it contains sensitive information.
        """
        for checkin in self.checkin_scheduler.checkin_handlers:
            print(
                f"Cancelling check-in from '{checkin.flight.departure_airport}' to "
                f"'{checkin.flight.destination_airport}' for {self.first_name} {self.last_name}"
            )
            checkin.stop_check_in()

    def _stop_monitoring(self) -> None:
        print(
            f"\nStopping monitoring for reservation with confirmation number "
            f"{self.config.confirmation_number} and name {self.first_name} {self.last_name}"
        )
        self._stop_checkins()


class AccountMonitor(ReservationMonitor):
    """Monitor an account for newly booked reservations"""

    def __init__(self, config: AccountConfig, lock: multiprocessing.Lock) -> None:
        super().__init__(config, lock)
        self.username = config.username
        self.password = config.password
        self.preferred_name = ""

    def _check(self) -> bool:
        """
        Check for newly booked reservations for the account. Returns true if future checks should
        not be performed.
        """
        reservations, skip_scheduling = self._get_reservations()

        if not skip_scheduling:
            self._schedule_reservations(reservations)
            self._check_flight_fares()

        # There are currently no scenarios where future checks should not be performed within
        # this scope
        return False

    def _get_reservations(self, max_retries: int = 2) -> tuple[list[dict[str, Any]], bool]:
        """
        Attempts to retrieve a list of reservations and returns a tuple containing the list
        of reservations and a boolean indicating whether reservation scheduling should be skipped.

        The method will retry fetching reservations once in case of a timeout
        or a Too Many Requests error. If the retry fails, reservation scheduling will be
        skipped until the next scheduled attempt.
        """
        logger.debug("Retrieving reservations for account (max retries: %d)", max_retries)

        for attempt in range(max_retries + 1):
            webdriver = WebDriver(self.checkin_scheduler)

            try:
                reservations = webdriver.get_reservations(self)
                logger.debug(
                    "Successfully retrieved %d reservations after %d attempts",
                    len(reservations),
                    attempt + 1,
                )
                return reservations, False

            except DriverTimeoutError:
                if attempt < max_retries:
                    logger.debug("Timeout while retrieving reservations during login. Retrying")
                    logger.debug("Waiting for %d seconds before retrying", RETRY_WAIT_SECONDS)
                    time.sleep(RETRY_WAIT_SECONDS)
                else:
                    logger.debug(
                        "Timeout persisted after %d retries. Skipping reservation retrieval",
                        max_retries,
                    )
                    self.notification_handler.timeout_during_retrieval("account")

            except LoginError as err:
                if err.status_code in [TOO_MANY_REQUESTS_CODE, INTERNAL_SERVER_ERROR_CODE]:
                    if attempt < max_retries:
                        logger.debug(
                            "Encountered an error (status: %d) while logging in. Retrying",
                            err.status_code,
                        )
                        logger.debug("Waiting for %d seconds before retrying", RETRY_WAIT_SECONDS)
                        time.sleep(RETRY_WAIT_SECONDS)
                    else:
                        logger.debug(
                            "Error (status: %d) persists. Skipping reservation retrieval",
                            err.status_code,
                        )
                        self.notification_handler.too_many_requests_during_login()
                else:
                    logger.debug("Error logging in. %s. Exiting", err)
                    self.notification_handler.failed_login(err)
                    sys.exit(1)

        return [], True

    def get_display_name(self) -> str:
        if not self.first_name:
            # No name has been set, so use the account's username.
            return self.username

        if self.preferred_name:
            return f"{self.preferred_name} {self.last_name}"

        return super().get_display_name()

    def _stop_monitoring(self) -> None:
        print(f"\nStopping monitoring for account with username {self.username}")
        self._stop_checkins()
