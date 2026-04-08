from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from threading import Lock

from .log import get_logger

IGNORE_FILE = Path("ignored_flights.json")

logger = get_logger(__name__)


class IgnoreManager:
    """
    Persistent storage for user-ignored alternate flights.

    Entries are keyed by (confirmation, flight_date, alt_flight_number) for
    per-flight ignores, or (confirmation, flight_date) for day-level ignores.

    The file is reloaded from disk on every read so that concurrent processes
    (each reservation monitor runs in its own process) pick up each other's writes.
    """

    def __init__(self, filepath: Path = IGNORE_FILE) -> None:
        self._filepath = filepath
        self._lock = Lock()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def ignore_flight(self, confirmation: str, flight_date: str, alt_flight: str) -> None:
        """Suppress future notifications for a specific alternate flight on a given date."""
        with self._lock:
            data = self._load()
            entry = {"confirmation": confirmation, "date": flight_date, "flight": alt_flight}
            if entry not in data["specific"]:
                data["specific"].append(entry)
                logger.debug(
                    "Ignoring alternate flight %s for %s on %s", alt_flight, confirmation, flight_date
                )
            self._cleanup(data)
            self._save(data)

    def ignore_all_day(self, confirmation: str, flight_date: str) -> None:
        """Suppress future notifications for ALL alternate flights on a given date."""
        with self._lock:
            data = self._load()
            entry = {"confirmation": confirmation, "date": flight_date}
            if entry not in data["all_day"]:
                data["all_day"].append(entry)
                logger.debug(
                    "Ignoring all alternates for %s on %s", confirmation, flight_date
                )
            self._cleanup(data)
            self._save(data)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def is_ignored(self, confirmation: str, flight_date: str, alt_flight: str) -> bool:
        """Return True if this specific alternate flight (or the whole day) is ignored."""
        data = self._load()
        if self._is_day_ignored_in(data, confirmation, flight_date):
            return True
        return any(
            e["confirmation"] == confirmation
            and e["date"] == flight_date
            and e["flight"] == alt_flight
            for e in data["specific"]
        )

    def is_day_ignored(self, confirmation: str, flight_date: str) -> bool:
        """Return True if all alternates for this confirmation/date are ignored."""
        data = self._load()
        return self._is_day_ignored_in(data, confirmation, flight_date)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_day_ignored_in(self, data: dict, confirmation: str, flight_date: str) -> bool:
        return any(
            e["confirmation"] == confirmation and e["date"] == flight_date
            for e in data["all_day"]
        )

    def _load(self) -> dict:
        if self._filepath.exists():
            try:
                raw = json.loads(self._filepath.read_text())
                if isinstance(raw, dict):
                    raw.setdefault("specific", [])
                    raw.setdefault("all_day", [])
                    return raw
            except (json.JSONDecodeError, OSError) as err:
                logger.debug("Could not read ignore file, starting fresh: %s", err)
        return {"specific": [], "all_day": []}

    def _save(self, data: dict) -> None:
        try:
            self._filepath.write_text(json.dumps(data, indent=2))
        except OSError as err:
            logger.error("Could not save ignore file: %s", err)

    def _cleanup(self, data: dict) -> None:
        """Remove entries whose flight date has already passed."""
        today = date.today().isoformat()
        data["specific"] = [e for e in data["specific"] if e["date"] >= today]
        data["all_day"] = [e for e in data["all_day"] if e["date"] >= today]
