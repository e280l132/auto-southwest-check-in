from __future__ import annotations

import json
import random
import socket
import time
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any

import ntplib
import requests

from .log import get_logger

# Type alias for JSON
JSON = dict[str, Any]

BASE_URL = "https://mobile.southwest.com/api/"
NTP_SERVER = "time.nist.gov"
NTP_BACKUP_SERVER = "time.cloudflare.com"

# Southwest's origin intermittently rejects otherwise-valid requests with this code. It is not bot
# detection: the request reaches the origin (the response carries a Server-Timing origin duration),
# and a real browser making the same call is rejected at the same rate. Measured failure rates of
# 50-70% are common, and Southwest has bad windows where it rejects nearly everything for minutes
# at a time. Retrying is the only mitigation, which is why these retries are spread over minutes
# rather than seconds.
TRANSIENT_ORIGIN_REJECTION = 403050700

# Retry backoff. Doubles each attempt up to the cap, so the default 20 attempts span roughly ten
# minutes instead of forty seconds and can ride out a bad window at Southwest.
BACKOFF_BASE_SECS = 2
BACKOFF_CAP_SECS = 45

# The fare change endpoints are rejected far more often than reservation lookups, so they need more
# attempts to get through. A lower cap keeps those extra attempts from stretching a fare check out
# over ten minutes: the rejections are random per request rather than rate limiting, so retrying
# sooner is both effective and harmless.
FARE_CHECK_MAX_ATTEMPTS = 20
FARE_CHECK_BACKOFF_CAP_SECS = 10

logger = get_logger(__name__)


def random_sleep_duration(min_duration: float, max_duration: float) -> float:
    return random.uniform(min_duration, max_duration)


def _backoff_duration(attempts: int, cap_secs: float = BACKOFF_CAP_SECS) -> float:
    """
    Exponential backoff with jitter, capped. The jitter keeps concurrent monitors from retrying
    in lockstep, and the cap keeps late attempts from drifting too far apart.
    """
    ceiling = min(cap_secs, BACKOFF_BASE_SECS * 2 ** (attempts - 1))
    return random_sleep_duration(ceiling / 2, ceiling)


def make_request(
    method: str,
    site: str,
    headers: JSON,
    info: JSON,
    max_attempts: int = 20,
    random_sleep: bool = True,
    backoff_cap_secs: float = BACKOFF_CAP_SECS,
) -> JSON:
    """
    Makes a request to the Southwest mobile API. For increased reliability, the request is
    performed multiple times on failure. This request retrying is also necessary for check-ins, as
    check-ins may start early.
    """
    # Ensure the URL is not malformed
    site = site.replace("//", "/").lstrip("/")
    url = BASE_URL + site
    return make_request_to_url(
        method, url, headers, info, max_attempts, random_sleep, backoff_cap_secs
    )


def make_request_to_url(
    method: str,
    url: str,
    headers: JSON,
    info: JSON,
    max_attempts: int = 20,
    random_sleep: bool = True,
    backoff_cap_secs: float = BACKOFF_CAP_SECS,
) -> JSON:
    """
    Same retrying/backoff behavior as make_request, for a request to a full URL rather than a path
    under the mobile API. Used for requests to www.southwest.com (the website reservation lookup
    and public flight search), which a plain one-shot request had no resilience against Southwest's
    intermittent rejections -- exactly what this retry loop exists to ride out for every other
    endpoint.
    """
    attempts = 0
    start = time.time()
    while attempts < max_attempts:
        attempts += 1

        try:
            request_start = time.time()
            response = _do_request(method, url, headers, info)
            request_elapsed = time.time() - request_start
            if response.status_code == 200:
                logger.info(
                    "%s succeeded after %d attempt(s), %.1fs total (last request took %.1fs)",
                    url,
                    attempts,
                    time.time() - start,
                    request_elapsed,
                )
                return response.json()

            response_body = response.content.decode()
            error_msg = f"{response.reason} ({response.status_code})"
        except requests.RequestException as err:
            response_body = ""
            error_msg = str(err)

        error = RequestError(error_msg, response_body)

        try:
            _handle_southwest_error_code(error)
        except (RequestError, AirportCheckInError) as err:
            # Stop requesting after one attempt for special codes, as the requests won't succeed
            error = err
            break

        transient = (
            " (transient Southwest origin rejection)"
            if error.southwest_code == TRANSIENT_ORIGIN_REJECTION
            else ""
        )

        # Sleeping after the final attempt only delays the failure, which matters now that the
        # backoff reaches tens of seconds
        if attempts >= max_attempts:
            logger.debug(
                "Request error on attempt %d/%d: %s%s", attempts, max_attempts, error_msg, transient
            )
            break

        sleep_time = _backoff_duration(attempts, backoff_cap_secs) if random_sleep else 0.5
        logger.debug(
            "Request error on attempt %d/%d: %s%s. Sleeping for %.2f seconds until next attempt",
            attempts,
            max_attempts,
            error_msg,
            transient,
            sleep_time,
        )
        time.sleep(sleep_time)

    logger.info(
        "%s failed after %d attempts, %.1fs total: %s",
        url,
        attempts,
        time.time() - start,
        error_msg,
    )
    logger.debug("Response body: %s", response_body)
    raise error


