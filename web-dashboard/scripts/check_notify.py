"""Check the remote notification channels, and optionally send a test message.

The SRS makes a remote notification mandatory on a confirmed alert, so the
question "is it actually configured?" needs an answer that does not involve
waiting for the fridge to go wrong. This reports what each channel has and
what it is missing, then sends one clearly-labelled test through each.

The test message says it is a test, in its subject and its body, so a phone
that lights up at two in the morning is never mistaken for a real alert.

Nothing here is a secret: a password is reported as present or absent and is
never printed, and neither is a token.

Usage:
    python scripts/check_notify.py            # report configuration only
    python scripts/check_notify.py --send     # also send a test to each channel
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

from core.config import get_settings  # noqa: E402
from core.notify import Notification, NtfyNotifier, SmtpNotifier  # noqa: E402

logger = logging.getLogger(__name__)

#: Printed instead of a password or a token, so a run can be pasted into the
#: test matrix as evidence without leaking anything.
PRESENT = "set"
ABSENT = "not set"


def _state(value: object) -> str:
    return PRESENT if value else ABSENT


def report_smtp(settings) -> bool:
    """Print what the email channel has, and whether it can be attempted."""
    notifier = SmtpNotifier(settings)
    logger.info("Email (SMTP)")
    logger.info("  host        %s:%s", settings.host or ABSENT, settings.port)
    logger.info("  user        %s", settings.user or ABSENT)
    logger.info("  password    %s", _state(settings.password))
    logger.info("  from        %s", settings.sender or ABSENT)
    logger.info("  to          %s", settings.recipient or ABSENT)
    logger.info("  STARTTLS    %s", "on" if settings.use_tls else "off")
    logger.info("  ready       %s", "yes" if notifier.configured else "NO")
    if not notifier.configured:
        logger.info("  -> set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_TO in .env")
        logger.info("  -> Gmail needs an app password, never the account password")
    return notifier.configured


def report_ntfy(settings) -> bool:
    """Print what the push channel has, and whether it can be attempted."""
    notifier = NtfyNotifier(settings)
    logger.info("Push (ntfy)")
    logger.info("  server      %s", settings.server or ABSENT)
    logger.info("  topic       %s", settings.topic or ABSENT)
    logger.info("  token       %s", _state(settings.token))
    logger.info("  ready       %s", "yes" if notifier.configured else "NO")
    if not notifier.configured:
        logger.info("  -> set NTFY_TOPIC in .env to a long random name")
        logger.info("  -> subscribe to that exact topic in the ntfy app")
    elif settings.server.rstrip("/") == "https://ntfy.sh":
        # Worth saying every time rather than once in a file nobody reopens.
        logger.info("  note        a topic on the public server is readable by anyone")
        logger.info("              who knows its name, and an alert names a zone")
    return notifier.configured


def test_notification() -> Notification:
    """The message sent by --send. It announces itself as a test."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Notification(
        subject="FreshGuard TEST: notification channel check",
        body=(
            "This is a test message from FreshGuard. Nothing is wrong.\n"
            "\n"
            "It was sent by scripts/check_notify.py to prove that this channel\n"
            "delivers, so that a real alert is known to be able to reach you.\n"
            f"\nTime (UTC): {stamp}"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send",
        action="store_true",
        help="send a test message through every configured channel",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()

    smtp_ready = report_smtp(settings.smtp)
    logger.info("")
    ntfy_ready = report_ntfy(settings.ntfy)
    logger.info("")

    if not (smtp_ready or ntfy_ready):
        logger.info("No channel is configured, so a confirmed alert would reach nobody.")
        return 1

    if not args.send:
        logger.info("Re-run with --send to prove delivery.")
        return 0

    message = test_notification()
    results: dict[str, bool] = {}
    if smtp_ready:
        results["email"] = SmtpNotifier(settings.smtp).send(message)
    if ntfy_ready:
        results["ntfy"] = NtfyNotifier(settings.ntfy).send(message)

    logger.info("")
    for channel, delivered in results.items():
        logger.info("  %-6s %s", channel, "delivered" if delivered else "FAILED")

    # Accepted by the server is not the same as arrived on the phone, so the
    # matrix evidence is the screenshot, not this line.
    logger.info("")
    logger.info("Check the inbox and the ntfy app. Delivery is what counts, not this report.")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
