"""
Saving a reservation from the web UI should make the check-in daemon reload config.json on its
own, the same as deleting one does, so companion-fare edits take effect without a manual "Reload
config" click. Inline paid-fare edits only touch display-only fields, so they skip the reload.
"""

from types import SimpleNamespace

import pytest
from pytest_mock import MockerFixture

from lib.webui.app import create_app

CONF = "ABC123"
STORED = {
    "confirmationNumber": CONF,
    "firstName": "Brian",
    "lastName": "Fenster",
    "originalFarePoints": 11500,
}


@pytest.fixture
def client_and_reload(mocker: MockerFixture):  # noqa: ANN201
    # Stand-in for GlobalConfig: the routes only read .reservations and .config_file_path
    fake_config = SimpleNamespace(
        reservations=[SimpleNamespace(confirmation_number=CONF)],
        config_file_path=SimpleNamespace(),
    )
    mocker.patch("lib.webui.app.config_writer.read_reservations", return_value=[dict(STORED)])
    mocker.patch("lib.webui.app.config_writer.write_reservations")
    mocker.patch("lib.webui.results_store.ResultsStore.get_result", return_value=None)
    reload_request = mocker.patch("lib.webui.app.app_control.request_reload")
    mocker.patch("lib.webui.app.ConfigState._load", return_value=fake_config)

    app = create_app(fake_config)
    app.config.update(TESTING=True)
    return app.test_client(), reload_request


def test_saving_companion_fare_requests_daemon_reload(client_and_reload) -> None:  # noqa: ANN001
    client, reload_request = client_and_reload
    resp = client.post(
        f"/api/reservations/{CONF}",
        data={
            "confirmationNumber": CONF,
            "firstName": "Brian",
            "lastName": "Fenster",
            "companionFarePoints": "14000",
        },
    )

    assert resp.status_code == 302
    reload_request.assert_called_once()


def test_inline_paid_fare_edit_does_not_reload_daemon(client_and_reload) -> None:  # noqa: ANN001
    client, reload_request = client_and_reload
    resp = client.post(
        f"/api/reservations/{CONF}/paid-fare",
        data={"originalFarePoints": "12000", "originalTaxesFees": ""},
    )

    assert resp.status_code == 302
    reload_request.assert_not_called()
