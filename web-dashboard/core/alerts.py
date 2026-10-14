"""Raising an alert: persist it, drive the local outputs, then notify.

The order matters. An alert row and its outbox row are written in one
transaction, so the cloud copy is exactly as durable as the local record and a
crash can never leave one without the other. Only then are the buzzer, lamps
and display driven, and only after that is the email attempted, because the
network is the slowest and least reliable part and must never delay the local
warning.

A notification that could not be delivered leaves the alert flagged, so the
sync worker can retry it when the connection returns.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass

from core import db
from core.notify import Notifier, build_alert_notification
from core.outputs import BuzzerPattern, OutputSet
from core.sensors.base import utc_now_iso

logger = logging.getLogger(__name__)

#: Alert types the schema accepts.
ALERT_TYPES = (
    "temp_high",
    "temp_low",
    "humidity_high",
    "humidity_low",
    "gas_abnormal",
    "door_open",
    "sensor_fault",
    "expiry",
    "storage_limit",
)

SEVERITIES = ("info", "warning", "critical")

#: Types that describe the zone as a whole and must never name an item.
ZONE_LEVEL_TYPES = frozenset({"gas_abnormal", "door_open", "sensor_fault"})

#: Which buzzer pattern each alert type uses.
DOOR_TYPES = frozenset({"door_open"})


@dataclass(frozen=True, slots=True)
class Alert:
    """An alert ready to be raised."""

    zone_id: int
    zone_name: str
    type: str
    severity: str
    message: str
    item_id: int | None = None
    item_name: str | None = None
    event_id: str = ""
    ts: str = ""

    def __post_init__(self) -> None:
        if self.type not in ALERT_TYPES:
            raise ValueError(f"unknown alert type {self.type!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")
        if self.type in ZONE_LEVEL_TYPES and self.item_id is not None:
            raise ValueError(f"{self.type} describes a zone and must not name an item")
        object.__setattr__(self, "event_id", self.event_id or str(uuid.uuid4()))
        object.__setattr__(self, "ts", self.ts or utc_now_iso())

    @property
    def buzzer_pattern(self) -> BuzzerPattern:
        """The pattern that distinguishes this alert audibly."""
        return BuzzerPattern.DOOR if self.type in DOOR_TYPES else BuzzerPattern.THRESHOLD

    def payload(self, device_id: str) -> dict[str, object]:
        """The document sent to the cloud for this alert."""
        return {
            "event_id": self.event_id,
            "entity": "alert",
            "op": "create",
            "ts": self.ts,
            "device_id": device_id,
            "zone_id": self.zone_id,
            "item_id": self.item_id,
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
        }


def persist(conn: sqlite3.Connection, alert: Alert, device_id: str) -> int:
    """Write the alert and its outbox row in one transaction.

    Returns the new alert's row id. Either both rows exist afterwards or
    neither does.
    """
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO alerts (event_id, zone_id, item_id, type, severity, message, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                alert.event_id,
                alert.zone_id,
                alert.item_id,
                alert.type,
                alert.severity,
                alert.message,
                alert.ts,
            ),
        )
        conn.execute(
            "INSERT INTO outbox (event_id, entity, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (
                alert.event_id,
                "alert",
                json.dumps(alert.payload(device_id), sort_keys=True),
                alert.ts,
            ),
        )
        return int(cursor.lastrowid)


def announce(outputs: OutputSet, alert: Alert, worst_status: str) -> None:
    """Drive the local outputs for a newly confirmed alert.

    This is the part that must happen within a few seconds of the violation,
    so it touches nothing that can block.
    """
    outputs.buzzer.sound(alert.buzzer_pattern)
    outputs.leds.show_status(worst_status)
    outputs.display.write(alert.message[:16], worst_status[:16])


def mark_notified(conn: sqlite3.Connection, alert_id: int) -> None:
    """Record that the remote notification for this alert was delivered."""
    with db.transaction(conn):
        conn.execute("UPDATE alerts SET notify_pending = 0 WHERE id = ?", (alert_id,))


def notify(conn: sqlite3.Connection, notifier: Notifier, alert: Alert, alert_id: int) -> bool:
    """Attempt the remote notification and record the outcome.

    Returns True when the message was delivered. A failure leaves
    ``notify_pending`` set so the alert is retried on reconnect.
    """
    notification = build_alert_notification(
        zone_name=alert.zone_name,
        alert_type=alert.type,
        severity=alert.severity,
        message=alert.message,
        timestamp=alert.ts,
        item_name=None if alert.type in ZONE_LEVEL_TYPES else alert.item_name,
    )
    if notifier.send(notification):
        mark_notified(conn, alert_id)
        return True
    logger.warning("alert %s remains pending notification", alert.event_id)
    return False


def raise_alert(
    conn: sqlite3.Connection,
    alert: Alert,
    *,
    outputs: OutputSet,
    notifier: Notifier,
    device_id: str,
    worst_status: str,
) -> int:
    """Persist, announce locally, then notify remotely. Returns the alert id.

    The local warning happens before the network is touched, so a slow or
    absent connection cannot delay it.
    """
    alert_id = persist(conn, alert, device_id)
    announce(outputs, alert, worst_status)
    notify(conn, notifier, alert, alert_id)
    return alert_id


def pending_notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return alerts whose remote notification has not been delivered."""
    return conn.execute(
        "SELECT a.id, a.event_id, a.zone_id, a.item_id, a.type, a.severity, a.message, a.ts, "
        "z.name AS zone_name FROM alerts a JOIN zones z ON z.id = a.zone_id "
        "WHERE a.notify_pending = 1 ORDER BY a.ts"
    ).fetchall()


def retry_pending(conn: sqlite3.Connection, notifier: Notifier) -> int:
    """Retry every undelivered notification. Returns how many were sent."""
    sent = 0
    for row in pending_notifications(conn):
        alert = Alert(
            zone_id=row["zone_id"],
            zone_name=row["zone_name"],
            type=row["type"],
            severity=row["severity"],
            message=row["message"],
            item_id=row["item_id"],
            event_id=row["event_id"],
            ts=row["ts"],
        )
        if notify(conn, notifier, alert, row["id"]):
            sent += 1
    return sent
