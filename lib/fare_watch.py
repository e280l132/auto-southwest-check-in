"""
Checks a single unbooked route/date for a points price at or below a configured threshold.

Unlike FareChecker (which prices a change against a reservation you already hold), this always
prices a brand new one-way search via WebDriver.get_public_flight_prices, so it needs no
reservation, login, or paid-fare baseline -- only origin, destination, date, and a threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .fare_checker import (
    extract_cards_from_search_response,
    get_card_departure_time,
    get_card_points,
    get_card_stop_description,
    search_public_flights_with_retry,
)
from .log import get_logger
from .utils import TRANSIENT_ORIGIN_REJECTION, DriverTimeoutError, RequestError

if TYPE_CHECKING:
    from .config import FareWatchConfig

JSON = dict[str, Any]

logger = get_logger(__name__)


def cheapest_points(fares: JSON, fare_types: list[str] | None) -> int | None:
    """
    The cheapest price across the selected fare classes, or across every class sold when none are
    selected.

    Module-level so the web UI can re-derive it when the user changes their selection, without
    re-running a search (see webui/service.list_watches).
    """
    if fare_types:
        prices = [fares[fare_type] for fare_type in fare_types if fare_type in fares]
    else:
        prices = list(fares.values())

    return min(prices, default=None)


@dataclass
class FareWatchResult:
    """The outcome of checking one fare watch."""

    watch_id: str
    checked_at: str

    # One of: "hit", "no_hit", "unavailable", "error"
    status: str
    message: str = ""

    # True when the failure is Southwest's own origin rejecting the request -- routine, not a
    # configuration problem. See fare_checker.search_public_flights_with_retry.
    transient: bool = False

    rows: list[JSON] = field(default_factory=list)
    lowest_points: int | None = None


class FareWatchChecker:
    """Runs one FareWatchConfig against the public flight search."""

    def __init__(self, monitor: Any) -> None:
        # 'monitor' plays the same role FareWatchMonitor plays for CheckInScheduler that
        # ReservationMonitor plays for FareChecker: it only needs a .checkin_scheduler whose
        # WebDriver can be constructed (browser_path lives on
        # checkin_scheduler.reservation_monitor.config).
        self.monitor = monitor

    def check(self, watch: FareWatchConfig, checked_at: str) -> FareWatchResult:
        logger.info(
            "Checking fare watch %s (%s→%s on %s, max %s points)",
            watch.id,
            watch.origin,
            watch.destination,
            watch.date,
            f"{watch.max_points:,}",
        )

        try:
            response = search_public_flights_with_retry(
                self.monitor.checkin_scheduler, watch.origin, watch.destination, watch.date
            )
        except Exception as err:
            is_transient = (
                isinstance(err, RequestError) and err.southwest_code == TRANSIENT_ORIGIN_REJECTION
            )

            if isinstance(err, DriverTimeoutError):
                logger.error("Fare watch %s failed: %s", watch.id, err)
                return FareWatchResult(
                    watch_id=watch.id,
                    checked_at=checked_at,
                    status="error",
                    message="Webdriver timeout",
                )

            message = (
                "Southwest rejected every attempt. This is usually temporary."
                if is_transient
                else str(err)
            )
            logger.error("Fare watch %s failed: %s", watch.id, err)
            return FareWatchResult(
                watch_id=watch.id,
                checked_at=checked_at,
                status="error",
                message=message,
                transient=is_transient,
            )

        cards = extract_cards_from_search_response(response)
        if cards is None:
            return FareWatchResult(
                watch_id=watch.id,
                checked_at=checked_at,
                status="unavailable",
                message="Unexpected search response structure",
            )

        rows = self._build_rows(watch, cards)
        lowest_points = min(
            (row["points"] for row in rows if row["points"] is not None), default=None
        )
        status = "hit" if any(row["isHit"] for row in rows) else "no_hit"

        return FareWatchResult(
            watch_id=watch.id,
            checked_at=checked_at,
            status=status,
            rows=rows,
            lowest_points=lowest_points,
        )

    def _build_rows(self, watch: FareWatchConfig, cards: list[JSON]) -> list[JSON]:
        """
        Build one row per flight on the board.

        Every flight that survives the nonstop filter is included, whether or not the user has
        selected it: the selection is an alert filter, not a search filter, so the board keeps
        showing the full picture and the user can change their mind without re-running a check.
        'isTracked' carries the selection through to alerting.
        """
        rows = []
        seen_fare_types = set()

        for card in cards:
            card_numbers = card.get("flightNumbers") or []
            is_nonstop = "NONSTOP" in card.get("filterTags", [])

            if watch.nonstop_only and not is_nonstop:
                continue

            fares = self._card_fares(card)
            seen_fare_types.update(fares)

            points = self._cheapest_points(fares, watch.fare_types)
            is_tracked = not watch.flight_numbers or bool(
                set(card_numbers) & set(watch.flight_numbers)
            )

            rows.append(
                {
                    "flightNumbers": "​/​".join(card_numbers),
                    "displayNumber": "/".join(card_numbers),
                    "departureTime": get_card_departure_time(card),
                    "stopDescription": get_card_stop_description(card),
                    "isNonstop": is_nonstop,
                    "fares": fares,
                    "points": points,
                    "isTracked": is_tracked,
                    "isHit": is_tracked and points is not None and points <= watch.max_points,
                }
            )

        # The only place Southwest's real fare product ids surface. Everything downstream
        # discovers them from the data rather than hardcoding them, so this log is what to check
        # when adding to FARE_CLASS_LABELS.
        logger.debug("Fare products seen for watch %s: %s", watch.id, sorted(seen_fare_types))

        rows.sort(key=lambda row: row["departureTime"])
        return rows

    def _card_fares(self, card: JSON) -> JSON:
        """
        Every fare product sold on this flight, as {fareProductId: points}.

        Products that aren't priced in points (or are missing/malformed) are left out rather than
        stored as None, so the presence of a key means there is a real price behind it.
        """
        products = card.get("fareProducts", {}).get("ADULT", {})
        if not isinstance(products, dict):
            return {}

        fares = {}
        for fare_type in products:
            points = get_card_points(card, fare_type)
            if points is not None:
                fares[fare_type] = points

        return fares

    def _cheapest_points(self, fares: JSON, fare_types: list[str] | None) -> int | None:
        """The cheapest price across the watch's selected fare classes. See cheapest_points."""
        return cheapest_points(fares, fare_types)
