"""On-demand photographs of the inside of the fridge.

A person asks from the Snapshots page; the Pi takes the picture. Between the
two sits the same split as everything else in FreshGuard:

* The **web app** only touches SQLite. It records a request and reads back
  what happened to it.
* The **ingest worker** holds the cloud connection. Each cycle it passes new
  requests to the device by raising ``trigger_snapshot`` on the device's live
  node, collects the photographs the device has uploaded, saves each one under
  ``var/captures`` with a row in ``snapshots``, and settles the request.
* The **device** sees the flag, takes the photograph, pushes it to
  ``pi_monitor/captures/<device>``, and lowers the flag.

The flag is a plain boolean because that is what the device firmware reads.
Requests are therefore served one at a time: while one is open, asking again
returns that same request rather than raising the flag a second time.

A photograph is a picture, not a measurement. Nothing here feeds the freshness
engine or raises an alert.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import db
from core.cloud_time import CLOCK_RESET_S, trusted_ts
from core.paths import CAPTURE_DIR, capture_path, safe_device_id

logger = logging.getLogger(__name__)

#: The device's live node, under the database root. The trigger lives here.
LIVE_PATH = "pi_monitor/live"

#: Where the device pushes each requested photograph.
CAPTURES_PATH = "pi_monitor/captures"

#: The flag the device watches, on its live node.
TRIGGER_FIELD = "trigger_snapshot"

#: A request the device has not answered in this long is given up, and the
#: flag lowered, so a Pi that comes back online hours later does not take a
#: photograph nobody is waiting for.
REQUEST_TIMEOUT_S = 90

#: Shortest gap between the end of one request and the start of the next. The
#: camera warms up for about a second per picture; this keeps a repeated tap
#: from queueing a burst of them.
COOLDOWN_S = 5

#: Photographs collected per cycle. Bounded so a long backlog is worked through
#: in steps rather than downloaded at once.
BATCH_SIZE = 20

#: The device sends each photograph as a data URI. Only JPEG is accepted: the
#: page shows it as a photograph, and anything else is a mistake.
JPEG_PREFIX = "data:image/jpeg;base64,"

#: A photograph larger than this is refused rather than written to disk. The
#: device sends about 20 kB; the ceiling is generous but finite.
MAX_PHOTO_BYTES = 4 * 1024 * 1024

#: Push keys name files, so they are checked rather than trusted.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

OPEN_STATUSES = ("pending", "sent")

#: The Pi and this machine keep their own clocks. A live photograph stamped up
#: to this long before the request is still accepted as its answer.
LIVE_CLOCK_SKEW_S = 60


class SnapshotError(RuntimeError):
    """A request the web app should refuse, with a message for the person."""


class SnapshotCloudError(RuntimeError):
    """The cloud could not be reached while handling snapshots."""


# ------------------------------------------------------------------ decoding
def decode_jpeg_data_uri(raw: Any) -> bytes | None:
    """Return the JPEG bytes in a device data URI, or None when it is unusable."""
    if not raw or not isinstance(raw, str):
        return None
    if not raw.startswith(JPEG_PREFIX):
        logger.warning("ignoring a photograph that is not a JPEG data URI")
        return None

    try:
        data = base64.b64decode(raw[len(JPEG_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        logger.warning("ignoring a photograph whose base64 could not be decoded")
        return None

    if len(data) > MAX_PHOTO_BYTES:
        logger.warning("ignoring a photograph of %d bytes; over the limit", len(data))
        return None
    # A JPEG always starts FF D8. Checking it keeps anything that merely decodes
    # from being written out with a .jpg name.
    if not data.startswith(b"\xff\xd8"):
        logger.warning("ignoring a photograph that does not begin with a JPEG marker")
        return None
    return data


# --------------------------------------------------------- the web app's side
def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def open_request(conn: sqlite3.Connection, device_id: str) -> dict[str, Any] | None:
    """The request still waiting on the device, if there is one."""
    return _row(
        conn.execute(
            "SELECT * FROM snapshot_requests WHERE device_id = ? AND status IN ('pending', 'sent') "
            "ORDER BY id LIMIT 1",
            (device_id,),
        ).fetchone()
    )


def request_capture(
    conn: sqlite3.Connection,
    device_id: str,
    user_id: int | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a request for a photograph and return it.

    While a request is open, that request is returned instead of a new one: the
    device answers a single flag, so a second request could only be answered by
    the same photograph. Raises SnapshotError inside the cool-down.
    """
    now = now or datetime.now(UTC)
    _close_abandoned(conn, device_id, now)

    existing = open_request(conn, device_id)
    if existing is not None:
        return existing

    last = conn.execute(
        # Only a photograph actually taken counts: a failed request did not use
        # the camera, and retrying it straight away is the natural thing to do.
        "SELECT finished_at FROM snapshot_requests WHERE device_id = ? AND status = 'done' "
        "ORDER BY finished_at DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    if last is not None:
        waited = (now - _parse(last["finished_at"])).total_seconds()
        if waited < COOLDOWN_S:
            raise SnapshotError(
                f"The camera has just been used. Try again in {COOLDOWN_S - int(waited)} s."
            )

    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO snapshot_requests (device_id, requested_at, requested_by) "
            "VALUES (?, ?, ?)",
            (device_id, _iso(now), user_id),
        )
    return request_status(conn, int(cursor.lastrowid))  # type: ignore[return-value]


