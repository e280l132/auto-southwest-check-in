import json
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from pytest_mock import MockerFixture

from lib.config import GlobalConfig, ReservationConfig
from lib.webui import app as app_module
from lib.webui.app import create_app

JSON = dict[str, Any]

CONFIG_WITH_SECRETS = {
    "$schema": "config.schema.json",
    "notifications": [{"url": "mailto://user:hunter2@example.com"}],
    "accounts": [{"username": "someone", "password": "topsykretts"}],
    "reservations": [
        {"confirmationNumber": "ABCDEF", "firstName": "John", "lastName": "Doe"},
    ],
}


def _reservation_config(confirmation_number: str) -> ReservationConfig:
    config = ReservationConfig()
    config.confirmation_number = confirmation_number
    config.first_name = "John"
    config.last_name = "Doe"
    return config


@pytest.fixture
def global_config() -> GlobalConfig:
    config = GlobalConfig()
    config.reservations = [_reservation_config("ABCDEF")]
    return config


@pytest.fixture
def client(global_config: GlobalConfig, mocker: MockerFixture) -> FlaskClient:
    # Avoid touching the real filesystem for results storage in tests
    mocker.patch("lib.webui.app.ResultsStore")
    app = create_app(global_config)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def config_path(tmp_path: Path, mocker: MockerFixture) -> Path:
    """Point the app at a throwaway config.json so CRUD tests never touch the real one."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG_WITH_SECRETS, indent=4))
    mocker.patch.object(GlobalConfig, "_get_config_file_path", return_value=path)
    return path


@pytest.fixture
def editing_client(config_path: Path, mocker: MockerFixture) -> FlaskClient:  # noqa: ARG001
    """A client backed by a real temp config file, so writes and reloads actually run."""
    mocker.patch("lib.webui.app.ResultsStore")
    mocker.patch("lib.webui.app.IgnoreManager")
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


class TestIndexRoute:
    def test_index_renders_ok(self, client: FlaskClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert b"ABCDEF" in response.data

    def test_index_renders_empty_state_with_no_reservations(self, mocker: MockerFixture) -> None:
        mocker.patch("lib.webui.app.ResultsStore")
        config = GlobalConfig()
        config.reservations = []
        app = create_app(config)
        client = app.test_client()

        response = client.get("/")
        assert response.status_code == 200
        assert b"No flights tracked yet" in response.data

    def test_index_clears_results_by_default(self, mocker: MockerFixture) -> None:
        mock_results_store_cls = mocker.patch("lib.webui.app.ResultsStore")
        app = create_app(GlobalConfig())
        client = app.test_client()

        client.get("/")

        mock_results_store_cls.return_value.clear_all.assert_called_once()

    def test_index_keeps_results_right_after_a_completed_check(
        self, mocker: MockerFixture
    ) -> None:
        mock_results_store_cls = mocker.patch("lib.webui.app.ResultsStore")
        app = create_app(GlobalConfig())
        client = app.test_client()

        with client.session_transaction() as sess:
            sess["keep_results"] = True

        client.get("/")

        mock_results_store_cls.return_value.clear_all.assert_not_called()

    def test_index_keep_results_flag_only_applies_once(self, mocker: MockerFixture) -> None:
        mock_results_store_cls = mocker.patch("lib.webui.app.ResultsStore")
        app = create_app(GlobalConfig())
        client = app.test_client()

        with client.session_transaction() as sess:
            sess["keep_results"] = True

        client.get("/")  # consumes the flag, results kept
        client.get("/")  # flag is gone now, so this load clears

        assert mock_results_store_cls.return_value.clear_all.call_count == 1


class TestCheckOneRoute:
    def test_check_one_starts_job_for_known_reservation(
        self, client: FlaskClient, mocker: MockerFixture
    ) -> None:
        mocker.patch("lib.webui.app.JobManager.start_single_check", return_value="job-123")

        response = client.post("/api/check/ABCDEF")

        assert response.status_code == 200
        assert response.get_json() == {"job_id": "job-123"}

    def test_check_one_404s_for_unknown_confirmation(self, client: FlaskClient) -> None:
        response = client.post("/api/check/UNKNOWN")
        assert response.status_code == 404


class TestCheckAllRoute:
    def test_check_all_starts_job(self, client: FlaskClient, mocker: MockerFixture) -> None:
        mocker.patch("lib.webui.app.JobManager.start_check_all", return_value="job-456")

        response = client.post("/api/check-all")

        assert response.status_code == 200
        assert response.get_json() == {"job_id": "job-456"}

    def test_check_all_404s_with_no_reservations(self, mocker: MockerFixture) -> None:
        mocker.patch("lib.webui.app.ResultsStore")
        config = GlobalConfig()
        config.reservations = []
        app = create_app(config)
        client = app.test_client()

        response = client.post("/api/check-all")
        assert response.status_code == 404


class TestReloadRoute:
    def test_reload_requests_a_config_reload(
        self, client: FlaskClient, mocker: MockerFixture
    ) -> None:
        mock_request_reload = mocker.patch("lib.app_control.request_reload")

        response = client.post("/api/reload")

        assert response.status_code == 200
        assert response.get_json() == {"status": "reloading"}
        mock_request_reload.assert_called_once()


class TestJobStatusRoute:
    def test_job_status_returns_job(self, client: FlaskClient, mocker: MockerFixture) -> None:
        mocker.patch(
            "lib.webui.app.JobManager.get_job",
            return_value={"status": "done", "results": {}, "error": None},
        )

        response = client.get("/api/jobs/job-123")

        assert response.status_code == 200
        assert response.get_json()["status"] == "done"

    def test_job_status_404s_for_unknown_job(
        self, client: FlaskClient, mocker: MockerFixture
    ) -> None:
        mocker.patch("lib.webui.app.JobManager.get_job", return_value=None)

        response = client.get("/api/jobs/does-not-exist")

        assert response.status_code == 404

    def test_job_status_sets_keep_results_flag_when_done(
        self, client: FlaskClient, mocker: MockerFixture
    ) -> None:
        mocker.patch(
            "lib.webui.app.JobManager.get_job",
            return_value={"status": "done", "results": {}, "error": None},
        )

        with client:
            client.get("/api/jobs/job-123")
            with client.session_transaction() as sess:
                assert sess.get("keep_results") is True

    def test_job_status_does_not_set_keep_results_flag_on_error(
        self, client: FlaskClient, mocker: MockerFixture
    ) -> None:
        mocker.patch(
            "lib.webui.app.JobManager.get_job",
            return_value={"status": "error", "results": {}, "error": "boom"},
        )

        with client:
            client.get("/api/jobs/job-123")
            with client.session_transaction() as sess:
                assert "keep_results" not in sess


class TestReservationForms:
    def test_new_reservation_form_renders(self, editing_client: FlaskClient) -> None:
        response = editing_client.get("/reservations/new")

        assert response.status_code == 200
        assert b"originalFarePoints" in response.data

    def test_edit_form_prefills_existing_values(self, editing_client: FlaskClient) -> None:
        response = editing_client.get("/reservations/ABCDEF/edit")

        assert response.status_code == 200
        assert b"ABCDEF" in response.data
        assert b"John" in response.data

    def test_edit_form_404s_for_unknown_reservation(self, editing_client: FlaskClient) -> None:
        assert editing_client.get("/reservations/UNKNOWN/edit").status_code == 404


class TestCreateReservation:
    def test_creates_a_reservation(self, editing_client: FlaskClient, config_path: Path) -> None:
        response = editing_client.post(
            "/api/reservations",
            data={
                "confirmationNumber": "GHIJKL",
                "firstName": "Jane",
                "lastName": "Doe",
                "originalFarePoints": "20000",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        reservations = json.loads(config_path.read_text())["reservations"]
        assert [r["confirmationNumber"] for r in reservations] == ["ABCDEF", "GHIJKL"]
        assert reservations[1]["originalFarePoints"] == 20000

    def test_leaves_accounts_and_notifications_untouched(
        self, editing_client: FlaskClient, config_path: Path
    ) -> None:
        editing_client.post(
            "/api/reservations",
            data={"confirmationNumber": "GHIJKL", "firstName": "Jane", "lastName": "Doe"},
            follow_redirects=True,
        )

        written = json.loads(config_path.read_text())
        assert written["accounts"] == CONFIG_WITH_SECRETS["accounts"]
        assert written["notifications"] == CONFIG_WITH_SECRETS["notifications"]

    def test_rejects_a_duplicate_confirmation_number(
        self, editing_client: FlaskClient, config_path: Path
    ) -> None:
        response = editing_client.post(
            "/api/reservations",
            data={"confirmationNumber": "ABCDEF", "firstName": "John", "lastName": "Doe"},
            follow_redirects=True,
        )

        assert b"already tracked" in response.data
        assert len(json.loads(config_path.read_text())["reservations"]) == 1

    def test_invalid_input_is_reported_and_not_written(
        self, editing_client: FlaskClient, config_path: Path
    ) -> None:
        response = editing_client.post(
            "/api/reservations",
            data={
                "confirmationNumber": "GHIJKL",
                "firstName": "Jane",
                "lastName": "Doe",
                "originalFarePoints": "-5",
            },
            follow_redirects=True,
        )

        assert b"originalFarePoints" in response.data
        assert len(json.loads(config_path.read_text())["reservations"]) == 1


class TestUpdateReservation:
    def test_updates_an_existing_reservation(
        self, editing_client: FlaskClient, config_path: Path
    ) -> None:
        editing_client.post(
            "/api/reservations/ABCDEF",
            data={
                "confirmationNumber": "ABCDEF",
                "firstName": "Jacob",
                "lastName": "Fenster",
                "companionFarePoints": "13500",
            },
            follow_redirects=True,
        )

        reservations = json.loads(config_path.read_text())["reservations"]
        assert reservations[0]["firstName"] == "Jacob"
        assert reservations[0]["companionFarePoints"] == 13500

    def test_404s_for_unknown_reservation(self, editing_client: FlaskClient) -> None:
        response = editing_client.post(
            "/api/reservations/UNKNOWN",
            data={"confirmationNumber": "UNKNOWN", "firstName": "A", "lastName": "B"},
        )

        assert response.status_code == 404

    def test_invalid_input_is_reported_and_not_written(
        self, editing_client: FlaskClient, config_path: Path
    ) -> None:
        editing_client.post(
            "/api/reservations/ABCDEF",
            data={"confirmationNumber": "ABCDEF", "firstName": "", "lastName": "Doe"},
            follow_redirects=True,
        )

        assert json.loads(config_path.read_text())["reservations"][0]["firstName"] == "John"


class TestFixReservationKey:
    @pytest.fixture
    def config_path_with_typo(self, tmp_path: Path, mocker: MockerFixture) -> Path:
        config = {
            "notifications": [],
            "accounts": [],
            "reservations": [
                {
                    "confirmationNumber": "ABCDEF",
                    "firstName": "John",
                    "lastName": "Doe",
                    "checkFares": "same_day_smart",
                }
            ],
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config, indent=4))
        mocker.patch.object(GlobalConfig, "_get_config_file_path", return_value=path)
        return path

    @pytest.fixture
    def typo_client(
        self,
        config_path_with_typo: Path,  # noqa: ARG002 - fixture dependency, drives a patch as a side effect
        mocker: MockerFixture,
    ) -> FlaskClient:
        mocker.patch("lib.webui.app.ResultsStore")
        mocker.patch("lib.webui.app.IgnoreManager")
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    def test_renames_the_key_and_keeps_its_value(
        self, typo_client: FlaskClient, config_path_with_typo: Path
    ) -> None:
        response = typo_client.post(
            "/api/reservations/ABCDEF/fix-key", data={"key": "checkFares"}, follow_redirects=True
        )

        assert response.status_code == 200
        reservation = json.loads(config_path_with_typo.read_text())["reservations"][0]
        assert "checkFares" not in reservation
        assert reservation["check_fares"] == "same_day_smart"

    def test_does_not_disturb_other_fields(
        self, typo_client: FlaskClient, config_path_with_typo: Path
    ) -> None:
        typo_client.post("/api/reservations/ABCDEF/fix-key", data={"key": "checkFares"})

        reservation = json.loads(config_path_with_typo.read_text())["reservations"][0]
        assert reservation["firstName"] == "John"
        assert reservation["lastName"] == "Doe"

    def test_rejects_a_key_with_no_known_correction(self, typo_client: FlaskClient) -> None:
        response = typo_client.post(
            "/api/reservations/ABCDEF/fix-key", data={"key": "someRandomKey"}
        )

        assert response.status_code == 400

    def test_404s_for_unknown_reservation(self, typo_client: FlaskClient) -> None:
        response = typo_client.post("/api/reservations/UNKNOWN/fix-key", data={"key": "checkFares"})

        assert response.status_code == 404


class TestUpdatePaidFare:
    def test_sets_the_paid_fare_inline(
        self, editing_client: FlaskClient, config_path: Path
    ) -> None:
        editing_client.post(
            "/api/reservations/ABCDEF/paid-fare",
            data={"originalFarePoints": "18500", "originalTaxesFees": "5.60"},
            follow_redirects=True,
        )

        reservation = json.loads(config_path.read_text())["reservations"][0]
        assert reservation["originalFarePoints"] == 18500
        assert reservation["originalTaxesFees"] == 5.60
        # Identifying fields are untouched by an inline fare edit
        assert reservation["firstName"] == "John"

    def test_blank_value_clears_the_paid_fare(
        self, editing_client: FlaskClient, config_path: Path
    ) -> None:
        editing_client.post(
            "/api/reservations/ABCDEF/paid-fare",
            data={"originalFarePoints": "18500"},
            follow_redirects=True,
        )
        editing_client.post(
            "/api/reservations/ABCDEF/paid-fare",
            data={"originalFarePoints": ""},
            follow_redirects=True,
        )

        assert "originalFarePoints" not in json.loads(config_path.read_text())["reservations"][0]

    def test_rejects_a_non_numeric_value(
        self, editing_client: FlaskClient, config_path: Path
    ) -> None:
        response = editing_client.post(
            "/api/reservations/ABCDEF/paid-fare",
            data={"originalFarePoints": "lots"},
            follow_redirects=True,
        )

        assert b"originalFarePoints" in response.data
        assert "originalFarePoints" not in json.loads(config_path.read_text())["reservations"][0]

    def test_404s_for_unknown_reservation(self, editing_client: FlaskClient) -> None:
        response = editing_client.post(
            "/api/reservations/UNKNOWN/paid-fare", data={"originalFarePoints": "100"}
        )

        assert response.status_code == 404


class TestDeleteReservation:
    def test_removes_the_reservation(self, editing_client: FlaskClient, config_path: Path) -> None:
        response = editing_client.post("/api/reservations/ABCDEF/delete", follow_redirects=True)

        assert response.status_code == 200
        assert json.loads(config_path.read_text())["reservations"] == []

    def test_purges_the_stored_result(self, editing_client: FlaskClient) -> None:
        editing_client.post("/api/reservations/ABCDEF/delete", follow_redirects=True)

        app_module.ResultsStore.return_value.delete_result.assert_called_with("ABCDEF")

    def test_404s_for_unknown_reservation(self, editing_client: FlaskClient) -> None:
        assert editing_client.post("/api/reservations/UNKNOWN/delete").status_code == 404
