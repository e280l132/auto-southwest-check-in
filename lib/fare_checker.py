from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .fare_check_result import FareCheckResult
from .log import get_logger
from .utils import (
    FARE_CHECK_BACKOFF_CAP_SECS,
    FARE_CHECK_MAX_ATTEMPTS,
    TRANSIENT_ORIGIN_REJECTION,
    CheckFaresOption,
    DriverTimeoutError,
    FlightChangeError,
    RequestError,
    make_request,
    time,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .flight import Flight
    from .reservation_monitor import ReservationMonitor

# Type alias for JSON
JSON = dict[str, Any]

BOOKING_URL = "mobile-air-booking/"
CHANGE_SHOPPING_URL = "mobile-air-booking/v1/mobile-air-booking/page/flights/change/shopping"
logger = get_logger(__name__)


class FareChecker:
    def __init__(self, reservation_monitor: ReservationMonitor) -> None:
        self.reservation_monitor = reservation_monitor
        self.headers = reservation_monitor.checkin_scheduler.headers
        self.filter = get_fare_check_filter(self.reservation_monitor.config.check_fares)

    def check_flight_price(self, flight: Flight) -> FareCheckResult:
        """
        Check if the price amount is negative (in either points or USD).
        If it is, send a notification to the user about the lower fare.

        For companion-pass flights where the change flow is unavailable, a
        direct change-page fetch is attempted using the known URL pattern.
        """
        logger.debug("Checking current price for flight")

        if self.reservation_monitor.config.check_fares == CheckFaresOption.SAME_DAY_SMART:
            return self._check_all_alternate_fares(flight)

        try:
            flight_price = self._get_flight_price(flight)
        except FlightChangeError as err:
            if not self._is_reaccommodated(flight):
                return self._check_fare_via_public_search(flight)
            raise err

        price_info = f"{flight_price['amount']:+,} {flight_price['currencyCode']}"
        logger.debug("Flight price change found for %s", price_info)

        # The Southwest website can report a fare price difference of -1 USD. This is a
        # false positive as no credit is actually received when the flight is changed.
        # Refer to this discussion for more information:
        # https://github.com/jdholtz/auto-southwest-check-in/discussions/102
        if flight_price["amount"] < -1:
            # Lower fare!
            self.reservation_monitor.notification_handler.lower_fare(flight, price_info)
            status = "lower_fare"
        else:
            logger.info(
                "Fare check for flight %s: %s (no lower fare found)",
                flight.confirmation_number,
                price_info,
            )
            status = "no_lower_fare"

        # Give the non-smart modes a single-entry board for the current flight so the UI renders
        # the same table shape regardless of which check_fares mode produced the result.
        current_entry = self._board_entry(
            flight_numbers=flight.flight_number,
            display_number=flight.flight_number.replace("​", ""),
            departure_time=flight.get_safe_display_fields()[1],
            stop_description="Nonstop" if self._is_nonstop(flight) else "",
            is_current=True,
            is_nonstop=self._is_nonstop(flight),
            points=None,
            fare=flight_price,
        )

        return self._make_result(
            flight,
            status=status,
            message=price_info,
            currency_code=flight_price["currencyCode"],
            difference_amount=flight_price["amount"],
            board=[current_entry],
        )

    def _make_result(
        self, flight: Flight, *, status: str, message: str = "", **kwargs: Any
    ) -> FareCheckResult:
        """Build a FareCheckResult carrying the flight's identifying details."""
        local_departure_date, display_time = flight.get_safe_display_fields()

        return FareCheckResult(
            confirmation_number=flight.confirmation_number,
            flight_number=flight.flight_number,
            departure_airport_code=flight.departure_airport_code,
            destination_airport_code=flight.destination_airport_code,
            local_departure_date=local_departure_date,
            display_time=display_time,
            is_companion=self._is_companion_flight(flight),
            status=status,
            message=message,
            **kwargs,
        )

    def _is_companion_flight(self, flight: Flight) -> bool:
        """Return True if the reservation has a companion pass attached."""
        return is_companion_flight(flight)

    def _is_reaccommodated(self, flight: Flight) -> bool:
        """Return True if the flight is reaccommodated (can be changed for free)."""
        return flight.reservation_info["_links"].get("reaccom") is not None

    def _is_nonstop(self, flight: Flight) -> bool:
        """Return True if the current flight is nonstop (no zero-width slash in flight number)."""
        return "\u200b/\u200b" not in flight.flight_number

    def _get_all_cheaper_flights(self, flight: Flight) -> list[JSON]:
        """
        Return all cheaper flight alternatives on the same day using the change-shopping API.

        Applies a smart filter based on the current flight's stop type:
          - Nonstop current flight → only nonstop alternatives are considered
          - Connecting current flight → any alternative (nonstop or connecting) is considered

        Each returned dict contains:
          flightNumbers, displayNumber, departureTime, stopDescription,
          savings (amount/currencyCode)
        Sorted by savings amount ascending (biggest savings first).
        """
        board, _fare_type = self._get_change_board(flight)
        return self._cheaper_from_board(board, flight)

    def _get_change_board(self, flight: Flight) -> tuple[list[JSON], str]:
        """Fetch the same-day flights from the change-shopping API and normalize them."""
        flights, fare_type = self._get_matching_flights(flight)
        return self._build_change_board(flights, fare_type, flight), fare_type

    def _cheaper_from_board(self, board: list[JSON], flight: Flight) -> list[JSON]:
        """
        Narrow a same-day board down to the cheaper alternatives that drive notifications and
        ignore links, applying the nonstop smart filter based on the current flight's stop type:
          - Nonstop current flight \u2192 only nonstop alternatives are considered
          - Connecting current flight \u2192 any alternative (nonstop or connecting) is considered
        """
        nonstop_only = self._is_nonstop(flight)

        cheaper = [
            {
                "flightNumbers": entry["flightNumbers"],
                "displayNumber": entry["displayNumber"],
                "departureTime": entry["departureTime"],
                "stopDescription": entry["stopDescription"],
                "savings": {
                    "amount": entry["difference"],
                    "currencyCode": entry["currencyCode"],
                },
            }
            for entry in board
            if entry["isCheaper"] and (entry["isNonstop"] or not nonstop_only)
        ]

        cheaper.sort(key=lambda x: x["savings"]["amount"])
        return cheaper

    def _build_change_board(self, cards: list[JSON], fare_type: str, flight: Flight) -> list[JSON]:
        """
        Normalize every same-day flight from the change-shopping API into board entries, whether
        or not it is cheaper. See _board_entry for the entry shape.

        This API reports a price *difference* relative to what was paid, never an absolute price,
        so 'points' is left as None.
        """
        board = [
            self._board_entry(
                flight_numbers=card.get("flightNumbers", ""),
                display_number=card.get("flightNumbers", "").replace("\u200b", ""),
                departure_time=card.get("departureTime", ""),
                stop_description=card.get("stopDescription", ""),
                is_current=card.get("flightNumbers", "") == flight.flight_number,
                is_nonstop=card.get("stopDescription") == "Nonstop",
                points=None,
                fare=self._get_matching_fare(card.get("fares") or [], fare_type),
            )
            for card in cards
        ]

        board.sort(key=lambda entry: entry["departureTime"])
        return board

    def _build_public_board(
        self, cards: list[JSON], fare_type: str, flight: Flight, paid_points: int | None
    ) -> list[JSON]:
        """
        Normalize every same-day flight from the public search API into board entries, whether or
        not it is cheaper. See _board_entry for the entry shape.

        This API reports absolute points prices, so 'points' is always populated and the
        difference is derived against what was paid (when that is known).
        """
        board = []
        for card in cards:
            card_numbers = card.get("flightNumbers") or []
            points = self._get_card_points(card, fare_type)

            fare = None
            if points is not None and paid_points is not None:
                fare = {"amount": points - paid_points, "currencyCode": "PTS"}

            board.append(
                self._board_entry(
                    flight_numbers="\u200b/\u200b".join(card_numbers),
                    display_number="/".join(card_numbers),
                    departure_time=self._get_card_departure_time(card),
                    stop_description=self._get_card_stop_description(card),
                    is_current="\u200b/\u200b".join(card_numbers) == flight.flight_number,
                    is_nonstop="NONSTOP" in card.get("filterTags", []),
                    points=points,
                    fare=fare,
                )
            )

        board.sort(key=lambda entry: entry["departureTime"])
        return board

    def _board_entry(
        self,
        *,
        flight_numbers: str,
        display_number: str,
        departure_time: str,
        stop_description: str,
        is_current: bool,
        is_nonstop: bool,
        points: int | None,
        fare: JSON | None,
    ) -> JSON:
        """
        Build one normalized same-day board entry, shared by the change-shopping and public search
        paths so the UI has a single shape to render.

        'unavailable' marks flights where the booked fare type isn't sold; those are surfaced as
        such rather than dropped. 'isCheaper' applies the same false-positive -1 rule used
        elsewhere (see https://github.com/jdholtz/auto-southwest-check-in/discussions/102).
        """
        difference = fare["amount"] if fare else None

        return {
            "flightNumbers": flight_numbers,
            "displayNumber": display_number,
            "departureTime": departure_time,
            "stopDescription": stop_description,
            "isCurrent": is_current,
            "isNonstop": is_nonstop,
            "points": points,
            "difference": difference,
            "currencyCode": fare["currencyCode"] if fare else None,
            "unavailable": difference is None and points is None,
            "isCheaper": difference is not None and difference < -1,
        }

    def _get_card_points(self, card: JSON, fare_type: str) -> int | None:
        """Extract the absolute points price for a fare type from a public search card."""
        return get_card_points(card, fare_type)

    def _get_card_departure_time(self, card: JSON) -> str:
        """Public search reports either departureTime or a full departureDateTime."""
        return get_card_departure_time(card)

    def _get_card_stop_description(self, card: JSON) -> str:
        """
        Build a human-readable stop description for a public search card.

        Unlike the change-shopping API, public search cards have no 'stopDescription' field.
        Each entry in 'segments' is one marketing flight leg, so the number of connections is
        len(segments) - 1, and the layover airport is the first segment's destination.
        """
        return get_card_stop_description(card)

    def _check_all_alternate_fares(self, flight: Flight) -> FareCheckResult:
        """
        same_day_smart entry point: find ALL cheaper same-day alternatives, filter out ignored
        flights, and send a single digest notification if any visible alternatives remain.
        """
        from .ignore_manager import IgnoreManager  # noqa: PLC0415 (avoids circular dependency)

        flight_date = flight.local_departure_date
        conf = flight.confirmation_number
        ignore_manager = IgnoreManager()

        if ignore_manager.is_day_ignored(conf, flight_date):
            logger.info(
                "All alternate fares for flight %s on %s are ignored — skipping", conf, flight_date
            )
            return self._make_result(
                flight, status="skipped", message="All alternate fares for this day are ignored"
            )

        try:
            board, fare_type = self._get_change_board(flight)
            alternatives = self._cheaper_from_board(board, flight)
        except FlightChangeError as err:
            # A reaccommodated flight can already be changed for free, so there is nothing to
            # compare. Anything else means Southwest's change API is unavailable to us for this
            # reservation — a companion pass blocks it, or the reservation came from the website
            # lookup, which carries no change link. The public search still prices the route, so
            # fall back to that instead of reporting nothing.
            if not self._is_reaccommodated(flight):
                return self._check_fare_via_public_search(flight)

            logger.info("Skipping alternate fare check for flight %s: %s", conf, err)
            return self._make_result(flight, status="skipped", message=str(err))
        except Exception as err:
            logger.error("Error checking alternate fares for flight %s: %s", conf, err)
            return self._make_result(flight, status="error", message=str(err))

        # The full board is carried on every result below so the UI can show all same-day
        # flights, not just the cheaper subset that drives notifications.
        board_fields = self._board_result_fields(board, fare_type)

        visible = [
            a
            for a in alternatives
            if not ignore_manager.is_ignored(conf, flight_date, a["flightNumbers"])
        ]

        if not visible:
            logger.info(
                "Alternate fare check for flight %s on %s: no new cheaper alternatives "
                "(none found or all ignored)",
                conf,
                flight_date,
            )
            return self._make_result(
                flight,
                status="no_lower_fare",
                message="No new cheaper alternatives found",
                **board_fields,
            )

        port = self.reservation_monitor.config.ignore_server_port
        base_url = (
            self.reservation_monitor.config.ignore_server_base_url or f"http://localhost:{port}"
        )
        token = self.reservation_monitor.config.ignore_server_token
        self.reservation_monitor.notification_handler.alternate_fares(
            flight, visible, flight_date, base_url, token
        )
        return self._make_result(
            flight,
            status="lower_fare",
            message=f"{len(visible)} cheaper alternative(s) found",
            alternatives=visible,
            **board_fields,
        )

    def _board_result_fields(self, board: list[JSON], fare_type: str | None) -> JSON:
        """
        Build the FareCheckResult fields derived from a same-day board: the board itself plus the
        current flight's own price, taken from whichever entry is flagged as the current flight.
        """
        current = next((entry for entry in board if entry["isCurrent"]), None)

        return {
            "board": board,
            "fare_type": fare_type,
            "current_points": current["points"] if current else None,
            "difference_amount": current["difference"] if current else None,
            "currency_code": current["currencyCode"] if current else None,
        }

    def _bound_matches_flight(self, bound: JSON, flight: Flight) -> bool:
        """Return True if a reservation bound's flight number matches the given flight."""
        flights = bound.get("flights", [])
        flight_number = "\u200b/\u200b".join(f["number"].removeprefix("WN") for f in flights)
        return flight_number == flight.flight_number

    def _paid_fare_setting_name(self, flight: Flight) -> str:
        """The config setting holding what was paid, which differs for companion reservations."""
        return "companionFarePoints" if self._is_companion_flight(flight) else "originalFarePoints"

    def _check_fare_via_public_search(self, flight: Flight) -> FareCheckResult:
        """
        Price a flight through Southwest's public search, for reservations their change API won't
        answer for.

        Note this prices the route as a new booking rather than as a change to this reservation,
        so it answers "what does this flight cost today" against what was paid, not "what would
        Southwest credit me". Which figure was paid depends on how the reservation was booked.
        """
        config = self.reservation_monitor.config
        if self._is_companion_flight(flight):
            paid_points = getattr(config, "companion_fare_points", None)
        else:
            paid_points = getattr(config, "original_fare_points", None)

        return self._check_companion_fare_via_webdriver(flight, paid_points)

    def _check_companion_fare_via_webdriver(
        self, flight: Flight, companion_fare_points: int | None
    ) -> FareCheckResult:
        """
        Last-resort companion fare check using a real browser session to load the public
        Southwest flight search page. The public search has no knowledge of companion-pass
        restrictions, so it returns normal points pricing for the route/date.

        The lowest points fare found is compared against companionFarePoints. If no
        companionFarePoints is configured, the current price is logged for reference.
        """
        from .webdriver import WebDriver  # noqa: PLC0415 (avoids circular dependency)

        # Find the bound that matches this flight to get route/date info
        bounds = flight.reservation_info.get("bounds", [])
        departure_date = None
        for bound in bounds:
            if self._bound_matches_flight(bound, flight):
                departure_date = bound.get("departureDate")
                break

        if departure_date is None:
            logger.error(
                "Public search fare check failed for %s: could not determine departure date.",
                flight.confirmation_number,
            )
            self._log_companion_unavailable(
                flight, companion_fare_points, reason="could not determine departure date"
            )
            return self._make_result(
                flight,
                status="unavailable",
                message="Could not determine departure date",
                paid_points=companion_fare_points,
            )

        origin = flight.departure_airport_code
        destination = flight.destination_airport_code

        logger.info(
            "Checking fare for flight %s via public search (route: %s→%s on %s)",
            flight.confirmation_number,
            origin,
            destination,
            departure_date,
        )

        # Each attempt is a fresh browser session. Driving sessions back to back (one per
        # reservation, plus the reservation lookups) makes the occasional startup failure likely,
        # and replaying a request Southwest has already rejected repeats an identical fingerprint,
        # so a new session is the only retry with a different input. Measured, that is not enough
        # on its own -- three fresh sessions have been rejected uniformly -- but it costs a third
        # of what retrying inside one session did and is the only lever available here.
        max_attempts = 3
        response = None
        for attempt in range(
            max_attempts
        ):  # pragma: no branch — loop always exits via break/return
            try:
                webdriver = WebDriver(self.reservation_monitor.checkin_scheduler)
                response = webdriver.get_public_flight_prices(origin, destination, departure_date)
                break
            except Exception as err:
                is_transient = (
                    isinstance(err, RequestError)
                    and err.southwest_code == TRANSIENT_ORIGIN_REJECTION
                )
                # Anything else (a bad name, a cancelled reservation) will fail the same way in a
                # new session, so don't spend two more browsers finding that out
                retryable = is_transient or isinstance(err, DriverTimeoutError)

                if retryable and attempt < max_attempts - 1:
                    logger.info(
                        "Public search failed for %s (%s). Retrying with a new browser session",
                        flight.confirmation_number,
                        err,
                    )
                    continue

                if isinstance(err, DriverTimeoutError):
                    # Not always a timeout: the browser reaching the page but the response not
                    # carrying prices surfaces here too, so report what actually went wrong
                    logger.error(
                        "Public search fare check failed for %s after %d attempts: %s",
                        flight.confirmation_number,
                        max_attempts,
                        err,
                    )
                    self._log_companion_unavailable(
                        flight, companion_fare_points, reason=f"public search failed: {err}"
                    )
                    return self._make_result(
                        flight,
                        status="unavailable",
                        message="Webdriver timeout",
                        paid_points=companion_fare_points,
                    )

                logger.error(
                    "Public search fare check failed for %s: %s",
                    flight.confirmation_number,
                    err,
                )
                self._log_companion_unavailable(flight, companion_fare_points, reason=str(err))

                # Southwest's own origin rejects the request at this rate for a real browser too
                # (measured), so a raw "Forbidden (403)" reads as this tool being broken when it is
                # actually Southwest. By this point every attempt across several browser sessions
                # was rejected, so there is nothing left to try now.
                message = (
                    "Southwest rejected every attempt. This is usually temporary and not a "
                    "problem with the reservation — try checking again shortly."
                    if is_transient
                    else str(err)
                )
                return self._make_result(
                    flight,
                    status="error",
                    message=message,
                    transient=is_transient,
                    paid_points=companion_fare_points,
                )

        logger.debug(
            "Public search response for %s: %s",
            flight.confirmation_number,
            json.dumps(response, indent=2, default=str),
        )

        # Try to extract flight cards from the response. The structure may differ
        # from the change-shopping response, so we inspect the response and log
        # what we find. Once we know the structure, parsing can be tightened.
        cards = self._extract_cards_from_search_response(response)
        if cards is None:
            logger.info(
                "Public search response received for flight %s but flight cards not found. "
                "See debug log for response structure.",
                flight.confirmation_number,
            )
            self._log_companion_unavailable(
                flight, companion_fare_points, reason="unexpected search response structure"
            )
            return self._make_result(
                flight,
                status="unavailable",
                message="Unexpected search response structure",
                paid_points=companion_fare_points,
            )

        # Determine the fare type from the reservation bounds
        bounds = flight.reservation_info.get("bounds", [])
        fare_type = None
        for bound in bounds:
            if self._bound_matches_flight(bound, flight):
                fare_type = bound.get("fareProductDetails", {}).get("fareProductId")
                break

        if fare_type is None:
            logger.error(
                "Public search fare check failed for %s: could not determine fare type.",
                flight.confirmation_number,
            )
            return self._make_result(
                flight,
                status="error",
                message="Could not determine fare type",
                paid_points=companion_fare_points,
            )

        # The full same-day board is attached to every result below so the UI can show all
        # flights that day, not just the ones that turn out to be cheaper.
        board = self._build_public_board(cards, fare_type, flight, companion_fare_points)

        # The public search returns absolute prices, not priceDifference.
        # Find the lowest points fare for the matching flight(s) and fare type.
        lowest_points = self._get_lowest_points_from_cards(cards, fare_type, flight)

        if lowest_points is None:
            logger.info(
                "Fare check for flight %s: no %s points fare available "
                "in public search results.",
                flight.confirmation_number,
                fare_type,
            )
            return self._make_result(
                flight,
                status="unavailable",
                message=f"No {fare_type} points fare available in public search results",
                paid_points=companion_fare_points,
                board=board,
                fare_type=fare_type,
            )

        # same_day_smart: find all cheaper alternate flights, not just the same flight
        if self.reservation_monitor.config.check_fares == CheckFaresOption.SAME_DAY_SMART:
            if companion_fare_points is not None:
                return self._check_companion_alternate_fares(
                    flight, cards, fare_type, companion_fare_points, departure_date
                )
            setting = self._paid_fare_setting_name(flight)
            logger.info(
                "Alternate fare check for flight %s: set '%s' "
                "in config to enable same_day_smart alternate fare checking.",
                flight.confirmation_number,
                setting,
            )
            return self._make_result(
                flight,
                status="unavailable",
                message=f"Set '{setting}' in config to enable same_day_smart checking",
                current_points=lowest_points,
                board=board,
                fare_type=fare_type,
            )

        if companion_fare_points is not None:
            difference = lowest_points - companion_fare_points
            price_info = (
                f"current: {lowest_points:,} PTS, paid: {companion_fare_points:,} PTS "
                f"(difference: {difference:+,} PTS)"
            )
            if difference < -1:
                self.reservation_monitor.notification_handler.lower_fare(
                    flight, f"{difference:+,} PTS"
                )
                status = "lower_fare"
            else:
                logger.info(
                    "Fare check for flight %s: %s (no lower fare found)",
                    flight.confirmation_number,
                    price_info,
                )
                status = "no_lower_fare"

            return self._make_result(
                flight,
                status=status,
                message=price_info,
                currency_code="PTS",
                difference_amount=difference,
                current_points=lowest_points,
                paid_points=companion_fare_points,
                board=board,
                fare_type=fare_type,
            )

        logger.info(
            "Fare check for flight %s: current %s fare is %s PTS. "
            "Set 'companionFarePoints' in config to detect lower fares.",
            flight.confirmation_number,
            fare_type,
            f"{lowest_points:,}",
        )
        return self._make_result(
            flight,
            status="unavailable",
            message="Set 'companionFarePoints' in config to detect lower fares",
            current_points=lowest_points,
            board=board,
            fare_type=fare_type,
        )

    def _check_companion_alternate_fares(
        self,
        flight: Flight,
        cards: list[JSON],
        fare_type: str,
        companion_fare_points: int,
        flight_date: str,
    ) -> FareCheckResult:
        """
        Find all cheaper alternate flights for a companion-pass flight using public search cards.
        Called from _check_companion_fare_via_webdriver when check_fares == SAME_DAY_SMART.

        Unlike the change-shopping API (which returns priceDifference), the public search returns
        absolute prices. Savings are computed as card_price - companion_fare_points.
        """
        from .ignore_manager import IgnoreManager  # noqa: PLC0415 (avoids circular dependency)

        conf = flight.confirmation_number
        ignore_manager = IgnoreManager()

        board = self._build_public_board(cards, fare_type, flight, companion_fare_points)
        board_fields = self._board_result_fields(board, fare_type)
        # The public search reports absolute prices, so the paid fare is what the board's
        # differences were derived against rather than something recoverable from the board.
        board_fields["paid_points"] = companion_fare_points

        if ignore_manager.is_day_ignored(conf, flight_date):
            logger.info(
                "All alternate fares for companion flight %s on %s are ignored", conf, flight_date
            )
            return self._make_result(
                flight,
                status="skipped",
                message="All alternate fares for this day are ignored",
                **board_fields,
            )

        alternatives = self._cheaper_from_board(board, flight)

        visible = [
            a
            for a in alternatives
            if not ignore_manager.is_ignored(conf, flight_date, a["flightNumbers"])
        ]

        if not visible:
            logger.info(
                "Alternate fare check for flight %s on %s: no new cheaper alternatives "
                "(none found or all ignored)",
                conf,
                flight_date,
            )
            return self._make_result(
                flight,
                status="no_lower_fare",
                message="No new cheaper alternatives found",
                **board_fields,
            )

        port = self.reservation_monitor.config.ignore_server_port
        base_url = (
            self.reservation_monitor.config.ignore_server_base_url or f"http://localhost:{port}"
        )
        token = self.reservation_monitor.config.ignore_server_token
        self.reservation_monitor.notification_handler.alternate_fares(
            flight, visible, flight_date, base_url, token
        )
        return self._make_result(
            flight,
            status="lower_fare",
            message=f"{len(visible)} cheaper alternative(s) found",
            alternatives=visible,
            **board_fields,
        )

    def _extract_cards_from_search_response(self, response: JSON) -> list[JSON] | None:
        """Extract flight cards from the public search API response."""
        return extract_cards_from_search_response(response)

    def _get_lowest_points_from_cards(
        self, cards: list[JSON], fare_type: str, flight: Flight
    ) -> int | None:
        """
        Find the lowest points price across filtered flight cards from the public search.
        Public search returns absolute prices (not priceDifference), structured as:
          card["fareProducts"]["ADULT"][fare_type]["fare"]["totalFare"]["value"]
        where value is a string like "12500" and currencyCode is "POINTS".
        Returns None if no matching points fares are found.
        """
        lowest = None

        for card in cards:
            if not self._public_search_filter(card, flight):
                continue

            try:
                total_fare = card["fareProducts"]["ADULT"][fare_type]["fare"]["totalFare"]
            except (KeyError, TypeError):
                continue

            if total_fare.get("currencyCode") != "POINTS":
                continue

            try:
                amount = int(str(total_fare.get("value", "")).replace(",", ""))
                if lowest is None or amount < lowest:
                    lowest = amount
            except (ValueError, TypeError):
                continue

        return lowest

    def _public_search_filter(self, card: JSON, flight: Flight) -> bool:
        """
        Apply the configured fare filter to a public search result card.
        Public search uses different field names than the change-shopping response:
          - flightNumbers is a list (e.g. ["2940"]) not a string
          - nonstop is indicated by "NONSTOP" in filterTags
        """
        if self.filter is same_flight_filter:
            return flight.flight_number in card.get("flightNumbers", [])
        if self.filter is nonstop_flight_filter:
            return "NONSTOP" in card.get("filterTags", [])
        # any_flight_filter — include everything
        return True

    def _log_companion_unavailable(
        self, flight: Flight, companion_fare_points: int | None, reason: str = ""
    ) -> None:
        """Log a clear INFO message when a public search fare check is unavailable."""
        suffix = f" ({reason})" if reason else ""
        if companion_fare_points is not None:
            logger.info(
                "Fare check for flight %s is unavailable%s. "
                "Paid fare: %s points. Cannot determine if a lower fare exists.",
                flight.confirmation_number,
                suffix,
                f"{companion_fare_points:,}",
            )
        else:
            logger.info(
                "Fare check for flight %s is unavailable%s. "
                "Set '%s' in the reservation config to enable fare tracking.",
                flight.confirmation_number,
                suffix,
                self._paid_fare_setting_name(flight),
            )

    def _get_flight_price(self, flight: Flight) -> JSON:
        """Get the price difference of the flight"""
        flights, fare_type = self._get_matching_flights(flight)
        logger.debug("Found %d matching flights", len(flights))

        lowest_fare = self._get_lowest_fare(flight, flights, fare_type)
        return lowest_fare

    def _get_matching_flights(self, flight: Flight) -> tuple[list[JSON], str]:
        """
        Get all of the flights that match the current flight's departure airport,
        arrival airport, and departure date.

        Additionally, retrieve the flight's fare type so we can check the correct
        fare for a price drop.
        """
        change_flight_page, fare_type_bounds = self._get_change_flight_page(flight.reservation_info)
        query = self._get_search_query(change_flight_page, flight)

        info = change_flight_page["_links"]["changeShopping"]
        site = BOOKING_URL + info["href"]
        logger.debug("changeShopping URL: %s", site)

        # Southwest will not display the other page if its prices aren't requested. Therefore
        # we need to know what page to get based on what flight we requested (in case two flights
        # (round-trip flights) are on the same reservation)
        if query.get("outbound", {}).get("isChangeBound"):
            bound_page = "outboundPage"
        elif query.get("inbound", {}).get("isChangeBound"):
            bound_page = "inboundPage"
        else:
            # This exception usually happens when Southwest changes the formatting of their flight
            # numbers
            raise ValueError("Flight number did not match any flight bound on the reservation")

        bound = 0 if bound_page == "outboundPage" else 1
        fare_type = fare_type_bounds[bound]["fareProductDetails"]["fareProductId"]

        logger.debug("Retrieving matching flights")
        time.sleep(2)

        response = make_request(
            "POST",
            site,
            self.headers,
            query,
            max_attempts=FARE_CHECK_MAX_ATTEMPTS,
            backoff_cap_secs=FARE_CHECK_BACKOFF_CAP_SECS,
        )
        return response["changeShoppingPage"]["flights"][bound_page]["cards"], fare_type

    def _get_change_flight_page(self, reservation_info: JSON) -> tuple[JSON, list[JSON]]:
        fare_type_bounds = reservation_info["bounds"]

        # Ensure the flight does not have a companion pass connected to it
        # as companion passes are not supported.
        self._check_for_companion(reservation_info)

        # Next, get the search information needed to change the flight
        logger.debug("Retrieving search information for the current flight")
        change_link = reservation_info["_links"]["change"]
        reaccom_link = reservation_info["_links"]["reaccom"]

        if reaccom_link is not None:
            # The flight is reaccommodated, so no fare checking is needed
            raise FlightChangeError("Flight can be changed for free (reaccommodated)")

        # The change link does not exist, so skip fare checking for this flight
        if change_link is None:
            raise FlightChangeError("Flight cannot be changed online")

        site = BOOKING_URL + change_link["href"]
        logger.debug("changeFlightPage URL: %s", site)
        logger.debug("changeFlightPage query params: %s", change_link["query"])
        time.sleep(2)

        response = make_request(
            "GET",
            site,
            self.headers,
            change_link["query"],
            max_attempts=FARE_CHECK_MAX_ATTEMPTS,
            backoff_cap_secs=FARE_CHECK_BACKOFF_CAP_SECS,
        )
        return response["changeFlightPage"], fare_type_bounds

    def _get_search_query(self, flight_page: JSON, flight: Flight) -> JSON:
        """
        Generate the search query needed to get matching flights. The search query
        is different if the reservation is one-way vs. round-trip
        """
        bound_references = flight_page["_links"]["changeShopping"]["body"]
        search_terms = []
        for idx, bound in enumerate(flight_page["boundSelections"]):
            search_terms.append(
                {
                    "boundReference": bound_references[idx]["boundReference"],
                    "date": bound["originalDate"],
                    "destination-airport": bound["toAirportCode"],
                    "origin-airport": bound["fromAirportCode"],
                    # This allows selecting the correct flight for a round-trip reservation.
                    "isChangeBound": bound["flight"] == flight.flight_number,
                }
            )

        # Only generate a query including both 'outbound' and 'inbound' if the reservation
        # is round-trip. Otherwise, just generate a query including 'outbound'
        bounds = ["outbound", "inbound"]
        return dict(zip(bounds, search_terms))

    def _check_for_companion(self, reservation_info: JSON) -> None:
        grey_box_message = reservation_info.get("greyBoxMessage")
        if grey_box_message and "companion" in (grey_box_message.get("body") or ""):
            raise FlightChangeError("Fare check is not supported with companion passes")

    def _get_lowest_fare(self, flight: Flight, flights: list[JSON], fare_type: str) -> JSON:
        """
        Get the lowest fare for the queried flights based on the filter being used. If no fare is
        available for the specific fare type, a 0 USD difference will be returned.
        """
        lowest_fare = None

        for new_flight in flights:
            # Only compare flight fares that match the current filter
            if self.filter(flight, new_flight):
                fare = self._get_matching_fare(new_flight["fares"], fare_type)
                # Check if this fare is the lowest encountered so far
                if not lowest_fare or (fare and fare["amount"] < lowest_fare["amount"]):
                    lowest_fare = fare

        if not lowest_fare:
            # No fares are available (most likely due to tickets of that fare type
            # not being sold anymore). Therefore, report back a 0 USD difference.
            logger.debug("Fare %s is not available. Setting price difference to 0 USD", fare_type)
            lowest_fare = {"amount": 0, "currencyCode": "USD"}

        return lowest_fare

    def _get_matching_fare(self, fares: list[JSON], fare_type: str) -> JSON | None:
        """
        Get the fare that matches the fare type. If a fare exists, the amount will be returned, as
        an integer, and the currency code (USD or points). If no fare exists, nothing will be
        returned.
        """
        if fares is None:
            fares = []

        for fare in fares:
            if fare["_meta"]["fareProductId"] == fare_type:
                if "priceDifference" in fare:
                    flight_price = fare["priceDifference"]
                    # Format the amount correctly
                    sign = flight_price.get("sign", "")
                    parsed_amount = int(sign + flight_price["amount"].replace(",", ""))
                    return {"amount": parsed_amount, "currencyCode": flight_price["currencyCode"]}

                break

        return None


def get_fare_check_filter(check_fares: CheckFaresOption) -> Callable[[Flight, JSON], bool]:
    if check_fares == CheckFaresOption.SAME_FLIGHT:
        return same_flight_filter
    if check_fares == CheckFaresOption.SAME_DAY_NONSTOP:
        return nonstop_flight_filter
    if check_fares in (CheckFaresOption.SAME_DAY, CheckFaresOption.SAME_DAY_SMART):
        # SAME_DAY_SMART uses its own smart-filter logic in _get_all_cheaper_flights;
        # any_flight_filter here is the fallback for the companion-pass webdriver path.
        return any_flight_filter

    raise ValueError(f"check_fares value ({check_fares}) did not match any valid option")


def same_flight_filter(flight: Flight, flight_json: JSON) -> bool:
    return flight_json["flightNumbers"] == flight.flight_number


def any_flight_filter(*_) -> bool:
    return True


def nonstop_flight_filter(_, flight_json: JSON) -> bool:
    return flight_json["stopDescription"] == "Nonstop"


def extract_cards_from_search_response(response: JSON) -> list[JSON] | None:
    """
    Extract flight cards from the public search API response.

    Module-level so other callers (e.g. fare watches) can reuse the same parsing without a
    FareChecker/reservation instance.
    """
    try:
        return response["data"]["searchResults"]["airProducts"][0]["details"]
    except KeyError as err:
        logger.debug("Public search response missing expected key: %s", err)
        return None
    except (TypeError, IndexError) as err:
        logger.debug("Public search response has unexpected structure: %s", err)
        return None


def get_card_points(card: JSON, fare_type: str) -> int | None:
    """Extract the absolute points price for a fare type from a public search card."""
    try:
        total_fare = card["fareProducts"]["ADULT"][fare_type]["fare"]["totalFare"]
    except (KeyError, TypeError):
        return None

    if total_fare.get("currencyCode") != "POINTS":
        return None

    try:
        return int(str(total_fare.get("value", "")).replace(",", ""))
    except (ValueError, TypeError):
        return None


def get_card_departure_time(card: JSON) -> str:
    """Public search reports either departureTime or a full departureDateTime."""
    departure_time = card.get("departureTime") or card.get("departureDateTime", "")
    if "T" in departure_time:
        departure_time = departure_time.split("T")[1][:5]
    return departure_time


def get_card_stop_description(card: JSON) -> str:
    """
    Build a human-readable stop description for a public search card.

    Unlike the change-shopping API, public search cards have no 'stopDescription' field. Each
    entry in 'segments' is one marketing flight leg, so the number of connections is
    len(segments) - 1, and the layover airport is the first segment's destination.
    """
    segments = card.get("segments") or []
    stops = len(segments) - 1

    if stops <= 0:
        return "Nonstop"

    connecting_airport = segments[0].get("destinationAirportCode", "")
    label = f"{stops} Stop" + ("s" if stops != 1 else "")
    return f"{label}, {connecting_airport}" if connecting_airport else label


def search_public_flights_with_retry(
    checkin_scheduler: Any, origin: str, destination: str, departure_date: str
) -> JSON:
    """
    Drive Southwest's public flight search, retrying with a fresh browser session on transient
    failures. Shared by the companion-fare fallback above and fare watches, both of which need
    the exact same "how many times is it worth trying" policy documented in
    FareChecker._check_companion_fare_via_webdriver.

    Raises the last error (RequestError, DriverTimeoutError, or another exception) if every
    attempt fails.
    """
    from .webdriver import WebDriver  # noqa: PLC0415 (avoids circular dependency)

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            webdriver = WebDriver(checkin_scheduler)
            return webdriver.get_public_flight_prices(origin, destination, departure_date)
        except Exception as err:
            is_transient = (
                isinstance(err, RequestError) and err.southwest_code == TRANSIENT_ORIGIN_REJECTION
            )
            retryable = is_transient or isinstance(err, DriverTimeoutError)

            if retryable and attempt < max_attempts - 1:
                logger.info(
                    "Public search failed (%s). Retrying with a new browser session (%d/%d)",
                    err,
                    attempt + 2,
                    max_attempts,
                )
                continue

            raise


def is_companion_flight(flight: Flight) -> bool:
    """
    Return True if the reservation has a companion pass attached.

    Module-level so callers other than FareChecker (e.g. the web UI) can detect this without
    duplicating the check.
    """
    reservation_info = flight.reservation_info

    # The website lookup states this outright; the mobile API only implies it in a grey box message
    if reservation_info.get("hasCompanion"):
        return True

    grey_box = reservation_info.get("greyBoxMessage") or {}
    return "companion" in (grey_box.get("body") or "").lower()
