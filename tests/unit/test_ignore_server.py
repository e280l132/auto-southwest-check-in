import logging
import socket
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests

import lib.ignore_server as ignore_server_module
from lib.ignore_manager import IgnoreManager
from lib.ignore_server import start_ignore_server

FUTURE_DATE = (date.today() + timedelta(days=30)).isoformat()


def _free_port() -> int:
    """Pick a free TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture
def ignore_manager(tmp_path: Path) -> IgnoreManager:
    return IgnoreManager(filepath=tmp_path / "test_ignored.json")


@pytest.fixture
def server_port(ignore_manager: IgnoreManager) -> int:
    """Start a real ignore server on a free port and return the port."""
    ignore_server_module._server_thread = None  # reset module-level state between tests

    port = _free_port()
    start_ignore_server(port, ignore_manager)
    # Give the daemon thread a moment to bind
    time.sleep(0.1)
    return port


class TestIgnoreServer:
    def test_ignore_flight_endpoint_calls_ignore_manager(
        self, server_port: int, ignore_manager: IgnoreManager
    ) -> None:
        url = f"http://localhost:{server_port}/ignore?conf=ABCDEF&date={FUTURE_DATE}&flight=100"
        resp = requests.get(url, timeout=5)
        assert resp.status_code == 200
        assert ignore_manager.is_ignored("ABCDEF", FUTURE_DATE, "100")

    def test_ignore_all_endpoint_calls_ignore_all_day(
        self, server_port: int, ignore_manager: IgnoreManager
    ) -> None:
        url = f"http://localhost:{server_port}/ignore-all?conf=ABCDEF&date={FUTURE_DATE}"
        resp = requests.get(url, timeout=5)
        assert resp.status_code == 200
        assert ignore_manager.is_day_ignored("ABCDEF", FUTURE_DATE)

    def test_missing_params_returns_400(self, server_port: int) -> None:
        url = f"http://localhost:{server_port}/ignore?conf=ABCDEF"
        resp = requests.get(url, timeout=5)
        assert resp.status_code == 400

    def test_unknown_path_returns_400(self, server_port: int) -> None:
        resp = requests.get(f"http://localhost:{server_port}/unknown", timeout=5)
        assert resp.status_code == 400

    def test_start_is_idempotent(self, server_port: int, ignore_manager: IgnoreManager) -> None:
        """Calling start_ignore_server a second time should not raise or start another thread."""
        thread_before = ignore_server_module._server_thread
        start_ignore_server(server_port, ignore_manager)
        assert ignore_server_module._server_thread is thread_before

    def test_request_without_token_returns_401_when_token_configured(
        self, ignore_manager: IgnoreManager
    ) -> None:
        ignore_server_module._server_thread = None

        port = _free_port()
        start_ignore_server(port, ignore_manager, token="mysecret")
        time.sleep(0.1)

        url = f"http://localhost:{port}/ignore?conf=ABCDEF&date={FUTURE_DATE}&flight=100"
        resp = requests.get(url, timeout=5)
        assert resp.status_code == 401

    def test_request_with_wrong_token_returns_401(self, ignore_manager: IgnoreManager) -> None:
        ignore_server_module._server_thread = None

        port = _free_port()
        start_ignore_server(port, ignore_manager, token="mysecret")
        time.sleep(0.1)

        url = (
            f"http://localhost:{port}/ignore?conf=ABCDEF&date={FUTURE_DATE}&flight=100&token=wrong"
        )
        resp = requests.get(url, timeout=5)
        assert resp.status_code == 401

    def test_start_logs_error_when_port_unavailable(
        self, ignore_manager: IgnoreManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        ignore_server_module._server_thread = None

        port = _free_port()
        # Occupy the port so HTTPServer's bind fails with OSError
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("0.0.0.0", port))
        blocker.listen(1)
        try:
            with caplog.at_level(logging.ERROR, logger="lib.ignore_server"):
                start_ignore_server(port, ignore_manager)
            assert ignore_server_module._server_thread is None
            assert "Could not start ignore server" in caplog.text
        finally:
            blocker.close()

    def test_request_with_correct_token_succeeds(self, ignore_manager: IgnoreManager) -> None:
        ignore_server_module._server_thread = None

        port = _free_port()
        start_ignore_server(port, ignore_manager, token="mysecret")
        time.sleep(0.1)

        url = (
            f"http://localhost:{port}/ignore"
            f"?conf=ABCDEF&date={FUTURE_DATE}&flight=100&token=mysecret"
        )
        resp = requests.get(url, timeout=5)
        assert resp.status_code == 200
        assert ignore_manager.is_ignored("ABCDEF", FUTURE_DATE, "100")
