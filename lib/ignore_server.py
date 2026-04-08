from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from .log import get_logger

if TYPE_CHECKING:
    from .ignore_manager import IgnoreManager

logger = get_logger(__name__)

_server_thread: threading.Thread | None = None


def start_ignore_server(
    port: int, ignore_manager: IgnoreManager, token: str | None = None
) -> None:
    """
    Start the ignore HTTP server as a daemon thread. Idempotent — safe to call
    multiple times. Binds to 0.0.0.0 so Docker port mapping works.

    If token is provided, all requests must include ?token=<value> or they receive 401.

    Endpoints:
      GET /ignore?conf=ABCDEF&date=2025-12-01&flight=100[&token=...]
      GET /ignore-all?conf=ABCDEF&date=2025-12-01[&token=...]
    """
    global _server_thread
    if _server_thread is not None and _server_thread.is_alive():
        return

    class IgnoreHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            # Token validation — if a token is configured, every request must supply it
            if token is not None:
                provided = (params.get("token") or [None])[0]
                if provided != token:
                    self._respond("Unauthorized — invalid or missing token.", status=401)
                    return

            conf = (params.get("conf") or [None])[0]
            flight_date = (params.get("date") or [None])[0]
            flight = (params.get("flight") or [None])[0]

            if parsed.path == "/ignore" and conf and flight_date and flight:
                ignore_manager.ignore_flight(conf, flight_date, flight)
                display_flight = flight.replace("\u200b", "")
                self._respond(
                    f"Done! Flight {display_flight} on {flight_date} "
                    f"for reservation {conf} will no longer appear in fare alerts."
                )

            elif parsed.path == "/ignore-all" and conf and flight_date:
                ignore_manager.ignore_all_day(conf, flight_date)
                self._respond(
                    f"Done! All cheaper alternate flights on {flight_date} "
                    f"for reservation {conf} will no longer appear in fare alerts."
                )

            else:
                self._respond("Invalid request — missing required parameters.", status=400)

        def _respond(self, message: str, status: int = 200) -> None:
            body = (
                "<html><body style='font-family:sans-serif;padding:2em'>"
                f"<h2>&#10003; {message}</h2>"
                "<p><a href='javascript:window.close()'>Close this tab</a></p>"
                "</body></html>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            logger.debug("Ignore server: " + format, *args)

    try:
        server = HTTPServer(("0.0.0.0", port), IgnoreHandler)
    except OSError as err:
        logger.error("Could not start ignore server on port %d: %s", port, err)
        return

    _server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="IgnoreServer")
    _server_thread.start()
    logger.info("Ignore server started on port %d (http://localhost:%d)", port, port)
