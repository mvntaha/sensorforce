"""Phase 0 smoke tests: the repository skeleton is importable and safe to clone.

These guard the tooling itself, not application behaviour. Real engine, API and sync
tests arrive from Phase 1 onwards.
"""

import importlib
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PACKAGES = ["core", "core.sensors", "web", "web.routes"]

EXPECTED_DIRS = [
    "core/migrations",
    "docs/diagrams",
    "docs/evidence",
    "scripts",
    "web/routes",
    "web/static/css",
    "web/static/js",
    "web/static/vendor",
    "web/templates",
    "tests",
]

REQUIRED_ENV_KEYS = [
    "FG_SECRET_KEY",
    "FG_DB_PATH",
    "FG_SENSOR_MODE",
    "FG_DEVICE_ID",
    "SMTP_HOST",
    "FIREBASE_CRED_PATH",
    "FIREBASE_DB_URL",
]


@pytest.mark.parametrize("module_name", PACKAGES)
def test_packages_import(module_name: str) -> None:
    """Every package in the skeleton imports without side effects."""
    assert importlib.import_module(module_name) is not None


@pytest.mark.parametrize("relative_path", EXPECTED_DIRS)
def test_skeleton_directory_exists(relative_path: str) -> None:
    assert (REPO_ROOT / relative_path).is_dir()


def test_env_example_documents_every_required_key() -> None:
    """.env.example is the single source of truth for configuration keys."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert set(REQUIRED_ENV_KEYS) <= declared


def test_env_example_carries_no_secret_values() -> None:
    """The template ships placeholders only, so a clone can never leak credentials."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    secret_keys = ("FG_ADMIN_PASSWORD", "SMTP_PASSWORD", "SMTP_USER", "FIREBASE_CRED_PATH")
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in secret_keys:
            assert value.strip() == "", f"{key.strip()} must be empty in .env.example"


def test_gitignore_excludes_secrets_and_databases() -> None:
    """Secrets, credentials and local databases must never reach the repository."""
    patterns = set((REPO_ROOT / ".gitignore").read_text(encoding="utf-8").split())
    for pattern in (".env", "*.db", "__pycache__/", "*serviceAccount*.json"):
        assert pattern in patterns


def test_tooling_is_configured() -> None:
    """ruff and pytest are configured in pyproject.toml, not by ad-hoc flags."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tools = config["tool"]
    assert tools["ruff"]["line-length"] > 0
    assert tools["ruff"]["lint"]["select"]
    assert tools["pytest"]["ini_options"]["testpaths"] == ["tests"]
