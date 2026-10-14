"""Per-item freshness status for the dashboard, derived from stored readings.

The web app never reads a sensor. It reconstructs the zone's confirmed state by
replaying the most recent stored readings through the same validation code the
monitor uses, so the dashboard and the buzzer agree about what is wrong.

The replay covers a short window rather than the whole history. Over a steady
condition that reaches the same answer as the monitor's long-lived trackers,
which is what matters; a condition that has only just appeared is reported once
it has been confirmed, exactly as the monitor reports it.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from core.engine import Assessment, Item, Status, ZoneState, assess, worst_status
from core.thresholds import gas_limit, humidity_limit, load_all, temperature_limit
from core.validation import ConfirmationTracker

logger = logging.getLogger(__name__)

#: How many stored readings to replay. Enough to confirm a breach several times
#: over without scanning the whole table on every dashboard poll.
REPLAY_READINGS = 12


@dataclass(frozen=True, slots=True)
class ZoneStatus:
    """Everything the dashboard needs to describe one zone."""

    zone_id: int
    name: str
    faults: frozenset[str] = frozenset()
    gas_breach: bool = False
    breaches: dict[str, tuple[bool, bool]] = field(default_factory=dict)
    latest: dict | None = None
    reading_count: int = 0

    @property
    def has_required_fault(self) -> bool:
        """True when a sensor an item's status depends on is unavailable."""
        return bool(self.faults & {"temp", "humidity"})

    def state_for(self, category: str) -> ZoneState:
        """The zone as it applies to one category's limits."""
        temp_breach, humidity_breach = self.breaches.get(category, (False, False))
        return ZoneState(
            zone_id=self.zone_id,
            name=self.name,
            faults=self.faults,
            temp_breach=temp_breach,
            humidity_breach=humidity_breach,
            gas_breach=self.gas_breach,
        )


def _recent_readings(conn: sqlite3.Connection, zone_id: int, limit: int) -> list[sqlite3.Row]:
    """Return the most recent readings for a zone, oldest first."""
    rows = conn.execute(
        "SELECT ts, temp_c, humidity, gas_raw, door_open, temp_ok, hum_ok, gas_ok "
        "FROM readings WHERE zone_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (zone_id, limit),
    ).fetchall()
    return list(reversed(rows))


def zone_status(
    conn: sqlite3.Connection, zone_id: int, name: str, *, replay: int = REPLAY_READINGS
) -> ZoneStatus:
    """Rebuild a zone's confirmed state by replaying its stored readings."""
    rows = _recent_readings(conn, zone_id, replay)
    if not rows:
        # No data at all is not the same as everything being fine.
        return ZoneStatus(zone_id=zone_id, name=name, faults=frozenset({"temp", "humidity", "gas"}))

    thresholds = load_all(conn)
    trackers = {
        category: (
            ConfirmationTracker(limit=temperature_limit(limits)),
            ConfirmationTracker(limit=humidity_limit(limits)),
        )
        for category, limits in thresholds.items()
    }
    gas = gas_limit(conn, zone_id)
    gas_tracker = ConfirmationTracker(limit=gas) if gas is not None else None

    breaches: dict[str, tuple[bool, bool]] = {}
    gas_breach = False
    for row in rows:
        temperature = row["temp_c"] if row["temp_ok"] else None
        moisture = row["humidity"] if row["hum_ok"] else None
        for category, (temp_tracker, hum_tracker) in trackers.items():
            breaches[category] = (
                temp_tracker.update(temperature),
                hum_tracker.update(moisture),
            )
        if gas_tracker is not None:
            gas_breach = gas_tracker.update(row["gas_raw"] if row["gas_ok"] else None)

    last = rows[-1]
    faults = {
        sensor
        for sensor, healthy in (
            ("temp", last["temp_ok"]),
            ("humidity", last["hum_ok"]),
            ("gas", last["gas_ok"]),
        )
        if not healthy
    }

    return ZoneStatus(
        zone_id=zone_id,
        name=name,
        faults=frozenset(faults),
        gas_breach=gas_breach,
        breaches=breaches,
        latest={key: last[key] for key in last.keys()},  # noqa: SIM118 - sqlite3.Row
        reading_count=len(rows),
    )


def zone_statuses(conn: sqlite3.Connection, **kwargs: object) -> dict[int, ZoneStatus]:
    """Rebuild the state of every zone, keyed by zone id."""
    rows = conn.execute("SELECT id, name FROM zones ORDER BY id").fetchall()
    return {
        row["id"]: zone_status(conn, row["id"], row["name"], **kwargs)  # type: ignore[arg-type]
        for row in rows
    }


