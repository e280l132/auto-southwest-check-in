"""Result type returned by FareChecker so callers (CLI daemon and web UI) can both consume it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JSON = dict[str, Any]


@dataclass
class FareCheckResult:
    """
    The outcome of a single fare check for one flight. Notifications and logging are still done
    by FareChecker/NotificationHandler as before; this is purely a summary for display purposes.
    """

    confirmation_number: str
    flight_number: str
    departure_airport_code: str
    destination_airport_code: str
    local_departure_date: str
    display_time: str
    is_companion: bool

    # One of: "lower_fare", "no_lower_fare", "unavailable", "skipped", "error"
    status: str

    message: str = ""

    # Populated for same_flight/same_day/same_day_nonstop checks (priceDifference from Southwest)
    currency_code: str | None = None
    difference_amount: int | None = None

    # Populated for companion-pass checks (absolute points, not a difference)
    current_points: int | None = None
    paid_points: int | None = None

    # Populated for same_day_smart checks (list of cheaper alternate flight dicts). This is the
    # subset that drives notifications and ignore links, so its contents are behavior-relevant.
    alternatives: list[JSON] = field(default_factory=list)

    # The fare product the reservation was booked in (e.g. "WANNA_GET_AWAY"), when known
    fare_type: str | None = None

    # Every same-day flight found, cheaper or not, for display purposes. See
    # FareChecker._build_change_board / _build_public_board for the entry shape.
    board: list[JSON] = field(default_factory=list)
