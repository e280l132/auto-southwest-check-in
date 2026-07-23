import json
from pathlib import Path

import pytest

from lib.config import ConfigError
from lib.webui import config_writer

EXISTING_CONFIG = {
    "$schema": "config.schema.json",
    "check_fares": "same_day_smart",
    "notifications": [{"url": "mailto://user:hunter2@example.com"}],
    "retrieval_interval": 12,
    "accounts": [{"username": "someone", "password": "topsykretts"}],
    "reservations": [
        {"confirmationNumber": "ABCDEF", "firstName": "John", "lastName": "Doe"},
    ],
}


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(EXISTING_CONFIG, indent=4))
    return path


class TestReadConfig:
    def test_raises_when_the_file_is_not_a_json_object(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(["not", "a", "dict"]))

        with pytest.raises(ValueError, match="JSON dictionary"):
            config_writer.read_config(path)


class TestReadReservations:
    def test_reads_the_reservations_list(self, config_path: Path) -> None:
        assert config_writer.read_reservations(config_path) == EXISTING_CONFIG["reservations"]

    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        assert config_writer.read_reservations(tmp_path / "nope.json") == []

    def test_missing_reservations_key_returns_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"check_fares": True}))
        assert config_writer.read_reservations(path) == []


class TestWriteReservations:
    def test_preserves_every_other_key(self, config_path: Path) -> None:
        config_writer.write_reservations(
            config_path, [{"confirmationNumber": "GHIJKL", "firstName": "A", "lastName": "B"}]
        )

        written = json.loads(config_path.read_text())

        # Credentials and notification URLs must survive a UI-driven write untouched
        assert written["accounts"] == EXISTING_CONFIG["accounts"]
        assert written["notifications"] == EXISTING_CONFIG["notifications"]
        assert written["$schema"] == EXISTING_CONFIG["$schema"]
        assert written["check_fares"] == EXISTING_CONFIG["check_fares"]
        assert written["retrieval_interval"] == EXISTING_CONFIG["retrieval_interval"]

    def test_replaces_the_reservations_list(self, config_path: Path) -> None:
        reservations = [{"confirmationNumber": "GHIJKL", "firstName": "A", "lastName": "B"}]
        config_writer.write_reservations(config_path, reservations)

        assert json.loads(config_path.read_text())["reservations"] == reservations

    def test_does_not_leave_temp_files_behind(self, config_path: Path) -> None:
        config_writer.write_reservations(config_path, [])

        assert [p.name for p in config_path.parent.iterdir()] == ["config.json"]

    def test_writes_to_a_config_that_does_not_exist_yet(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        reservations = [{"confirmationNumber": "GHIJKL", "firstName": "A", "lastName": "B"}]

        config_writer.write_reservations(path, reservations)

        assert json.loads(path.read_text()) == {"reservations": reservations}


class TestRenameKey:
    def test_renames_the_key_preserving_value(self) -> None:
        reservation = {
            "confirmationNumber": "ABCDEF",
            "checkFares": "same_day_smart",
            "firstName": "John",
        }

        renamed = config_writer.rename_key(reservation, "checkFares", "check_fares")

        assert "checkFares" not in renamed
        assert renamed["check_fares"] == "same_day_smart"

    def test_preserves_key_order(self) -> None:
        reservation = {
            "confirmationNumber": "ABCDEF",
            "checkFares": "same_day_smart",
            "firstName": "John",
        }

        renamed = config_writer.rename_key(reservation, "checkFares", "check_fares")

        assert list(renamed) == ["confirmationNumber", "check_fares", "firstName"]

    def test_is_a_no_op_when_the_key_is_absent(self) -> None:
        reservation = {"confirmationNumber": "ABCDEF", "firstName": "John"}

        renamed = config_writer.rename_key(reservation, "checkFares", "check_fares")

        assert renamed == reservation


class TestValidateReservation:
    def test_accepts_a_valid_reservation(self) -> None:
        config_writer.validate_reservation(
            {
                "confirmationNumber": "ABCDEF",
                "firstName": "John",
                "lastName": "Doe",
                "originalFarePoints": 20000,
                "originalTaxesFees": 11.20,
            }
        )

    @pytest.mark.parametrize(
        "reservation",
        [
            {"firstName": "John", "lastName": "Doe"},
            {"confirmationNumber": "ABCDEF", "lastName": "Doe"},
            {
                "confirmationNumber": "ABCDEF",
                "firstName": "John",
                "lastName": "Doe",
                "originalFarePoints": 0,
            },
            {
                "confirmationNumber": "ABCDEF",
                "firstName": "John",
                "lastName": "Doe",
                "check_fares": "not_a_mode",
            },
        ],
    )
    def test_rejects_invalid_reservations(self, reservation: dict) -> None:
        with pytest.raises(ConfigError):
            config_writer.validate_reservation(reservation)


class TestMergeReservation:
    def test_preserves_keys_the_parser_does_not_recognize(self) -> None:
        stored = {
            "confirmationNumber": "ABCDEF",
            "firstName": "John",
            "lastName": "Doe",
            "checkFares": "same_day_smart",
        }
        submitted = {"confirmationNumber": "ABCDEF", "firstName": "Jane", "lastName": "Doe"}

        merged = config_writer.merge_reservation(stored, submitted)

        # A typo'd key stays in the file rather than being silently deleted by a UI save
        assert merged["checkFares"] == "same_day_smart"
        assert merged["firstName"] == "Jane"

    def test_clears_editable_fields_left_out_of_the_submission(self) -> None:
        stored = {
            "confirmationNumber": "ABCDEF",
            "firstName": "John",
            "lastName": "Doe",
            "originalFarePoints": 20000,
        }
        submitted = {"confirmationNumber": "ABCDEF", "firstName": "John", "lastName": "Doe"}

        merged = config_writer.merge_reservation(stored, submitted)

        assert "originalFarePoints" not in merged

    def test_keeps_original_key_order(self) -> None:
        stored = {
            "confirmationNumber": "ABCDEF",
            "checkFares": "same_day",
            "firstName": "John",
            "lastName": "Doe",
        }
        submitted = {"confirmationNumber": "ABCDEF", "firstName": "John", "lastName": "Doe"}

        merged = config_writer.merge_reservation(stored, submitted)

        assert list(merged) == ["confirmationNumber", "checkFares", "firstName", "lastName"]


class TestBuildReservation:
    def test_keeps_only_editable_fields(self) -> None:
        reservation = config_writer.build_reservation(
            {
                "confirmationNumber": "ABCDEF",
                "firstName": "John",
                "lastName": "Doe",
                "notifications": "should-be-ignored",
                "username": "should-be-ignored",
            }
        )

        assert reservation == {
            "confirmationNumber": "ABCDEF",
            "firstName": "John",
            "lastName": "Doe",
        }

    def test_omits_blank_optional_fields(self) -> None:
        reservation = config_writer.build_reservation(
            {
                "confirmationNumber": "ABCDEF",
                "firstName": "John",
                "lastName": "Doe",
                "check_fares": "",
                "companionFarePoints": "",
                "originalFarePoints": "",
                "originalTaxesFees": "",
            }
        )

        assert "companionFarePoints" not in reservation
        assert "originalFarePoints" not in reservation
        assert "originalTaxesFees" not in reservation
        assert "check_fares" not in reservation

    def test_converts_numeric_fields(self) -> None:
        reservation = config_writer.build_reservation(
            {
                "confirmationNumber": "ABCDEF",
                "firstName": "John",
                "lastName": "Doe",
                "companionFarePoints": "13500",
                "originalFarePoints": "20000",
                "originalTaxesFees": "11.20",
            }
        )

        assert reservation["companionFarePoints"] == 13500
        assert reservation["originalFarePoints"] == 20000
        assert reservation["originalTaxesFees"] == 11.20

    def test_raises_a_readable_error_for_a_bad_number(self) -> None:
        with pytest.raises(ValueError, match="originalFarePoints"):
            config_writer.build_reservation(
                {
                    "confirmationNumber": "ABCDEF",
                    "firstName": "John",
                    "lastName": "Doe",
                    "originalFarePoints": "twenty thousand",
                }
            )
