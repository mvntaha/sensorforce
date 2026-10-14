"""Tests for environment-driven settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import ConfigError, Settings, load_env_file

MINIMAL = {"FG_SECRET_KEY": "x" * 32}


def test_defaults_are_safe_for_a_laptop() -> None:
    settings = Settings.from_env(MINIMAL)
    assert settings.sensor_mode == "sim"
    assert settings.simulated is True
    assert settings.sim_scenario == "normal"
    assert settings.poll_interval_s == 1.0
    assert settings.device_id == "pi-01"


def test_secret_key_is_required_outside_debug() -> None:
    with pytest.raises(ConfigError, match="FG_SECRET_KEY"):
        Settings.from_env({})


def test_debug_mode_falls_back_to_a_development_key() -> None:
    settings = Settings.from_env({"FG_DEBUG": "1"})
    assert settings.debug is True
    assert settings.secret_key


def test_secrets_are_kept_out_of_the_repr() -> None:
    """Logging a settings object must never leak a credential."""
    settings = Settings.from_env(
        MINIMAL | {"SMTP_PASSWORD": "hunter2", "FG_ADMIN_PASSWORD": "letmein"}
    )
    text = f"{settings!r} {settings.smtp!r}"
    assert "hunter2" not in text
    assert "letmein" not in text
    assert "x" * 32 not in text


@pytest.mark.parametrize("mode", ["real", "sim"])
def test_accepts_both_sensor_modes(mode: str) -> None:
    assert Settings.from_env(MINIMAL | {"FG_SENSOR_MODE": mode}).sensor_mode == mode


def test_rejects_an_unknown_sensor_mode() -> None:
    with pytest.raises(ConfigError, match="FG_SENSOR_MODE"):
        Settings.from_env(MINIMAL | {"FG_SENSOR_MODE": "magic"})


def test_rejects_an_unknown_scenario() -> None:
    with pytest.raises(ConfigError, match="FG_SIM_SCENARIO"):
        Settings.from_env(MINIMAL | {"FG_SIM_SCENARIO": "explode"})


def test_rejects_a_non_numeric_port() -> None:
    with pytest.raises(ConfigError, match="FG_PORT"):
        Settings.from_env(MINIMAL | {"FG_PORT": "eighty"})


def test_rejects_a_zero_poll_interval() -> None:
    with pytest.raises(ConfigError, match="FG_POLL_INTERVAL_S"):
        Settings.from_env(MINIMAL | {"FG_POLL_INTERVAL_S": "0"})


def test_rejects_an_unparseable_boolean() -> None:
    with pytest.raises(ConfigError, match="FG_DEBUG"):
        Settings.from_env(MINIMAL | {"FG_DEBUG": "maybe"})


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("0", False)])
def test_boolean_spellings(raw: str, expected: bool) -> None:
    assert Settings.from_env(MINIMAL | {"FG_DEBUG": raw}).debug is expected


def test_relative_database_paths_resolve_against_the_repository() -> None:
    settings = Settings.from_env(MINIMAL | {"FG_DB_PATH": "data/fg.db"})
    assert settings.db_path.is_absolute()
    assert settings.db_path.name == "fg.db"


def test_smtp_is_unconfigured_until_every_part_is_present() -> None:
    assert Settings.from_env(MINIMAL).smtp.configured is False
    full = Settings.from_env(
        MINIMAL
        | {
            "SMTP_HOST": "smtp.example.org",
            "SMTP_USER": "fg@example.org",
            "SMTP_PASSWORD": "app-password",
            "SMTP_TO": "kitchen@example.org",
        }
    )
    assert full.smtp.configured is True
    assert full.smtp.sender == "fg@example.org"


def test_firebase_is_unconfigured_without_a_credential_file(tmp_path: Path) -> None:
    assert Settings.from_env(MINIMAL).firebase.configured is False
    missing = Settings.from_env(
        MINIMAL
        | {"FIREBASE_DB_URL": "https://demo.firebaseio.com", "FIREBASE_CRED_PATH": "nope.json"}
    )
    assert missing.firebase.configured is False

    cred = tmp_path / "svc.json"
    cred.write_text("{}", encoding="utf-8")
    present = Settings.from_env(
        MINIMAL
        | {"FIREBASE_DB_URL": "https://demo.firebaseio.com", "FIREBASE_CRED_PATH": str(cred)}
    )
    assert present.firebase.configured is True


def test_tls_needs_both_halves(tmp_path: Path) -> None:
    assert Settings.from_env(MINIMAL).tls_enabled is False
    half = Settings.from_env(MINIMAL | {"FG_TLS_CERT": str(tmp_path / "c.pem")})
    assert half.tls_enabled is False
    both = Settings.from_env(
        MINIMAL | {"FG_TLS_CERT": str(tmp_path / "c.pem"), "FG_TLS_KEY": str(tmp_path / "k.pem")}
    )
    assert both.tls_enabled is True


def test_load_env_file_ignores_a_missing_file(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "absent.env") is False


def test_load_env_file_does_not_override_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FG_DEVICE_ID=from-file\n", encoding="utf-8")
    monkeypatch.setenv("FG_DEVICE_ID", "from-environment")
    load_env_file(env_file)
    import os

    assert os.environ["FG_DEVICE_ID"] == "from-environment"
