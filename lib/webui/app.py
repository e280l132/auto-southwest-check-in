"""
Local, unauthenticated Flask web UI for viewing tracked reservations, triggering fare checks, and
editing the reservations section of config.json. See README.md "Web UI" for how to start it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .. import daemon_status
from ..config import ConfigError, GlobalConfig, ReservationConfig
from ..ignore_manager import IgnoreManager
from ..log import get_logger
from . import config_writer, service
from .jobs import JobManager
from .results_store import ResultsStore

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class ConfigState:
    """
    Holds the GlobalConfig the UI renders from, so it can be swapped after an edit without
    restarting the process.
    """

    def __init__(self, config: GlobalConfig | None = None) -> None:
        self.config = config if config is not None else self._load()

    @property
    def config_path(self) -> Path:
        return self.config.config_file_path

    def reload(self) -> None:
        self.config = self._load()

    def find_reservation(self, confirmation_number: str) -> ReservationConfig | None:
        for reservation_config in self.config.reservations:
            if reservation_config.confirmation_number == confirmation_number:
                return reservation_config
        return None

    @staticmethod
    def _load() -> GlobalConfig:
        """
        Build a GlobalConfig without GlobalConfig.initialize(), which calls sys.exit(1) on a bad
        config file — fatal inside a web request. This raises ConfigError instead.
        """
        config = GlobalConfig()
        config.initialize_or_raise()
        return config


def create_app(config: GlobalConfig | None = None) -> Flask:
    app = Flask(__name__)
    # Only used to sign flash-message cookies for a local, unauthenticated UI.
    app.secret_key = os.urandom(32)

    state = ConfigState(config)

    results_store = ResultsStore()
    job_manager = JobManager(results_store)

    def _require_reservation(confirmation_number: str) -> ReservationConfig:
        reservation_config = state.find_reservation(confirmation_number)
        if reservation_config is None:
            abort(404, description=f"No tracked reservation '{confirmation_number}'")
        return reservation_config

    def _stored_reservation(confirmation_number: str) -> dict | None:
        """
        The reservation exactly as it appears in config.json, including any keys the parser
        doesn't recognize, so editing round-trips them instead of dropping them.
        """
        return next(
            (
                r
                for r in config_writer.read_reservations(state.config_path)
                if r.get("confirmationNumber") == confirmation_number
            ),
            None,
        )

    def _save(reservations: list[dict], success_message: str) -> Response:
        """Persist reservations, reload the in-memory config, and report the outcome."""
        try:
            config_writer.write_reservations(state.config_path, reservations)
            state.reload()
        except (OSError, ConfigError, ValueError) as err:
            logger.exception("Failed to save reservations")
            flash(f"Could not save configuration: {err}", "error")
            return redirect(url_for("index"))

        flash(success_message, "success")
        return redirect(url_for("index"))

    @app.route("/")
    def index() -> str:
        # Fare-check results are cleared on every page load except the one right after a check
        # finishes (flagged below in job_status), so a plain browser refresh always starts clean.
        if session.pop("keep_results", False):
            logger.debug("Keeping fare-check results for the post-check page load")
        else:
            results_store.clear_all()

        reservations = service.list_reservations(state.config, results_store)
        return render_template(
            "index.html",
            reservations=reservations,
            summary=service.build_summary(reservations),
            daemon_running=daemon_status.is_running(),
        )

    # ------------------------------------------------------------------
    # Fare checks
    # ------------------------------------------------------------------

    @app.route("/api/check/<confirmation_number>", methods=["POST"])
    def check_one(confirmation_number: str) -> Response:
        reservation_config = _require_reservation(confirmation_number)
        return jsonify({"job_id": job_manager.start_single_check(reservation_config)})

    @app.route("/api/check-all", methods=["POST"])
    def check_all() -> Response:
        if not state.config.reservations:
            abort(404, description="No tracked reservations configured")
        return jsonify({"job_id": job_manager.start_check_all(list(state.config.reservations))})

    @app.route("/api/jobs/<job_id>")
    def job_status(job_id: str) -> Response:
        job = job_manager.get_job(job_id)
        if job is None:
            abort(404, description=f"No job with id '{job_id}'")

        if job["status"] == "done":
            # The client reloads '/' right after seeing this, to show the result -- that one load
            # should keep the result it just computed rather than immediately clearing it. Only
            # "done" reloads (see app.js); "error" doesn't, so it doesn't need the flag.
            session["keep_results"] = True

        return jsonify(job)

    # ------------------------------------------------------------------
    # Reservation editing
    # ------------------------------------------------------------------

    @app.route("/reservations/new")
    def new_reservation() -> str:
        return render_template("reservation_form.html", reservation=None)

    @app.route("/reservations/<confirmation_number>/edit")
    def edit_reservation(confirmation_number: str) -> str:
        reservation_config = _require_reservation(confirmation_number)
        stored = _stored_reservation(confirmation_number)
        if stored is None:
            abort(404, description=f"No tracked reservation '{confirmation_number}'")

        # A failed lookup is usually a name or confirmation-number typo, so show Southwest's own
        # wording right next to the fields that would fix it.
        last_check = results_store.get_result(confirmation_number) or {}

        unknown_keys = list(getattr(reservation_config, "unknown_keys", []))
        corrections = ReservationConfig.KEY_CORRECTIONS

        return render_template(
            "reservation_form.html",
            reservation=stored,
            unknown_keys=unknown_keys,
            fixable_keys={key: corrections[key] for key in unknown_keys if key in corrections},
            last_error=last_check.get("error"),
        )

    @app.route("/api/reservations/<confirmation_number>/fix-key", methods=["POST"])
    def fix_reservation_key(confirmation_number: str) -> Response:
        """
        Rename a known-typo key (e.g. 'checkFares' -> 'check_fares') in place. Only keys listed in
        ReservationConfig.KEY_CORRECTIONS are accepted, so this can't be used to rename arbitrary
        keys based on a guess.
        """
        _require_reservation(confirmation_number)
        stored = _stored_reservation(confirmation_number)
        if stored is None:
            abort(404, description=f"No tracked reservation '{confirmation_number}'")

        old_key = request.form.get("key", "")
        new_key = ReservationConfig.KEY_CORRECTIONS.get(old_key)
        if new_key is None:
            abort(400, description=f"No known correction for '{old_key}'")

        updated = config_writer.rename_key(stored, old_key, new_key)
        try:
            config_writer.validate_reservation(updated)
        except (ConfigError, ValueError) as err:
            flash(str(err), "error")
            return redirect(url_for("index"))

        reservations = [
            updated if r.get("confirmationNumber") == confirmation_number else r
            for r in config_writer.read_reservations(state.config_path)
        ]
        return _save(reservations, f"Renamed '{old_key}' to '{new_key}'")

    @app.route("/api/reservations/<confirmation_number>/paid-fare", methods=["POST"])
    def update_paid_fare(confirmation_number: str) -> Response:
        """
        Inline update of just the paid fare, from the flight board. Reservations are one-way, so
        the reservation's paid fare is the flight's paid fare.
        """
        _require_reservation(confirmation_number)
        stored = _stored_reservation(confirmation_number)
        if stored is None:
            abort(404, description=f"No tracked reservation '{confirmation_number}'")

        updated = dict(stored)
        try:
            for field in ("originalFarePoints", "originalTaxesFees"):
                value = (request.form.get(field) or "").strip()
                if value:
                    updated[field] = config_writer.parse_number(
                        field, value, as_int=field == "originalFarePoints"
                    )
                else:
                    # Blank clears the value rather than writing null
                    updated.pop(field, None)

            config_writer.validate_reservation(updated)
        except (ConfigError, ValueError) as err:
            flash(str(err), "error")
            return redirect(url_for("index"))

        reservations = [
            updated if r.get("confirmationNumber") == confirmation_number else r
            for r in config_writer.read_reservations(state.config_path)
        ]
        return _save(reservations, f"Updated the paid fare for {confirmation_number}")

    @app.route("/api/reservations", methods=["POST"])
    def create_reservation() -> Response:
        try:
            reservation = config_writer.build_reservation(request.form)
            config_writer.validate_reservation(reservation)
        except (ConfigError, ValueError) as err:
            flash(str(err), "error")
            return redirect(url_for("new_reservation"))

        reservations = config_writer.read_reservations(state.config_path)
        confirmation_number = reservation["confirmationNumber"]

        if any(r.get("confirmationNumber") == confirmation_number for r in reservations):
            flash(f"Reservation '{confirmation_number}' is already tracked", "error")
            return redirect(url_for("new_reservation"))

        reservations.append(reservation)
        return _save(reservations, f"Added reservation {confirmation_number}")

    @app.route("/api/reservations/<confirmation_number>", methods=["POST"])
    def update_reservation(confirmation_number: str) -> Response:
        _require_reservation(confirmation_number)

        try:
            submitted = config_writer.build_reservation(request.form)
            # Merge onto the stored entry so keys the parser doesn't recognize (and therefore
            # aren't on the form) survive the write instead of being dropped
            reservation = config_writer.merge_reservation(
                _stored_reservation(confirmation_number) or {}, submitted
            )
            config_writer.validate_reservation(reservation)
        except (ConfigError, ValueError) as err:
            flash(str(err), "error")
            return redirect(url_for("edit_reservation", confirmation_number=confirmation_number))

        reservations = [
            reservation if r.get("confirmationNumber") == confirmation_number else r
            for r in config_writer.read_reservations(state.config_path)
        ]

        # The confirmation number itself is editable, so clean up state keyed to the old one
        if reservation["confirmationNumber"] != confirmation_number:
            results_store.delete_result(confirmation_number)

        return _save(reservations, f"Updated reservation {reservation['confirmationNumber']}")

    @app.route("/api/reservations/<confirmation_number>/delete", methods=["POST"])
    def delete_reservation(confirmation_number: str) -> Response:
        _require_reservation(confirmation_number)

        reservations = [
            r
            for r in config_writer.read_reservations(state.config_path)
            if r.get("confirmationNumber") != confirmation_number
        ]

        response = _save(reservations, f"Removed reservation {confirmation_number}")

        # Drop state belonging to a reservation that is no longer tracked
        results_store.delete_result(confirmation_number)
        IgnoreManager().cleanup_confirmations(
            {r.confirmation_number for r in state.config.reservations}
        )
        return response

    # ------------------------------------------------------------------
    # App control
    # ------------------------------------------------------------------

    @app.route("/api/reload", methods=["POST"])
    def reload_config() -> Response:
        """
        Ask the check-in daemon to reload config.json, so edits made here take effect without
        restarting the app. See lib.app_control for how the reload actually happens.
        """
        from .. import app_control  # noqa: PLC0415 (avoid import cost when webui unused)

        app_control.request_reload()
        return jsonify({"status": "reloading"})

    return app
