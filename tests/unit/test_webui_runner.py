import pytest
from pytest_mock import MockerFixture

from lib.checkin_scheduler import CheckInScheduler
from lib.config import ReservationConfig
from lib.fare_check_result import FareCheckResult
from lib.flight import Flight
from lib.reservation_monitor import ReservationMonitor
from lib.utils import DriverTimeoutError
from lib.webui import runner


@pytest.fixture
def reservation_config() -> ReservationConfig:
    config = ReservationConfig()
    config.confirmation_number = "ABCDEF"
    config.first_name = "John"
    config.last_name = "Doe"
    return config


@pytest.fixture
def results_store(mocker: MockerFixture) -> MockerFixture:
    return mocker.Mock()


class TestRunCheck:
    def test_saves_and_returns_the_check_payload_on_success(
        self,
        reservation_config: ReservationConfig,
        results_store: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        mocker.patch.object(CheckInScheduler, "refresh_headers")
        flight = mocker.Mock(spec=Flight)
        mocker.patch.object(CheckInScheduler, "fetch_flights", return_value=[flight])
        fare_result = FareCheckResult(
            confirmation_number="ABCDEF",
            flight_number="100",
            departure_airport_code="LAX",
            destination_airport_code="SFO",
            local_departure_date="2024-01-01",
            display_time="10:00 AM",
            is_companion=False,
            status="no_lower_fare",
        )
        mocker.patch.object(
            ReservationMonitor, "check_fares_for_flights", return_value=[fare_result]
        )

        payload = runner.run_check(reservation_config, results_store)

        assert payload["error"] is None
        assert len(payload["flights"]) == 1
        results_store.save_result.assert_called_once_with("ABCDEF", payload)

    def test_reports_a_timeout_refreshing_headers(
        self,
        reservation_config: ReservationConfig,
        results_store: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        mocker.patch.object(
            CheckInScheduler, "refresh_headers", side_effect=DriverTimeoutError("timed out")
        )

        payload = runner.run_check(reservation_config, results_store)

        assert "Timed out refreshing session" in payload["error"]
        results_store.save_result.assert_called_once_with("ABCDEF", payload)

    def test_reports_an_unexpected_error_refreshing_headers(
        self,
        reservation_config: ReservationConfig,
        results_store: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        mocker.patch.object(CheckInScheduler, "refresh_headers", side_effect=RuntimeError("boom"))

        payload = runner.run_check(reservation_config, results_store)

        assert "Failed to refresh session" in payload["error"]
        results_store.save_result.assert_called_once_with("ABCDEF", payload)

    def test_reports_the_scheduler_fetch_error_when_no_flights_found(
        self,
        reservation_config: ReservationConfig,
        results_store: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        mocker.patch.object(CheckInScheduler, "refresh_headers")

        def fake_fetch_flights(self: CheckInScheduler, _confirmation_number: str) -> list:
            self.last_fetch_error = "could not find reservation"
            return []

        mocker.patch.object(CheckInScheduler, "fetch_flights", fake_fetch_flights)

        payload = runner.run_check(reservation_config, results_store)

        assert payload["error"] == "could not find reservation"

    def test_reports_a_default_error_when_no_flights_and_no_fetch_error(
        self,
        reservation_config: ReservationConfig,
        results_store: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        mocker.patch.object(CheckInScheduler, "refresh_headers")
        mocker.patch.object(CheckInScheduler, "fetch_flights", return_value=[])

        payload = runner.run_check(reservation_config, results_store)

        assert payload["error"] == "No upcoming flights found for this reservation"