#: Past this, an open request has outlived the worker's own timeout, which can
#: only mean the worker was not running to time it out.
ABANDONED_AFTER_S = REQUEST_TIMEOUT_S + 30


def _close_abandoned(conn: sqlite3.Connection, device_id: str, now: datetime) -> None:
    """Fail open requests nobody is serving, so they cannot block new ones."""
    stale = [
        row["id"]
        for row in conn.execute(
            "SELECT id, requested_at FROM snapshot_requests "
            "WHERE device_id = ? AND status IN ('pending', 'sent')",
            (device_id,),
        ).fetchall()
        if (now - _parse(row["requested_at"])).total_seconds() > ABANDONED_AFTER_S
    ]
    if not stale:
        return
    with db.transaction(conn):
        conn.executemany(
            "UPDATE snapshot_requests SET status = 'failed', finished_at = ?, error = ? "
            "WHERE id = ?",
            [(_iso(now), "The FreshGuard ingest worker was not running.", i) for i in stale],
        )


def request_status(conn: sqlite3.Connection, request_id: int) -> dict[str, Any] | None:
    """One request, with the photograph it produced when there is one."""
    return _row(
        conn.execute(
            "SELECT r.id, r.device_id, r.requested_at, r.status, r.sent_at, r.finished_at, "
            "r.error, r.snapshot_id, s.taken_at AS snapshot_taken_at "
            "FROM snapshot_requests r LEFT JOIN snapshots s "
            "ON s.id = r.snapshot_id AND s.deleted = 0 WHERE r.id = ?",
            (request_id,),
        ).fetchone()
    )


