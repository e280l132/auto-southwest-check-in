"""
Fare-check results are cleared on a plain browser page load, but an uptime monitor polling the
page must not wipe them: it landed between a check finishing and the browser's post-check reload,
so the results never appeared.
"""

from pytest_mock import MockerFixture

from lib.config import GlobalConfig
from lib.webui.app import create_app

BROWSER_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/150.0 Safari/537.36"


def _client(mocker: MockerFixture):  # noqa: ANN202
    clear_all = mocker.patch("lib.webui.results_store.ResultsStore.clear_all")
    mocker.patch("lib.webui.results_store.ResultsStore.get_all", return_value={})
    mocker.patch("lib.webui.app.config_writer.read_reservations", return_value=[])
    app = create_app(GlobalConfig())
    app.config.update(TESTING=True)
    return app.test_client(), clear_all


def test_monitor_poll_does_not_clear_results(mocker: MockerFixture) -> None:
    client, clear_all = _client(mocker)
    with client:
        resp = client.get("/", headers={"User-Agent": "Basecamp/0.1"})

    assert resp.status_code == 200
    clear_all.assert_not_called()


def test_browser_load_clears_results(mocker: MockerFixture) -> None:
    client, clear_all = _client(mocker)
    with client:
        resp = client.get("/", headers={"User-Agent": BROWSER_UA})

    assert resp.status_code == 200
    clear_all.assert_called()
