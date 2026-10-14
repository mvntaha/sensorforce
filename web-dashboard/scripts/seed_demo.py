"""Fill a database with a small, believable kitchen for the demo and evidence.

This is not test data and it is not evidence. It exists so the screenshots in
the report show a working kitchen rather than one carton of milk, and so a
demonstration has something to walk through. Every figure it writes is
generated here, and the readings are the simulator's own, produced by the same
backend ``monitor.py`` uses.

It refuses to touch a database that already holds items, so it can never
overwrite a real one by accident.

Usage:
    python scripts/seed_demo.py --db var/demo.db --password <admin password>
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

from core import db  # noqa: E402
from core.sensors.sim import SimulatedSensor  # noqa: E402
from core.users import create_user  # noqa: E402
from scripts.seed_thresholds import seed  # noqa: E402

logger = logging.getLogger(__name__)

#: name, category, quantity, unit, days until it runs out, lifecycle state.
#: Only the categories seed_thresholds configures, and spread across the
#: statuses so every badge appears somewhere in the evidence.
DEMO_ITEMS = (
    ("Whole milk", "dairy", 2, "L", 5, "stored"),
    ("Greek yoghurt", "dairy", 500, "g", 1, "stored"),
    ("Leftover curry", "cooked", 1, "pack", 0, "stored"),
    ("Spinach", "produce", 200, "g", 3, "stored"),
    ("Carrots", "rootveg", 1, "kg", 21, "stored"),
    ("Cheddar", "dairy", 250, "g", 14, "stored"),
    ("Tomatoes", "produce", 6, "pcs", -1, "stored"),
    ("Orange juice", "beverage", 1, "L", 8, "stored"),
    ("Rice pudding", "cooked", 1, "pack", 4, "consumed"),
    ("Spring onions", "produce", 1, "pack", -3, "discarded"),
)

#: How many simulated readings to write, so the chart has a line to draw.
READINGS = 90


def populate(conn, *, username: str, password: str) -> dict[str, int]:
    """Fill an empty database. Returns what was written."""
    if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
        raise SystemExit("This database already holds items; refusing to seed over it.")

    seed(conn)
    zone_id = conn.execute("SELECT id FROM zones ORDER BY id LIMIT 1").fetchone()["id"]

    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        create_user(conn, username, password, "admin")

    today = datetime.now(UTC).date()
    with db.transaction(conn):
        for index, (name, category, quantity, unit, days, state) in enumerate(DEMO_ITEMS, start=1):
            conn.execute(
                "INSERT INTO items (qr_id, name, category, quantity, unit, zone_id, "
                "storage_date, expiry_date, state, state_changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"FG-{index:04d}",
                    name,
                    category,
                    quantity,
                    unit,
                    zone_id,
                    (today - timedelta(days=3)).isoformat(),
                    (today + timedelta(days=days)).isoformat(),
                    state,
                    None if state == "stored" else _utc_iso(datetime.now(UTC)),
                ),
            )

    written = _write_readings(conn, zone_id)
    return {"items": len(DEMO_ITEMS), "readings": written}


def _utc_iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_readings(conn, zone_id: int) -> int:
    """Write a run of simulated readings, ending now.

    The simulator produces them, so the values move the way the real backend's
    do rather than being drawn on by hand.
    """
    sensor = SimulatedSensor(scenario="normal", seed=7)
    start = datetime.now(UTC) - timedelta(seconds=READINGS)

    with db.transaction(conn):
        for tick in range(READINGS):
            reading = sensor.read()
            conn.execute(
                "INSERT INTO readings (zone_id, ts, temp_c, humidity, gas_raw, door_open, "
                "temp_ok, hum_ok, gas_ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    zone_id,
                    _utc_iso(start + timedelta(seconds=tick)),
                    reading.temp_c,
                    reading.humidity,
                    reading.gas_raw,
                    int(reading.door_open),
                    # Health, not compliance: a sensor named in faults reports
                    # nothing, and the schema refuses a value without one.
                    int("temp" not in reading.faults),
                    int("humidity" not in reading.faults),
                    int("gas" not in reading.faults),
                ),
            )
    sensor.close()
    return READINGS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = db.bootstrap(args.db)
    try:
        written = populate(conn, username=args.username, password=args.password)
    finally:
        conn.close()

    logger.info(
        "seeded %s items and %s readings into %s", written["items"], written["readings"], args.db
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