def _do_request(method: str, url: str, headers: JSON, info: JSON) -> requests.Response:
    if method.upper() == "POST":
        response = requests.post(url, headers=headers, json=info)
    else:
        response = requests.get(url, headers=headers, params=info)

    return response


class SouthwestErrorCode(IntEnum):
    AIRPORT_CHECKIN_REQUIRED = 400511206
    FLIGHT_IN_PAST = 400520413
    INVALID_CONFIRMATION_NUMBER_LENGTH = 400310456
    PASSENGER_NOT_FOUND = 400620480
    RESERVATION_CANCELLED = 400520414
    RESERVATION_NOT_FOUND = 400620389


def _handle_southwest_error_code(error: RequestError) -> None:
    if error.southwest_code == SouthwestErrorCode.AIRPORT_CHECKIN_REQUIRED:
        raise AirportCheckInError("Airport check-in is required")

    if error.southwest_code == SouthwestErrorCode.FLIGHT_IN_PAST:
        raise RequestError("Flight has already departed", southwest_code=error.southwest_code)

    if error.southwest_code == SouthwestErrorCode.INVALID_CONFIRMATION_NUMBER_LENGTH:
        raise RequestError(
            "Invalid confirmation number length", southwest_code=error.southwest_code
        )

    if error.southwest_code == SouthwestErrorCode.PASSENGER_NOT_FOUND:
        raise RequestError(
            "Passenger not found on reservation", southwest_code=error.southwest_code
        )

    if error.southwest_code == SouthwestErrorCode.RESERVATION_NOT_FOUND:
        raise RequestError("Reservation not found", southwest_code=error.southwest_code)

    if error.southwest_code == SouthwestErrorCode.RESERVATION_CANCELLED:
        raise RequestError("Reservation has been cancelled", southwest_code=error.southwest_code)


def get_current_time() -> datetime:
    """
    Fetch the current time from an NTP server. Times are sometimes off on computers running the
    script and since check-ins rely on exact times, this ensures check-ins are done at the correct
    time. Falls back to local time if the request to the NTP servers fail.

    Times are returned in UTC.
    """
    c = ntplib.NTPClient()

    try:
        # Set a longer timeout to make the request more reliable
        response = c.request(NTP_SERVER, version=3, timeout=10)
    except (socket.gaierror, ntplib.NTPException):
        try:
            # Try the backup NTP server before falling back to local time. Increases reliability of
            # fetching the time significantly
            response = c.request(NTP_BACKUP_SERVER, version=3, timeout=10)
        except (socket.gaierror, ntplib.NTPException):
            logger.debug("Error requesting time from NTP servers. Using local time")
            return datetime.now(timezone.utc)

    return datetime.fromtimestamp(response.tx_time, timezone.utc)


class RequestError(Exception):
    """A custom exception when a request fails"""

    def __init__(
        self, message: str, response_body: str = "", southwest_code: int | None = None
    ) -> None:
        super().__init__(message)

        try:
            response_json = json.loads(response_body)
        except json.decoder.JSONDecodeError:
            response_json = {}

        # Callers that re-raise a friendlier error pass the code explicitly so it survives; without
        # it, code-based handling downstream silently stops matching.
        self.southwest_code = (
            southwest_code if southwest_code is not None else response_json.get("code")
        )


class AirportCheckInError(Exception):
    """A custom exception when airport check-in is required"""


class LoginError(Exception):
    """A custom exception when a login fails"""

    def __init__(self, reason: str, status_code: int) -> None:
        super().__init__(f"Reason: {reason}. Status code: {status_code}")
        self.status_code = status_code


class FlightChangeError(Exception):
    """A custom exception for flights that cannot be changed"""


class DriverTimeoutError(Exception):
    """A custom exception for when the webdriver times out waiting for attributes to be set"""


class NotificationLevel(IntEnum):
    NOTICE = 1
    INFO = 2
    CHECKIN = 3
    ERROR = 4


# Switch to StrEnum when Python 3.10 support is dropped
class CheckFaresOption(str, Enum):
    NO = "no"
    SAME_FLIGHT = "same_flight"
    SAME_DAY_NONSTOP = "same_day_nonstop"
    SAME_DAY = "same_day"
    SAME_DAY_SMART = "same_day_smart"


def is_truthy(arg: bool | int | str) -> bool:
    """
    Convert "truthy" strings into Booleans.

    Examples:
        >>> is_truthy('yes')
        True

    Args:
        arg: Truthy value (True values are y, yes, t, true, on and 1; false values are n, no,
        f, false, off and 0. Raises ValueError if val is anything else.
    """
    if isinstance(arg, bool):
        return arg

    val = str(arg).lower()
    if val in ("y", "yes", "t", "true", "on", "1"):
        return True
    if val in ("n", "no", "f", "false", "off", "0"):
        return False
    raise ValueError(f"Invalid truthy value: `{arg}`")
