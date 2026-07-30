"""
Tests that a single retrieval cycle fetches "now" once, not once per reservation.

get_current_time() makes a real NTP round trip (with up to a 20s timeout if unreachable, and
another 20s trying the backup server). CheckInScheduler._get_flights used to call it once per
confirmation number, so an account with many reservations paid that cost on every single one,
every cycle, instead of sharing one "now" for the whole cycle.
"""

from datetime import datetime, timezone

from pytest_mock import MockerFixture

from lib.config import GlobalConfig
from lib.reservation_monitor import ReservationMonitor

RESERVATION = {
    "viewReservationViewPage": {
        "bounds": [
            {
                "arrivalAirport": {"name": "test_inbound", "country": None},
                "departureAirport": {"code": "LAX", "name": "test_outbound"},
                "departureDate": "2020-10-13",
                "departureTime": "14:40",
                "flights": [{"number": "WN100"}],
            },
        ],
        "_links": {"reaccom": None},
    }
}


def test_one_ntp_call_serves_every_reservation_in_the_cycle(mocker: MockerFixture) -> None:
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": f"TEST{i}", "firstName": "A", "lastName": "B"} for i in range(4)]
    )

    scheduler = ReservationMonitor(
        config.reservations[0], lock=None, send_external=False
    ).checkin_scheduler

    mocker.patch("lib.checkin_scheduler.make_request", return_value=RESERVATION)
    mock_now = mocker.patch(
        "lib.checkin_scheduler.get_current_time",
        return_value=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    scheduler.process_reservations([f"TEST{i}" for i in range(4)])

    assert mock_now.call_count == 1, "one shared 'now' should serve all 4 reservations"


def test_a_lookup_outside_a_cycle_still_gets_a_fresh_timestamp(mocker: MockerFixture) -> None:
    """fetch_flights (used by the web UI for a single on-demand lookup) has no cycle to share."""
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "TEST", "firstName": "A", "lastName": "B"}]
    )
    scheduler = ReservationMonitor(
        config.reservations[0], lock=None, send_external=False
    ).checkin_scheduler

    mocker.patch("lib.checkin_scheduler.make_request", return_value=RESERVATION)
    mock_now = mocker.patch(
        "lib.checkin_scheduler.get_current_time",
        return_value=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    scheduler.fetch_flights("TEST")

    mock_now.assert_called_once()
