from typing import Any

from lib.config import ReservationConfig
from lib.fare_check_result import FareCheckResult
from lib.webui import service

JSON = dict[str, Any]


def _reservation_config(**overrides: Any) -> ReservationConfig:
    config = ReservationConfig()
    config.confirmation_number = overrides.get("confirmation_number", "ABCDEF")
    config.first_name = overrides.get("first_name", "John")
    config.last_name = overrides.get("last_name", "Doe")
    config.companion_fare_points = overrides.get("companion_fare_points")
    config.original_fare_points = overrides.get("original_fare_points")
    config.original_taxes_fees = overrides.get("original_taxes_fees")
    return config


def _board_entry(
    flight_numbers: str,
    difference: int | None,
    *,
    is_current: bool = False,
    points: int | None = None,
) -> JSON:
    """Build a board entry matching FareChecker._board_entry."""
    return {
        "flightNumbers": flight_numbers,
        "displayNumber": flight_numbers,
        "departureTime": "08:00",
        "stopDescription": "Nonstop",
        "isCurrent": is_current,
        "isNonstop": True,
        "points": points,
        "difference": difference,
        "currencyCode": "PTS" if difference is not None else None,
        "unavailable": difference is None and points is None,
        "isCheaper": difference is not None and difference < -1,
    }


def _result(**overrides: Any) -> FareCheckResult:
    defaults = {
        "confirmation_number": "ABCDEF",
        "flight_number": "100",
        "departure_airport_code": "LAX",
        "destination_airport_code": "STL",
        "local_departure_date": "2025-12-01",
        "display_time": "2025-12-01 2:40 PM",
        "is_companion": False,
        "status": "no_lower_fare",
    }
    defaults.update(overrides)
    return FareCheckResult(**defaults)


class TestResolvePaidPoints:
    def test_prefers_original_fare_points(self) -> None:
        config = _reservation_config(original_fare_points=20000, companion_fare_points=14000)
        assert service.resolve_paid_points(config) == (20000, "original")

    def test_falls_back_to_companion_fare_points(self) -> None:
        config = _reservation_config(companion_fare_points=14000)
        assert service.resolve_paid_points(config) == (14000, "companion")

    def test_none_when_neither_set(self) -> None:
        config = _reservation_config()
        assert service.resolve_paid_points(config) == (None, None)


class TestReservationSummary:
    def test_includes_expected_fields(self) -> None:
        config = _reservation_config(
            original_fare_points=20000, original_taxes_fees=11.20, companion_fare_points=14000
        )
        summary = service.reservation_summary(config)

        assert summary["confirmation_number"] == "ABCDEF"
        assert summary["paid_points"] == 20000
        assert summary["paid_points_source"] == "original"
        assert summary["original_taxes_fees"] == 11.20
        assert summary["is_companion_configured"] is True

    def test_surfaces_unrecognized_config_keys(self) -> None:
        config = _reservation_config()
        config.unknown_keys = ["checkFares"]

        assert service.reservation_summary(config)["unknown_keys"] == ["checkFares"]

    def test_flags_known_typos_as_fixable(self) -> None:
        config = _reservation_config()
        config.unknown_keys = ["checkFares"]

        assert service.reservation_summary(config)["fixable_keys"] == {"checkFares": "check_fares"}

    def test_does_not_flag_unknown_keys_with_no_known_correction(self) -> None:
        config = _reservation_config()
        config.unknown_keys = ["someRandomKey"]

        assert service.reservation_summary(config)["fixable_keys"] == {}

    def test_never_leaks_secrets(self) -> None:
        config = _reservation_config()
        config.notifications = ["should-not-appear"]
        summary = service.reservation_summary(config)

        serialized = str(summary)
        assert "notification" not in serialized.lower()
        assert "should-not-appear" not in serialized
        for forbidden in ("username", "password", "headers", "reservation_info"):
            assert forbidden not in summary