def to_engine_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        qr_id=row["qr_id"],
        name=row["name"],
        category=row["category"],
        zone_id=row["zone_id"],
        storage_date=date.fromisoformat(row["storage_date"]),
        expiry_date=date.fromisoformat(row["expiry_date"]) if row["expiry_date"] else None,
        storage_limit_days=row["storage_limit_days"],
    )


@dataclass(frozen=True, slots=True)
class ItemStatus:
    """One item plus the verdict the engine reached for it."""

    item: dict
    assessment: Assessment
    #: Whole days the item has been in storage. SRS 1.6 xvii lists storage
    #: duration as a field the dashboard must show, separately from the
    #: expiry information, so it is carried here rather than recomputed in
    #: each template.
    days_stored: int = 0

    def as_dict(self) -> dict:
        """A JSON-ready view for the dashboard."""
        return {
            **self.item,
            "status": str(self.assessment.status),
            "rule": self.assessment.rule,
            "message": self.assessment.message,
            "recommendation": self.assessment.recommendation,
            "zone_level": self.assessment.zone_level,
            "days_stored": self.days_stored,
        }


def _days_stored(storage_date: str | None, today: date) -> int:
    """Whole days since the item was put into storage.

    A missing or unreadable date gives 0 rather than an exception: a display
    field must never be the reason a status cannot be shown.
    """
    if not storage_date:
        return 0
    try:
        stored = date.fromisoformat(str(storage_date))
    except ValueError:
        logger.warning("item has an unreadable storage date %r", storage_date)
        return 0
    return max(0, (today - stored).days)


def item_statuses(
    conn: sqlite3.Connection, *, today: date | None = None, replay: int = REPLAY_READINGS
) -> list[ItemStatus]:
    """Return every stored item with its current freshness verdict."""
    today = today or datetime.now(UTC).date()
    thresholds = load_all(conn)
    zones = zone_statuses(conn, replay=replay)

    # Every row here is something still in the kitchen: an item that has been
    # eaten or thrown away is deleted and recorded in consumption_log instead.
    rows = conn.execute(
        "SELECT id, qr_id, name, category, quantity, unit, zone_id, manufacture_date, "
        "storage_date, expiry_date, storage_limit_days FROM items ORDER BY qr_id"
    ).fetchall()

    statuses: list[ItemStatus] = []
    for row in rows:
        if row["category"] not in thresholds or row["zone_id"] not in zones:
            logger.warning("item %s references missing configuration", row["qr_id"])
            continue
        zone = zones[row["zone_id"]]
        assessment = assess(
            to_engine_item(row),
            zone.state_for(row["category"]),
            thresholds[row["category"]],
            today,
        )
        statuses.append(
            ItemStatus(
                item={key: row[key] for key in row.keys()},  # noqa: SIM118
                assessment=assessment,
                days_stored=_days_stored(row["storage_date"], today),
            )
        )
    return statuses


def overall_status(statuses: list[ItemStatus], zones: dict[int, ZoneStatus]) -> Status:
    """The worst status across every item, including zone-level faults.

    A zone fault is folded in separately so an empty zone with dead sensors is
    not reported as Fresh.
    """
    assessments = [status.assessment for status in statuses]
    for zone in zones.values():
        if zone.has_required_fault:
            assessments.append(
                Assessment(Status.FAULT, 2, f"Sensor data for {zone.name} is unavailable")
            )
    return worst_status(assessments)


def use_first(statuses: list[ItemStatus], limit: int = 5) -> list[ItemStatus]:
    """Items to use first: the most urgent, then the nearest to its deadline.

    Drives the dashboard's use-first list, which is the feature that actually
    reduces waste.
    """
    order = {
        str(Status.CHECK): 0,
        str(Status.USE_SOON): 1,
        str(Status.FAULT): 2,
        str(Status.FRESH): 3,
    }
    return sorted(
        statuses,
        key=lambda status: (
            order.get(str(status.assessment.status), 4),
            status.item.get("expiry_date") or "9999-12-31",
            status.item["qr_id"],
        ),
    )[:limit]


__all__ = [
    "REPLAY_READINGS",
    "ItemStatus",
    "ZoneStatus",
    "item_statuses",
    "to_engine_item",
    "overall_status",
    "use_first",
    "zone_status",
    "zone_statuses",
]
