"""
Remembers what each watched flight has cost on previous checks, so the board can show whether a
price is actually a good one ("15,500" means nothing without "was 18,000 yesterday, never below
15,500") rather than just what it is right now.

Separate from lib/fare_watch_state.py on purpose: that file exists to stop duplicate alerts and is
only read by the daemon, while this is display data both the daemon and the web UI write. Same
atomic-write pattern as that module.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any

from .fare_watch_state import flight_key
from .log import get_logger

JSON = dict[str, Any]

_data_dir = Path("/app/data") if os.environ.get("AUTO_SOUTHWEST_CHECK_IN_DOCKER") else Path(".")
HISTORY_FILE = _data_dir / "fare_watch_history.json"

# How many past prices to keep per flight. Enough to see a trend over a couple of weeks of
# twice-daily checks without the file growing without bound.
MAX_HISTORY_ENTRIES = 30

logger = get_logger(__name__)


class FareWatchHistory:
    """
    Keyed by watch id, then by flight_key:
    {watch_id: {flight_key: {"lowest": int, "last": int, "history": [[iso_ts, points], ...]}}}
    """

    def __init__(self, filepath: Path = HISTORY_FILE) -> None:
        self._filepath = filepath
        self._lock = Lock()

    def annotate(self, watch_id: str, rows: list[JSON]) -> None:
        """
        Attach each row's previous price, the change since then, and the lowest price ever seen.

        Must be called before record() for the same check, or every delta compares a price against
        itself and comes out as zero.
        """
        with self._lock:
            watch_history = self._load().get(watch_id, {})

        for row in rows:
            entry = watch_history.get(row["_historyKey"], {})
            previous = entry.get("last")
            lowest = entry.get("lowest")
            points = row.get("points")

            row["previousPoints"] = previous
            row["delta"] = (
                points - previous if points is not None and previous is not None else None
            )
            # Before the first record() the lowest ever seen is simply today's price
            row["lowestEver"] = min(
                [value for value in (lowest, points) if value is not None], default=None
            )

    def record(self, watch_id: str, rows: list[JSON], checked_at: str) -> None:
        """Store this check's prices, updating each flight's running low."""
        with self._lock:
            data = self._load()
            watch_history = data.setdefault(watch_id, {})

            for row in rows:
                points = row.get("points")
                if points is None:
                    continue

                entry = watch_history.setdefault(
                    row["_historyKey"], {"lowest": points, "last": points, "history": []}
                )
                entry["last"] = points
                entry["lowest"] = min(entry.get("lowest", points), points)
                entry["history"].append([checked_at, points])
                entry["history"] = entry["history"][-MAX_HISTORY_ENTRIES:]

            self._save(data)

    def prune(self, active_watch_ids: set[str]) -> None:
        """Drop history for watches no longer in the config."""
        with self._lock:
            data = self._load()
            pruned = {
                watch_id: flights
                for watch_id, flights in data.items()
                if watch_id in active_watch_ids
            }
            if pruned != data:
                self._save(pruned)

    def _load(self) -> JSON:
        if self._filepath.exists():
            try:
                raw = json.loads(self._filepath.read_text())
                if isinstance(raw, dict):
                    return raw
            except (json.JSONDecodeError, OSError) as err:
                logger.debug("Could not read fare watch history file, starting fresh: %s", err)
        return {}

    def _save(self, data: JSON) -> None:
        try:
            directory = self._filepath.parent
            with tempfile.NamedTemporaryFile(
                "w", dir=directory, prefix=f".{self._filepath.name}.", suffix=".tmp", delete=False
            ) as tmp:
                json.dump(data, tmp, indent=2)
                tmp_path = tmp.name

            try:
                os.replace(tmp_path, self._filepath)
            except OSError as err:
                if err.errno != errno.EBUSY:
                    raise
                logger.debug(
                    "Could not atomically replace %s (resource busy); writing in place instead",
                    self._filepath,
                )
                with open(tmp_path, encoding="utf-8") as tmp_file:
                    content = tmp_file.read()
                self._filepath.write_text(content)
                os.unlink(tmp_path)
        except OSError as err:
            logger.error("Could not save fare watch history file: %s", err)


def add_price_history(watch_id: str, watch_date: str, rows: list[JSON], checked_at: str) -> None:
    """
    Annotate rows with their price trend and then record today's prices.

    Shared by the daemon (FareWatchMonitor) and the web UI (webui/watch_runner) so both produce
    identically-shaped rows; FareWatchChecker itself stays free of file I/O.
    """
    for row in rows:
        row["_historyKey"] = flight_key(watch_date, row["displayNumber"])

    history = FareWatchHistory()
    history.annotate(watch_id, rows)
    history.record(watch_id, rows, checked_at)

    for row in rows:
        del row["_historyKey"]
