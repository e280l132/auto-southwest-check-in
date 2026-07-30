"""Tests the retry behaviour of make_request against Southwest's intermittent rejections."""

import pytest
from pytest_mock import MockerFixture
from requests_mock.mocker import Mocker as RequestMocker

from lib.utils import (
    BACKOFF_CAP_SECS,
    TRANSIENT_ORIGIN_REJECTION,
    RequestError,
    make_request,
)
from lib.utils import BASE_URL as API_BASE

TEST_URL = API_BASE + "test/endpoint"
REJECTION = {"json": {"code": TRANSIENT_ORIGIN_REJECTION}, "status_code": 403}
SUCCESS = {"json": {"ok": True}, "status_code": 200}


def test_retries_until_southwest_stops_rejecting(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    mocker.patch("time.sleep")
    requests_mock.post(TEST_URL, [REJECTION, REJECTION, SUCCESS])

    assert make_request("POST", "test/endpoint", {}, {}) == {"ok": True}


def test_no_sleep_after_the_final_attempt(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    """Sleeping after the last attempt only delays the failure by up to the backoff cap."""
    mock_sleep = mocker.patch("time.sleep")
    requests_mock.post(TEST_URL, [REJECTION] * 3)

    with pytest.raises(RequestError):
        make_request("POST", "test/endpoint", {}, {}, max_attempts=3)

    assert mock_sleep.call_count == 2, "3 attempts should sleep only in the 2 gaps between them"


def test_backoff_grows_and_respects_the_cap(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    mock_sleep = mocker.patch("time.sleep")
    requests_mock.post(TEST_URL, [REJECTION] * 12)

    with pytest.raises(RequestError):
        make_request("POST", "test/endpoint", {}, {}, max_attempts=12)

    waits = [call.args[0] for call in mock_sleep.call_args_list]
    assert waits[0] < waits[-1], "backoff should grow"
    assert all(w <= BACKOFF_CAP_SECS for w in waits), "backoff should never exceed the cap"


def test_lower_cap_keeps_fare_checks_from_dragging_on(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    mock_sleep = mocker.patch("time.sleep")
    requests_mock.post(TEST_URL, [REJECTION] * 12)

    with pytest.raises(RequestError):
        make_request("POST", "test/endpoint", {}, {}, max_attempts=12, backoff_cap_secs=10)

    assert all(call.args[0] <= 10 for call in mock_sleep.call_args_list)


def test_check_in_requests_retry_without_backing_off(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    """Check-ins are time critical, so they keep retrying quickly instead of backing off."""
    mock_sleep = mocker.patch("time.sleep")
    requests_mock.post(TEST_URL, [REJECTION] * 4)

    with pytest.raises(RequestError):
        make_request("POST", "test/endpoint", {}, {}, max_attempts=4, random_sleep=False)

    assert [call.args[0] for call in mock_sleep.call_args_list] == [0.5, 0.5, 0.5]


def test_known_error_codes_stop_retrying_immediately(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    """A reservation that does not exist will never succeed, so it must not be retried."""
    mock_sleep = mocker.patch("time.sleep")
    # 400620389 == SouthwestErrorCode.RESERVATION_NOT_FOUND
    requests_mock.post(TEST_URL, [{"json": {"code": 400620389}, "status_code": 400}] * 20)

    with pytest.raises(RequestError, match="Reservation not found"):
        make_request("POST", "test/endpoint", {}, {})

    mock_sleep.assert_not_called()


def test_southwest_code_survives_being_re_raised(
    requests_mock: RequestMocker, mocker: MockerFixture
) -> None:
    """Downstream code branches on the error code, so a friendlier message must not drop it."""
    mocker.patch("time.sleep")
    # 400520413 == SouthwestErrorCode.FLIGHT_IN_PAST
    requests_mock.post(TEST_URL, [{"json": {"code": 400520413}, "status_code": 400}])

    with pytest.raises(RequestError) as err:
        make_request("POST", "test/endpoint", {}, {})

    assert err.value.southwest_code == 400520413
