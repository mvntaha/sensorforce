"""When something really happened, even if the Pi's clock is wrong.

Every timestamp the Pi sends comes from its own clock. Without network time,
or with a DS3231 that was never set, a Pi boots believing it is January 2000,
and every reading it sends then looks 26 years old: too old to be evaluated,
so no alert is ever raised, and sorted below everything else on the dashboard.

Firebase gives an independent clock for free. A push key's first eight
characters encode, in milliseconds, when Firebase received the entry. When the
Pi's own timestamp disagrees with that by more than a few minutes, the push
time is the one to believe.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

#: The alphabet Firebase push keys are written in, in sort order.
_PUSH_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"

#: How far the Pi's clock may disagree with Firebase before it is overruled.
#: Generous enough for upload delay and ordinary drift; a reset clock is off by
#: years.
MAX_CLOCK_SKEW_S = 600

#: Beyond this, a timestamp is not late but from a clock that has reset. Used
#: where there is no push key to compare against.
CLOCK_RESET_S = 24 * 60 * 60

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The leading characters of a push key that carry its timestamp.
_TIME_CHARS = 8
_warned: set[str] = set()


def push_key_time(key: str) -> datetime | None:
    """When Firebase received the entry with this push key, or None."""
    if not isinstance(key, str) or len(key) < _TIME_CHARS:
        return None
    millis = 0
    for char in key[:_TIME_CHARS]:
        index = _PUSH_CHARS.find(char)
        if index < 0:
            return None
        millis = millis * 64 + index
    try:
        return datetime.fromtimestamp(millis / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def trusted_ts(key: str, device_ts: object, *, device_id: str = "pi") -> object:
    """The device's timestamp, or the push time when the device's clock is wrong.

    Returns ``device_ts`` unchanged when it agrees with Firebase (or when either
    cannot be read), so a healthy Pi's own timestamps are always kept.
    """
    received = push_key_time(key)
    if received is None:
        return device_ts
    try:
        claimed = datetime.strptime(str(device_ts), _FORMAT).replace(tzinfo=UTC)
    except ValueError:
        # No usable timestamp is the payload's own problem, and is rejected
        # where payloads are checked; this only corrects a clock that is wrong.
        return device_ts
    if abs((claimed - received).total_seconds()) <= MAX_CLOCK_SKEW_S:
        return device_ts

    if device_id not in _warned:
        _warned.add(device_id)
        logger.warning(
            "%s's clock says %s but Firebase received it at %s; using Firebase's time. "
            "Fix the Pi's clock (sudo timedatectl set-ntp true, then sudo hwclock -w).",
            device_id,
            device_ts,
            received.strftime(_FORMAT),
        )
    return received.strftime(_FORMAT)
