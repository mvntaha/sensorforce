"""Telling the Pi which lamp to light, from what the database knows.

The Pi can measure its zone but it cannot know that the yoghurt expires
tomorrow: expiry dates, storage limits and the freshness rules all live on the
server. This module turns the engine's verdict into a small command the device
reads, so the yellow lamp comes on for *Use Soon* without the firmware having
to learn any of that.

Direction of travel is the opposite of ``cloud_ingest``. Readings flow device
-> cloud -> SQLite; this flows SQLite -> cloud -> device. SQLite remains the
source of truth in both: nothing here decides anything, it only publishes a
verdict the engine already reached.

The command is written with ``set`` under a fixed key per device, so it is
always the current instruction and never a queue the device could fall behind
on. A device that has been offline reads the latest state when it returns,
which is the only state that matters.

Lamp mapping, from the project's LED plan:

===========================  ======  ===========================================
status                       led     why
===========================  ======  ===========================================
Fresh/Normal                 green   nothing needs attention
Use Soon                     yellow  something is near its deadline
Check Food                   red     something is past a limit or out of range
Sensor Fault/Data Unavail.   red     never green: a fault must not look healthy
===========================  ======  ===========================================

The exact status is published alongside ``led`` so the LCD can show the right
words even though only three lamps exist.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from core.engine import Status
from core.status import ItemStatus, item_statuses, overall_status, use_first, zone_statuses

logger = logging.getLogger(__name__)

#: Where the server publishes, under the database root. The device reads the
#: node named by its own id.
COMMANDS_PATH = "pi_monitor/commands"

#: How many items to name in the command. The device does not need the whole
#: inventory, only enough to show something useful on a 16x2 LCD.
NAMED_ITEMS = 5

#: status -> lamp. FAULT maps to red deliberately: SRS rule 1 forbids showing
#: a healthy signal while a required sensor is faulty.
LED_FOR_STATUS: dict[Status, str] = {
    Status.FRESH: "green",
    Status.USE_SOON: "yellow",
    Status.CHECK: "red",
    Status.FAULT: "red",
}

#: Statuses that should also sound the buzzer. Use Soon is deliberately silent:
#: a lamp is a reminder, a noise in a kitchen is an emergency.
AUDIBLE = frozenset({Status.CHECK})


def _item_summary(status: ItemStatus) -> dict[str, Any]:
    """The few fields the device needs about one item."""
    item = status.item
    return {
        "name": item.get("name", ""),
        "qr_id": item.get("qr_id", ""),
        "zone_id": item.get("zone_id"),
        "status": str(status.assessment.status),
        "days_stored": status.days_stored,
    }


def build_command(
    conn: sqlite3.Connection,
    *,
    device_id: str = "pi-01",
    today: date | None = None,
) -> dict[str, Any]:
    """Return the command describing what the device should currently show.

    Computed from the same engine call the dashboard uses, so the lamp and the
    screen can never disagree about an item.
    """
    statuses = item_statuses(conn, today=today)
    zones = zone_statuses(conn)
    worst = overall_status(statuses, zones)

    by_status: dict[Status, list[ItemStatus]] = {}
    for status in statuses:
        by_status.setdefault(status.assessment.status, []).append(status)

    soon = by_status.get(Status.USE_SOON, [])
    check = by_status.get(Status.CHECK, [])

    return {
        "device_id": device_id,
        "led": LED_FOR_STATUS[worst],
        "status": str(worst),
        "buzzer": worst in AUDIBLE,
        "use_soon_count": len(soon),
        "check_count": len(check),
        "item_count": len(statuses),
        # The items worth naming, most urgent first, as the dashboard orders
        # them. Empty list when nothing needs attention.
        "attention": [_item_summary(s) for s in use_first(statuses, limit=NAMED_ITEMS)],
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "freshguard-server",
    }


def publish(reference: Any, command: dict[str, Any], *, device_id: str | None = None) -> None:
    """Write the command to the device's node, replacing what was there.

    ``reference`` is a Firebase reference for the database root; the caller
    owns the connection so tests can pass a fake.
    """
    target = device_id or command.get("device_id") or "pi-01"
    try:
        reference.child(f"{COMMANDS_PATH}/{target}").set(command)
    except Exception as exc:
        raise RuntimeError(f"could not publish the command for {target}: {exc}") from exc
    logger.info("published led=%s status=%s for %s", command["led"], command["status"], target)
