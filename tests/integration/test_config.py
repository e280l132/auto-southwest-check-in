"""Tests the config module to ensure global and local configuration values are respected"""

import json

import pytest
from pytest_mock import MockerFixture

from lib.config import ConfigError, GlobalConfig, NotificationConfig
from lib.utils import CheckFaresOption, NotificationLevel


def assert_notification_config_matches(
    notification_config: NotificationConfig,
    expected_url: str,
    expected_level: NotificationLevel,
    expected_24_hr_time: bool,
) -> None:
    assert notification_config.url == expected_url
    assert notification_config.level == expected_level
    assert notification_config.twenty_four_hour_time is expected_24_hr_time


def test_config(mocker: MockerFixture) -> None:
    config = {
        "browser_path": "chrome_path",
        "check_fares": CheckFaresOption.SAME_DAY_NONSTOP,
        "notifications": [{"url": "test1.com", "level": 1}, {"url": "test2.com"}],
        "retrieval_interval": 16,
        "accounts": [
            {"username": "test_user1", "password": "test_pass1"},
            {
                "username": "test_user2",
                "password": "test_pass2",
                "check_fares": False,
                "notifications": [{"url": "test1.com", "level": 3}, {"url": "test3.com"}],
                "retrieval_interval": 10,
            },
        ],
        "reservations": [
            {"confirmationNumber": "test_num1", "firstName": "Winston", "lastName": "Smith"},
            {
                "confirmationNumber": "test_num2",
                "firstName": "Edmond",
                "lastName": "Dantès",
                "check_fares": False,
                "notifications": [{"url": "test4.com", "24_hour_time": True}],
                "retrieval_interval": 8,
            },
        ],
    }

    mocker.patch("pathlib.Path.read_text", return_value=json.dumps(config))

    config = GlobalConfig()
    config.initialize()

    assert len(config.accounts) == 2
    assert len(config.reservations) == 2

    # Check the account configurations
    account_one = config.accounts[0]
    account_two = config.accounts[1]

    assert account_one.browser_path == "chrome_path"
    assert account_one.check_fares == CheckFaresOption.SAME_DAY_NONSTOP
    assert account_one.retrieval_interval == 16 * 3600
    assert account_one.username == "test_user1"
    assert account_one.password == "test_pass1"

    assert len(account_one.notifications) == 2
    assert_notification_config_matches(
        account_one.notifications[0], "test1.com", NotificationLevel.NOTICE, False
    )
    assert_notification_config_matches(
        account_one.notifications[1], "test2.com", NotificationLevel.INFO, False
    )

    assert account_two.browser_path == "chrome_path"
    assert account_two.check_fares == CheckFaresOption.NO
    assert account_two.retrieval_interval == 10 * 3600
    assert account_two.username == "test_user2"
    assert account_two.password == "test_pass2"

    assert len(account_two.notifications) == 3
    assert_notification_config_matches(
        account_two.notifications[0], "test1.com", NotificationLevel.CHECKIN, False
    )
    assert_notification_config_matches(
        account_two.notifications[1], "test3.com", NotificationLevel.INFO, False
    )
    assert_notification_config_matches(
        account_two.notifications[2], "test2.com", NotificationLevel.INFO, False
    )

    # Check the reservation configurations
    reservation_one = config.reservations[0]
    reservation_two = config.reservations[1]

    assert reservation_one.browser_path == "chrome_path"
    assert reservation_one.check_fares == CheckFaresOption.SAME_DAY_NONSTOP
    assert reservation_one.confirmation_number == "test_num1"
    assert reservation_one.first_name == "Winston"
    assert reservation_one.last_name == "Smith"
    assert reservation_one.retrieval_interval == 16 * 3600

    assert len(reservation_one.notifications) == 2
    assert_notification_config_matches(
        reservation_one.notifications[0], "test1.com", NotificationLevel.NOTICE, False
    )
    assert_notification_config_matches(
        reservation_one.notifications[1], "test2.com", NotificationLevel.INFO, False
    )

    assert reservation_two.browser_path == "chrome_path"
    assert reservation_two.check_fares == CheckFaresOption.NO
    assert reservation_two.confirmation_number == "test_num2"
    assert reservation_two.first_name == "Edmond"
    assert reservation_two.last_name == "Dantès"
    assert reservation_two.retrieval_interval == 8 * 3600

    assert len(reservation_two.notifications) == 3
    assert_notification_config_matches(
        reservation_two.notifications[0], "test4.com", NotificationLevel.INFO, True
    )
    assert_notification_config_matches(
        reservation_two.notifications[1], "test1.com", NotificationLevel.NOTICE, False
    )
    assert_notification_config_matches(
        reservation_two.notifications[2], "test2.com", NotificationLevel.INFO, False
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("retrieval_interval", False),
        ("retrieval_interval", True),
        ("ignoreServerPort", False),
        ("ignoreServerPort", True),
    ],
)
def test_bool_is_rejected_for_integer_only_global_settings(
    mocker: MockerFixture, key: str, value: bool
) -> None:
    """
    isinstance(True, int) is True in Python, so these were silently coerced instead of rejected.
    'retrieval_interval: false' in particular silently disabled monitoring entirely (0 hours) with
    no warning at all.
    """
    mocker.patch("pathlib.Path.read_text", return_value=json.dumps({key: value}))

    config = GlobalConfig()
    with pytest.raises(ConfigError):
        config.initialize_or_raise()