class TestFlightViewKeys:
    def test_matches_what_result_view_actually_produces(self) -> None:
        """
        Guards the cache-invalidation contract: if a field is added to a flight view without
        updating FLIGHT_VIEW_KEYS, stale caches would be rendered and 500 on the missing key.
        """
        payload = service.build_check_payload(_reservation_config(), [_result()], checked_at="now")

        assert payload["flights"][0].keys() == set(service.FLIGHT_VIEW_KEYS)


class TestListReservations:
    def test_merges_last_check_from_results_store(self) -> None:
        stored = {
            "checked_at": "now",
            "flights": [],
        }

        class FakeResultsStore:
            def get_all(self) -> JSON:
                return {"ABCDEF": stored}

        class FakeGlobalConfig:
            def __init__(self) -> None:
                self.reservations = [_reservation_config()]

        views = service.list_reservations(FakeGlobalConfig(), FakeResultsStore())

        assert len(views) == 1
        assert views[0]["last_check"] == stored

    def test_discards_a_result_cached_by_an_older_version(self) -> None:
        class FakeResultsStore:
            def get_all(self) -> JSON:
                # A flight view from before the board existed, missing most of today's keys
                return {"ABCDEF": {"checked_at": "now", "flights": [{"status": "no_lower_fare"}]}}

        class FakeGlobalConfig:
            def __init__(self) -> None:
                self.reservations = [_reservation_config()]

        views = service.list_reservations(FakeGlobalConfig(), FakeResultsStore())

        assert views[0]["last_check"] is None

    def test_none_when_never_checked(self) -> None:
        class FakeResultsStore:
            def get_all(self) -> JSON:
                return {}

        class FakeGlobalConfig:
            def __init__(self) -> None:
                self.reservations = [_reservation_config()]

        views = service.list_reservations(FakeGlobalConfig(), FakeResultsStore())
        assert views[0]["last_check"] is None


class TestBuildSummary:
    @staticmethod
    def _view(paid_points: int | None = 20000, flights: list[JSON] | None = None) -> JSON:
        last_check = None if flights is None else {"checked_at": "2026-07-22", "flights": flights}
        return {"paid_points": paid_points, "last_check": last_check}

    def test_counts_tracked_and_checked(self) -> None:
        summary = service.build_summary([self._view(flights=[]), self._view()])

        assert summary["tracked_count"] == 2
        assert summary["checked_count"] == 1

    def test_reports_the_biggest_drop(self) -> None:
        views = [
            self._view(flights=[{"difference_amount": -2500, "cheaper_count": 1}]),
            self._view(flights=[{"difference_amount": -4000, "cheaper_count": 2}]),
            self._view(flights=[{"difference_amount": 1000, "cheaper_count": 0}]),
        ]

        summary = service.build_summary(views)

        assert summary["best_savings"] == 4000
        assert summary["cheaper_count"] == 2

    def test_no_drops_reports_zero(self) -> None:
        summary = service.build_summary(
            [self._view(flights=[{"difference_amount": 0, "cheaper_count": 0}])]
        )

        assert summary["best_savings"] == 0
        assert summary["cheaper_count"] == 0

    def test_counts_reservations_missing_a_paid_fare(self) -> None:
        summary = service.build_summary([self._view(paid_points=None), self._view()])

        assert summary["needs_paid_fare"] == 1

    def test_handles_no_reservations(self) -> None:
        summary = service.build_summary([])

        assert summary["tracked_count"] == 0
        assert summary["best_savings"] == 0
        assert summary["last_checked"] is None


