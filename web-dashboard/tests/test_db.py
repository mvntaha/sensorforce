"""Tests for the connection helper, migrations and the start-up check."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app as app_module
from core import db


def latest_migration_version() -> int:
    """The version a freshly bootstrapped database should report.

    Derived from the migration files rather than hard-coded, so adding a
    migration does not silently leave a stale number in the tests.
    """
    versions = [int(path.name[:3]) for path in db.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")]
    return max([1, *versions])


def test_connection_enables_the_required_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_rows_are_accessible_by_column_name(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO zones (name) VALUES (?)", ("Shelf",))
    assert conn.execute("SELECT name FROM zones").fetchone()["name"] == "Shelf"


def test_bootstrap_is_idempotent(db_path: Path) -> None:
    first = db.bootstrap(db_path)
    version = db.schema_version(first)
    rows = first.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    first.close()
    second = db.bootstrap(db_path)
    try:
        # A second bootstrap must neither re-run a migration nor bump the version.
        assert db.schema_version(second) == version
        assert second.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == rows
    finally:
        second.close()


def test_transaction_commits_on_success(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        conn.execute("INSERT INTO zones (name) VALUES (?)", ("Committed",))
    assert conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 1


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    """An alert and its outbox row are written together or not at all."""
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        conn.execute("INSERT INTO zones (name) VALUES (?)", ("Doomed",))
        conn.execute(
            "INSERT INTO outbox (event_id, entity, payload_json, sync_status) "
            "VALUES (?, ?, ?, ?)",
            ("e1", "not-an-entity", "{}", "pending"),
        )
    assert conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_transaction_reraises_the_original_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="boom"), db.transaction(conn):
        conn.execute("INSERT INTO zones (name) VALUES (?)", ("Doomed",))
        raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 0


def test_connection_context_manager_closes(db_path: Path) -> None:
    db.bootstrap(db_path).close()
    with db.connection(db_path) as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_schema_version_of_an_initialised_database(conn: sqlite3.Connection) -> None:
    assert db.schema_version(conn) == latest_migration_version()


def test_apply_migrations_runs_only_newer_files(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = db.schema_version(conn)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # One file at the version already applied, one above it. Only the newer
    # one may run, or a re-bootstrap would replay every migration ever written.
    (migrations / f"{current:03d}_already_applied.sql").write_text(
        "ALTER TABLE zones ADD COLUMN must_not_appear INTEGER;", encoding="utf-8"
    )
    (migrations / f"{current + 1:03d}_add_demo_flag.sql").write_text(
        "ALTER TABLE zones ADD COLUMN demo_flag INTEGER;", encoding="utf-8"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)

    assert db.apply_migrations(conn) == [current + 1]
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(zones)")}
    assert "demo_flag" in columns
    assert "must_not_appear" not in columns
    assert db.schema_version(conn) == current + 1
    # Running again is a no-op.
    assert db.apply_migrations(conn) == []


def test_creates_the_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "does" / "not" / "exist" / "freshguard.db"
    conn = db.bootstrap(nested)
    try:
        assert nested.is_file()
    finally:
        conn.close()


def test_latest_version_matches_the_files_on_disk() -> None:
    assert db.latest_version() == latest_migration_version()


def test_a_bootstrapped_database_has_nothing_pending(conn: sqlite3.Connection) -> None:
    assert db.pending_migrations(conn) == []


def test_pending_migrations_names_what_a_stale_database_is_missing(
    conn: sqlite3.Connection,
) -> None:
    """The name is the point: it says which file to look at."""
    conn.execute("DELETE FROM schema_version WHERE version > 1")
    conn.commit()

    pending = db.pending_migrations(conn)

    assert pending, "a database rolled back to version 1 must report the rest as pending"
    assert all(name.endswith(".sql") for name in pending)
    assert pending == sorted(pending), "migrations are reported in the order they apply"


class TestStartupDatabaseCheck:
    """The app refuses a database it cannot serve, and says why.

    This exists because of a real failure: a database left at version 1 while
    two migrations had landed passed the old check, started fine, and then
    returned 500 from the dashboard and the consume endpoint with "no such
    table: consumption_log". Being behind is a start-up problem, not a
    per-request surprise.
    """

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        problem = app_module.check_database(tmp_path / "nothing.db")
        assert problem is not None
        assert "does not exist" in problem

    def test_a_file_with_no_schema_is_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.db"
        empty.write_bytes(b"")

        problem = app_module.check_database(empty)

        assert problem is not None
        assert "no FreshGuard schema" in problem

    def test_a_current_database_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "current.db"
        db.bootstrap(path).close()

        assert app_module.check_database(path) is None

    def test_a_database_behind_its_migrations_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "stale.db"
        connection = db.bootstrap(path)
        try:
            connection.execute("DELETE FROM schema_version WHERE version > 1")
            connection.commit()
        finally:
            connection.close()

        problem = app_module.check_database(path)

        assert problem is not None
        assert "schema version 1" in problem
        assert str(db.latest_version()) in problem

    def test_the_refusal_names_the_migrations_that_are_missing(self, tmp_path: Path) -> None:
        """A version number alone does not tell anyone what to look at."""
        path = tmp_path / "stale.db"
        connection = db.bootstrap(path)
        try:
            connection.execute("DELETE FROM schema_version WHERE version > 1")
            connection.commit()
            expected = db.pending_migrations(connection)
        finally:
            connection.close()

        problem = app_module.check_database(path)

        assert problem is not None
        for name in expected:
            assert name in problem
