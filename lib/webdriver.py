from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sbvirtualdisplay import Display
from seleniumbase import Driver
from seleniumbase import config as sb_config
from seleniumbase.fixtures import page_actions as seleniumbase_actions

from .config import IS_DOCKER
from .log import LOGS_DIRECTORY, get_logger
from .utils import DriverTimeoutError, LoginError, random_sleep_duration

if TYPE_CHECKING:
    from .checkin_scheduler import CheckInScheduler
    from .reservation_monitor import AccountMonitor

# URLs for the normal website
BASE_URL = "https://www.southwest.com"
ACCOUNT_URL = BASE_URL + "/loyalty/myaccount"
SUCCESSFUL_LOGIN_URL = BASE_URL + "/api/security/v4/security/token"
TRIPS_URL = (
    BASE_URL
    + "/api/loyalty-management/v2/loyalty-management/accounts/self/future-air-reservations-secure"
)
# Public flight search page — fareType=POINTS so we get points pricing
SEARCH_PAGE_URL = (
    BASE_URL
    + "/air/booking/select.html"
    + "?adultPassengersCount=1"
    + "&departureDate={date}"
    + "&destinationAirportCode={destination}"
    + "&fareType=POINTS"
    + "&int=HOMEQBOMAIR"
    + "&originationAirportCode={origin}"
    + "&passengerType=ADULT"
    + "&returnAirportCode=&returnDate="
    + "&tripType=oneway"
)

# URLs for the mobile website
MOBILE_BASE_URL = "https://mobile.southwest.com"
# The webView=true parameter is necessary so we don't get redirected to www.southwest.com
MOBILE_LOGIN_URL = MOBILE_BASE_URL + "/login?webView=true"
MOBILE_HEADERS_URL = (
    MOBILE_BASE_URL + "/api/mobile-air-booking/v1/mobile-air-booking/feature/shopping-details"
)

# URL prefix for the public air-booking search API — used to identify pricing responses
SEARCH_RESPONSE_URL = BASE_URL + "/api/air-booking/"

# Southwest's code when logging in with the incorrect information
INVALID_CREDENTIALS_CODE = 400518024

WAIT_TIMEOUT_SECS = 180

JSON = dict[str, Any]

logger = get_logger(__name__)

# Prevent SeleniumBase from missing requests when reinstantiating the driver for multiple accounts.
# See https://github.com/jdholtz/auto-southwest-check-in/issues/387 for details.
sb_config.skip_133_patch = True


