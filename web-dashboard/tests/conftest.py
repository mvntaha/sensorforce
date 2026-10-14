"""Shared fixtures. Every test runs against a throwaway database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from core import db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path to a database file inside the test's temporary directory."""
    return tmp_path / "freshguard.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """An initialised, empty FreshGuard database."""
    connection = db.bootstrap(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def zone_id(conn: sqlite3.Connection) -> int:
    """A zone with documented gas limits."""
    cur = conn.execute(
        "INSERT INTO zones (name, gas_baseline, gas_abnormal, gas_source_note) "
        "VALUES (?, ?, ?, ?)",
        ("Main shelf", 1200, 2500, "prototype/demo assumption"),
    )
    return int(cur.lastrowid)


@pytest.fixture
def category(conn: sqlite3.Connection) -> str:
    """A food category with thresholds attached."""
    conn.execute(
        "INSERT INTO thresholds (category, temp_min, temp_max, hum_min, hum_max, "
        "storage_limit_days, use_soon_days, source_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("dairy", 0.0, 5.0, 30.0, 70.0, 7, 2, "prototype/demo assumption"),
    )
    return "dairy"


@pytest.fixture
def item_id(conn: sqlite3.Connection, zone_id: int, category: str) -> int:
    """A stored item in the test zone."""
    cur = conn.execute(
        "INSERT INTO items (qr_id, name, category, quantity, unit, zone_id, storage_date, "
        "expiry_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("FG-0001", "Milk", category, 2, "L", zone_id, "2026-09-20", "2026-09-27"),
    )
    return int(cur.lastrowid)
