"""Fixtures shared across the whole test suite."""

from collections.abc import Iterator

import pytest
from pytest_mock import MockerFixture

from lib.flight import clear_airport_timezone_cache


@pytest.fixture(autouse=True)
def _reset_airport_timezone_cache() -> Iterator[None]:
    """
    lib.flight caches the airport-timezone file for the life of the process (it's static, and
    re-reading it per flight was wasteful). Many tests mock Path.read_text with their own
    airport-timezone data, so each test needs to start from an empty cache or it would see
    whatever an earlier test happened to load first.
    """
    clear_airport_timezone_cache()
    yield
    clear_airport_timezone_cache()


@pytest.fixture(autouse=True)
def _no_real_browser(mocker: MockerFixture) -> None:
    """
    Fail loudly instead of launching a real browser.

    Reservation lookups go through the website (and therefore a webdriver) on their normal path, so
    a test that forgets to mock the driver would otherwise start Chrome, reach out to Southwest, and
    hang for minutes. Patching the constructor rather than `_get_driver` leaves tests that exercise
    driver behaviour free to patch either one over the top of this.
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(
            "A test tried to launch a real browser. Mock lib.webdriver.Driver (or the WebDriver "
            "method being called) in the test."
        )

    mocker.patch("lib.webdriver.Driver", side_effect=refuse)
