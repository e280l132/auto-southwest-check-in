"""
Tests that reading a response body back out of the browser via CDP is never how these paths get
their data.

`execute_cdp_cmd("Network.getResponseBody", ...)` races the page's lifecycle and intermittently
raises "unhandled inspector error: No resource with given identifier found" — hit in production for
the public flight search. The fix is the same one already used for the website reservation lookup:
capture the request the browser makes (a plain navigation event with no such race) and repeat it
with `requests` instead of reading the response back from Chrome. This covers that the search path
was actually converted, and that the two remaining CDP body-reads (login, trips) degrade to a clean
DriverTimeoutError instead of crashing the process and leaking the browser.
"""

from unittest import mock

import pytest
from pytest_mock import MockerFixture

from lib.utils import DriverTimeoutError, RequestError
from lib.webdriver import WebDriver


@pytest.fixture(autouse=True)
def _offline(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")


def _webdriver(mocker: MockerFixture) -> WebDriver:
    return WebDriver(mocker.Mock())


def test_search_never_reads_the_response_body_via_cdp(mocker: MockerFixture) -> None:
    """The whole point of the fix: CDP's response-body command must not be touched for search."""
    wd = _webdriver(mocker)
    mock_cdp = mocker.patch.object(wd, "_get_response_body")

    driver = mock.Mock()
    mocker.patch.object(WebDriver, "_get_driver", return_value=driver)
    mocker.patch.object(WebDriver, "_quit_driver")

    def fake_wait(_driver: mock.Mock, attribute: str) -> None:
        assert attribute == "search_request"
        wd.search_request = {
            "url": "https://www.southwest.com/api/air-booking/v1/shopping",
            "method": "GET",
            "headers": {"x-api-key": "test"},
            "body": None,
        }

    mocker.patch.object(WebDriver, "_wait_for_attribute", side_effect=fake_wait)
    mock_get = mocker.patch(
        "lib.webdriver.requests.get",
        return_value=mock.Mock(
            status_code=200,
            json=lambda: {"data": {"searchResults": {"airProducts": [{}]}}},
        ),
    )

    result = wd.get_public_flight_prices("LGA", "STL", "2026-08-21")

    mock_cdp.assert_not_called()
    mock_get.assert_called_once()
    assert result["data"]["searchResults"]["airProducts"] == [{}]


def test_search_replays_a_captured_post_request(mocker: MockerFixture) -> None:
    """The pricing call may be a POST with a body rather than a GET; both shapes must work."""
    wd = _webdriver(mocker)
    wd.search_request = {
        "url": "https://www.southwest.com/api/air-booking/v1/shopping",
        "method": "POST",
        "headers": {"x-api-key": "test"},
        "body": '{"origin": "LGA"}',
    }
    mock_post = mocker.patch(
        "lib.webdriver.requests.post",
        return_value=mock.Mock(
            status_code=200,
            json=lambda: {"data": {"searchResults": {"airProducts": [{}]}}},
        ),
    )

    result = wd._replay_search_request()

    mock_post.assert_called_once_with(
        wd.search_request["url"],
        headers=wd.search_request["headers"],
        json={"origin": "LGA"},
        timeout=30,
    )
    assert result["data"]["searchResults"]["airProducts"] == [{}]


def test_search_replay_raises_cleanly_on_rejection(mocker: MockerFixture) -> None:
    wd = _webdriver(mocker)
    wd.search_request = {
        "url": "https://www.southwest.com/api/air-booking/v1/shopping",
        "method": "GET",
        "headers": {},
        "body": None,
    }
    mocker.patch(
        "lib.webdriver.requests.get",
        return_value=mock.Mock(
            status_code=403, reason="Forbidden", content=b'{"code": 403050700}'
        ),
    )

    with pytest.raises(RequestError):
        wd._replay_search_request()


def test_search_replay_rejects_a_response_missing_pricing_data(mocker: MockerFixture) -> None:
    wd = _webdriver(mocker)
    wd.search_request = {
        "url": "https://www.southwest.com/api/air-booking/v1/shopping",
        "method": "GET",
        "headers": {},
        "body": None,
    }
    mocker.patch(
        "lib.webdriver.requests.get",
        return_value=mock.Mock(status_code=200, json=lambda: {"data": {}}),
    )

    with pytest.raises(DriverTimeoutError):
        wd._replay_search_request()


def test_capture_only_takes_the_first_matching_request(mocker: MockerFixture) -> None:
    """The page can make more than one matching-looking call; only the first should stick."""
    wd = _webdriver(mocker)
    first = {
        "url": "https://www.southwest.com/api/air-booking/v1/shopping?first=1",
        "method": "GET",
        "headers": {},
    }
    second = {
        "url": "https://www.southwest.com/api/air-booking/v1/shopping?second=1",
        "method": "GET",
        "headers": {},
    }

    wd._capture_search_request(first)
    wd._capture_search_request(second)

    assert wd.search_request["url"] == first["url"]


@pytest.mark.parametrize("target_attribute", ["login_request_id", "trips_request_id"])
def test_a_cdp_body_read_failure_is_a_clean_timeout_not_a_crash(
    mocker: MockerFixture, target_attribute: str
) -> None:
    """
    Before this fix, a CDP race reading the login/trips response propagated raw out of
    get_reservations() uncaught, crashing the whole account-monitoring process and leaking Chrome.
    """
    wd = _webdriver(mocker)
    setattr(wd, target_attribute, "some-request-id")
    mocker.patch.object(wd, "_get_response_body", side_effect=RuntimeError("no resource"))
    mock_quit = mocker.patch.object(wd, "_quit_driver")
    driver = mock.Mock()

    if target_attribute == "login_request_id":
        mocker.patch.object(wd, "_click_login_button")
        mocker.patch.object(WebDriver, "_wait_for_attribute")
        with pytest.raises(DriverTimeoutError):
            wd._wait_for_login(driver, mocker.Mock())
    else:
        mocker.patch.object(WebDriver, "_wait_for_attribute")
        with pytest.raises(DriverTimeoutError):
            wd._fetch_reservations(driver)

    mock_quit.assert_called_once_with(driver)
