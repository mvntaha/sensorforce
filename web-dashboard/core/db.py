"""The single place where FreshGuard opens SQLite connections.

``monitor.py`` and the Flask app are separate processes sharing one database
file, so every connection is opened in WAL mode with a busy timeout. Foreign
keys are a per-connection pragma and are switched on here rather than in
``schema.sql``, where they would have no lasting effect.

Callers never build SQL by string formatting; every helper below takes bound
parameters.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from core.config import REPO_ROOT, get_settings

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: How long a writer waits for a competing write lock before giving up.
BUSY_TIMEOUT_MS = 5000


def _configure(conn: sqlite3.Connection) -> None:
    """Apply the pragmas every FreshGuard connection depends on."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Durable enough for this workload and much kinder to the Pi's SD card.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a configured connection. The caller is responsible for closing it.

    ``isolation_level=None`` disables the driver's implicit transactions so
    that :func:`transaction` is the only thing that opens one.
    """
    path = Path(db_path) if db_path is not None else get_settings().db_path
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    _configure(conn)
    return conn


@contextmanager
def connection(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection for the duration of a block and close it afterwards."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one transaction, rolling back on any exception.

    ``BEGIN IMMEDIATE`` takes the write lock up front so two writers fail fast
    instead of deadlocking halfway through. Alerts, item changes and their
    outbox rows are written inside one of these blocks, which is what makes the
    outbox exactly as durable as the change it describes.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def read_schema_sql() -> str:
    """Return the frozen schema DDL."""
    return SCHEMA_PATH.read_text(encoding="utf-8")


def initialise(conn: sqlite3.Connection) -> None:
    """Create every table and index if they are not already present."""
    conn.executescript(read_schema_sql())
    # executescript commits and drops the pragma state on some builds.
    _configure(conn)
    logger.info("schema initialised at version %s", schema_version(conn))


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or 0 for an empty database."""
    row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    return int(row["version"]) if row and row["version"] is not None else 0


def migration_files() -> list[tuple[int, Path]]:
    """Every migration on disk as (version, path), oldest first."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    return [
        (int(path.name[:3]), path) for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    ]


def latest_version() -> int:
    """The version a fully migrated database should be at.

    Derived from the files rather than written down, so adding a migration
    cannot leave a constant behind pointing at the previous one.
    """
    versions = [version for version, _path in migration_files()]
    return max(versions) if versions else 1


def pending_migrations(conn: sqlite3.Connection) -> list[str]:
    """The names of migrations this database has not had applied."""
    current = schema_version(conn)
    return [path.name for version, path in migration_files() if version > current]


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply any ``NNN_name.sql`` migration newer than the current version.

    Returns the versions applied, oldest first. Every migration runs in its own
    transaction together with its ``schema_version`` row, so a failure leaves
    the database on the previous version rather than half-migrated.
    """
    if not MIGRATIONS_DIR.is_dir():
        return []
    current = schema_version(conn)
    applied: list[int] = []
    for version, path in migration_files():
        if version <= current:
            continue
        with transaction(conn):
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        applied.append(version)
        logger.info("applied migration %s", path.name)
    if applied:
        _configure(conn)
    return applied


def bootstrap(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection, create the schema if needed and apply migrations."""
    conn = connect(db_path)
    initialise(conn)
    apply_migrations(conn)
    return conn


__all__ = [
    "MIGRATIONS_DIR",
    "REPO_ROOT",
    "SCHEMA_PATH",
    "apply_migrations",
    "bootstrap",
    "connect",
    "connection",
    "initialise",
    "latest_version",
    "migration_files",
    "pending_migrations",
    "read_schema_sql",
    "schema_version",
    "transaction",
]
