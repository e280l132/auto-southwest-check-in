"""
Persistent storage for the last known fare-check result per reservation, so the web UI's
main page has data to show across restarts. Modeled directly on lib/ignore_manager.py.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any

from ..log import get_logger

JSON = dict[str, Any]

_data_dir = Path("/app/data") if os.environ.get("AUTO_SOUTHWEST_CHECK_IN_DOCKER") else Path(".")
RESULTS_FILE = _data_dir / "webui_last_results.json"

logger = get_logger(__name__)


class ResultsStore:
    """Keyed by confirmation number. Stores only view-safe fields (see webui/service.py)."""

    def __init__(self, filepath: Path = RESULTS_FILE) -> None:
        self._filepath = filepath
        self._lock = Lock()

    def save_result(self, confirmation_number: str, view_model: JSON) -> None:
        with self._lock:
            data = self._load()
            data[confirmation_number] = view_model
            self._save(data)

    def delete_result(self, confirmation_number: str) -> None:
        """Drop a reservation's cached result, used when the reservation is removed."""
        with self._lock:
            data = self._load()
            if data.pop(confirmation_number, None) is not None:
                self._save(data)

    def get_result(self, confirmation_number: str) -> JSON | None:
        with self._lock:
            data = self._load()
            return data.get(confirmation_number)

    def get_all(self) -> JSON:
        with self._lock:
            return self._load()

    def clear_all(self) -> None:
        """Drop every cached result, e.g. when the browser loads a fresh (non-post-check) page."""
        with self._lock:
            self._save({})

    def _load(self) -> JSON:
        if self._filepath.exists():
            try:
                raw = json.loads(self._filepath.read_text())
                if isinstance(raw, dict):
                    return raw
            except (json.JSONDecodeError, OSError) as err:
                logger.debug("Could not read results file, starting fresh: %s", err)
        return {}

    def _save(self, data: JSON) -> None:
        try:
            if self._filepath.is_dir():
                shutil.rmtree(self._filepath)

            # Write to a temp file in the same directory so os.replace is atomic on the same
            # filesystem, which keeps a crash mid-write (or a concurrent read, now that get_result/
            # get_all take the same lock) from ever observing a torn write.
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
                # self._filepath is likely a single-file bind mount (as config.json can be in
                # Docker), which the kernel refuses to rename over. Fall back to an in-place,
                # non-atomic write instead.
                logger.debug(
                    "Could not atomically replace %s (resource busy); writing in place instead",
                    self._filepath,
                )
                with open(tmp_path, encoding="utf-8") as tmp_file:
                    content = tmp_file.read()
                self._filepath.write_text(content)
                os.unlink(tmp_path)
        except OSError as err:
            logger.error("Could not save web UI results file: %s", err)
