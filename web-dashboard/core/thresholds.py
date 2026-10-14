"""Reading admin-configured limits out of the database.

Nothing here hard-codes a threshold. Every limit an administrator can change
lives in the database and is read through this module, so changing a value
never requires a code change.
"""

from __future__ import annotations

import logging
import sqlite3

from core.engine import CategoryThresholds
from core.validation import Limit

logger = logging.getLogger(__name__)

#: Global settings and their fallbacks, used only when the row is absent.
DOOR_OPEN_TIMEOUT_KEY = "door_open_timeout_s"
DEFAULT_DOOR_OPEN_TIMEOUT_S = 60


class ThresholdError(LookupError):
    """Raised when a required threshold row is missing."""


def _row_to_thresholds(row: sqlite3.Row) -> CategoryThresholds:
    return CategoryThresholds(
        category=row["category"],
        temp_min=float(row["temp_min"]),
        temp_max=float(row["temp_max"]),
        hum_min=float(row["hum_min"]),
        hum_max=float(row["hum_max"]),
        storage_limit_days=int(row["storage_limit_days"]),
        use_soon_days=int(row["use_soon_days"]),
    )


def load_all(conn: sqlite3.Connection) -> dict[str, CategoryThresholds]:
    """Return every category's thresholds, keyed by category name."""
    rows = conn.execute(
        "SELECT category, temp_min, temp_max, hum_min, hum_max, "
        "storage_limit_days, use_soon_days FROM thresholds ORDER BY category"
    ).fetchall()
    return {row["category"]: _row_to_thresholds(row) for row in rows}


def load_for_category(conn: sqlite3.Connection, category: str) -> CategoryThresholds:
    """Return one category's thresholds.

    Raises:
        ThresholdError: when the category has no row. Guessing a limit for an
            unknown category would put a number on the dashboard that no
            source note covers.
    """
    row = conn.execute(
        "SELECT category, temp_min, temp_max, hum_min, hum_max, "
        "storage_limit_days, use_soon_days FROM thresholds WHERE category = ?",
        (category,),
    ).fetchone()
    if row is None:
        raise ThresholdError(f"no thresholds configured for category {category!r}")
    return _row_to_thresholds(row)


def temperature_limit(thresholds: CategoryThresholds) -> Limit:
    """Return the temperature range as a validation limit."""
    return Limit(low=thresholds.temp_min, high=thresholds.temp_max)


def humidity_limit(thresholds: CategoryThresholds) -> Limit:
    """Return the humidity range as a validation limit."""
    return Limit(low=thresholds.hum_min, high=thresholds.hum_max)


def gas_limit(conn: sqlite3.Connection, zone_id: int) -> Limit | None:
    """Return the zone's gas limit, or None when it has not been calibrated.

    Gas limits live on the zone because one sensor serves the whole zone. The
    low end is open: only a rise above the abnormal threshold is meaningful.
    """
    row = conn.execute(
        "SELECT gas_baseline, gas_abnormal FROM zones WHERE id = ?", (zone_id,)
    ).fetchone()
    if row is None:
        raise ThresholdError(f"no zone with id {zone_id}")
    if row["gas_abnormal"] is None:
        logger.info("zone %s has no gas threshold configured", zone_id)
        return None
    return Limit(low=float("-inf"), high=float(row["gas_abnormal"]))


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Return a global setting's value, or ``default`` when it is not set."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insert or replace a global setting."""
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) "
        "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value),
    )


def door_open_timeout_s(conn: sqlite3.Connection) -> int:
    """Return the configured door-open timeout in seconds.

    Falls back to the documented default when unset or unparseable, so a
    missing row cannot stop the door alert working entirely.
    """
    raw = get_setting(conn, DOOR_OPEN_TIMEOUT_KEY)
    if raw is None:
        return DEFAULT_DOOR_OPEN_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not a whole number: %r", DOOR_OPEN_TIMEOUT_KEY, raw)
        return DEFAULT_DOOR_OPEN_TIMEOUT_S
    if value <= 0:
        logger.warning("%s must be positive, got %d", DOOR_OPEN_TIMEOUT_KEY, value)
        return DEFAULT_DOOR_OPEN_TIMEOUT_S
    return value
