"""Signal validation: fault detection, smoothing, confirmation and hysteresis.

Raw sensor readings are noisy and occasionally wrong. Nothing downstream may
act on a single reading, so this module turns a stream of readings into a small
number of confirmed facts:

* which sensors are faulty, so a fault is never mistaken for a measurement;
* a smoothed value, so one spike does not move a status;
* whether a limit breach has been *confirmed* by three consecutive bad
  readings, and whether a previously confirmed breach has *cleared*, using
  hysteresis so a value sitting on the limit does not flap;
* whether the door has been open longer than the configured timeout, reported
  once rather than on every poll.

Everything here is pure or holds only its own small state, so the monitor loop
can be tested without hardware and without sleeping.

Every function below is covered by tests/test_validation.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from core.sensors.base import SENSOR_FIELDS, Reading, is_plausible

#: Consecutive out-of-range readings required before a breach is acted on.
CONFIRMATION_READINGS = 3

#: How many readings the moving average covers.
DEFAULT_SMOOTHING_WINDOW = 5

#: A confirmed breach only clears once the value is back inside the limit by
#: this margin, so a value resting on the limit does not flap between states.
DEFAULT_HYSTERESIS = 0.5

#: Identical consecutive readings beyond this count mean the sensor is stuck.
STUCK_READING_LIMIT = 10

#: Values within this distance count as identical when detecting a stuck sensor.
STUCK_TOLERANCE = 0.001


@dataclass(frozen=True, slots=True)
class Limit:
    """An inclusive acceptable range for one measurement."""

    low: float
    high: float

    def breaches(self, value: float) -> bool:
        """True when ``value`` is outside the range.

        The range is inclusive, so a value resting exactly on a limit is
        acceptable and only a value beyond it counts.
        """
        return not (self.low <= value <= self.high)

    def clears(self, value: float, hysteresis: float = DEFAULT_HYSTERESIS) -> bool:
        """True when ``value`` is back inside the range by ``hysteresis``.

        A value merely back inside the range is not enough: it must be inside
        by the hysteresis margin, otherwise a reading sitting on the limit
        alternates between breaching and clearing on every poll.
        """
        return (self.low + hysteresis) <= value <= (self.high - hysteresis)


def detect_faults(reading: Reading) -> frozenset[str]:
    """Return the sensors in ``reading`` that did not produce a usable value.

    A sensor is faulty when the driver reported an error, returned ``None``, or
    returned a physically impossible value. The returned set uses the names in
    ``core.sensors.base.SENSOR_FIELDS``.

    The reading already guarantees that a value is absent exactly when its
    sensor is listed as faulty; the plausibility check is repeated here so a
    driver that reports a wild value is caught even if it claims success.
    """
    values: dict[str, float | int | None] = {
        "temp": reading.temp_c,
        "humidity": reading.humidity,
        "gas": reading.gas_raw,
    }
    faults = set(reading.faults)
    for name in SENSOR_FIELDS:
        value = values[name]
        if value is None or not is_plausible(name, value):
            faults.add(name)
    return frozenset(faults)


def moving_average(
    values: Sequence[float | None], window: int = DEFAULT_SMOOTHING_WINDOW
) -> float | None:
    """Return the mean of the last ``window`` non-None values.

    ``None`` entries are gaps from faulty reads and are skipped rather than
    treated as zero. Returns ``None`` when the window holds no usable value.
    """
    if window <= 0:
        return None
    usable = [value for value in list(values)[-window:] if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def is_flatlined(
    values: Sequence[float | None],
    limit: int = STUCK_READING_LIMIT,
    tolerance: float = STUCK_TOLERANCE,
) -> bool:
    """True when the last ``limit`` readings are all identical within ``tolerance``.

    A sensor that never changes is almost certainly stuck rather than reporting
    a genuinely constant environment, so this counts as a fault.

    A window that is not yet full, or that contains a gap, is not enough
    evidence to call the sensor stuck.
    """
    if limit <= 0:
        return False
    recent = list(values)[-limit:]
    if len(recent) < limit or any(value is None for value in recent):
        return False
    return (max(recent) - min(recent)) <= tolerance  # type: ignore[type-var]


@dataclass(slots=True)
class ConfirmationTracker:
    """Turns a stream of values into a confirmed breach state.

    A breach becomes confirmed only after ``required`` consecutive readings are
    outside the limit. Once confirmed it stays confirmed until a reading clears
    the limit by the hysteresis margin. A faulty reading (``None``) breaks the
    streak without clearing an existing confirmation, because an unknown value
    is not evidence that the problem went away.
    """

    limit: Limit
    required: int = CONFIRMATION_READINGS
    hysteresis: float = DEFAULT_HYSTERESIS
    confirmed: bool = field(default=False, init=False)
    streak: int = field(default=0, init=False)

    def update(self, value: float | None) -> bool:
        """Feed one reading and return whether a breach is currently confirmed.

        A faulty reading breaks the streak, because an unknown value is not
        evidence of a breach, but it never clears an existing confirmation,
        because it is not evidence that the problem ended either.
        """
        if value is None:
            self.streak = 0
            return self.confirmed

        if self.limit.breaches(value):
            self.streak += 1
            if self.streak >= self.required:
                self.confirmed = True
            return self.confirmed

        self.streak = 0
        if self.confirmed and self.limit.clears(value, self.hysteresis):
            self.confirmed = False
        return self.confirmed

    def reset(self) -> None:
        """Forget the streak and any confirmed breach."""
        self.confirmed = False
        self.streak = 0


@dataclass(slots=True)
class DoorTracker:
    """Reports the door being open past the timeout exactly once per opening."""

    timeout_s: float
    opened_at: float | None = field(default=None, init=False)
    alerted: bool = field(default=False, init=False)

    def update(self, door_open: bool, now: float) -> bool:
        """Feed the door state and return True only on the poll that first
        exceeds the timeout.

        Closing the door rearms the tracker, so the next long opening alerts
        again. While the door stays open past the timeout this keeps returning
        False, so the buzzer is not retriggered every second.
        """
        if not door_open:
            self.opened_at = None
            self.alerted = False
            return False

        if self.opened_at is None:
            self.opened_at = now
            return False

        if self.alerted:
            return False

        if (now - self.opened_at) >= self.timeout_s:
            self.alerted = True
            return True
        return False
