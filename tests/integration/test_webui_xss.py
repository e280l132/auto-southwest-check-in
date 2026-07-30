"""
Tests that untrusted data (confirmation numbers, values from query strings) can't execute as JS.

Confirmation numbers are only validated as "must be a non-empty string" (lib/config.py), so nothing
stops one from containing quotes or HTML/JS syntax. Two places embedded such values in a way that
could execute:

- The reservation edit page built a delete-confirmation dialog by interpolating the confirmation
  number into an inline `onsubmit="...confirm('...')"` JS string inside an HTML attribute --
  Jinja's HTML-escaping doesn't protect a JS string embedded in an HTML attribute value, since the
  browser HTML-decodes the attribute before the JS parser ever sees it. A confirmation number
  containing `"><script>...` could close the attribute, close the tag, and inject a fresh element.
- The ignore-link server interpolated query-string values directly into its HTML response body
  with an f-string and no escaping at all. That response is reachable from a link an attacker
  controls (e.g. a spoofed notification), making it reflected rather than stored.
"""

import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

from pytest_mock import MockerFixture

import lib.ignore_server as ignore_server_module
from lib.config import GlobalConfig
from lib.ignore_manager import IgnoreManager
from lib.webui.app import create_app

# Styled for breaking out of an HTML attribute value: closes the quote, closes the tag, injects
# a fresh element. No '/' -- a confirmation number containing one wouldn't route to a single URL
# segment regardless of the XSS question, so it isn't relevant to what this test is checking.
ATTRIBUTE_BREAKOUT_PAYLOAD = 'x"><img src=x onerror=alert(document.cookie)>'

# Styled for injecting straight into HTML element text content.
ELEMENT_INJECTION_PAYLOAD = "<script>alert(document.cookie)</script>"


def test_delete_confirmation_never_builds_js_from_the_confirmation_number(
    mocker: MockerFixture,
) -> None:
    config = GlobalConfig()
    config.create_reservation_config(
        [
            {
                "confirmationNumber": ATTRIBUTE_BREAKOUT_PAYLOAD,
                "firstName": "Berkant",
                "lastName": "Marika",
            }
        ]
    )

    mocker.patch(
        "lib.webui.app.config_writer.read_reservations",
        return_value=[
            {
                "confirmationNumber": ATTRIBUTE_BREAKOUT_PAYLOAD,
                "firstName": "Berkant",
                "lastName": "Marika",
            }
        ],
    )
    mocker.patch("lib.webui.results_store.ResultsStore.get_result", return_value=None)

    app = create_app(config)
    app.config.update(TESTING=True)
    with app.test_client() as client:
        resp = client.get(f"/reservations/{quote(ATTRIBUTE_BREAKOUT_PAYLOAD, safe='')}/edit")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # No live <img onerror> anywhere -- proves the attribute/tag was never actually broken out of
    assert "<img src=x onerror=alert(document.cookie)>" not in html
    # The old vulnerable pattern is gone entirely: no inline event handler at all
    assert "onsubmit=" not in html
    # The value must still reach the page (as an ordinary, safely-escaped attribute), just never
    # as source text inside a <script> or event-handler attribute
    assert "data-confirmation=" in html


def test_ignore_server_escapes_query_string_values(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch.object(ignore_server_module, "_server_thread", None)
    manager = IgnoreManager(filepath=tmp_path / "ignored_flights.json")

    port = 18765
    ignore_server_module.start_ignore_server(port, manager, token=None)
    time.sleep(0.3)  # give the daemon thread a moment to bind and start serving

    query = urlencode(
        {"conf": ELEMENT_INJECTION_PAYLOAD, "date": "2026-08-21", "flight": "100"}
    )
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/ignore?{query}", timeout=5) as resp:
        body = resp.read().decode()

    # No live <script> tag -- proves the value landed as inert text, not as markup
    assert "<script>alert(document.cookie)</script>" not in body
    # The escaped form should still be present, proving the value actually reached the response
    # rather than these assertions passing because the request failed outright
    assert "&lt;script&gt;" in body