@pytest.mark.parametrize("key", ["companionFarePoints", "originalFarePoints"])
@pytest.mark.parametrize("value", [False, True])
def test_bool_is_rejected_for_integer_only_reservation_settings(
    mocker: MockerFixture, key: str, value: bool
) -> None:
    config = {
        "reservations": [
            {"confirmationNumber": "TEST", "firstName": "Winston", "lastName": "Smith", key: value}
        ]
    }
    mocker.patch("pathlib.Path.read_text", return_value=json.dumps(config))

    global_config = GlobalConfig()
    with pytest.raises(ConfigError):
        global_config.initialize_or_raise()


def test_fare_watch_config(mocker: MockerFixture) -> None:
    config = {
        "fare_watch_interval": 12,
        "fare_watches": [
            {
                "id": "watch1",
                "name": "Thanksgiving MCO",
                "origin": "lga",
                "destination": "mco",
                "date": "2099-11-14",
                "maxPoints": 8000,
                "nonstopOnly": True,
                "fareTypes": ["WGA"],
                "flightNumbers": ["1234"],
            },
            {"origin": "JFK", "destination": "LAX", "date": "2099-01-01", "maxPoints": 10000},
        ],
    }
    mocker.patch("pathlib.Path.read_text", return_value=json.dumps(config))

    global_config = GlobalConfig()
    global_config.initialize()

    assert global_config.fare_watch_interval == 12 * 3600
    assert len(global_config.fare_watches) == 2

    watch_one = global_config.fare_watches[0]
    assert watch_one.id == "watch1"
    assert watch_one.name == "Thanksgiving MCO"
    assert watch_one.origin == "LGA"
    assert watch_one.destination == "MCO"
    assert watch_one.date == "2099-11-14"
    assert watch_one.max_points == 8000
    assert watch_one.nonstop_only is True
    assert watch_one.fare_types == ["WGA"]
    assert watch_one.flight_numbers == ["1234"]
    assert watch_one.enabled is True

    watch_two = global_config.fare_watches[1]
    assert watch_two.id  # auto-generated
    assert watch_two.nonstop_only is False
    assert watch_two.fare_types is None


def test_fare_watch_in_the_past_is_disabled(mocker: MockerFixture) -> None:
    config = {
        "fare_watches": [
            {"origin": "JFK", "destination": "LAX", "date": "2000-01-01", "maxPoints": 8000}
        ]
    }
    mocker.patch("pathlib.Path.read_text", return_value=json.dumps(config))

    global_config = GlobalConfig()
    global_config.initialize()

    assert global_config.fare_watches[0].enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "LGAA"),
        ("destination", "L"),
        ("date", "11-14-2099"),
        ("maxPoints", -100),
        ("maxPoints", 0),
        ("maxPoints", True),
        ("nonstopOnly", "yes"),
    ],
)
def test_invalid_fare_watch_settings_are_rejected(
    mocker: MockerFixture, field: str, value: object
) -> None:
    watch = {"origin": "JFK", "destination": "LAX", "date": "2099-01-01", "maxPoints": 8000}
    watch[field] = value
    mocker.patch("pathlib.Path.read_text", return_value=json.dumps({"fare_watches": [watch]}))

    global_config = GlobalConfig()
    with pytest.raises(ConfigError):
        global_config.initialize_or_raise()


@pytest.mark.parametrize("missing_field", ["origin", "destination", "date", "maxPoints"])
def test_fare_watch_missing_required_field_is_rejected(
    mocker: MockerFixture, missing_field: str
) -> None:
    watch = {"origin": "JFK", "destination": "LAX", "date": "2099-01-01", "maxPoints": 8000}
    del watch[missing_field]
    mocker.patch("pathlib.Path.read_text", return_value=json.dumps({"fare_watches": [watch]}))

    global_config = GlobalConfig()
    with pytest.raises(ConfigError):
        global_config.initialize_or_raise()
