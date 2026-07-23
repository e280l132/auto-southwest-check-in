"""
Tracks whether the check-in daemon is running, so the web UI can warn before starting a fare
check of its own.

Both drive a real browser session against Southwest, and running two at once is a good way to get
rate limited or fingerprinted. This is advisory only: it cannot see a daemon running on another
machine or in a different container, and nothing here ever blocks the daemon itself.
"""

from __future__ import annotations

import os
from pathlib import Path

from .log import get_logger

_data_dir = Path("/app/data") if os.environ.get("AUTO_SOUTHWEST_CHECK_IN_DOCKER") else Path(".")
PID_FILE = _data_dir / "daemon.pid"

logger = get_logger(__name__)


def write_pid_file(filepath: Path = PID_FILE) -> None:
    """
    Record this process as the running daemon. Failing to write must never stop the daemon, so
    every error here is logged and swallowed.
    """
    try:
        filepath.write_text(str(os.getpid()))
        logger.debug("Wrote daemon PID file to %s", filepath)
    except OSError as err:
        logger.debug("Could not write daemon PID file: %s", err)


def remove_pid_file(filepath: Path = PID_FILE) -> None:
    """
    Clear the PID file on shutdown, but only if it belongs to this process.

    Monitoring runs in child processes, and depending on the multiprocessing start method a child
    can inherit this atexit handler. Without the ownership check, the first child to exit would
    delete the PID file while the daemon is still running.
    """
    try:
        if filepath.read_text().strip() != str(os.getpid()):
            return
    except (OSError, ValueError):
        return

    try:
        filepath.unlink(missing_ok=True)
        logger.debug("Removed daemon PID file")
    except OSError as err:
        logger.debug("Could not remove daemon PID file: %s", err)


def get_running_pid(filepath: Path = PID_FILE) -> int | None:
    """
    Return the PID of the running daemon, or None if it isn't running.

    A PID file left behind by a crashed daemon is treated as not running, so a stale file never
    produces a permanent false warning.
    """
    try:
        pid = int(filepath.read_text().strip())
    except (OSError, ValueError):
        return None

    if pid <= 0:
        return None

    try:
        # Signal 0 performs the existence and permission checks without actually signalling
        os.kill(pid, 0)
    except ProcessLookupError:
        logger.debug("Daemon PID file is stale (process %d is gone)", pid)
        return None
    except PermissionError:
        # The process exists but belongs to another user, which still counts as running
        return pid
    except OSError:
        return None

    return pid


def is_running(filepath: Path = PID_FILE) -> bool:
    return get_running_pid(filepath) is not None
