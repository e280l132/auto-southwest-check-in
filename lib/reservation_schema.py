"""
Translates a reservation from Southwest's www 'manage reservation' API into the shape the rest of
the script already speaks (the mobile view-reservation shape).

Why this exists: the mobile view-reservation endpoint the script has always used now rejects a
large share of requests with code 403050700, while the endpoint the live website itself uses
answers reliably. Measured back to back in the same browser session, the website's endpoint
returned 200 on 12/12 requests while the mobile one managed 4/12.

The two payloads are not equivalent, so this is a lossy translation:

  * The website's payload carries airport CODES but no airport names, so codes stand in for names.
    Notifications read "STL" rather than "St. Louis" when a reservation comes through this path.
  * It has no '_links', so there is no change link to drive a fare check with. Fare checks are
    skipped (not failed) for flights sourced this way, which is why the mobile endpoint is still
    tried first — see CheckInScheduler._get_reservation_info.

What it does carry, and what check-ins actually need, is complete: airports, flight numbers, and
departure times.
"""

from __future__ import annotations

from typing import Any

JSON = dict[str, Any]


def translate_manage_reservation(data: JSON) -> JSON:
    """
    Convert the 'data' object of a www manage-reservation response into a mobile-style
    reservation_info dict. Raises KeyError/ValueError if the payload isn't shaped as expected, so
    a silently-wrong translation can't reach the check-in scheduler.
    """
    bounds = [_translate_bound(bound) for bound in data["bounds"]]

    return {
        "bounds": bounds,
        # Which fare was paid depends on this, so it has to survive the translation. The mobile
        # payload only implies a companion through a grey box message; this one states it.
        "hasCompanion": any(
            reservation.get("type") == "COMPANION"
            for reservation in data.get("associated_reservations") or []
        ),
        # No change/reaccom links exist on this endpoint. Present-but-None keeps the fare checker
        # on its "cannot be changed online" path, which skips the check rather than erroring.
        "_links": {"change": None, "reaccom": None},
    }


def _translate_bound(bound: JSON) -> JSON:
    segments = bound.get("segments") or []
    if not segments:
        raise ValueError("Reservation bound has no segments")

    departure_date, departure_time = _split_local_departure(segments[0]["depart_at"])
    departure_code = bound["origination_airport_code"]
    destination_code = bound["destination_airport_code"]

    return {
        # Airport names aren't in this payload; the code is the most honest stand-in
        "departureAirport": {"code": departure_code, "name": departure_code},
        "arrivalAirport": {
            "code": destination_code,
            "name": destination_code,
            # Only used to decide whether to nag about passport details, and this payload states
            # it outright rather than implying it from a country field
            "country": destination_code if bound.get("international") else None,
        },
        "flights": [{"number": segment["flight_number"]} for segment in segments],
        "departureDate": departure_date,
        "departureTime": departure_time,
        # The fare checker uses this to pick the matching price out of a public search. This
        # payload calls it the fare family; if it doesn't line up with a searched fare, the check
        # reports no price rather than the wrong one.
        "fareProductDetails": {"fareProductId": segments[0].get("fare_family")},
    }


def _split_local_departure(depart_at: str) -> tuple[str, str]:
    """
    'depart_at' is already local to the departure airport and carries its offset
    ("2026-08-22T16:20:00.000-05:00"). The rest of the script re-derives the timezone from the
    airport code, so hand back the local date and time and drop the offset.
    """
    date_part, _, time_part = depart_at.partition("T")
    if not time_part:
        raise ValueError(f"Unrecognised departure timestamp: {depart_at!r}")

    return date_part, time_part[:5]