class WebDriver:
    """
    Controls fetching valid headers for use with the Southwest API.

    This class can be instantiated in two ways:
    1. Setting/refreshing headers before a check-in to ensure the headers are valid. The
    check-in URL is requested in the browser. One of the requests from this initial request
    contains valid headers which are then set for the CheckIn Scheduler.

    2. Logging into an account. In this case, the headers are refreshed and a list of scheduled
    flights are retrieved.

    Some of this code is based off of:
    https://github.com/byalextran/southwest-headers/commit/d2969306edb0976290bfa256d41badcc9698f6ed
    """

    def __init__(self, checkin_scheduler: CheckInScheduler) -> None:
        self.checkin_scheduler = checkin_scheduler
        self.headers_set = False
        self.debug_screenshots = self._should_take_screenshots()
        self.display = None

        # For account login
        self.login_request_id = None
        self.login_status_code = None
        self.trips_request_id = None

        # For public flight price scraping
        self.search_request_id = None

    def _should_take_screenshots(self) -> bool:
        """
        Determines if the webdriver should take screenshots for debugging based on the CLI arguments
        of the script. Similarly to setting verbose logs, this cannot be kept track of easily in a
        global variable due to the script's use of multiprocessing.
        """
        arguments = sys.argv[1:]
        if "--debug-screenshots" in arguments:
            logger.debug("Taking debug screenshots")
            return True

        return False

    def _take_debug_screenshot(self, driver: Driver, name: str) -> None:
        """Take a screenshot of the browser and save the image as 'name' in LOGS_DIRECTORY"""
        if self.debug_screenshots:
            driver.save_screenshot(Path(LOGS_DIRECTORY) / name)

    def set_headers(self) -> None:
        """
        The check-in URL is requested. Since another request contains valid headers
        during the initial request, those headers are set in the CheckIn Scheduler.
        """
        driver = self._get_driver()
        self._take_debug_screenshot(driver, "pre_headers.png")
        logger.debug("Waiting for valid headers")
        # Once this attribute is set, the headers have been set in the checkin_scheduler
        self._wait_for_attribute(driver, "headers_set")
        self._take_debug_screenshot(driver, "post_headers.png")

        self._quit_driver(driver)

    def get_reservations(self, account_monitor: AccountMonitor) -> list[JSON]:
        """
        Logs into the account being monitored to retrieve a list of reservations. Since
        valid headers are produced, they are also grabbed and updated in the check-in scheduler.
        Last, if the account name is not set, it will be set based on the response information.

        Headers are retrieved from the mobile Southwest site as the rest of the script uses
        the mobile API. Then, logging in and retrieving reservations is done through the normal
        Southwest website, as the mobile site is not navigable with a desktop browser.
        """
        driver = self._get_driver()
        driver.add_cdp_listener("Network.responseReceived", self._login_listener)

        # Now, load the normal website (not the mobile site) to log in and get reservations
        logger.debug("Loading Southwest login page (this may take a moment)")
        driver.get(ACCOUNT_URL)

        # Log in to retrieve the account's reservations and needed headers for later requests
        logger.debug("Logging into account to get a list of reservations and valid headers")
        self._take_debug_screenshot(driver, "pre_login.png")
        time.sleep(random_sleep_duration(1, 3))
        driver.type('input[id="username"]', account_monitor.username)
        driver.type('input[id="password"]', f"{account_monitor.password}\n")

        # Wait for the necessary information to be set
        self._wait_for_attribute(driver, "headers_set")
        self._wait_for_login(driver, account_monitor)
        self._take_debug_screenshot(driver, "post_login.png")

        # The upcoming trips page is also loaded when we log in, so we might as well grab it
        # instead of requesting again later
        reservations = self._fetch_reservations(driver)

        self._quit_driver(driver)
        return reservations

    def _get_driver(self) -> Driver:
        logger.debug("Starting webdriver for current session")
        browser_path = self.checkin_scheduler.reservation_monitor.config.browser_path

        driver_version = "mlatest"
        if IS_DOCKER:
            self._start_display()
            # Make sure a new driver is not downloaded as the Docker image
            # already has the correct driver
            driver_version = "keep"

        driver = Driver(
            binary_location=browser_path,
            driver_version=driver_version,
            headed=IS_DOCKER,
            headless1=not IS_DOCKER,
            uc_cdp_events=True,
            undetectable=True,
            incognito=True,
        )
        logger.debug("Using browser version: %s", driver.caps["browserVersion"])

        driver.add_cdp_listener("Network.requestWillBeSent", self._headers_listener)

        # Load the login page to get valid headers
        logger.debug("Loading mobile Southwest login page (this may take a moment)")
        driver.get(MOBILE_LOGIN_URL)
        self._take_debug_screenshot(driver, "after_page_load.png")

        return driver

    def _headers_listener(self, data: JSON) -> None:
        """
        Wait for the correct URL request has gone through. Once it has, set the headers
        in the checkin_scheduler.
        """
        request = data["params"]["request"]
        url = request["url"]

        # Log all mobile API requests at debug level to help discover endpoint paths
        if url.startswith(MOBILE_BASE_URL + "/api/"):
            logger.debug("Mobile API request observed: %s %s", request.get("method", "?"), url)

        if url == MOBILE_HEADERS_URL:
            self.checkin_scheduler.headers = self._get_needed_headers(request["headers"])
            self.headers_set = True

    def _login_listener(self, data: JSON) -> None:
        """
        Wait for various responses that are needed once the account is logged in. The request IDs
        are kept track of to get the response body associated with them later.
        """
        response = data["params"]["response"]
        if response["url"] == SUCCESSFUL_LOGIN_URL:
            logger.debug("Login response has been received")
            self.login_request_id = data["params"]["requestId"]
            self.login_status_code = response["status"]
        elif response["url"] == TRIPS_URL:
            logger.debug("Upcoming trips response has been received")
            self.trips_request_id = data["params"]["requestId"]

    def _wait_for_attribute(self, driver: Driver, attribute: str) -> None:
        logger.debug("Waiting for %s to be set (timeout: %d seconds)", attribute, WAIT_TIMEOUT_SECS)
        poll_interval = 0.5

        attempts = 0
        max_attempts = WAIT_TIMEOUT_SECS / poll_interval
        while not getattr(self, attribute) and attempts < max_attempts:
            time.sleep(poll_interval)
            attempts += 1

        if attempts >= max_attempts:
            self._quit_driver(driver)
            timeout_err = DriverTimeoutError(f"Timeout waiting for the '{attribute}' attribute")
            logger.debug(timeout_err)
            raise timeout_err

        logger.debug("%s set successfully", attribute)

    def _wait_for_login(self, driver: Driver, account_monitor: AccountMonitor) -> None:
        """
        Waits for the login request to go through and sets the account name appropriately.
        Handles login errors, if necessary.
        """
        self._click_login_button(driver)
        self._wait_for_attribute(driver, "login_request_id")
        login_response = self._get_response_body(driver, self.login_request_id)

        # Handle login errors
        if self.login_status_code != 200:
            self._quit_driver(driver)
            error = self._handle_login_error(login_response)
            raise error

        self._set_account_name(account_monitor, login_response)

    def _click_login_button(self, driver: Driver) -> None:
        """
        In some cases, the submit action on the login form may fail. Therefore, try clicking
        again, if necessary.
        """
        if driver.is_element_visible("div[class^='errorMessage']"):
            # Don't attempt to click the login button again if the submission form went through,
            # yet there was an error message
            return

        login_button = "button#submit"
        try:
            seleniumbase_actions.wait_for_element_not_visible(driver, login_button, timeout=5)
        except Exception:
            logger.debug("Login form failed to submit. Clicking login button again")
            driver.click(login_button)

    def _fetch_reservations(self, driver: Driver) -> list[JSON]:
        """
        Waits for the reservations request to go through and returns only reservations
        that are flights.
        """
        self._wait_for_attribute(driver, "trips_request_id")
        trips_response = self._get_response_body(driver, self.trips_request_id)
        reservations = trips_response["data"]
        return reservations

    def get_public_flight_prices(self, origin: str, destination: str, date: str) -> JSON:
        """
        Navigate to the public Southwest flight search page and capture the API response
        that contains flight pricing. Returns the raw JSON response body.

        This is used as a fallback for companion-pass flights where the change-flow
        API is blocked — the public search has no knowledge of companion restrictions.
        """
        driver = self._get_driver()
        driver.add_cdp_listener("Network.responseReceived", self._search_listener)

        search_url = SEARCH_PAGE_URL.format(origin=origin, destination=destination, date=date)
        logger.debug("Loading public flight search page (route: %s→%s on %s)", origin, destination, date)
        driver.get(search_url)
        self._take_debug_screenshot(driver, "search_page.png")

        self._wait_for_attribute(driver, "search_request_id")
        response = self._get_response_body(driver, self.search_request_id)
        self._quit_driver(driver)

        # Validate that the captured response contains flight pricing data
        try:
            response["data"]["searchResults"]["airProducts"]
        except (KeyError, TypeError) as err:
            raise DriverTimeoutError(
                f"Public search response missing expected pricing data: {err}"
            ) from err

        return response

    def _search_listener(self, data: JSON) -> None:
        """
        Capture the flight search API response from the public Southwest search page.
        Logs all SW API responses at debug level for discovery, and records the first
        response that looks like flight search results (contains 'products' or 'flights'
        in the URL path).
        """
        response = data["params"]["response"]
        url = response["url"]

        # Log all SW API responses at debug level to help identify the right endpoint
        if SEARCH_RESPONSE_URL in url:
            logger.debug(
                "Public search API response: %s %s",
                response.get("status"),
                url,
            )

        # Capture the first air-booking shopping response (specific to the pricing endpoint)
        if self.search_request_id is None and SEARCH_RESPONSE_URL in url and "shopping" in url:
            logger.debug("Captured flight search response from: %s", url)
            self.search_request_id = data["params"]["requestId"]

    def _get_response_body(self, driver: Driver, request_id: str) -> JSON:
        response = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
        return json.loads(response["body"])

    def _handle_login_error(self, response: JSON) -> LoginError:
        if response.get("code") == INVALID_CREDENTIALS_CODE:
            logger.debug("Invalid credentials provided when attempting to log in")
            reason = "Invalid credentials"
        else:
            logger.debug("Logging in failed for an unknown reason")
            reason = "Unknown"

        return LoginError(reason, self.login_status_code)

    def _get_needed_headers(self, request_headers: JSON) -> JSON:
        headers = {}
        for header in request_headers:
            if re.match(r"x-api-key|x-channel-id|user-agent|^[\w-]+?-\w$", header, re.IGNORECASE):
                headers[header] = request_headers[header]

        return headers

    def _set_account_name(self, account_monitor: AccountMonitor, response: JSON) -> None:
        if account_monitor.first_name:
            # No need to set the name if this isn't the first time logging in
            return

        logger.debug("First time logging in. Setting account name")
        account_monitor.first_name = response["customers.userInformation.firstName"]
        account_monitor.preferred_name = response.get("customers.userInformation.preferredName", "")
        account_monitor.last_name = response["customers.userInformation.lastName"]

        print(
            f"Successfully logged in to {account_monitor.get_display_name()}'s account\n"
        )  # Don't log as it contains sensitive information

    def _quit_driver(self, driver: Driver) -> None:
        temp_browser_dir = self._get_temp_browser_dir(driver)
        driver.quit()
        self._stop_display()
        self._cleanup_browser_dir(temp_browser_dir)

    def _start_display(self) -> None:
        try:
            self.display = Display(size=(1440, 1880), backend="xvfb")
            self.display.start()

            if self.display.is_alive():
                logger.debug("Started virtual display successfully")
            else:
                logger.debug("Started virtual display but is not active")
        except Exception as e:
            logger.debug("Failed to start display: %s", e)

    @staticmethod
    def _get_temp_browser_dir(driver: Driver) -> Path | None:
        """
        Get the temporary browser directory. This is different than the driver's user data directory
        and isn't automatically cleaned up when the driver quits. This directory isn't directly
        accessible via the driver object, so it is retrieved from a symlink in the user data
        directory.

        To make sure the correct directory is removed, it needs to start with
        '.org.chromium.Chromium.'. Removing this won't cause issues when a custom user data
        directory is set as Chromium always initializes a new temporary browser directory on start.
        """
        # The SingletonSocket file is symlinked from the user data directory to the temporary
        # browser directory, so we can get that directory by reading the source of the symlink
        socket_path = Path(driver.user_data_dir) / "SingletonSocket"
        try:
            socket_path = socket_path.readlink().absolute()
        except FileNotFoundError:
            return None

        temp_chromium_dir = socket_path.parent
        # A safety measure. Only declare it as a valid temporary browser directory if is the
        # Chromium browser directory.
        if not temp_chromium_dir.name.startswith(".org.chromium.Chromium."):
            return None

        return temp_chromium_dir

    def _stop_display(self) -> None:
        if self.display is not None:
            self.display.stop()
            logger.debug("Stopped virtual display successfully")

    @staticmethod
    def _cleanup_browser_dir(temp_browser_dir: Path | None) -> None:
        """
        Cleanup the temporary browser directory used by the current driver instance.
        This is done to prevent accumulation of temporary browser directories over time.
        """

        if temp_browser_dir is not None and temp_browser_dir.exists():
            logger.debug("Removing temporary browser directory: %s", temp_browser_dir)
            shutil.rmtree(temp_browser_dir)
