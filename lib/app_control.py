"""
Coordinates reloading the check-in daemon's configuration (accounts/reservations) from
config.json without restarting the process, so edits made through the web UI take effect without
interrupting the web UI itself.

A reload works by asking every monitoring child process to stop the way Ctrl+C already does
(SIGINT, which each AccountMonitor/ReservationMonitor already handles via KeyboardInterrupt); the
main thread (see lib.main._wait_for_children_or_reload) then re-reads config.json and starts
fresh monitoring processes.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import threading

from .log import get_logger

logger = get_logger(__name__)

_reload_event = threading.Event()


def reload_requested() -> bool:
    return _reload_event.is_set()


def clear_reload_request() -> None:
    _reload_event.clear()


def request_reload() -> None:
    """
    Ask the check-in daemon to reload config.json. Safe to call from any thread, and safe to call
    again even if a previous request hasn't been picked up yet. Signals monitoring processes to
    stop gracefully but does not block or join them -- the main thread does that once it notices
    the request.
    """
    logger.info("Configuration reload requested")
    _reload_event.set()

    for child in multiprocessing.active_children():
        try:
            os.kill(child.pid, signal.SIGINT)
        except ProcessLookupError:
            pass


def stop_monitoring_processes(timeout: float = 30) -> None:
    """Join any still-running monitoring processes, force-stopping stragglers past timeout."""
    for child in multiprocessing.active_children():
        child.join(timeout)
        if child.is_alive():
            logger.warning(
                "Force stopping monitoring process %s that did not exit in time", child.pid
            )
            child.terminate()
            child.join(5)
