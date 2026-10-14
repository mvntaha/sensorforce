"""Measure the local alert path: first bad reading to local output.

The SRS asks for a local alert within 2-5 seconds of a confirmed threshold
violation. "Confirmed" means three consecutive bad readings, and the monitor
polls once a second, so the floor is about two seconds by design; this script
measures what it actually is, on this machine, over a number of runs.

It measures the local path only. The buzzer, the lamps and the display are
driven by the controller with no network involved, which is the point: the
figure must not depend on a cloud round trip.

Timing is wall-clock, so the poll interval is real rather than simulated, and
the outputs are the recording fakes: the sensor and the siren are what differ
from the fridge, and everything between them is the shipped code.

Usage:
    python scripts/measure_alert_path.py --runs 20
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

from core import db  # noqa: E402
from core.notify import FakeNotifier  # noqa: E402
from core.outputs import BuzzerPattern, fake_outputs  # noqa: E402
from core.sensors.sim import SimulatedSensor  # noqa: E402
from monitor import Monitor  # noqa: E402
from scripts.seed_thresholds import seed  # noqa: E402

logger = logging.getLogger(__name__)

#: The SRS window, in seconds.
LOWER_BOUND_S = 2.0
UPPER_BOUND_S = 5.0

#: Enough ticks for three consecutive bad readings plus the tick that alerts.
MAX_TICKS = 6


def one_run(db_path: Path, scenario: str) -> float:
    """Return the seconds from the first reading to the buzzer sounding."""
    conn = db.bootstrap(db_path)
    try:
        seed(conn)
        zone_id = conn.execute("SELECT id FROM zones ORDER BY id LIMIT 1").fetchone()["id"]
        category = conn.execute(
            "SELECT category FROM thresholds ORDER BY category LIMIT 1"
        ).fetchone()["category"]
        conn.execute(
            "INSERT INTO items (qr_id, name, category, quantity, unit, zone_id, storage_date) "
            "VALUES ('FG-0001', 'Measured item', ?, 1, 'pcs', ?, date('now'))",
            (category, zone_id),
        )

        monitor = Monitor(
            conn,
            sensor=SimulatedSensor(scenario=scenario),
            outputs=fake_outputs(),
            notifier=FakeNotifier(),
            zone_id=zone_id,
            zone_name="Measured zone",
            device_id="pi-measure",
        )

        # Tick by tick with a real sleep between polls, stopping the clock the
        # moment the buzzer sounds. Running the whole loop and timing that
        # would measure the loop, not the alert.
        started = time.perf_counter()
        for tick in range(MAX_TICKS):
            if tick:
                time.sleep(monitor.poll_interval_s)
            monitor.tick()
            if BuzzerPattern.THRESHOLD in monitor.outputs.buzzer.patterns:
                elapsed = time.perf_counter() - started
                monitor.sensor.close()
                return elapsed

        raise RuntimeError(
            f"the {scenario} scenario raised no local alert within {MAX_TICKS} ticks"
        )
    finally:
        conn.close()


def measure(runs: int, scenario: str, workspace: Path) -> list[float]:
    """Run the scenario ``runs`` times and return every elapsed time."""
    timings = []
    for run in range(1, runs + 1):
        db_path = workspace / f"alert-path-{run:02d}.db"
        timings.append(one_run(db_path, scenario))
        logger.info("run %2d: %.3f s", run, timings[-1])
    return timings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--scenario", default="high_temp")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=REPO_ROOT / "var" / "alert-path",
        help="Where the throwaway databases go. One per run.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args.workspace.mkdir(parents=True, exist_ok=True)

    timings = measure(args.runs, args.scenario, args.workspace)
    low, high = min(timings), max(timings)
    median = statistics.median(timings)
    inside = sum(1 for t in timings if LOWER_BOUND_S <= t <= UPPER_BOUND_S)

    logger.info("")
    logger.info("scenario   %s", args.scenario)
    logger.info("runs       %d", len(timings))
    logger.info("min        %.3f s", low)
    logger.info("median     %.3f s", median)
    logger.info("max        %.3f s", high)
    logger.info("inside %.0f-%.0f s   %d of %d", LOWER_BOUND_S, UPPER_BOUND_S, inside, len(timings))

    # A run outside the window is the finding, so it decides the exit code.
    return 0 if inside == len(timings) else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
