"""
Reads and writes the 'reservations' section of config.json on behalf of the web UI.

Deliberately scoped to reservations only. 'accounts' holds Southwest credentials and
'notifications' holds Apprise URLs that can embed tokens; neither is ever read into a view model
or rewritten here. Saving preserves every other key in the file untouched.

Validation reuses ReservationConfig.parse (lib/config.py) rather than restating the rules, so the
web UI and the CLI accept exactly the same input.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from typing import TYPE_CHECKING, Any

from ..config import ReservationConfig
from ..log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

JSON = dict[str, Any]

logger = get_logger(__name__)

# The only reservation keys the web UI is allowed to write
EDITABLE_FIELDS = (
    "confirmationNumber",
    "firstName",
    "lastName",
    "check_fares",
    "companionFarePoints",
    "originalFarePoints",
    "originalTaxesFees",
)


def read_config(config_path: Path) -> JSON:
    """Read the raw config file. Returns an empty dict if it doesn't exist yet."""
    if not config_path.exists():
        return {}

    config = json.loads(config_path.read_text())
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON dictionary")

    return config


def read_reservations(config_path: Path) -> list[JSON]:
    """Read the raw 'reservations' list, without any Config parsing."""
    reservations = read_config(config_path).get("reservations", [])
    return reservations if isinstance(reservations, list) else []


def validate_reservation(reservation: JSON) -> None:
    """
    Validate a reservation using the real config parser. Raises ConfigError on bad input.

    Config.parse only reads the keys it knows about and raises rather than exiting, which makes
    it safe to call inside a web request.
    """
    ReservationConfig().parse(reservation)


def write_reservations(config_path: Path, reservations: list[JSON]) -> None:
    """
    Replace only the 'reservations' key and write the file back atomically.

    Every other key (accounts, notifications, $schema, global settings) is carried over
    unmodified, and json.load preserves key order so the file's layout is stable.
    """
    config = read_config(config_path)
    config["reservations"] = reservations

    # Write to a temp file in the same directory so os.replace is atomic on the same filesystem,
    # which keeps a crash mid-write from truncating the user's config.
    directory = config_path.parent
    with tempfile.NamedTemporaryFile(
        "w", dir=directory, prefix=f".{config_path.name}.", suffix=".tmp", delete=False
    ) as tmp:
        json.dump(config, tmp, indent=4)
        tmp.write("\n")
        tmp_path = tmp.name

    try:
        os.replace(tmp_path, config_path)
    except OSError as err:
        if err.errno != errno.EBUSY:
            raise
        # config_path is likely a single-file bind mount (common in Docker -- see the Docker
        # Compose examples in the README), which the kernel refuses to rename over since the
        # path itself is a mount point. Fall back to an in-place, non-atomic write instead.
        logger.debug(
            "Could not atomically replace %s (resource busy); writing in place instead",
            config_path,
        )
        with open(tmp_path, encoding="utf-8") as tmp_file:
            content = tmp_file.read()
        config_path.write_text(content)
        os.unlink(tmp_path)

    logger.debug("Wrote %d reservations to the configuration file", len(reservations))


def update_cached_flight(
    config_path: Path,
    confirmation_number: str,
    *,
    flight_number: str,
    departure_airport_code: str,
    destination_airport_code: str,
    local_departure_date: str,
) -> bool:
    """
    Persist the last known flight identity (not fare) for a reservation, so the web UI can show
    the route/flight number/date without re-running a check. Purely cosmetic cache: the check-in
    daemon never reads these fields, so this has no effect on monitoring or check-in behavior.

    Returns whether anything actually changed, so the caller can skip a config reload when the
    values already matched (e.g. re-checking a flight whose schedule hasn't changed).
    """
    new_values = {
        "cachedFlightNumber": flight_number,
        "cachedDepartureAirportCode": departure_airport_code,
        "cachedDestinationAirportCode": destination_airport_code,
        "cachedLocalDepartureDate": local_departure_date,
    }

    reservations = read_reservations(config_path)
    changed = False
    updated = []
    for reservation in reservations:
        if reservation.get("confirmationNumber") == confirmation_number and any(
            reservation.get(key) != value for key, value in new_values.items()
        ):
            reservation = {**reservation, **new_values}
            changed = True
        updated.append(reservation)

    if changed:
        write_reservations(config_path, updated)

    return changed


def rename_key(reservation: JSON, old_key: str, new_key: str) -> JSON:
    """
    Rename a key in place, preserving its value and position — used to fix a known typo (e.g.
    'checkFares' -> 'check_fares') without disturbing anything else in the entry.

    A no-op if old_key isn't present, so a stale form submission can't rename the wrong thing.
    """
    if old_key not in reservation:
        return reservation

    return {(new_key if key == old_key else key): value for key, value in reservation.items()}


def build_reservation(form: dict[str, str]) -> JSON:
    """
    Build a reservation dict from submitted form fields, keeping only editable keys and omitting
    blanks so optional settings stay absent from the file rather than being written as null.
    """
    reservation: JSON = {}

    for field in ("confirmationNumber", "firstName", "lastName", "check_fares"):
        value = (form.get(field) or "").strip()
        if value:
            reservation[field] = value

    for field in ("companionFarePoints", "originalFarePoints"):
        value = (form.get(field) or "").strip()
        if value:
            reservation[field] = parse_number(field, value, as_int=True)

    taxes = (form.get("originalTaxesFees") or "").strip()
    if taxes:
        reservation["originalTaxesFees"] = parse_number("originalTaxesFees", taxes, as_int=False)

    return reservation


def merge_reservation(stored: JSON, submitted: JSON) -> JSON:
    """
    Apply submitted form values onto the stored reservation.

    Editable fields are taken from the form (absent means "cleared", so optional settings can be
    removed). Every other key in the stored entry is carried over untouched, so anything the
    parser doesn't recognize — a hand-added key, or a typo like 'checkFares' — survives a UI save
    rather than being silently deleted.
    """
    merged = {key: value for key, value in stored.items() if key not in EDITABLE_FIELDS}
    merged.update(submitted)

    # Preserve the original key order where possible so the file stays readable
    ordered = {key: merged[key] for key in stored if key in merged}
    ordered.update(merged)
    return ordered


def parse_number(field: str, value: str, *, as_int: bool) -> int | float:
    """Convert a form value to a number, raising a readable error the UI can show."""
    try:
        return int(value) if as_int else float(value)
    except ValueError as err:
        expected = "a whole number" if as_int else "a number"
        raise ValueError(f"'{field}' must be {expected}") from err
