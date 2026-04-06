from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .log import get_logger
from .utils import CheckFaresOption, FlightChangeError, RequestError, make_request, time

if TYPE_CHECKING:
    from collections.abc import Callable

    from .flight import Flight
    from .reservation_monitor import ReservationMonitor

# Type alias for JSON
JSON = dict[str, Any]

BOOKING_URL = "mobile-air-booking/"
# Endpoint for searching available flights without a reservation change context.
# Used for companion-pass flights where change_link is null.
COMPANION_SHOPPING_URL = (
    "mobile-air-booking/v1/mobile-air-booking/page/flights/products"
)
logger = get_logger(__name__)


class FareChecker:
    def __init__(self, reservation_monitor: ReservationMonitor) -> None:
        self.reservation_monitor = reservation_monitor
        self.headers = reservation_monitor.checkin_scheduler.headers
        self.filter = get_fare_check_filter(self.reservation_monitor.config.check_fares)

    def check_flight_price(self, flight: Flight) -> None:
        """
        Check if the price amount is negative (in either points or USD).
        If it is, send a notification to the user about the lower fare.

        For companion-pass flights where the change flow is unavailable, an
        alternative direct-search path is used if companionFarePoints is configured.
        """
        logger.debug("Checking current price for flight")

        try:
            flight_price = self._get_flight_price(flight)
        except FlightChangeError as err:
            # If this is a companion flight and the user has configured a paid fare,
            # use the alternative companion fare check instead of skipping.
            if self._is_companion_flight(flight) and not self._is_reaccommodated(flight):
                self._check_companion_flight_price(flight)
            else:
                raise err
            return

        price_info = f"{flight_price['amount']:+,} {flight_price['currencyCode']}"
        logger.debug("Flight price change found for %s", price_info)

        # The Southwest website can report a fare price difference of -1 USD. This is a
        # false positive as no credit is actually received when the flight is changed.
        # Refer to this discussion for more information:
        # https://github.com/jdholtz/auto-southwest-check-in/discussions/102
        if flight_price["amount"] < -1:
            # Lower fare!
            self.reservation_monitor.notification_handler.lower_fare(flight, price_info)
        else:
            logger.info(
                "Fare check for flight %s: %s (no lower fare found)",
                flight.confirmation_number,
                price_info,
            )

    def _is_companion_flight(self, flight: Flight) -> bool:
        """Return True if the reservation has a companion pass attached."""
        grey_box = flight.reservation_info.get("greyBoxMessage") or {}
        return "companion" in (grey_box.get("body") or "").lower()

    def _is_reaccommodated(self, flight: Flight) -> bool:
        """Return True if the flight is reaccommodated (can be changed for free)."""
        return flight.reservation_info["_links"].get("reaccom") is not None

    def _check_companion_flight_price(self, flight: Flight) -> None:
        """
        Check fares for a companion-pass flight by searching for current prices
        directly (without the change flow) and comparing against the configured
        paid fare.

        Requires companionFarePoints to be set in the reservation config.
        """
        paid_points = self.reservation_monitor.config.companion_fare_points
        if paid_points is None:
            logger.info(
                "Skipping companion fare check for flight %s: "
                "add 'companionFarePoints' to this reservation in config.json "
                "to enable fare checking for companion-pass flights",
                flight.confirmation_number,
            )
            return

        if not flight.destination_airport_code:
            logger.warning(
                "Cannot check companion fare for flight %s: "
                "destination airport code not available from Southwest API. "
                "This may resolve itself; no action needed.",
                flight.confirmation_number,
            )
            return

        logger.info(
            "Checking companion fare for flight %s (%s → %s on %s), paid %d PTS",
            flight.confirmation_number,
            flight.departure_airport_code,
            flight.destination_airport_code,
            flight.reservation_info["bounds"][0]["departureDate"],
            paid_points,
        )

        try:
            current_price = self._get_companion_current_price(flight)
        except RequestError as err:
            logger.error(
                "Failed to retrieve companion flight prices for %s: %s",
                flight.confirmation_number,
                err,
            )
            return

        if current_price is None:
            logger.info(
                "No matching fare found for companion flight %s (flight may be sold out or "
                "fare type unavailable)",
                flight.confirmation_number,
            )
            return

        price_diff = current_price - paid_points
        if price_diff < -100:
            price_info = f"{price_diff:+,} PTS (current: {current_price:,} PTS, paid: {paid_points:,} PTS)"
            self.reservation_monitor.notification_handler.lower_fare(flight, price_info)
        else:
            logger.info(
                "Companion fare check for flight %s: current=%d PTS, paid=%d PTS (no lower fare found)",
                flight.confirmation_number,
                current_price,
                paid_points,
            )

    def _get_companion_current_price(self, flight: Flight) -> int | None:
        """
        Search for available flights on the same route/date and return the lowest
        absolute price in points matching the fare type and filter.

        Returns None if no matching fare is available.
        """
        bound = self._get_flight_bound(flight)
        if bound is None:
            return None

        fare_type = bound.get("fareProductDetails", {}).get("fareProductId", "")
        departure_date = bound.get("departureDate", "")

        query = {
            "departureDate": departure_date,
            "origin-airport": flight.departure_airport_code,
            "destination-airport": flight.destination_airport_code,
            "fare-type": "POINTS",
            "number-adults": 1,
            "number-seniors": 0,
            "trip-type": "OW",
            "carrier-of-choice": "WN",
            "promo-code": "",
        }

        logger.debug("Searching for companion flight prices")
        time.sleep(2)

        response = make_request("GET", COMPANION_SHOPPING_URL, self.headers, query, max_attempts=7)
        cards = self._extract_companion_flight_cards(response, flight)
        return self._get_lowest_absolute_fare(cards, fare_type)

    def _get_flight_bound(self, flight: Flight) -> JSON | None:
        """Find the bound in reservation_info that matches this flight."""
        for bound in flight.reservation_info.get("bounds", []):
            bound_flight_number = self._format_bound_flight_number(bound.get("flights", []))
            if bound_flight_number == flight.flight_number:
                return bound
        # Fall back to first bound if only one exists
        bounds = flight.reservation_info.get("bounds", [])
        return bounds[0] if len(bounds) == 1 else None

    def _format_bound_flight_number(self, flights: list[JSON]) -> str:
        """Format flight numbers from a bound the same way Flight._get_flight_number does."""
        flight_number = ""
        for f in flights:
            flight_number += f["number"].removeprefix("WN")
            flight_number += "\u200b/\u200b"
        return flight_number.rstrip("/\u200b")

    def _extract_companion_flight_cards(self, response: JSON, flight: Flight) -> list[JSON]:
        """
        Extract flight cards from the companion shopping response.
        The response structure may differ from changeShopping; try common paths.
        """
        # Try the standard changeShopping structure first (in case SW reuses it)
        change_page = response.get("changeShoppingPage", {})
        if change_page:
            flights_data = change_page.get("flights", {})
            return (
                flights_data.get("outboundPage", {}).get("cards", [])
                or flights_data.get("inboundPage", {}).get("cards", [])
            )

        # Try a direct flight products structure
        flights_page = response.get("flightsPage", response.get("availableFlightsPage", {}))
        return flights_page.get("cards", [])

    def _get_lowest_absolute_fare(self, cards: list[JSON], fare_type: str) -> int | None:
        """
        Return the lowest absolute price (in points) from flight cards, filtered
        by the configured check_fares filter and fare type.
        Uses 'price' instead of 'priceDifference' since these are absolute prices.
        """
        lowest = None

        for card in cards:
            fares = card.get("fares") or []
            for fare in fares:
                if fare.get("_meta", {}).get("fareProductId") != fare_type:
                    continue

                # Try absolute price fields; SW may use 'price' or 'farePrice'
                price_data = fare.get("price") or fare.get("farePrice")
                if price_data is None:
                    continue

                try:
                    amount_str = price_data.get("totalFare") or price_data.get("amount", "")
                    amount = int(str(amount_str).replace(",", ""))
                except (ValueError, TypeError):
                    continue

                if lowest is None or amount < lowest:
                    lowest = amount

        return lowest

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

        response = make_request("POST", site, self.headers, query, max_attempts=7)
        return response["changeShoppingPage"]["flights"][bound_page]["cards"], fare_type

    def _get_change_flight_page(self, reservation_info: JSON) -> tuple[JSON, list[JSON]]:
        fare_type_bounds = reservation_info["bounds"]

        # Ensure the flight does not have a companion pass connected to it
        # as companion passes are not supported.
        #self._check_for_companion(reservation_info) # COMMENTED THIS OUT BECAUSE IT PREVENTS CHECKING FARES FOR ANY RESERVATION WITH A COMPANION PASS, EVEN IF THE COMPANION PASS IS NOT CONNECTED TO THE FLIGHT BEING CHECKED

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
        time.sleep(2)

        response = make_request("GET", site, self.headers, change_link["query"], max_attempts=7)
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
        grey_box_message = reservation_info["greyBoxMessage"]
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
    if check_fares == CheckFaresOption.SAME_DAY:
        return any_flight_filter

    raise ValueError(f"check_fares value ({check_fares}) did not match any valid option")


def same_flight_filter(flight: Flight, flight_json: JSON) -> bool:
    return flight_json["flightNumbers"] == flight.flight_number


def any_flight_filter(*_) -> bool:
    return True


def nonstop_flight_filter(_, flight_json: JSON) -> bool:
    return flight_json["stopDescription"] == "Nonstop"
