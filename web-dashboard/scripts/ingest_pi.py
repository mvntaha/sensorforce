"""Pull the Raspberry Pi's Firebase readings into SQLite and alert on them.

Run this alongside the Flask app when the Pi publishes to Firebase rather than
writing to the local database directly. Each cycle fetches the readings the Pi
has pushed since the last one and passes each through the monitor, so it is
stored, assessed against the zone's limits and, when a condition has just
become true, sent to email and ntfy within seconds of the Pi taking it.

Do not also run monitor.py for the same zone: it would write the simulator's
readings, and raise alerts on them, beside the Pi's.

Safe to run twice, and safe to stop and restart: identity is the reading's
timestamp, so nothing is stored a second time.

Usage:
    python scripts/ingest_pi.py --once        # one cycle, then exit
    python scripts/ingest_pi.py               # loop until interrupted
    python scripts/ingest_pi.py --zone 2      # store into a different zone
    python scripts/ingest_pi.py --no-alerts   # store only, never notify
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

from core import db  # noqa: E402
from core.alerts import retry_pending  # noqa: E402
from core.cloud_ingest import (  # noqa: E402
    CloudSensor,
    IngestError,
    fetch_payloads,
    fetch_snapshot,
    ingest,
    ingest_live,
    save_snapshot,
)
from core.config import get_settings  # noqa: E402
from core.paths import snapshot_path  # noqa: E402
from core.rfid import RfidBridge, RfidCloudError  # noqa: E402
from core.snapshots import SnapshotBridge, SnapshotCloudError  # noqa: E402
from monitor import build_monitor  # noqa: E402

logger = logging.getLogger(__name__)

#: Seconds between cycles. The device publishes every 5 to 15 s; after the first
#: cycle only readings newer than the last one seen are fetched, so a short
#: interval costs one small request and keeps alerts within seconds of the Pi.
DEFAULT_INTERVAL_S = 2.0

#: The camera frame is a convenience, so it is fetched less often than readings.
SNAPSHOT_EVERY_S = 15.0

#: How often undelivered notifications are retried. Not every cycle: while the
#: mail server is unreachable each attempt can take seconds to time out.
RETRY_EVERY_S = 60.0


def _reference(settings):
    """Open a Firebase reference for the database root."""
    import firebase_admin
    from firebase_admin import credentials
    from firebase_admin import db as firebase_db

    name = "freshguard-ingest"
    try:
        app = firebase_admin.get_app(name)
    except ValueError:
        app = firebase_admin.initialize_app(
            credentials.Certificate(str(settings.firebase.cred_path)),
            # A call that cannot finish gives up rather than stalling every
            # reading, alert and snapshot behind it.
            {"databaseURL": settings.firebase.db_url, "httpTimeout": 10},
            name=name,
        )
    return firebase_db.reference("/", app=app)


class Worker:
    """One ingest cycle at a time, keeping what must survive between cycles.

    The monitor lives as long as the worker, so confirmation streaks and the
    set of conditions already alerting carry over from one poll to the next.
    """

    def __init__(self, settings, conn, reference, args) -> None:
        self.conn = conn
        self.reference = reference
        self.args = args
        self.device = args.device or settings.device_id
        self.sensor: CloudSensor | None = None
        self.monitor = None
        if not args.no_alerts:
            self.sensor = CloudSensor()
            self.monitor = build_monitor(settings, conn, args.zone, sensor=self.sensor)
            self.monitor.device_id = self.device
            logger.info(
                "evaluating %s readings for alerts in %s", self.device, self.monitor.zone_name
            )
        self.snapshots = SnapshotBridge(conn, reference, self.device)
        self.rfid = RfidBridge(conn, reference, self.device)
        self.last_key: str | None = None
        self.last_snapshot = self.last_retry = float("-inf")

    def cycle(self) -> None:
        """Fetch what is new, store and evaluate it, and refresh the camera frame."""
        # Snapshots first, and on their own: a person is watching the page
        # waiting for the photograph, and a readings failure must not hold it up.
        try:
            self.snapshots.cycle()
        except SnapshotCloudError as exc:
            logger.warning("%s", exc)

        # RFID taps next, also on their own, for the same reason: a tag tapped
        # in should reach the dashboard's banner even if readings are failing.
        try:
            self.rfid.cycle()
        except RfidCloudError as exc:
            logger.warning("%s", exc)

        payloads = fetch_payloads(self.reference, self.device, after=self.last_key)
        if payloads:
            self.last_key = max(payloads)

        if self.monitor is not None:
            result = ingest_live(self.monitor, self.sensor, payloads)
            if time.monotonic() - self.last_retry >= RETRY_EVERY_S:
                # Anything that failed to send earlier, say while offline.
                self.last_retry = time.monotonic()
                retry_pending(self.conn, self.monitor.notifier)
        else:
            result = ingest(
                self.conn,
                payloads,
                self.args.zone,
                device_id=self.device,
                queue_event=self.args.mirror,
            )
        if result.stored or result.rejected:
            logger.info(
                "stored %d, already had %d, unusable %d",
                result.stored,
                result.skipped,
                result.rejected,
            )

        if not self.args.no_snapshot and time.monotonic() - self.last_snapshot >= SNAPSHOT_EVERY_S:
            # The photograph is a convenience, never a measurement, so a camera
            # problem must not stop readings being ingested.
            self.last_snapshot = time.monotonic()
            frame = fetch_snapshot(self.reference, self.device)
            if frame is not None:
                try:
                    save_snapshot(frame, snapshot_path(self.device))
                except OSError as exc:
                    logger.warning("could not save the camera frame: %s", exc)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--zone", type=int, default=1, help="zone to store readings into")
    parser.add_argument("--device", default=None, help="device id (default: FG_DEVICE_ID)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument(
        "--mirror",
        action="store_true",
        help="with --no-alerts, also queue each ingested reading to the outbox",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="skip the camera frame, and fetch readings only",
    )
    parser.add_argument(
        "--no-alerts",
        action="store_true",
        help="store readings only; do not evaluate them or send notifications",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Ingest once or in a loop. Returns a process exit code."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = get_settings()
    if not settings.firebase.configured:
        logger.error("Firebase is not configured; set FIREBASE_DB_URL and FIREBASE_CRED_PATH")
        return 1

    # Bootstrap rather than connect: it applies any pending migration, so a
    # new table such as the snapshots one exists before the first cycle.
    conn = db.bootstrap(settings.db_path)
    try:
        worker = Worker(settings, conn, _reference(settings), args)
        while True:
            try:
                worker.cycle()
            except IngestError as exc:
                # The link is allowed to fail. The device keeps publishing and
                # the next cycle picks up whatever was missed.
                logger.warning("%s", exc)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        logger.info("stopped")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