def list_snapshots(conn: sqlite3.Connection, device_id: str) -> list[dict[str, Any]]:
    """Every photograph kept for a device, newest first."""
    rows = conn.execute(
        "SELECT id, device_id, taken_at, received_at, size_bytes FROM snapshots "
        "WHERE device_id = ? AND deleted = 0 ORDER BY taken_at DESC, id DESC",
        (device_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, Any] | None:
    """One kept photograph, or None when it does not exist or was deleted."""
    return _row(
        conn.execute(
            "SELECT * FROM snapshots WHERE id = ? AND deleted = 0", (snapshot_id,)
        ).fetchone()
    )


def snapshot_file(row: dict[str, Any], folder: Path | None = None) -> Path:
    """The JPEG on disk for a snapshot row."""
    return capture_path(row["file_name"], folder)


def taken_label(row: dict[str, Any]) -> str:
    """When the photograph was taken, readable before any script runs.

    13 Oct 2026, 08:35 UTC. The page's script then restates it in the
    viewer's own time zone.
    """
    try:
        moment = _parse(row["taken_at"])
    except ValueError:
        return row["taken_at"]
    return f"{moment.day} {moment:%b %Y, %H:%M} UTC"


def download_name(row: dict[str, Any]) -> str:
    """A name a person can recognise in their downloads folder.

    fridge-pi-01-2026-10-13-0835.jpg, from the moment the device took it.
    """
    try:
        stamp = _parse(row["taken_at"]).strftime("%Y-%m-%d-%H%M%S")
    except ValueError:
        stamp = str(row["id"])
    return f"fridge-{safe_device_id(row['device_id'])}-{stamp}.jpg"


def delete_snapshot(conn: sqlite3.Connection, snapshot_id: int, folder: Path | None = None) -> bool:
    """Remove a photograph. Returns False when there was nothing to remove.

    The file goes at once. The row stays, marked deleted, so the worker can
    remove the cloud copy and never collects this photograph again.
    """
    row = get_snapshot(conn, snapshot_id)
    if row is None:
        return False
    with db.transaction(conn):
        conn.execute(
            "UPDATE snapshots SET deleted = 1, file_name = NULL WHERE id = ?", (snapshot_id,)
        )
    try:
        snapshot_file(row, folder).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        # The row is already hidden; a file left behind is untidy, not wrong.
        logger.warning("could not remove the file for snapshot %s: %s", snapshot_id, exc)
    return True


# ------------------------------------------------------------ the worker's side
class SnapshotBridge:
    """Passes requests to the device and collects its photographs.

    Owned by the ingest worker. ``reference`` is a Firebase reference for the
    database root, so tests can hand in a fake.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        reference: Any,
        device_id: str,
        *,
        folder: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.reference = reference
        self.device_id = safe_device_id(device_id)
        self.folder = folder or CAPTURE_DIR
        self._clock = clock or (lambda: datetime.now(UTC))
        row = conn.execute(
            "SELECT MAX(cloud_key) AS last FROM snapshots WHERE device_id = ?", (device_id,)
        ).fetchone()
        self.last_key: str | None = row["last"]

    # -- cloud calls, each one place to fail ---------------------------------
    def _trigger(self) -> Any:
        return self.reference.child(f"{LIVE_PATH}/{self.device_id}/{TRIGGER_FIELD}")

    def _call(self, what: str, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except Exception as exc:
            raise SnapshotCloudError(f"could not {what}: {exc}") from exc

    # -- one cycle --------------------------------------------------------------
    def cycle(self) -> int:
        """Run one pass. Returns how many photographs were stored."""
        now = self._clock()
        # Expire first: a request that went stale while the worker was stopped
        # must be failed, never passed to the device, or the Pi would take a
        # photograph minutes after the person stopped waiting for it.
        self._expire(now)
        self._send_pending(now)

        # Read the flag before collecting: the device pushes the photograph and
        # only then lowers the flag, so a lowered flag seen here guarantees the
        # photograph is already there to be collected below.
        trigger = None
        if self._requests("sent"):
            trigger = self._call("read the snapshot trigger", self._trigger().get)

        stored = self._collect(now)

        if trigger is False and not stored:
            stored = self._collect_from_live(now)
        if trigger is False and not stored:
            self._fail_sent(now, "The fridge camera could not take a photo. Try again.")
        self._purge_deleted()
        return stored

    def _requests(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, requested_at, sent_at FROM snapshot_requests "
            "WHERE device_id = ? AND status = ? ORDER BY id",
            (self.device_id, status),
        ).fetchall()

    def _send_pending(self, now: datetime) -> None:
        pending = self._requests("pending")
        if not pending:
            return
        self._call("raise the snapshot trigger", lambda: self._trigger().set(True))
        with db.transaction(self.conn):
            self.conn.executemany(
                "UPDATE snapshot_requests SET status = 'sent', sent_at = ? WHERE id = ?",
                [(_iso(now), row["id"]) for row in pending],
            )
        logger.info("asked %s for a snapshot", self.device_id)

    def _fetch(self) -> dict[str, Any]:
        node = self.reference.child(f"{CAPTURES_PATH}/{self.device_id}").order_by_key()
        if self.last_key is None:
            return self._call("read the snapshots", node.limit_to_last(BATCH_SIZE).get) or {}
        payloads = self._call(
            "read the snapshots", node.start_at(self.last_key).limit_to_first(BATCH_SIZE + 1).get
        )
        payloads = payloads or {}
        payloads.pop(self.last_key, None)
        return payloads

    def _collect(self, now: datetime) -> int:
        stored = 0
        for key, payload in sorted(self._fetch().items()):
            self.last_key = key if self.last_key is None else max(self.last_key, key)
            entry = payload
            if isinstance(payload, dict):
                entry = {
                    **payload,
                    "ts": trusted_ts(key, payload.get("ts"), device_id=self.device_id),
                }
            snapshot_id = self._store(key, entry, now)
            if snapshot_id is None:
                continue
            stored += 1
            self._settle(snapshot_id, now)
        return stored

    def _collect_from_live(self, now: datetime) -> int:
        """Take the photograph from the live node, for firmware that puts it only there.

        Some versions of the Pi script answer the trigger by writing the new
        photograph to ``live/<device>/image_base64`` with ``last_snapshot_ts``,
        and never push to the captures list. A lowered flag with a live photo
        no older than the request is that answer, and is stored like any other.
        """
        waiting = self._requests("sent")
        if not waiting:
            return 0
        live = f"{LIVE_PATH}/{self.device_id}"
        taken = self._call(
            "read the live snapshot time",
            self.reference.child(f"{live}/last_snapshot_ts").get,
        )
        if not isinstance(taken, str) or not _valid_iso(taken):
            return 0
        asked = _parse(waiting[0]["requested_at"])
        # A Pi whose clock has reset stamps its photo in 2000. That is a wrong
        # clock, not an old photo: the lowered flag says it just answered. The
        # unique key below still stops the same photo being stored twice.
        clock_wrong = abs((_parse(taken) - now).total_seconds()) > CLOCK_RESET_S
        if not clock_wrong and (asked - _parse(taken)).total_seconds() > LIVE_CLOCK_SKEW_S:
            return 0  # an older photograph, not the answer to this request

        image = self._call(
            "read the live snapshot", self.reference.child(f"{live}/image_base64").get
        )
        key = "live-" + _parse(taken).strftime("%Y%m%dT%H%M%SZ")
        stamped = _iso(now) if clock_wrong else taken
        snapshot_id = self._store(key, {"ts": stamped, "image_base64": image}, now)
        if snapshot_id is None:
            return 0
        self._settle(snapshot_id, now)
        return 1

    def _store(self, key: str, payload: Any, now: datetime) -> int | None:
        """Save one photograph and its row. Returns the new id, or None."""
        if not _SAFE_KEY.match(key) or not isinstance(payload, dict):
            logger.warning("ignoring an unusable snapshot entry %r", key)
            return None
        if self.conn.execute("SELECT 1 FROM snapshots WHERE cloud_key = ?", (key,)).fetchone():
            return None

        data = decode_jpeg_data_uri(payload.get("image_base64"))
        if data is None:
            return None

        taken_at = payload.get("ts")
        if not isinstance(taken_at, str) or not _valid_iso(taken_at):
            taken_at = _iso(now)

        file_name = f"{self.device_id}-{key}.jpg"
        path = capture_path(file_name, self.folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        temporary.write_bytes(data)
        temporary.replace(path)

        try:
            with db.transaction(self.conn):
                cursor = self.conn.execute(
                    "INSERT INTO snapshots (device_id, cloud_key, taken_at, received_at, "
                    "file_name, size_bytes) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.device_id, key, taken_at, _iso(now), file_name, len(data)),
                )
        except sqlite3.Error:
            path.unlink(missing_ok=True)
            raise
        logger.info("stored snapshot %s from %s (%d bytes)", key, self.device_id, len(data))
        return int(cursor.lastrowid)

    def _settle(self, snapshot_id: int, now: datetime) -> None:
        """Mark the oldest waiting request as answered by this photograph."""
        waiting = self._requests("sent")
        if not waiting:
            return
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE snapshot_requests SET status = 'done', finished_at = ?, snapshot_id = ? "
                "WHERE id = ?",
                (_iso(now), snapshot_id, waiting[0]["id"]),
            )

    def _fail_sent(self, now: datetime, reason: str) -> None:
        sent = self._requests("sent")
        if not sent:
            return
        with db.transaction(self.conn):
            self.conn.executemany(
                "UPDATE snapshot_requests SET status = 'failed', finished_at = ?, error = ? "
                "WHERE id = ?",
                [(_iso(now), reason, row["id"]) for row in sent],
            )
        logger.warning("snapshot request failed on %s: %s", self.device_id, reason)

    def _expire(self, now: datetime) -> None:
        stale = [
            row["id"]
            for status in OPEN_STATUSES
            for row in self._requests(status)
            if (now - _parse(row["requested_at"])).total_seconds() > REQUEST_TIMEOUT_S
        ]
        if not stale:
            return
        with db.transaction(self.conn):
            self.conn.executemany(
                "UPDATE snapshot_requests SET status = 'failed', finished_at = ?, error = ? "
                "WHERE id = ?",
                [
                    (_iso(now), "The fridge did not respond. Is the Pi switched on and online?", i)
                    for i in stale
                ],
            )
        logger.warning("%d snapshot request(s) to %s timed out", len(stale), self.device_id)
        # Lower the flag so an offline Pi does not take the photograph hours later.
        self._call("lower the snapshot trigger", lambda: self._trigger().set(False))

    def _purge_deleted(self) -> None:
        rows = self.conn.execute(
            "SELECT id, cloud_key FROM snapshots "
            "WHERE device_id = ? AND deleted = 1 AND cloud_deleted = 0",
            (self.device_id,),
        ).fetchall()
        for row in rows:
            key = row["cloud_key"]
            node = self.reference.child(f"{CAPTURES_PATH}/{self.device_id}/{key}")
            self._call("delete a snapshot from the cloud", node.delete)
            with db.transaction(self.conn):
                self.conn.execute(
                    "UPDATE snapshots SET cloud_deleted = 1 WHERE id = ?", (row["id"],)
                )


# ------------------------------------------------------------------- helpers
def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _valid_iso(value: str) -> bool:
    try:
        _parse(value)
    except ValueError:
        return False
    return True


__all__ = [
    "BATCH_SIZE",
    "CAPTURES_PATH",
    "COOLDOWN_S",
    "LIVE_PATH",
    "REQUEST_TIMEOUT_S",
    "SnapshotBridge",
    "SnapshotCloudError",
    "SnapshotError",
    "decode_jpeg_data_uri",
    "delete_snapshot",
    "download_name",
    "get_snapshot",
    "list_snapshots",
    "open_request",
    "request_capture",
    "request_status",
    "snapshot_file",
    "taken_label",
]
