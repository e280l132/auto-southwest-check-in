from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .log import get_logger
from .utils import CheckFaresOption, DriverTimeoutError, FlightChangeError, RequestError, make_request, time

if TYPE_CHECKING:
    from collections.abc import Callable

    from .flight import Flight
    from .ignore_manager import IgnoreManager
    from .reservation_monitor import ReservationMonitor
    from .webdriver import WebDriver

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

    def check_flight_price(self, flight: Flight) -> None:
        """
        Check if the price amount is negative (in either points or USD).
        If it is, send a notification to the user about the lower fare.

        For companion-pass flights where the change flow is unavailable, a
        direct change-page fetch is attempted using the known URL pattern.
        """
        logger.debug("Checking current price for flight")

        if self.reservation_monitor.config.check_fares == CheckFaresOption.SAME_DAY_SMART:
            self._check_all_alternate_fares(flight)
            return

        try:
            flight_price = self._get_flight_price(flight)
        except FlightChangeError as err:
            if self._is_companion_flight(flight) and not self._is_reaccommodated(flight):
                companion_fare_points = getattr(
                    self.reservation_monitor.config, "companion_fare_points", None
                )
                self._check_companion_fare_via_webdriver(flight, companion_fare_points)
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
          flightNumbers, displayNumber, departureTime, stopDescription, savings (amount/currencyCode)
        Sorted by savings amount ascending (biggest savings first).
        """
        flights, fare_type = self._get_matching_flights(flight)

        # Smart filter: match nonstop preference to the current flight's stop type
        if self._is_nonstop(flight):
            passes_filter = lambda f: f.get("stopDescription") == "Nonstop"  # noqa: E731
        else:
            passes_filter = lambda f: True  # noqa: E731

        cheaper = []
        for card in flights:
            if not passes_filter(card):
                continue
            fare = self._get_matching_fare(card["fares"], fare_type)
            if fare is None or fare["amount"] >= -1:
                # No fare available or false-positive -1 USD difference
                continue
            cheaper.append(
                {
                    "flightNumbers": card.get("flightNumbers", ""),
                    "displayNumber": card.get("flightNumbers", "").replace("\u200b", ""),
                    "departureTime": card.get("departureTime", ""),
                    "stopDescription": card.get("stopDescription", ""),
                    "savings": fare,
                }
            )

        cheaper.sort(key=lambda x: x["savings"]["amount"])
        return cheaper

    def _check_all_alternate_fares(self, flight: Flight) -> None:
        """
        same_day_smart entry point: find ALL cheaper same-day alternatives, filter out ignored
        flights, and send a single digest notification if any visible alternatives remain.
        """
        from .ignore_manager import IgnoreManager  # local import avoids circular dependency

        flight_date = flight._local_departure_time.strftime("%Y-%m-%d")
        conf = flight.confirmation_number
        ignore_manager = IgnoreManager()

        if ignore_manager.is_day_ignored(conf, flight_date):
            logger.info(
                "All alternate fares for flight %s on %s are ignored — skipping", conf, flight_date
            )
            return

        try:
            alternatives = self._get_all_cheaper_flights(flight)
        except FlightChangeError as err:
            if self._is_companion_flight(flight) and not self._is_reaccommodated(flight):
                companion_fare_points = getattr(
                    self.reservation_monitor.config, "companion_fare_points", None
                )
                self._check_companion_fare_via_webdriver(flight, companion_fare_points)
            else:
                logger.info(
                    "Skipping alternate fare check for flight %s: %s", conf, err
                )
            return
        except Exception as err:
            logger.error("Error checking alternate fares for flight %s: %s", conf, err)
            return

        visible = [
            a for a in alternatives
            if not ignore_manager.is_ignored(conf, flight_date, a["flightNumbers"])
        ]

        if not visible:
            logger.info(
                "Alternate fare check for flight %s on %s: no new cheaper alternatives "
                "(none found or all ignored)",
                conf,
                flight_date,
            )
            return

        port = self.reservation_monitor.config.ignore_server_port
        base_url = (
            self.reservation_monitor.config.ignore_server_base_url
            or f"http://localhost:{port}"
        )
        token = self.reservation_monitor.config.ignore_server_token
        self.reservation_monitor.notification_handler.alternate_fares(
            flight, visible, flight_date, base_url, token
        )

    def _bound_matches_flight(self, bound: JSON, flight: Flight) -> bool:
        """Return True if a reservation bound's flight number matches the given flight."""
        flights = bound.get("flights", [])
        flight_number = "\u200b/\u200b".join(f["number"].removeprefix("WN") for f in flights)
        return flight_number == flight.flight_number

    def _check_companion_fare_via_webdriver(
        self, flight: Flight, companion_fare_points: int | None
    ) -> None:
        """
        Last-resort companion fare check using a real browser session to load the public
        Southwest flight search page. The public search has no knowledge of companion-pass
        restrictions, so it returns normal points pricing for the route/date.

        The lowest points fare found is compared against companionFarePoints. If no
        companionFarePoints is configured, the current price is logged for reference.
        """
        from .webdriver import WebDriver

        # Find the bound that matches this flight to get route/date info
        bounds = flight.reservation_info.get("bounds", [])
        departure_date = None
        for bound in bounds:
            if self._bound_matches_flight(bound, flight):
                departure_date = bound.get("departureDate")
                break

        if departure_date is None:
            logger.error(
                "Companion webdriver fare check failed for %s: could not determine departure date.",
                flight.confirmation_number,
            )
            self._log_companion_unavailable(flight, companion_fare_points, reason="could not determine departure date")
            return

        origin = flight.departure_airport_code
        destination = flight.destination_airport_code

        logger.info(
            "Checking companion fare for flight %s via public search (route: %s→%s on %s)",
            flight.confirmation_number,
            origin,
            destination,
            departure_date,
        )

        max_attempts = 2
        response = None
        for attempt in range(max_attempts):
            try:
                webdriver = WebDriver(self.reservation_monitor.checkin_scheduler)
                response = webdriver.get_public_flight_prices(origin, destination, departure_date)
                break
            except DriverTimeoutError:
                if attempt < max_attempts - 1:
                    logger.debug(
                        "Webdriver search timed out for %s, retrying...",
                        flight.confirmation_number,
                    )
                else:
                    logger.error(
                        "Companion webdriver fare check timed out for %s after %d attempts.",
                        flight.confirmation_number,
                        max_attempts,
                    )
                    self._log_companion_unavailable(
                        flight, companion_fare_points, reason="webdriver timeout"
                    )
                    return
            except Exception as err:
                logger.error(
                    "Companion webdriver fare check failed for %s: %s",
                    flight.confirmation_number,
                    err,
                )
                self._log_companion_unavailable(flight, companion_fare_points, reason=str(err))
                return

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
            self._log_companion_unavailable(flight, companion_fare_points, reason="unexpected search response structure")
            return

        # Determine the fare type from the reservation bounds
        bounds = flight.reservation_info.get("bounds", [])
        fare_type = None
        for bound in bounds:
            if self._bound_matches_flight(bound, flight):
                fare_type = bound.get("fareProductDetails", {}).get("fareProductId")
                break

        if fare_type is None:
            logger.error(
                "Companion webdriver fare check failed for %s: could not determine fare type.",
                flight.confirmation_number,
            )
            return

        # The public search returns absolute prices, not priceDifference.
        # Find the lowest points fare for the matching flight(s) and fare type.
        lowest_points = self._get_lowest_points_from_cards(cards, fare_type, flight)

        if lowest_points is None:
            logger.info(
                "Companion fare check for flight %s: no %s points fare available in public search results.",
                flight.confirmation_number,
                fare_type,
            )
            return

        # same_day_smart: find all cheaper alternate flights, not just the same flight
        if self.reservation_monitor.config.check_fares == CheckFaresOption.SAME_DAY_SMART:
            if companion_fare_points is not None:
                self._check_companion_alternate_fares(
                    flight, cards, fare_type, companion_fare_points, departure_date
                )
            else:
                logger.info(
                    "Companion alternate fare check for flight %s: set 'companionFarePoints' "
                    "in config to enable same_day_smart alternate fare checking.",
                    flight.confirmation_number,
                )
            return

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
            else:
                logger.info(
                    "Companion fare check for flight %s: %s (no lower fare found)",
                    flight.confirmation_number,
                    price_info,
                )
        else:
            logger.info(
                "Companion fare check for flight %s: current %s fare is %s PTS. "
                "Set 'companionFarePoints' in config to detect lower fares.",
                flight.confirmation_number,
                fare_type,
                f"{lowest_points:,}",
            )

    def _check_companion_alternate_fares(
        self,
        flight: Flight,
        cards: list[JSON],
        fare_type: str,
        companion_fare_points: int,
        flight_date: str,
    ) -> None:
        """
        Find all cheaper alternate flights for a companion-pass flight using public search cards.
        Called from _check_companion_fare_via_webdriver when check_fares == SAME_DAY_SMART.

        Unlike the change-shopping API (which returns priceDifference), the public search returns
        absolute prices. Savings are computed as card_price - companion_fare_points.
        """
        from .ignore_manager import IgnoreManager

        conf = flight.confirmation_number
        ignore_manager = IgnoreManager()

        if ignore_manager.is_day_ignored(conf, flight_date):
            logger.info(
                "All alternate fares for companion flight %s on %s are ignored", conf, flight_date
            )
            return

        # Smart nonstop filter: match the current flight's stop preference
        if self._is_nonstop(flight):
            def passes_filter(card: JSON) -> bool:
                return "NONSTOP" in card.get("filterTags", [])
        else:
            def passes_filter(card: JSON) -> bool:
                return True

        alternatives = []
        for card in cards:
            if not passes_filter(card):
                continue

            card_nums = card.get("flightNumbers", [])

            # Get the absolute points price
            try:
                total_fare = card["fareProducts"]["ADULT"][fare_type]["fare"]["totalFare"]
            except (KeyError, TypeError):
                continue

            if total_fare.get("currencyCode") != "POINTS":
                continue

            try:
                price = int(str(total_fare.get("value", "")).replace(",", ""))
            except (ValueError, TypeError):
                continue

            savings_amount = price - companion_fare_points
            if savings_amount >= -1:
                continue  # Not genuinely cheaper

            # Build flight number strings
            flight_numbers_str = "\u200b/\u200b".join(card_nums)
            display_number = "/".join(card_nums)

            # Extract departure time — public search may use departureTime or departureDateTime
            dep_time = card.get("departureTime") or card.get("departureDateTime", "")
            if "T" in dep_time:
                dep_time = dep_time.split("T")[1][:5]

            stop_desc = card.get("stopDescription", "")

            alternatives.append(
                {
                    "flightNumbers": flight_numbers_str,
                    "displayNumber": display_number,
                    "departureTime": dep_time,
                    "stopDescription": stop_desc,
                    "savings": {"amount": savings_amount, "currencyCode": "PTS"},
                }
            )

        alternatives.sort(key=lambda x: x["savings"]["amount"])

        visible = [
            a
            for a in alternatives
            if not ignore_manager.is_ignored(conf, flight_date, a["flightNumbers"])
        ]

        if not visible:
            logger.info(
                "Companion alternate fare check for flight %s on %s: no new cheaper alternatives "
                "(none found or all ignored)",
                conf,
                flight_date,
            )
            return

        port = self.reservation_monitor.config.ignore_server_port
        base_url = (
            self.reservation_monitor.config.ignore_server_base_url
            or f"http://localhost:{port}"
        )
        token = self.reservation_monitor.config.ignore_server_token
        self.reservation_monitor.notification_handler.alternate_fares(
            flight, visible, flight_date, base_url, token
        )

    def _extract_cards_from_search_response(self, response: JSON) -> list[JSON] | None:
        """Extract flight cards from the public search API response."""
        try:
            return response["data"]["searchResults"]["airProducts"][0]["details"]
        except KeyError as err:
            logger.debug("Public search response missing expected key: %s", err)
            return None
        except (TypeError, IndexError) as err:
            logger.debug("Public search response has unexpected structure: %s", err)
            return None

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

    def _log_companion_unavailable(self, flight: Flight, companion_fare_points: int | None, reason: str = "") -> None:
        """Log a clear INFO message when companion fare checking is unavailable."""
        suffix = f" ({reason})" if reason else ""
        if companion_fare_points is not None:
            logger.info(
                "Companion fare check for flight %s is unavailable%s. "
                "Paid fare: %s points. Cannot determine if a lower fare exists.",
                flight.confirmation_number,
                suffix,
                f"{companion_fare_points:,}",
            )
        else:
            logger.info(
                "Companion fare check for flight %s is unavailable%s. "
                "Set 'companionFarePoints' in the reservation config to enable fare tracking.",
                flight.confirmation_number,
                suffix,
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
        logger.debug("changeFlightPage URL: %s", site)
        logger.debug("changeFlightPage query params: %s", change_link["query"])
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
