"""Fixtures shared across the whole test suite."""

from collections.abc import Iterator

import pytest

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
