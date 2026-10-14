"""RFID taps from the Pi: a tag tapped in asks for details, tapped out is eaten.

The Pi's reader toggles each tag in or out of the fridge and logs every tap to
``pi_monitor/rfid_history/<device>`` as ``{tag_id, action, item_name, ts}``,
where action is ``ADDED`` or ``REMOVED``. The ingest worker replays those taps
here, oldest first, exactly once each:

* **ADDED**, and no item carries the tag: the tag waits in ``rfid_pending``
  ("details needed") until someone fills in the form, which links the tag to a
  new item with the next free FG-nnnn.
* **REMOVED**, and an item carries the tag: the item is removed and logged as
  consumed, the same as pressing "eaten" on the dashboard.
* **REMOVED** for a tag still waiting for details: the wait is simply dropped.
* Anything else is recorded and ignored.

Taps are replayed rather than the Pi's current ``items_inside_tags`` list being
compared, because the Pi keeps that list in memory. When the Pi restarts the
list starts empty, and comparing against it would mark everything in the fridge
as eaten. A replayed tap after a restart is harmless: an ADDED for a tag whose
item already exists is ignored.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core import db
from core import items as item_store
from core.cloud_time import trusted_ts
from core.items import RFID_PATTERN

logger = logging.getLogger(__name__)

#: Where the Pi logs every tap, under the database root.
HISTORY_PATH = "pi_monitor/rfid_history"

#: The Pi's map of tags it believes are inside, tag id -> name. Read once, on
#: the very first run, so tags already in the fridge are not missed.
INSIDE_PATH = "pi_monitor/live/{device}/items_inside_tags"

#: Taps applied per cycle. Bounded so a long backlog is worked through in steps.
BATCH_SIZE = 100


class RfidCloudError(RuntimeError):
    """The cloud could not be reached while reading taps."""


# --------------------------------------------------------- the web app's side
def list_pending(conn: sqlite3.Connection, device_id: str | None = None) -> list[dict[str, Any]]:
    """Tags tapped in that still need their details, oldest first."""
    if device_id is None:
        rows = conn.execute("SELECT * FROM rfid_pending ORDER BY tapped_in_at, tag_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM rfid_pending WHERE device_id = ? ORDER BY tapped_in_at, tag_id",
            (device_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_pending(conn: sqlite3.Connection, tag_id: str) -> dict[str, Any] | None:
    """One tag waiting for details, or None."""
    row = conn.execute("SELECT * FROM rfid_pending WHERE tag_id = ?", (tag_id,)).fetchone()
    return dict(row) if row is not None else None


def short_tag(tag_id: str) -> str:
    """The last four digits, which is how the Pi names a tag (Item-0456)."""
    return tag_id[-4:]


# ------------------------------------------------------------ the worker's side
class RfidBridge:
    """Replays the Pi's taps into SQLite. Owned by the ingest worker."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        reference: Any,
        device_id: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.reference = reference
        self.device_id = device_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def _call(self, what: str, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except Exception as exc:
            raise RfidCloudError(f"could not {what}: {exc}") from exc

    # -- cursor ------------------------------------------------------------------
    def _cursor(self) -> str | None:
        row = self.conn.execute(
            "SELECT last_key FROM rfid_cursor WHERE device_id = ?", (self.device_id,)
        ).fetchone()
        return None if row is None else row["last_key"]

    def _set_cursor(self, key: str) -> None:
        self.conn.execute(
            "INSERT INTO rfid_cursor (device_id, last_key) VALUES (?, ?) "
            "ON CONFLICT (device_id) DO UPDATE SET last_key = excluded.last_key, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            (self.device_id, key),
        )

    # -- one cycle --------------------------------------------------------------
    def cycle(self) -> int:
        """Apply any new taps. Returns how many were applied."""
        cursor = self._cursor()
        if cursor is None:
            self._seed()
            return 0

        node = self.reference.child(f"{HISTORY_PATH}/{self.device_id}").order_by_key()
        if cursor:
            taps = self._call(
                "read the RFID taps", node.start_at(cursor).limit_to_first(BATCH_SIZE + 1).get
            )
        else:
            taps = self._call("read the RFID taps", node.limit_to_first(BATCH_SIZE).get)
        taps = dict(taps or {})
        taps.pop(cursor, None)

        applied = 0
        for key, tap in sorted(taps.items()):
            self._apply(key, tap)
            with db.transaction(self.conn):
                self._set_cursor(key)
            applied += 1
        return applied

    def _seed(self) -> None:
        """First run: take the Pi's current list as the starting point.

        Tags the Pi believes are inside and that no item carries become "details
        needed". Older taps are not replayed: they describe a past the inventory
        has already been built from by hand.
        """
        inside = self._call(
            "read the tags inside",
            self.reference.child(INSIDE_PATH.format(device=self.device_id)).get,
        )
        latest = self._call(
            "read the RFID taps",
            self.reference.child(f"{HISTORY_PATH}/{self.device_id}")
            .order_by_key()
            .limit_to_last(1)
            .get,
        )
        now = _iso(self._clock())
        with db.transaction(self.conn):
            if isinstance(inside, dict):
                for raw, label in inside.items():
                    tag = str(raw).strip()
                    if RFID_PATTERN.match(tag) and not self._known(tag):
                        self._add_pending(tag, now, label)
            self._set_cursor(max(latest) if latest else "")
        logger.info("RFID: started from the Pi's current list of tags")

    def _known(self, tag: str) -> bool:
        return (
            item_store.get_by_tag(self.conn, tag) is not None
            or get_pending(self.conn, tag) is not None
        )

    def _add_pending(self, tag: str, when: str, label: Any) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO rfid_pending (tag_id, device_id, tapped_in_at, device_label) "
            "VALUES (?, ?, ?, ?)",
            (tag, self.device_id, when, str(label)[:64] if label else None),
        )

    def _apply(self, key: str, tap: Any) -> None:
        """Act on one tap and record what was done."""
        if self.conn.execute("SELECT 1 FROM rfid_events WHERE cloud_key = ?", (key,)).fetchone():
            return
        if not isinstance(tap, dict):
            return
        tag = str(tap.get("tag_id") or "").strip()
        if not RFID_PATTERN.match(tag):
            logger.warning("RFID: ignoring a tap with an unusable tag id %r", tag)
            return

        action = str(tap.get("action") or "").strip().upper()
        if action not in ("ADDED", "REMOVED"):
            action = "OTHER"
        stamped = trusted_ts(key, tap.get("ts"), device_id=self.device_id)
        when = stamped if _valid_iso(stamped) else _iso(self._clock())

        result, qr_id = "ignored", None
        item = item_store.get_by_tag(self.conn, tag)
        if action == "ADDED":
            if item is None and get_pending(self.conn, tag) is None:
                with db.transaction(self.conn):
                    self._add_pending(tag, when, tap.get("item_name"))
                result = "pending"
                logger.info("RFID: tag %s tapped in; waiting for its details", tag)
        elif action == "REMOVED":
            if item is not None:
                qr_id = item["qr_id"]
                self._consume(item)
                result = "consumed"
            elif get_pending(self.conn, tag) is not None:
                with db.transaction(self.conn):
                    self.conn.execute("DELETE FROM rfid_pending WHERE tag_id = ?", (tag,))
                result = "cleared"
                logger.info("RFID: tag %s tapped out before its details were added", tag)

        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO rfid_events (device_id, cloud_key, tag_id, action, tapped_at, "
                "result, qr_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.device_id, key, tag, action, when, result, qr_id),
            )

    def _consume(self, item: dict[str, Any]) -> None:
        """Remove a tapped-out item and log it as consumed, with its verdict."""
        # Imported here: core.status reads thresholds and readings, which this
        # module otherwise has no need of.
        from core.status import item_statuses

        current = next(
            (s for s in item_statuses(self.conn) if s.item["qr_id"] == item["qr_id"]), None
        )
        item_store.consume(
            self.conn,
            item["qr_id"],
            "consumed",
            device_id=self.device_id,
            status=str(current.assessment.status) if current else "",
            days_stored=current.days_stored if current else None,
        )
        logger.info("RFID: %s (%s) tapped out; recorded as consumed", item["qr_id"], item["name"])


# ------------------------------------------------------------------- helpers
def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_iso(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True
