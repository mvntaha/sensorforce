"""Seed a zone, food categories and global settings for a demo run.

Every value written here is labelled ``prototype/demo assumption``. None of it
is a food-safety standard, and none of it is cited: it exists so the dashboard
has something to show before the team agrees real limits and records their
sources in docs/threshold_matrix.md.

Nothing is overwritten. A category or setting that already exists is left
alone, so running this against a configured database is safe.

Usage:
    python scripts/seed_thresholds.py [--db PATH] [--force]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

from core import db  # noqa: E402
from core.config import get_settings  # noqa: E402

logger = logging.getLogger(__name__)

#: The label the SRS requires when a threshold has no cited source.
ASSUMPTION = "prototype/demo assumption"

DEMO_ZONE = {
    "name": "Main shelf",
    "gas_baseline": 1200,
    "gas_abnormal": 2500,
    "gas_source_note": ASSUMPTION,
}

#: category -> (temp_min, temp_max, hum_min, hum_max, storage_limit_days, use_soon_days)
DEMO_CATEGORIES: dict[str, tuple[float, float, float, float, int, int]] = {
    "dairy": (0.0, 5.0, 30.0, 70.0, 7, 2),
    "cooked": (0.0, 5.0, 30.0, 70.0, 3, 1),
    "produce": (2.0, 10.0, 60.0, 95.0, 10, 3),
    "rootveg": (2.0, 12.0, 50.0, 90.0, 30, 5),
    "beverage": (0.0, 8.0, 20.0, 80.0, 21, 4),
}

DEMO_SETTINGS = {"door_open_timeout_s": "60"}


def seed(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, int]:
    """Insert the demo configuration. Returns how many rows were added."""
    added = {"zones": 0, "categories": 0, "settings": 0}

    with db.transaction(conn):
        existing_zone = conn.execute(
            "SELECT id FROM zones WHERE name = ?", (DEMO_ZONE["name"],)
        ).fetchone()
        if existing_zone is None:
            conn.execute(
                "INSERT INTO zones (name, gas_baseline, gas_abnormal, gas_source_note) "
                "VALUES (:name, :gas_baseline, :gas_abnormal, :gas_source_note)",
                DEMO_ZONE,
            )
            added["zones"] += 1

        for category, limits in DEMO_CATEGORIES.items():
            present = conn.execute(
                "SELECT 1 FROM thresholds WHERE category = ?", (category,)
            ).fetchone()
            if present and not force:
                continue
            conn.execute(
                "INSERT INTO thresholds (category, temp_min, temp_max, hum_min, hum_max, "
                "storage_limit_days, use_soon_days, source_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(category) DO UPDATE SET temp_min = excluded.temp_min, "
                "temp_max = excluded.temp_max, hum_min = excluded.hum_min, "
                "hum_max = excluded.hum_max, storage_limit_days = excluded.storage_limit_days, "
                "use_soon_days = excluded.use_soon_days, source_note = excluded.source_note",
                (category, *limits, ASSUMPTION),
            )
            added["categories"] += 1

        for key, value in DEMO_SETTINGS.items():
            present = conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone()
            if present and not force:
                continue
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            added["settings"] += 1

    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="database file")
    parser.add_argument("--force", action="store_true", help="overwrite existing rows")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    path = args.db or get_settings().db_path
    conn = db.bootstrap(path)
    try:
        added = seed(conn, force=args.force)
    finally:
        conn.close()

    print(
        f"seeded {added['zones']} zone(s), {added['categories']} categor(ies), "
        f"{added['settings']} setting(s) into {path}"
    )
    print(f"every limit is labelled {ASSUMPTION!r} - cite real sources before submission")
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
