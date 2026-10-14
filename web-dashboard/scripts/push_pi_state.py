"""Publish the freshness verdict to Firebase so the Pi can light the right lamp.

The device measures its zone; the server knows the expiry dates. This carries
the second half to the device: which lamp to light, whether to sound, and which
items are behind the decision.

Run it beside the Flask app. It is safe to run twice and safe to restart: each
cycle replaces the device's command node rather than adding to it.

Usage:
    python scripts/push_pi_state.py --once     # publish once, then exit
    python scripts/push_pi_state.py            # loop until interrupted
    python scripts/push_pi_state.py --show     # print it without publishing
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

from core import db  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.pi_commands import build_command, publish  # noqa: E402

logger = logging.getLogger(__name__)

#: Expiry moves in days, not seconds. Half a minute is ample and keeps the
#: cloud write count low.
DEFAULT_INTERVAL_S = 30.0


def _reference(settings):
    """Open a Firebase reference for the database root."""
    import firebase_admin
    from firebase_admin import credentials
    from firebase_admin import db as firebase_db

    name = "freshguard-commands"
    try:
        app = firebase_admin.get_app(name)
    except ValueError:
        app = firebase_admin.initialize_app(
            credentials.Certificate(str(settings.firebase.cred_path)),
            {"databaseURL": settings.firebase.db_url},
            name=name,
        )
    return firebase_db.reference("/", app=app)


def main(argv: list[str] | None = None) -> int:
    """Publish once or in a loop. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="publish one command and exit")
    parser.add_argument("--show", action="store_true", help="print the command, publish nothing")
    parser.add_argument("--device", default=None, help="device id (default: FG_DEVICE_ID)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = get_settings()
    device = args.device or settings.device_id
    conn = db.connect(settings.db_path)

    if args.show:
        try:
            print(json.dumps(build_command(conn, device_id=device), indent=2))
        finally:
            conn.close()
        return 0

    if not settings.firebase.configured:
        logger.error("Firebase is not configured; set FIREBASE_DB_URL and FIREBASE_CRED_PATH")
        conn.close()
        return 1

    reference = _reference(settings)
    try:
        while True:
            try:
                publish(reference, build_command(conn, device_id=device), device_id=device)
            except RuntimeError as exc:
                # The link may fail. The device keeps its last command, which
                # is better than going dark, and the next cycle refreshes it.
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