class TestBuildCheckPayload:
    def test_derives_current_fare_and_savings_for_non_companion_result(self) -> None:
        config = _reservation_config(original_fare_points=20000)
        result = _result(
            status="lower_fare",
            currency_code="PTS",
            difference_amount=-3500,
        )

        payload = service.build_check_payload(config, [result], checked_at="now")

        flight = payload["flights"][0]
        assert flight["current_fare_points"] == 16500
        assert flight["savings_amount"] == 3500
        assert flight["savings_currency"] == "PTS"
        assert flight["paid_points"] == 20000

    def test_uses_absolute_current_points_for_companion_result(self) -> None:
        config = _reservation_config(companion_fare_points=14000)
        result = _result(
            is_companion=True,
            status="lower_fare",
            currency_code="PTS",
            difference_amount=-2500,
            current_points=11500,
            paid_points=14000,
        )

        payload = service.build_check_payload(config, [result], checked_at="now")

        flight = payload["flights"][0]
        assert flight["current_fare_points"] == 11500
        assert flight["paid_points"] == 14000
        assert flight["savings_amount"] == 2500

    def test_paid_points_not_tracked_when_unconfigured(self) -> None:
        config = _reservation_config()
        result = _result(status="no_lower_fare", currency_code="USD", difference_amount=0)

        payload = service.build_check_payload(config, [result], checked_at="now")

        flight = payload["flights"][0]
        assert flight["paid_points"] is None
        assert flight["current_fare_points"] is None

    def test_carries_top_level_error(self) -> None:
        config = _reservation_config()
        payload = service.build_check_payload(config, [], checked_at="now", error="boom")
        assert payload["error"] == "boom"
        assert payload["flights"] == []

    def test_board_rows_include_flights_that_are_not_cheaper(self) -> None:
        config = _reservation_config(original_fare_points=20000)
        result = _result(
            status="no_lower_fare",
            board=[
                _board_entry("100", -1500, is_current=True),
                _board_entry("200", 3000),
            ],
        )

        payload = service.build_check_payload(config, [result], checked_at="now")
        board = payload["flights"][0]["board"]

        assert [row["flight_number"] for row in board] == ["100", "200"]
        # Absolute prices are derived from the paid fare for the change-shopping path
        assert [row["points"] for row in board] == [18500, 23000]
        assert [row["savings"] for row in board] == [1500, -3000]

    def test_board_uses_absolute_prices_when_the_search_reports_them(self) -> None:
        config = _reservation_config(companion_fare_points=13500)
        result = _result(
            is_companion=True,
            status="no_lower_fare",
            board=[_board_entry("100", -2500, is_current=True, points=11000)],
        )

        payload = service.build_check_payload(config, [result], checked_at="now")

        assert payload["flights"][0]["board"][0]["points"] == 11000

    def test_top_line_price_comes_from_the_current_flight_row(self) -> None:
        config = _reservation_config(original_fare_points=20000)
        result = _result(
            status="no_lower_fare",
            board=[
                _board_entry("100", 0, is_current=True),
                _board_entry("200", -4000),
            ],
        )

        payload = service.build_check_payload(config, [result], checked_at="now")
        flight = payload["flights"][0]

        # The reported bug: these were n/a whenever nothing was cheaper
        assert flight["current_fare_points"] == 20000
        assert flight["savings_amount"] == 0
        assert flight["cheaper_count"] == 1

    def test_board_row_without_a_fare_is_marked_unavailable(self) -> None:
        config = _reservation_config(original_fare_points=20000)
        result = _result(status="no_lower_fare", board=[_board_entry("200", None)])

        payload = service.build_check_payload(config, [result], checked_at="now")
        row = payload["flights"][0]["board"][0]

        assert row["unavailable"] is True
        assert row["points"] is None
        assert row["savings"] is None

    def test_board_without_paid_points_still_reports_differences(self) -> None:
        config = _reservation_config()
        result = _result(status="no_lower_fare", board=[_board_entry("200", -3000)])

        payload = service.build_check_payload(config, [result], checked_at="now")
        row = payload["flights"][0]["board"][0]

        assert row["points"] is None
        assert row["savings"] == 3000

    def test_alternatives_are_passed_through(self) -> None:
        config = _reservation_config()
        alternatives = [
            {"displayNumber": "200", "savings": {"amount": -1000, "currencyCode": "PTS"}}
        ]
        result = _result(status="lower_fare", alternatives=alternatives)

        payload = service.build_check_payload(config, [result], checked_at="now")

        assert payload["flights"][0]["alternatives"] == alternatives
