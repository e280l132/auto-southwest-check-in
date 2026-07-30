from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .checkin_handler import CheckInHandler
from .flight import Flight
from .log import get_logger
from .reservation_schema import translate_manage_reservation
from .utils import (
    TRANSIENT_ORIGIN_REJECTION,
    NotificationLevel,
    RequestError,
    SouthwestErrorCode,
    get_current_time,
    make_request,
)
from .webdriver import WebDriver

if TYPE_CHECKING:
    from .reservation_monitor import ReservationMonitor

VIEW_RESERVATION_URL = "mobile-air-booking/v1/mobile-air-booking/page/view-reservation/"

# How many retrieval cycles in a row must hit Southwest's transient rejection before it is worth
# waking the user. A single cycle failing is routine and self-heals, but a reservation that cannot
# be retrieved for several cycles running is worth knowing about.
TRANSIENT_FAILURE_ALERT_THRESHOLD = 3

# Fewer attempts than the default, because a rejection here is no longer the end of the road: the
# website lookup takes over. Spending ten minutes retrying an endpoint that mostly says no, before
# trying the one that mostly says yes, would be the wrong way round.
RESERVATION_MAX_ATTEMPTS = 6

logger = get_logger(__name__)


class CheckInScheduler:
    """
    Handles scheduling flights from reservations. Retrieves the necessary
    information and schedules a check-in for each flight via the CheckinHandler.
    """

    def __init__(self, reservation_monitor: ReservationMonitor) -> None:
        self.reservation_monitor = reservation_monitor
        self.notification_handler = reservation_monitor.notification_handler

        self.headers = {}
        self.flights = []
        self.checkin_handlers = []

        # Set when _get_reservation_info fails, so callers (e.g. the web UI) can surface the
        # actual Southwest error instead of just an empty flight list. Not used by the daemon.
        self.last_fetch_error = None
        self.last_fetch_error_is_transient = False

        # Consecutive transient rejections per confirmation number. The daemon's monitor process is
        # long lived, so this survives across retrieval cycles without needing to be persisted.
        self.transient_failures = {}

    def process_reservations(self, confirmation_numbers: list[str]) -> None:
        """
        Flights from all confirmation numbers are retrieved. Then, any new
        flights are scheduled and any flights now longer found are removed.
        """
        flights = []
        # Southwest intermittently rejects reservation lookups even when the reservation is fine.
        # A failed lookup returns no flights, which is indistinguishable from a reservation whose
        # flights are gone, so track it explicitly: scheduled check-ins must not be torn down just
        # because Southwest was unreachable this cycle.
        all_retrievals_authoritative = True
        for confirmation_number in confirmation_numbers:
            reservation_flights, is_authoritative = self._get_flights(confirmation_number)
            flights.extend(reservation_flights)
            all_retrievals_authoritative &= is_authoritative

        logger.debug("%d total flights were found", len(flights))
        self._update_scheduled_flights(flights, remove_old=all_retrievals_authoritative)

    def refresh_headers(self) -> None:
        logger.debug("Refreshing headers for current session")
        webdriver = WebDriver(self)
        webdriver.set_headers()

    def fetch_flights(self, confirmation_number: str) -> list[Flight]:
        """
        Retrieve a reservation's upcoming flights without scheduling any check-ins. Used by the
        web UI to look up flight/fare details on demand without triggering CheckInHandler.
        """
        return self._get_flights(confirmation_number)[0]

    def _get_flights(self, confirmation_number: str) -> tuple[list[Flight], bool]:
        """
        Get all flights booked on a single reservation.

        Also returns whether the result is authoritative, i.e. whether Southwest actually told us
        the current state of the reservation. An empty list with `False` means the lookup failed,
        not that the reservation is empty.
        """
        reservation_info, is_authoritative = self._get_reservation_info(confirmation_number)
        bounds = reservation_info.get("bounds", [])
        logger.debug("%d flights found under current reservation", len(bounds))

        current_utc_time = get_current_time()
        flights = []
        # If multiple flights are under the same confirmation number, it will schedule all checkins
        for flight_info in bounds:
            # For simplicity, reservation_info is only cached in the Flight constructor even though
            # it can get the flight_info
            flight = Flight(flight_info, reservation_info, confirmation_number)

            if flight.departure_time > current_utc_time:
                self._set_same_day_flight(flight, flights)
                flights.append(flight)

        return flights, is_authoritative

    def _get_reservation_info(self, confirmation_number: str) -> tuple[dict[str, Any], bool]:
        info = {
            "firstName": self.reservation_monitor.first_name,
            "lastName": self.reservation_monitor.last_name,
            "recordLocator": confirmation_number,
        }
        site = VIEW_RESERVATION_URL + confirmation_number

        try:
            logger.debug("Retrieving reservation information")
            response = make_request(
                "POST", site, self.headers, info, max_attempts=RESERVATION_MAX_ATTEMPTS
            )
        except RequestError as err:
            self.last_fetch_error = str(err)
            flights_departed = err.southwest_code == SouthwestErrorCode.FLIGHT_IN_PAST
            is_transient = err.southwest_code == TRANSIENT_ORIGIN_REJECTION
            self.last_fetch_error_is_transient = is_transient

            # The mobile API rejects most requests these days, so rather than give up, ask the
            # website to look the reservation up instead.
            if is_transient:
                reservation_info = self._get_reservation_from_website(confirmation_number)
                if reservation_info is not None:
                    return reservation_info, True

            # Don't send a notification if flights have already been scheduled and all flights
            # from this reservation are old. This is how old flights are removed.
            if len(self.flights) == 0 or not flights_departed:
                logger.debug("Failed to retrieve reservation info. Error: %s. Exiting", err)
                level = self._retrieval_failure_level(confirmation_number, is_transient)
                self.notification_handler.failed_reservation_retrieval(
                    err, confirmation_number, level
                )
            else:
                logger.debug("Flights on the reservation have already departed")

            # Departed flights are a real answer about the reservation, so removal should still
            # run. Any other failure tells us nothing, so the caller must leave check-ins alone.
            return {}, flights_departed

        self.last_fetch_error = None
        self.last_fetch_error_is_transient = False
        self.transient_failures.pop(confirmation_number, None)
        logger.debug("Successfully retrieved reservation information")
        return response["viewReservationViewPage"], True

    def _get_reservation_from_website(self, confirmation_number: str) -> dict[str, Any] | None:
        """
        Fall back to Southwest's own reservation lookup when the mobile API refuses to answer.

        Returns reservation info in the mobile API's shape, or None if the lookup also failed —
        in which case the caller reports the original mobile API error, since that is the one the
        user's reservation details actually relate to.
        """
        logger.debug("Mobile API rejected the lookup. Falling back to the Southwest website")
        try:
            data = WebDriver(self).get_reservation(
                confirmation_number,
                self.reservation_monitor.first_name,
                self.reservation_monitor.last_name,
            )
            reservation_info = translate_manage_reservation(data)
        except Exception:
            logger.exception("Website reservation lookup failed")
            return None

        logger.debug("Retrieved the reservation from the website instead")
        self.last_fetch_error = None
        self.last_fetch_error_is_transient = False
        self.transient_failures.pop(confirmation_number, None)
        return reservation_info

    def _retrieval_failure_level(
        self, confirmation_number: str, is_transient: bool
    ) -> NotificationLevel:
        """
        Decide how loudly to report a failed retrieval.

        Southwest rejecting a lookup outright is routine and self-heals, so an isolated occurrence
        is reported at NOTICE (filtered out by the default notification level) and only escalates
        once it keeps happening. Every other failure — a cancelled reservation, a wrong name, a
        confirmation number that doesn't exist — returns a different Southwest code, stops retrying
        immediately, and still reports at ERROR the first time it happens.
        """
        if not is_transient:
            self.transient_failures.pop(confirmation_number, None)
            return NotificationLevel.ERROR

        failures = self.transient_failures.get(confirmation_number, 0) + 1
        self.transient_failures[confirmation_number] = failures

        if failures >= TRANSIENT_FAILURE_ALERT_THRESHOLD:
            logger.debug("Reservation failed to be retrieved %d cycles in a row", failures)
            return NotificationLevel.ERROR

        logger.debug(
            "Transient retrieval failure %d of %d before alerting",
            failures,
            TRANSIENT_FAILURE_ALERT_THRESHOLD,
        )
        return NotificationLevel.NOTICE

    def _set_same_day_flight(self, flight: Flight, previous_flights: list[Flight]) -> None:
        for prev_flight in previous_flights:
            if flight.departure_time - prev_flight.departure_time <= timedelta(hours=24):
                logger.debug("Flight is on the same day")
                flight.is_same_day = True
                break

    def _update_scheduled_flights(self, flights: list[Flight], remove_old: bool = True) -> None:
        """
        Responsible for three tasks to update scheduled flights:
          1. Schedule check-ins for any new flights
          2. Remove scheduled flights that no longer exist
          3. Update the cached reservation info for any scheduled flights that do still exist

        Task 2 is skipped when `remove_old` is False, which means at least one reservation lookup
        failed this cycle. Removing flights based on an incomplete list would cancel check-ins for
        flights that are still booked.
        """
        logger.debug(
            "Updating scheduled flights (%d scheduled, %d found)", len(self.flights), len(flights)
        )

        new_flights = []
        for flight in flights:
            try:
                matching_flight_idx = self.flights.index(flight)
                # Flight has already been scheduled, so update the cached reservation info
                self.flights[matching_flight_idx].reservation_info = flight.reservation_info
            except ValueError:
                # Flight has not been scheduled yet
                new_flights.append(flight)

        logger.debug("%d new flights found", len(new_flights))
        self._schedule_flights(new_flights)

        if remove_old:
            self._remove_old_flights(flights)
        else:
            logger.debug(
                "Skipping removal of old flights because a reservation lookup failed. "
                "%d flights remain scheduled",
                len(self.flights),
            )

    def _schedule_flights(self, flights: list[Flight]) -> None:
        logger.debug("Scheduling %d flights for check-in", len(flights))

        reacommodated_flights = []
        for flight in flights:
            checkin_handler = CheckInHandler(self, flight, self.reservation_monitor.lock)
            checkin_handler.schedule_check_in()

            self.flights.append(flight)
            self.checkin_handlers.append(checkin_handler)

            if flight.can_be_reaccommodated:
                reacommodated_flights.append(flight)

        self.notification_handler.new_flights(flights)
        self.notification_handler.reaccommodated_flights(reacommodated_flights)

    def _remove_old_flights(self, flights: list[Flight]) -> None:
        """Remove all scheduled flights that are not in the current flight list"""
        logger.debug("%d flights are currently scheduled. Removing old flights", len(self.flights))

        # Copy the list because it can potentially change inside the loop
        for flight in self.flights[:]:
            if flight in flights:
                continue

            # Print console messages with a 12-hour time format
            flight_time = flight.get_display_time(False)
            print(
                f"Flight from {flight.departure_airport} to {flight.destination_airport} on "
                f"{flight_time} is no longer scheduled. Stopping its check-in\n"
            )  # Don't log as it has sensitive information

            flight_idx = self.flights.index(flight)
            self.checkin_handlers[flight_idx].stop_check_in()

            self.checkin_handlers.pop(flight_idx)
            self.flights.pop(flight_idx)

        logger.debug(
            "Successfully removed old flights. %d flights are now scheduled", len(self.flights)
        )
