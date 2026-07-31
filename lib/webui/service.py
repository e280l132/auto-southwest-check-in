"""
Adapts existing config/fare-check data into UI-friendly view models.

This module does no fare searching, comparison, or companion logic of its own — it only
reshapes ReservationConfig and FareCheckResult objects (both defined elsewhere) for display,
and computes simple presentation-only savings math from numbers those objects already carry.

Every dict returned from this module is what actually reaches the browser, so it is also the
one place responsible for never leaking secrets: notification URLs, account username/password,
request headers, or raw Southwest reservation JSON must never be added to a view model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import GlobalConfig, ReservationConfig
    from ..fare_check_result import FareCheckResult
    from .results_store import ResultsStore

JSON = dict[str, Any]

# Every key a rendered flight view is expected to have. Cached results are validated against this
# rather than a hand-maintained version number, so adding a field to _result_view automatically
# invalidates older caches instead of 500ing the page on a missing key. A test asserts this stays
# in sync with what _result_view actually produces.
FLIGHT_VIEW_KEYS = frozenset(
    {
        "flight_number",
        "route",
        "departure_airport_code",
        "destination_airport_code",
        "local_departure_date",
        "display_time",
        "is_companion",
        "status",
        "message",
        "fare_type",
        "paid_points",
        "paid_taxes_fees",
        "current_fare_points",
        "difference_amount",
        "savings_amount",
        "savings_currency",
        "board",
        "cheaper_count",
        "alternatives",
    }
)


def _is_renderable(last_check: JSON) -> bool:
    """
    Whether a cached check payload has the shape the templates expect.

    Payloads written by an older version are discarded rather than rendered, so a stale cache can
    never break the page — the user just re-runs the check.
    """
    flights = last_check.get("flights")
    if not isinstance(flights, list):
        return False

    return all(isinstance(flight, dict) and FLIGHT_VIEW_KEYS <= flight.keys() for flight in flights)


def resolve_paid_points(reservation_config: ReservationConfig) -> tuple[int | None, str | None]:
    """
    Determine the points originally paid for a reservation and where that figure came from.

    'originalFarePoints' (if configured) is preferred since it applies to any reservation.
    Otherwise, for companion-pass reservations, 'companionFarePoints' is used since that is
    what the companion fare check compares against.
    """
    if reservation_config.original_fare_points is not None:
        return reservation_config.original_fare_points, "original"
    if reservation_config.companion_fare_points is not None:
        return reservation_config.companion_fare_points, "companion"
    return None, None


def cached_flight_summary(reservation_config: ReservationConfig) -> JSON | None:
    """
    The last known flight identity (route/number/date) written to config.json after a check, or
    None if nothing has been cached yet. Purely cosmetic -- no fare data, since that's never
    persisted to config.json.
    """
    if not (
        reservation_config.cached_flight_number
        and reservation_config.cached_departure_airport_code
        and reservation_config.cached_destination_airport_code
        and reservation_config.cached_local_departure_date
    ):
        return None

    return {
        "flight_number": reservation_config.cached_flight_number,
        "route": (
            f"{reservation_config.cached_departure_airport_code} → "
            f"{reservation_config.cached_destination_airport_code}"
        ),
        "local_departure_date": reservation_config.cached_local_departure_date,
    }


def reservation_summary(reservation_config: ReservationConfig) -> JSON:
    """Reservation-level view model, available immediately without running any fare check."""
    paid_points, paid_points_source = resolve_paid_points(reservation_config)
    unknown_keys = list(getattr(reservation_config, "unknown_keys", []))
    corrections = getattr(reservation_config, "KEY_CORRECTIONS", {})

    return {
        "confirmation_number": reservation_config.confirmation_number,
        "first_name": reservation_config.first_name,
        "last_name": reservation_config.last_name,
        "check_fares": reservation_config.check_fares.value,
        "is_companion_configured": reservation_config.companion_fare_points is not None,
        "companion_fare_points": reservation_config.companion_fare_points,
        "original_fare_points": reservation_config.original_fare_points,
        "original_taxes_fees": reservation_config.original_taxes_fees,
        "paid_points": paid_points,
        "paid_points_source": paid_points_source,
        "cached_flight": cached_flight_summary(reservation_config),
        # Config keys the parser doesn't understand, so a typo like 'checkFares' is visible
        # instead of silently having no effect
        "unknown_keys": unknown_keys,
        # The subset of unknown_keys with a known, unambiguous correction — safe to offer a
        # one-click "rename" fix for in the UI
        "fixable_keys": {key: corrections[key] for key in unknown_keys if key in corrections},
    }


def list_reservations(config: GlobalConfig, results_store: ResultsStore) -> list[JSON]:
    """
    Build the main page's view models: one per configured reservation, merged with the last
    known fare-check result (if any) for that reservation.
    """
    all_results = results_store.get_all()
    views = []
    for reservation_config in config.reservations:
        view = reservation_summary(reservation_config)
        last_check = all_results.get(reservation_config.confirmation_number)

        if last_check is not None and not _is_renderable(last_check):
            # Written by an older version whose shape the templates no longer understand
            last_check = None

        view["last_check"] = last_check
        views.append(view)
    return views


def build_summary(reservations: list[JSON]) -> JSON:
    """
    Aggregate the dashboard's headline numbers from the reservation views.

    'best_savings' is the largest single drop found across all flights, expressed as a positive
    number of points, since that is the figure worth acting on.
    """
    checks = [view["last_check"] for view in reservations if view["last_check"]]
    flights = [flight for check in checks for flight in check.get("flights", [])]

    cheaper_flights = [f for f in flights if f.get("cheaper_count")]
    drops = [-f["difference_amount"] for f in flights if (f.get("difference_amount") or 0) < 0]
    checked_times = [check["checked_at"] for check in checks]

    return {
        "tracked_count": len(reservations),
        "checked_count": len(checks),
        "cheaper_count": len(cheaper_flights),
        "best_savings": max(drops, default=0),
        "last_checked": max(checked_times, default=None),
        "needs_paid_fare": sum(1 for view in reservations if view["paid_points"] is None),
    }


def _absolute_points(
    points: int | None, difference: int | None, paid_points: int | None
) -> int | None:
    """
    Resolve an absolute points price. The public search reports one directly; the change-shopping
    API only reports a difference from what was paid, so derive it when the paid fare is known.
    """
    if points is not None:
        return points
    if difference is not None and paid_points is not None:
        return paid_points + difference
    return None


def _board_row(entry: JSON, paid_points: int | None) -> JSON:
    """
    Turn one FareChecker board entry into a display row. Only presentation math happens here —
    whether a flight counts as cheaper was already decided by FareChecker.
    """
    difference = entry.get("difference")

    return {
        "flight_number": entry.get("displayNumber", ""),
        "departure_time": entry.get("departureTime", ""),
        "stop_description": entry.get("stopDescription", ""),
        "is_current": bool(entry.get("isCurrent")),
        "is_cheaper": bool(entry.get("isCheaper")),
        "is_nonstop": bool(entry.get("isNonstop")),
        "unavailable": bool(entry.get("unavailable")),
        "points": _absolute_points(entry.get("points"), difference, paid_points),
        "difference": difference,
        # A negative difference means the current fare is cheaper (a savings).
        "savings": None if difference is None else -difference,
        "currency_code": entry.get("currencyCode"),
    }


def _result_view(
    result: FareCheckResult, paid_points: int | None, paid_taxes_fees: float | None = None
) -> JSON:
    """
    Build a display view for a single FareCheckResult, including the full same-day board so the
    UI can show every flight that day rather than only the cheaper ones.
    """
    paid_points_for_flight = result.paid_points if result.paid_points is not None else paid_points

    board = [_board_row(entry, paid_points_for_flight) for entry in result.board]
    current_row = next((row for row in board if row["is_current"]), None)

    # Prefer the current flight's own board entry; fall back to the top-level result for modes
    # that report a price without producing a board.
    current_fare_points = _absolute_points(
        result.current_points, result.difference_amount, paid_points_for_flight
    )
    savings_amount = None if result.difference_amount is None else -result.difference_amount
    savings_currency = result.currency_code

    if current_row is not None:
        current_fare_points = current_row["points"]
        savings_amount = current_row["savings"]
        savings_currency = current_row["currency_code"] or savings_currency

    return {
        "flight_number": result.flight_number,
        "route": f"{result.departure_airport_code} → {result.destination_airport_code}",
        "departure_airport_code": result.departure_airport_code,
        "destination_airport_code": result.destination_airport_code,
        "local_departure_date": result.local_departure_date,
        "display_time": result.display_time,
        "is_companion": result.is_companion,
        "status": result.status,
        "message": result.message,
        "transient": result.transient,
        "fare_type": result.fare_type,
        "paid_points": paid_points_for_flight,
        "paid_taxes_fees": paid_taxes_fees,
        "current_fare_points": current_fare_points,
        # Difference is shown the way Southwest and the notifications express it: negative means
        # today's fare is cheaper than what was paid. 'savings' is the same number negated.
        "difference_amount": None if savings_amount is None else -savings_amount,
        "savings_amount": savings_amount,
        "savings_currency": savings_currency,
        "board": board,
        "cheaper_count": sum(1 for row in board if row["is_cheaper"] and not row["is_current"]),
        "alternatives": result.alternatives,
    }


def build_check_payload(
    reservation_config: ReservationConfig,
    results: list[FareCheckResult],
    *,
    checked_at: str,
    error: str | None = None,
    transient: bool = False,
) -> JSON:
    """
    Build the payload persisted to ResultsStore (and returned to the UI) after running a fare
    check for one reservation. 'error' is set when the check couldn't even retrieve flights
    (e.g. header refresh failed) so the UI can show a clear error instead of an empty list.
    'transient' marks an error as Southwest rejecting the request rather than anything the user
    can fix, so the UI can avoid pointing them at their own reservation details.
    """
    paid_points, _source = resolve_paid_points(reservation_config)
    paid_taxes_fees = reservation_config.original_taxes_fees

    return {
        "checked_at": checked_at,
        "error": error,
        "transient": transient,
        # Reservations are one-way, so the reservation's paid fare is this flight's paid fare
        "flights": [_result_view(result, paid_points, paid_taxes_fees) for result in results],
    }
