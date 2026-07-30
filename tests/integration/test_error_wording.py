"""
Tests that the page blames the right party when a check fails.

A transient Southwest rejection is not something the user can fix by editing their reservation, so
pointing them at the edit form sends them chasing a problem that isn't theirs.
"""

from pytest_mock import MockerFixture

from lib.config import GlobalConfig
from lib.webui.app import create_app

EDIT_PROMPT = "Check the name and confirmation number"
TRANSIENT_PROMPT = "Southwest is rejecting requests right now"


def _page(mocker: MockerFixture, *, error: str, transient: bool) -> str:
    config = GlobalConfig()
    config.create_reservation_config(
        [{"confirmationNumber": "TEST", "firstName": "Berkant", "lastName": "Marika"}]
    )

    mocker.patch(
        "lib.webui.app.service.list_reservations",
        return_value=[
            {
                "confirmation_number": "TEST",
                "display_name": "Berkant Marika",
                "cached_flight": None,
                "last_check": {
                    "checked_at": "2026-07-30T12:00:00+00:00",
                    "error": error,
                    "transient": transient,
                    "flights": [],
                },
            }
        ],
    )
    mocker.patch("lib.webui.app.service.build_summary", return_value={})

    app = create_app(config)
    app.config.update(TESTING=True)
    with app.test_client() as client:
        return client.get("/").get_data(as_text=True)


def test_transient_failure_does_not_blame_the_user(mocker: MockerFixture) -> None:
    html = _page(mocker, error="Forbidden (403)", transient=True)

    assert TRANSIENT_PROMPT in html
    assert EDIT_PROMPT not in html, "a Southwest-side rejection is not the user's to fix"


def test_real_failure_still_points_at_the_reservation_details(mocker: MockerFixture) -> None:
    html = _page(mocker, error="Reservation not found", transient=False)

    assert EDIT_PROMPT in html
    assert TRANSIENT_PROMPT not in html
