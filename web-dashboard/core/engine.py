"""Freshness decision engine.

Pure functions over plain data: no database, no sensors, no clock of its own.
Everything it needs is passed in, so every rule in the Decision Rule Matrix can
be asserted directly.

The six rules, first match wins:

1. Item expired or storage limit exceeded            -> Check Food
2. Required zone sensor faulty                       -> Sensor Fault/Data Unavailable
3. Zone temp/humidity out of range (confirmed)       -> Check Food
4. Zone gas above abnormal threshold (confirmed)     -> Check Food, message names the ZONE
5. Within use_soon_days of expiry or storage limit   -> Use Soon
6. Otherwise                                         -> Fresh/Normal

Two consequences of that ordering are easy to get wrong and are tested
explicitly: an expired item is Check Food even when a sensor is faulty
(rule 1 precedes rule 2), and a faulty sensor can never produce Fresh
(rule 2 precedes rules 5 and 6).

Gas is a property of the zone. Rule 4 never names an item, because one MQ-135
serves a whole zone and a gas reading cannot be attributed to one item.

Every rule below is covered by tests/test_engine.py, which asserts each rule
in isolation and the precedence between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum


class Status(StrEnum):
    """The freshness statuses shown to the user. The text is the label."""

    FRESH = "Fresh/Normal"
    USE_SOON = "Use Soon"
    CHECK = "Check Food"
    FAULT = "Sensor Fault/Data Unavailable"


#: Sensors an item's status depends on. Gas is excluded: a faulty gas sensor
#: does not make an item's temperature-based status unknown, it only means the
#: zone's gas state is unknown.
REQUIRED_SENSORS = frozenset({"temp", "humidity"})

#: Phrases from the SRS. Never a safe/unsafe verdict.
#: The wording the SRS lists for each outcome. Two of the five belong to a
#: particular cause rather than to a status, so they are kept separately below.
RECOMMENDATIONS = {
    Status.USE_SOON: "Use the food soon",
    Status.CHECK: "Inspect the food before use",
    Status.FAULT: "Check the sensor connection",
}

#: Shown when temperature or humidity is what went out of range. "Check the
#: storage temperature" is an SRS recommendation about the storage conditions,
#: so it belongs to rule 3 and not to a failed sensor, which tells you nothing
#: about the temperature.
CONDITIONS_RECOMMENDATION = "Check the storage temperature"

#: The SRS recommendation for a door left open. Carried by the door alert.
DOOR_RECOMMENDATION = "Close the refrigerator door"

#: Offered only after a person has inspected the food and judged it unusable.
#: Never an automated conclusion: FreshGuard does not decide this.
DISPOSAL_RECOMMENDATION = (
    "If inspection finds the food unsuitable for use, follow the appropriate " "disposal procedure"
)


@dataclass(frozen=True, slots=True)
class CategoryThresholds:
    """Admin-configured limits for one food category.

    Gas limits are deliberately absent: they live on the zone.
    """

    category: str
    temp_min: float
    temp_max: float
    hum_min: float
    hum_max: float
    storage_limit_days: int
    use_soon_days: int


@dataclass(frozen=True, slots=True)
class ZoneState:
    """What is currently known about a zone, after validation.

    The three ``*_breach`` flags are *confirmed* breaches from
    ``core.validation.ConfirmationTracker``, not raw comparisons.
    """

    zone_id: int
    name: str
    faults: frozenset[str] = frozenset()
    temp_breach: bool = False
    humidity_breach: bool = False
    gas_breach: bool = False
    door_open_too_long: bool = False

    @property
    def has_required_fault(self) -> bool:
        """True when a sensor an item's status depends on is unavailable.

        A failed gas sensor does not qualify: it makes the zone's gas state
        unknown, not the item's freshness.
        """
        return bool(self.faults & REQUIRED_SENSORS)


@dataclass(frozen=True, slots=True)
class Item:
    """A stored item, as far as the engine is concerned."""

    id: int
    qr_id: str
    name: str
    category: str
    zone_id: int
    storage_date: date
    expiry_date: date | None = None
    storage_limit_days: int | None = None


@dataclass(frozen=True, slots=True)
class Assessment:
    """The engine's verdict for one item.

    ``rule`` records which of the six rules fired, so the dashboard, the
    Decision Rule Matrix and the tests all refer to the same numbering.
    """

    status: Status
    rule: int
    message: str
    recommendation: str = ""
    #: Set when the message describes the zone rather than the item.
    zone_level: bool = field(default=False)


def effective_deadline(item: Item, thresholds: CategoryThresholds) -> date:
    """Return the date this item must be used by.

    Precedence: the item's own ``expiry_date``, else ``storage_date`` plus the
    item's ``storage_limit_days``, else ``storage_date`` plus the category's
    ``storage_limit_days``. When an explicit expiry date and a storage limit
    both apply, the earlier of the two wins.
    """
    candidates: list[date] = []
    if item.expiry_date is not None:
        candidates.append(item.expiry_date)
    if item.storage_limit_days is not None:
        candidates.append(item.storage_date + timedelta(days=item.storage_limit_days))
    if not candidates:
        candidates.append(item.storage_date + timedelta(days=thresholds.storage_limit_days))
    return min(candidates)


def days_remaining(item: Item, thresholds: CategoryThresholds, today: date) -> int:
    """Return whole days until the deadline. Negative once it has passed."""
    return (effective_deadline(item, thresholds) - today).days


def assess(
    item: Item,
    zone: ZoneState,
    thresholds: CategoryThresholds,
    today: date,
) -> Assessment:
    """Return the freshness verdict for one item, applying the six rules in order."""
    remaining = days_remaining(item, thresholds, today)

    # 1. Past its deadline. A known problem outranks an unknown one, which is
    #    why this is checked before the sensor fault below.
    if remaining < 0:
        return Assessment(
            status=Status.CHECK,
            rule=1,
            message=f"{item.name} is past its use-by date or storage limit",
            recommendation=RECOMMENDATIONS[Status.CHECK],
        )

    # 2. A required sensor is unavailable, so freshness cannot be judged.
    #    Never Fresh: an unknown condition is not a good one.
    if zone.has_required_fault:
        return Assessment(
            status=Status.FAULT,
            rule=2,
            message=f"Sensor data for {zone.name} is unavailable",
            recommendation=RECOMMENDATIONS[Status.FAULT],
            zone_level=True,
        )

    # 3. Storage conditions are confirmed out of range.
    if zone.temp_breach or zone.humidity_breach:
        return Assessment(
            status=Status.CHECK,
            rule=3,
            message=f"Storage conditions in {zone.name} are outside the configured range",
            recommendation=CONDITIONS_RECOMMENDATION,
            zone_level=True,
        )

    # 4. Air quality is confirmed abnormal. One sensor serves the whole zone,
    #    so this names the zone and never an item, and reports no ppm figure.
    if zone.gas_breach:
        return Assessment(
            status=Status.CHECK,
            rule=4,
            message=f"Air quality in {zone.name} is above its normal reading",
            recommendation=RECOMMENDATIONS[Status.CHECK],
            zone_level=True,
        )

    # 5. Approaching the deadline.
    if remaining <= thresholds.use_soon_days:
        return Assessment(
            status=Status.USE_SOON,
            rule=5,
            message=f"{item.name} is within {remaining} day(s) of its use-by date",
            recommendation=RECOMMENDATIONS[Status.USE_SOON],
        )

    # 6. Nothing matched.
    return Assessment(
        status=Status.FRESH,
        rule=6,
        message=f"{item.name} is within its normal storage conditions",
    )


def worst_status(assessments: list[Assessment]) -> Status:
    """Return the most serious status in ``assessments``.

    Severity order, most serious first: Check Food, Sensor Fault/Data
    Unavailable, Use Soon, Fresh/Normal. Check Food outranks a fault because a
    known problem is more actionable than an unknown one. An empty list is
    Fresh/Normal.
    """
    severity = {Status.FRESH: 0, Status.USE_SOON: 1, Status.FAULT: 2, Status.CHECK: 3}
    if not assessments:
        return Status.FRESH
    return max((item.status for item in assessments), key=lambda status: severity[status])
