"""Item records: validation, persistence and the outbox rows that mirror them.

Every write goes through here so that the change and its outbox row are always
committed together. A caller that inserts into ``items`` directly would leave
the cloud replica silently behind.

Validation is server-side and total: nothing trusts the browser.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from core import db
from core.sensors.base import utc_now_iso

logger = logging.getLogger(__name__)

#: QR identifiers are exactly FG- followed by four digits.
QR_PATTERN = re.compile(r"^FG-\d{4}$")

#: RFID tag ids as the Pi's reader reports them: digits only. Matches the
#: CHECK on items.rfid_tag in migration 005.
RFID_PATTERN = re.compile(r"^\d{4,32}$")

MAX_NAME_LENGTH = 120
MAX_UNIT_LENGTH = 16

#: Columns returned to the browser, in a stable order.
ITEM_COLUMNS = (
    "id",
    "qr_id",
    "name",
    "category",
    "quantity",
    "unit",
    "zone_id",
    "manufacture_date",
    "storage_date",
    "expiry_date",
    "storage_limit_days",
    "state",
    "state_changed_at",
    "created_by",
    "created_at",
    "updated_at",
    "rfid_tag",
)

#: Migration 002 gave items a lifecycle state. Migration 003 superseded it:
#: an item that has been eaten or thrown away is deleted and recorded in
#: consumption_log instead, so a live item is always 'stored'. The column
#: remains because dropping one in SQLite is awkward and a defaulted column
#: costs nothing.
ITEM_STATES = ("stored",)

# Built from the fixed tuple above, never from user input.
_SELECT = f"SELECT {', '.join(ITEM_COLUMNS)} FROM items"  # noqa: S608


class ItemError(ValueError):
    """Raised when a submitted item cannot be accepted."""


@dataclass(frozen=True, slots=True)
class ItemInput:
    """A validated item, ready to be written."""

    qr_id: str
    name: str
    category: str
    quantity: float
    unit: str
    zone_id: int
    storage_date: date
    manufacture_date: date | None
    expiry_date: date | None
    storage_limit_days: int | None
    #: The RFID tag tapped in for this container, when it arrived that way.
    rfid_tag: str | None = None


def _text(payload: dict, field: str, *, required: bool = True, limit: int = 255) -> str:
    raw = payload.get(field)
    if raw is None:
        if required:
            raise ItemError(f"{field} is required.")
        return ""
    if not isinstance(raw, str):
        raise ItemError(f"{field} must be text.")
    value = raw.strip()
    if required and not value:
        raise ItemError(f"{field} is required.")
    if len(value) > limit:
        raise ItemError(f"{field} must be {limit} characters or fewer.")
    return value


def _date(payload: dict, field: str, *, required: bool = False) -> date | None:
    raw = payload.get(field)
    if raw in (None, ""):
        if required:
            raise ItemError(f"{field} is required.")
        return None
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        raise ItemError(f"{field} must be a date in YYYY-MM-DD form.")
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ItemError(f"{field} must be a real date in YYYY-MM-DD form.") from exc


def _positive_number(payload: dict, field: str) -> float:
    raw = payload.get(field)
    if raw is None or raw == "":
        raise ItemError(f"{field} is required.")
    if isinstance(raw, bool):
        raise ItemError(f"{field} must be a number.")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ItemError(f"{field} must be a number.") from exc
    if value <= 0:
        raise ItemError(f"{field} must be greater than zero.")
    return value


def _optional_positive_int(payload: dict, field: str) -> int | None:
    raw = payload.get(field)
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise ItemError(f"{field} must be a whole number.")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ItemError(f"{field} must be a whole number.") from exc
    if value <= 0:
        raise ItemError(f"{field} must be greater than zero.")
    return value


def validate(conn: sqlite3.Connection, payload: dict, *, qr_id: str | None = None) -> ItemInput:
    """Validate a submitted item against the database and the schema rules.

    ``qr_id`` overrides any identifier in the payload, which is what an update
    needs: the identifier comes from the URL, not from the body.
    """
    identifier = (qr_id or _text(payload, "qr_id")).strip().upper()
    if not QR_PATTERN.match(identifier):
        raise ItemError("qr_id must look like FG-0001.")

    name = _text(payload, "name", limit=MAX_NAME_LENGTH)
    category = _text(payload, "category")
    unit = _text(payload, "unit", required=False, limit=MAX_UNIT_LENGTH) or "pcs"
    quantity = _positive_number(payload, "quantity")

    zone_raw = payload.get("zone_id")
    try:
        zone_id = int(zone_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ItemError("zone_id must be a whole number.") from exc

    storage_date = _date(payload, "storage_date", required=True)
    manufacture_date = _date(payload, "manufacture_date")
    expiry_date = _date(payload, "expiry_date")
    storage_limit_days = _optional_positive_int(payload, "storage_limit_days")

    rfid_tag = str(payload.get("rfid_tag") or "").strip() or None
    if rfid_tag is not None and not RFID_PATTERN.match(rfid_tag):
        raise ItemError("rfid_tag must be the tag's number, digits only.")

    assert storage_date is not None  # noqa: S101 - required=True guarantees it

    if expiry_date is not None and manufacture_date is not None and expiry_date < manufacture_date:
        raise ItemError("expiry_date cannot be before manufacture_date.")
    if expiry_date is not None and expiry_date < storage_date:
        raise ItemError("expiry_date cannot be before storage_date.")
    if manufacture_date is not None and manufacture_date > storage_date:
        raise ItemError("manufacture_date cannot be after storage_date.")

    if conn.execute("SELECT 1 FROM thresholds WHERE category = ?", (category,)).fetchone() is None:
        raise ItemError(f"Unknown category {category!r}.")
    if conn.execute("SELECT 1 FROM zones WHERE id = ?", (zone_id,)).fetchone() is None:
        raise ItemError(f"Unknown zone {zone_id}.")

    return ItemInput(
        qr_id=identifier,
        name=name,
        category=category,
        quantity=quantity,
        unit=unit,
        zone_id=zone_id,
        storage_date=storage_date,
        manufacture_date=manufacture_date,
        expiry_date=expiry_date,
        storage_limit_days=storage_limit_days,
        rfid_tag=rfid_tag,
    )


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else {key: row[key] for key in ITEM_COLUMNS}


def get(conn: sqlite3.Connection, qr_id: str) -> dict | None:
    """Return one item by QR identifier, or None."""
    return _row_to_dict(
        conn.execute(f"{_SELECT} WHERE qr_id = ?", (qr_id.strip().upper(),)).fetchone()
    )


def get_by_tag(conn: sqlite3.Connection, rfid_tag: str) -> dict | None:
    """Return the item carrying an RFID tag, or None."""
    return _row_to_dict(
        conn.execute(f"{_SELECT} WHERE rfid_tag = ?", (rfid_tag.strip(),)).fetchone()
    )


def list_items(
    conn: sqlite3.Connection,
    *,
    zone_id: int | None = None,
    category: str | None = None,
    state: str | None = None,
) -> list[dict]:
    """Return items, optionally filtered by zone, category or lifecycle state."""
    clauses, params = [], []
    if zone_id is not None:
        clauses.append("zone_id = ?")
        params.append(zone_id)
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    # Clause text is fixed; every value is bound.
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"{_SELECT}{where} ORDER BY qr_id", params).fetchall()
    return [row for row in (_row_to_dict(r) for r in rows) if row is not None]


def next_free_identifier(conn: sqlite3.Connection) -> str:
    """The lowest FG-nnnn not currently in use.

    Identifiers come back into circulation: consuming an item deletes it, and
    the sticker on that container can be put on the next thing. So this looks
    for the first gap rather than counting upwards forever.
    """
    taken = {row["qr_id"] for row in conn.execute("SELECT qr_id FROM items ORDER BY qr_id")}
    for number in range(1, 10000):
        candidate = f"FG-{number:04d}"
        if candidate not in taken:
            return candidate
    # Ten thousand containers in one kitchen is not a real situation, but
    # returning something wrong would be worse than saying so.
    raise ItemError("Every FG-nnnn identifier is in use.")


def _outbox_payload(item: dict, operation: str, device_id: str, event_id: str) -> str:
    return json.dumps(
        {
            "event_id": event_id,
            "entity": "item",
            "op": operation,
            "ts": utc_now_iso(),
            "device_id": device_id,
            "item": {key: _jsonable(value) for key, value in item.items()},
        },
        sort_keys=True,
    )


def _jsonable(value: object) -> object:
    return value.isoformat() if isinstance(value, date) else value


def _queue(conn: sqlite3.Connection, item: dict, operation: str, device_id: str) -> str:
    """Insert the outbox row for a change. Must run inside the same transaction."""
    event_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO outbox (event_id, entity, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (event_id, "item", _outbox_payload(item, operation, device_id, event_id), utc_now_iso()),
    )
    return event_id


def create(
    conn: sqlite3.Connection, data: ItemInput, *, created_by: int | None, device_id: str
) -> dict:
    """Insert an item and its outbox row in one transaction."""
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM items WHERE qr_id = ?", (data.qr_id,)).fetchone():
            raise ItemError(f"{data.qr_id} is already in use.")
        if data.rfid_tag and get_by_tag(conn, data.rfid_tag) is not None:
            raise ItemError(f"Tag {data.rfid_tag} is already linked to another item.")
        conn.execute(
            "INSERT INTO items (qr_id, name, category, quantity, unit, zone_id, "
            "manufacture_date, storage_date, expiry_date, storage_limit_days, created_by, "
            "rfid_tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.qr_id,
                data.name,
                data.category,
                data.quantity,
                data.unit,
                data.zone_id,
                data.manufacture_date.isoformat() if data.manufacture_date else None,
                data.storage_date.isoformat(),
                data.expiry_date.isoformat() if data.expiry_date else None,
                data.storage_limit_days,
                created_by,
                data.rfid_tag,
            ),
        )
        if data.rfid_tag:
            # The tag now has its details, so it no longer waits for them.
            conn.execute("DELETE FROM rfid_pending WHERE tag_id = ?", (data.rfid_tag,))
        item = get(conn, data.qr_id)
        assert item is not None  # noqa: S101 - just inserted
        _queue(conn, item, "create", device_id)
    logger.info("created item %s", data.qr_id)
    return item


def update(conn: sqlite3.Connection, data: ItemInput, *, device_id: str) -> dict:
    """Update an item and its outbox row in one transaction."""
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM items WHERE qr_id = ?", (data.qr_id,)).fetchone() is None:
            raise ItemError(f"{data.qr_id} does not exist.")
        conn.execute(
            "UPDATE items SET name = ?, category = ?, quantity = ?, unit = ?, zone_id = ?, "
            "manufacture_date = ?, storage_date = ?, expiry_date = ?, storage_limit_days = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE qr_id = ?",
            (
                data.name,
                data.category,
                data.quantity,
                data.unit,
                data.zone_id,
                data.manufacture_date.isoformat() if data.manufacture_date else None,
                data.storage_date.isoformat(),
                data.expiry_date.isoformat() if data.expiry_date else None,
                data.storage_limit_days,
                data.qr_id,
            ),
        )
        item = get(conn, data.qr_id)
        assert item is not None  # noqa: S101 - just updated
        _queue(conn, item, "update", device_id)
    logger.info("updated item %s", data.qr_id)
    return item


def list_untagged(conn: sqlite3.Connection) -> list[dict]:
    """Items with no RFID tag yet, newest first: what a new tag could belong to."""
    rows = conn.execute(
        "SELECT qr_id, name, category, storage_date FROM items "
        "WHERE rfid_tag IS NULL ORDER BY created_at DESC, qr_id"
    ).fetchall()
    return [dict(row) for row in rows]


def attach_tag(conn: sqlite3.Connection, qr_id: str, rfid_tag: str, *, device_id: str) -> dict:
    """Give an existing item its RFID tag, so one container is never two items.

    Refused when the item already carries a tag or the tag is already on
    another item. Runs in its own transaction with the outbox row.
    """
    identifier = qr_id.strip().upper()
    tag = rfid_tag.strip()
    if not RFID_PATTERN.match(tag):
        raise ItemError("rfid_tag must be the tag's number, digits only.")
    with db.transaction(conn):
        item = get(conn, identifier)
        if item is None:
            raise ItemError(f"{identifier} does not exist.")
        if item["rfid_tag"]:
            raise ItemError(f"{identifier} already has tag {item['rfid_tag']}.")
        other = get_by_tag(conn, tag)
        if other is not None:
            raise ItemError(f"Tag {tag} is already linked to {other['qr_id']}.")
        conn.execute(
            "UPDATE items SET rfid_tag = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE qr_id = ?",
            (tag, identifier),
        )
        conn.execute("DELETE FROM rfid_pending WHERE tag_id = ?", (tag,))
        item = get(conn, identifier)
        assert item is not None  # noqa: S101 - just updated
        _queue(conn, item, "update", device_id)
    logger.info("linked tag %s to %s", tag, identifier)
    return item


#: What can become of an item. Both outcomes remove it and both are recorded;
#: only the word differs, and that word is the food-waste figure.
OUTCOMES = ("consumed", "discarded")


def _log_payload(row: dict, device_id: str, event_id: str) -> str:
    return json.dumps(
        {
            "event_id": event_id,
            "entity": "item",
            "op": f"consume:{row['outcome']}",
            "ts": row["consumed_at"],
            "device_id": device_id,
            "consumption": {key: _jsonable(value) for key, value in row.items()},
        },
        sort_keys=True,
    )


def consume(
    conn: sqlite3.Connection,
    qr_id: str,
    outcome: str,
    *,
    device_id: str,
    status: str = "",
    days_stored: int | None = None,
    created_by: int | None = None,
) -> dict:
    """Record what became of an item, then remove it.

    The history row carries its own copy of the item, because the item is
    deleted in this same transaction: a kitchen reuses its printed stickers,
    and a container that has been eaten is not on a shelf. Either both rows
    change or neither does.

    ``status`` is the engine's verdict at this moment, frozen into the log:
    the readings behind it will roll off long before anyone reads the history.
    """
    identifier = qr_id.strip().upper()
    if outcome not in OUTCOMES:
        raise ItemError(f"Outcome must be one of {', '.join(OUTCOMES)}.")

    with db.transaction(conn):
        item = get(conn, identifier)
        if item is None:
            raise ItemError(f"{identifier} does not exist.")

        zone = conn.execute("SELECT name FROM zones WHERE id = ?", (item["zone_id"],)).fetchone()
        event_id = str(uuid.uuid4())
        entry = {
            "event_id": event_id,
            "item_id": item["id"],
            "qr_id": item["qr_id"],
            "item_name": item["name"],
            "category": item["category"],
            "zone_id": item["zone_id"],
            "zone_name": zone["name"] if zone else None,
            "quantity": item["quantity"],
            "unit": item["unit"],
            "storage_date": item["storage_date"],
            "expiry_date": item["expiry_date"],
            "days_stored": days_stored,
            "outcome": outcome,
            "status_at_consumption": status or "Not assessed",
            "consumed_at": utc_now_iso(),
            "created_by": created_by,
        }
        columns = ", ".join(entry)
        placeholders = ", ".join("?" for _ in entry)
        conn.execute(
            f"INSERT INTO consumption_log ({columns}) VALUES ({placeholders})",  # noqa: S608
            tuple(entry.values()),
        )
        conn.execute("DELETE FROM items WHERE qr_id = ?", (identifier,))
        conn.execute(
            "INSERT INTO outbox (event_id, entity, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (event_id, "item", _log_payload(entry, device_id, event_id), utc_now_iso()),
        )

    logger.info("item %s recorded as %s and removed", identifier, outcome)
    return entry


def history(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """The consumption history, newest first."""
    rows = conn.execute(
        "SELECT * FROM consumption_log ORDER BY consumed_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


#: A starter set of names per category, so the kiosk's tap-to-choose grid is
#: useful on the day it is switched on, before this kitchen has stored
#: anything. It is a convenience for entry only: nothing here affects a
#: freshness verdict, which is decided by the item's category and dates.
#: A name that is not on this list is typed once and then appears on the grid
#: by itself, because `popular_names` below promotes whatever is actually used.
STARTER_NAMES: dict[str, tuple[str, ...]] = {
    "dairy": ("Milk", "Cheese", "Butter", "Yoghurt", "Cream"),
    "produce": ("Apples", "Tomatoes", "Lettuce", "Bananas", "Spinach"),
    "rootveg": ("Potatoes", "Carrots", "Onions", "Garlic", "Ginger"),
    "meat": ("Chicken", "Beef", "Mutton", "Mince", "Sausages"),
    "fish": ("Fish", "Prawns", "Salmon"),
    "cooked": ("Curry", "Rice", "Pasta", "Biryani", "Soup"),
    "bakery": ("Bread", "Naan", "Cake", "Buns"),
    "beverage": ("Orange juice", "Water", "Lemonade", "Milkshake"),
}


def popular_names(
    conn: sqlite3.Connection, category: str | None = None, limit: int = 8
) -> list[str]:
    """Names this kitchen has actually stored, most used first.

    Counted across both the live items and the consumption history, because a
    carton that has been eaten is still evidence of what this kitchen buys --
    arguably better evidence, since it got used.

    This exists so the kiosk can be operated by tapping. Someone standing at
    the fridge holding a bag should not have to type, and after a week the six
    things they actually buy are the six things on the screen.
    """
    sql = (
        "SELECT name, COUNT(*) AS total FROM ("
        "  SELECT name, category FROM items"
        "  UNION ALL"
        "  SELECT item_name AS name, category FROM consumption_log"
        ") WHERE (? IS NULL OR category = ?)"
        " GROUP BY name ORDER BY total DESC, name LIMIT ?"
    )
    rows = conn.execute(sql, (category, category, limit)).fetchall()
    return [row["name"] for row in rows]


def name_suggestions(conn: sqlite3.Connection, category: str, limit: int = 12) -> list[str]:
    """What to offer on the tap-to-choose grid for one category.

    This kitchen's own names come first and the starter list fills the rest,
    so the grid is never empty and never stops being relevant. Matching is
    case-insensitive so "milk" typed once does not sit beside "Milk".
    """
    chosen = popular_names(conn, category, limit)
    seen = {name.casefold() for name in chosen}
    for name in STARTER_NAMES.get(category, ()):
        if len(chosen) >= limit:
            break
        if name.casefold() not in seen:
            chosen.append(name)
            seen.add(name.casefold())
    return chosen


def outcome_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many items were eaten and how many were thrown away."""
    counts = {
        row["outcome"]: row["total"]
        for row in conn.execute(
            "SELECT outcome, COUNT(*) AS total FROM consumption_log GROUP BY outcome"
        )
    }
    return {name: counts.get(name, 0) for name in OUTCOMES}


def delete(conn: sqlite3.Connection, qr_id: str, *, device_id: str) -> dict:
    """Delete an item, queueing the deletion so the cloud copy follows.

    A delete is an event like any other: without the outbox row the cloud
    replica would keep an item the kitchen has thrown away.
    """
    identifier = qr_id.strip().upper()
    with db.transaction(conn):
        item = get(conn, identifier)
        if item is None:
            raise ItemError(f"{identifier} does not exist.")
        conn.execute("DELETE FROM items WHERE qr_id = ?", (identifier,))
        _queue(conn, item, "delete", device_id)
    logger.info("deleted item %s", identifier)
    return item
