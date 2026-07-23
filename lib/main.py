"""Primary script entrypoint where arguments are processed and flights are set up."""

from __future__ import annotations

import atexit
import multiprocessing
import os
import sys
import threading
import time

import requests

from lib import log

from . import app_control, daemon_status
from .config import IS_DOCKER, GlobalConfig, ReservationConfig
from .ignore_manager import IgnoreManager
from .ignore_server import start_ignore_server
from .reservation_monitor import AccountMonitor, ReservationMonitor
from .utils import CheckFaresOption

IP_TIMEZONE_URL = "https://ipinfo.io/timezone"
LOG_FILE = "logs/auto-southwest-check-in.log"
DEFAULT_WEB_PORT = 9000

logger = log.get_logger(__name__)


def start_web_ui_background(config: GlobalConfig, host: str, port: int) -> None:
    """
    Start the web UI on a background daemon thread so the calling thread stays free to wait for
    monitoring child processes (and a config reload request). The web UI itself is never
    restarted after this -- reloading config.json only restarts the monitoring processes, not
    the web server, so this only ever runs once per process.
    """
    # Imported here so Flask is only required when the web UI is actually used
    from .webui.app import create_app  # noqa: PLC0415

    app = create_app(config)
    logger.info("Starting web UI on http://%s:%d", host, port)
    print(f"Web UI running at http://{host}:{port}")

    run_kwargs = {
        "host": host,
        "port": port,
        "debug": False,
        "use_reloader": False,
        "threaded": True,
    }
    thread = threading.Thread(target=app.run, kwargs=run_kwargs, daemon=True, name="WebUI")
    thread.start()


def get_timezone() -> str:
    """Fetches the local timezone based on the system's IP address"""
    try:
        logger.debug("Fetching local timezone")
        response = requests.get(IP_TIMEZONE_URL, timeout=5)
        response.raise_for_status()
        return response.text.strip()
    except requests.RequestException:
        logger.debug("Timezone request failed, reverting to UTC")
        return "UTC"


def test_notifications(config: GlobalConfig) -> None:
    """
    Send a test notification to all configured sources. The notification configs for every account
    and reservation are merged to ensure only one test notification is sent to each source, even
    if a URL is specified for multiple accounts or reservations.
    """
    for account in config.accounts:
        config.merge_notification_config(account)

    for reservation in config.reservations:
        config.merge_notification_config(reservation)

    new_config = ReservationConfig()
    new_config.notifications = config.notifications
    reservation_monitor = ReservationMonitor(new_config)

    logger.info("Sending test notifications to %d sources", len(new_config.notifications))
    reservation_monitor.notification_handler.send_notification("This is a test message")


def pluralize(word: str, count: int) -> str:
    """Pluralize a word to improve grammar for printed messages"""
    return word if count == 1 else word + "s"


def set_up_accounts(config: GlobalConfig, lock: multiprocessing.Lock) -> None:
    for account in config.accounts:
        account_monitor = AccountMonitor(account, lock)
        account_monitor.start()


def set_up_reservations(config: GlobalConfig, lock: multiprocessing.Lock) -> None:
    for reservation in config.reservations:
        reservation_monitor = ReservationMonitor(reservation, lock)
        reservation_monitor.start()


def _extract_web_flags(arguments: list[str]) -> tuple[list[str], bool, bool, int | None]:
    """
    Pull '--web', '--no-web', and '--web-port <port>' out of the argument list so they don't
    interfere with the positional account/reservation argument parsing below. Returns the
    remaining arguments, whether '--web' was present, whether '--no-web' was present, and the
    port if '--web-port' was given.
    """
    remaining = []
    web_flag_present = False
    no_web_flag_present = False
    web_port = None

    i = 0
    while i < len(arguments):
        arg = arguments[i]
        if arg == "--web":
            web_flag_present = True
        elif arg == "--no-web":
            no_web_flag_present = True
        elif arg == "--web-port":
            i += 1
            if i >= len(arguments):
                logger.error("'--web-port' requires a port number")
                sys.exit(2)
            web_port = int(arguments[i])
        else:
            remaining.append(arg)
        i += 1

    if web_flag_present and no_web_flag_present:
        logger.error("'--web' and '--no-web' cannot be used together")
        sys.exit(2)

    return remaining, web_flag_present, no_web_flag_present, web_port


def _wait_for_children_or_reload() -> None:
    """
    Block the main thread until either every monitoring process has exited on its own or a
    config reload is requested (e.g. from the web UI's "Reload config" button). Polling (rather
    than a single blocking join) lets a reload request unblock promptly even while monitoring
    processes are still running, and keeps the process alive even when there are no monitoring
    processes at all (e.g. '--web' mode, or an empty config) so the web UI stays reachable.
    """
    while not app_control.reload_requested():
        children = multiprocessing.active_children()
        if children:
            for child in children:
                child.join(timeout=1)
                if app_control.reload_requested():
                    return
        else:
            time.sleep(1)


