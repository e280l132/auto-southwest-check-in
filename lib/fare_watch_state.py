"""
Tracks the last points price a fare watch alerted at, so the daemon alerts once when a flight
drops to or below the threshold and only again if it drops further -- never every cycle it stays
qualified. Modeled on webui/results_store.py's atomic-write pattern.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any

from .log import get_logger

JSON = dict[str, Any]

_data_dir = Path("/app/data") if os.environ.get("AUTO_SOUTHWEST_CHECK_IN_DOCKER") else Path(".")
STATE_FILE = _data_dir / "fare_watch_state.json"

logger = get_logger(__name__)


def flight_key(watch_date: str, display_number: str) -> str:
    return f"{watch_date}:{display_number}"


class FareWatchState:
    """
    Keyed by watch id, then by flight_key, storing the points price last alerted at:
    {watch_id: {flight_key: last_alerted_points}}
    """

    def __init__(self, filepath: Path = STATE_FILE) -> None:
        self._filepath = filepath
        self._lock = Lock()

    def should_alert(self, watch_id: str, key: str, points: int) -> bool:
        with self._lock:
            data = self._load()
            last = data.get(watch_id, {}).get(key)
            return last is None or points < last

    def record_alert(self, watch_id: str, key: str, points: int) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(watch_id, {})[key] = points
            self._save(data)

    def prune(self, active_watch_ids: set[str]) -> None:
        """Drop state for watches no longer in the config."""
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
                logger.debug("Could not read fare watch state file, starting fresh: %s", err)
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
            logger.error("Could not save fare watch state file: %s", err)