def _apply_cli_overrides(config: GlobalConfig, arguments: list[str]) -> None:
    """
    Add the single account/reservation given as positional CLI arguments (if any) to a freshly
    loaded config. These were never written to config.json, so they have to be reapplied every
    time the config is (re)loaded, not just on the first run.
    """
    if len(arguments) == 2:
        logger.debug("Adding account through CLI arguments")
        account = {"username": arguments[0], "password": arguments[1]}
        config.create_account_config([account])
    elif len(arguments) == 3:
        logger.debug("Adding reservation through CLI arguments")
        reservation = {
            "confirmationNumber": arguments[0],
            "firstName": arguments[1],
            "lastName": arguments[2],
        }
        config.create_reservation_config([reservation])
    elif len(arguments) > 3:
        logger.error("Invalid arguments. For more information, try '--help'")
        sys.exit(2)


def _start_monitoring(config: GlobalConfig, lock: multiprocessing.Lock) -> None:
    """Start a monitoring process for every account/reservation in the given config."""
    num_accounts = len(config.accounts)
    num_reservations = len(config.reservations)
    logger.info(
        "Monitoring %s %s and %s %s\n",
        num_accounts,
        pluralize("account", num_accounts),
        num_reservations,
        pluralize("reservation", num_reservations),
    )

    # Record that the daemon is running so the web UI can warn before starting a browser session
    # of its own.
    daemon_status.write_pid_file()

    # Remove ignore entries for reservations no longer in the config.
    # Accounts are excluded because their confirmation numbers aren't known until runtime.
    if config.reservations:
        active_confirmations = {r.confirmation_number for r in config.reservations}
        IgnoreManager().cleanup_confirmations(active_confirmations)

    # Start the ignore server if any config uses same_day_smart fare checking. Idempotent, so
    # this is a no-op on a reload if it's already running. The server runs as a daemon thread in
    # the main process so it is accessible to the user when clicking ignore links in
    # notification emails.
    all_configs = list(config.accounts) + list(config.reservations)
    if any(c.check_fares == CheckFaresOption.SAME_DAY_SMART for c in all_configs):
        start_ignore_server(config.ignore_server_port, IgnoreManager(), config.ignore_server_token)

    set_up_accounts(config, lock)
    set_up_reservations(config, lock)


def set_up_check_in(arguments: list[str]) -> None:
    """
    Initialize reservation and account monitoring based on the configuration
    and arguments passed in
    """
    logger.debug("Called with %d arguments", len(arguments))

    config = GlobalConfig()
    config.initialize()

    if "--test-notifications" in arguments:
        test_notifications(config)
        sys.exit()

    arguments, web_flag_present, no_web_flag_present, web_port = _extract_web_flags(arguments)

    # The web UI runs alongside monitoring by default. '--web' runs it *instead of* monitoring;
    # '--no-web' disables it entirely.
    web_ui_enabled = not no_web_flag_present
    web_only = web_flag_present

    if web_ui_enabled:
        default_host = "0.0.0.0" if IS_DOCKER else "127.0.0.1"
        host = os.getenv("AUTO_SOUTHWEST_CHECK_IN_WEB_HOST") or default_host
        port = web_port
        if port is None:
            port = int(os.getenv("AUTO_SOUTHWEST_CHECK_IN_WEB_PORT", DEFAULT_WEB_PORT))
        start_web_ui_background(config, host, port)

    if web_only:
        # Nothing is monitoring config.json in this mode, so a reload request has nothing to do
        # other than clear itself and go back to waiting.
        while True:
            _wait_for_children_or_reload()
            if not app_control.reload_requested():
                return
            app_control.clear_reload_request()

    _apply_cli_overrides(config, arguments)

    atexit.register(daemon_status.remove_pid_file)
    lock = multiprocessing.Lock()

    _start_monitoring(config, lock)

    while True:
        # Keep the main process alive until all processes are done (or a reload is requested) so
        # it can handle keyboard interrupts and the web UI's "Reload config" button
        _wait_for_children_or_reload()
        if not app_control.reload_requested():
            return

        app_control.clear_reload_request()
        app_control.stop_monitoring_processes()

        logger.info("Reloading configuration")
        config = GlobalConfig()
        config.initialize()
        _apply_cli_overrides(config, arguments)
        _start_monitoring(config, lock)


def main(arguments: list[str], version: str) -> None:
    log.init_main_logging()
    logger.debug("Auto-Southwest Check-In %s", version)

    if IS_DOCKER:
        # Setting timezone to avoid Southwest fingerprinting (based on browser timezone)
        timezone = get_timezone()
        os.environ["TZ"] = timezone

    # Remove flags now that they are not needed (and will mess up parsing)
    flags_to_remove = ["--debug-screenshots", "-v", "--verbose"]
    arguments = [x for x in arguments if x not in flags_to_remove]

    try:
        set_up_check_in(arguments)
    except KeyboardInterrupt:
        logger.info("\nCtrl+C pressed. Stopping all check-ins")
        sys.exit(130)
