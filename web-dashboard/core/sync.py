"""Draining the outbox to the cloud replica, exactly once.

SQLite is the source of truth and always works. Firebase is a replica, and the
network is assumed to be absent most of the time: local sensing, freshness
analysis and local alerts never wait for it.

Duplicates are prevented by construction rather than by bookkeeping. Every
change generates a UUID4 ``event_id`` at the moment it happens, and the cloud
write is ``set`` on the node named by that id, never ``push``. A retry after a
lost acknowledgement therefore overwrites the same node, so sending the same
event twice leaves one record. Deletions are events too, so the replica cannot
keep an item the kitchen has thrown away.

A row is never deleted and never marked synced until the write is confirmed. A
worker that dies mid-batch resends; it cannot lose anything.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import db
from core.sensors.base import utc_now_iso

logger = logging.getLogger(__name__)

#: Rows sent per cycle. Small, so a flaky link makes progress rather than
#: repeatedly failing one enormous batch.
BATCH_SIZE = 25

#: Seconds between cycles when everything is working.
DEFAULT_INTERVAL_S = 10.0

#: Backoff after a failed cycle: interval * 2^failures, capped here.
MAX_BACKOFF_S = 60.0

#: Network calls are given a short deadline so the worker cannot hang.
HTTP_TIMEOUT_S = 10.0

#: Statuses that still need sending.
UNSENT = ("pending", "failed")


class CloudError(RuntimeError):
    """Raised when the replica could not be written."""


class CloudClient(ABC):
    """Somewhere to mirror events to."""

    @abstractmethod
    def put(self, event_id: str, payload: dict[str, Any]) -> None:
        """Write one event under its id, replacing anything already there.

        Must be idempotent: writing the same id twice leaves one record.
        """

    @abstractmethod
    def get(self, event_id: str) -> dict[str, Any] | None:
        """Return a stored event, or None. Used to verify a sync."""

    def ping(self) -> bool:
        """True when the replica looks reachable."""
        return True

    def close(self) -> None:  # noqa: B027 - optional hook; the fake holds nothing
        """Release any connection. Safe to call more than once."""


@dataclass
class FakeCloudClient(CloudClient):
    """An in-memory replica for tests and for running with no network.

    ``writes`` counts attempts rather than records, which is what makes the
    idempotency test meaningful: two writes of one id must leave one record.
    """

    store: dict[str, dict[str, Any]] = field(default_factory=dict)
    writes: int = 0
    #: Number of upcoming writes to fail before succeeding. -1 fails forever.
    fail_times: int = 0
    online: bool = True

    def put(self, event_id: str, payload: dict[str, Any]) -> None:
        """Record the event unless this call is scripted to fail."""
        self.writes += 1
        if self.fail_times != 0:
            if self.fail_times > 0:
                self.fail_times -= 1
            raise CloudError("scripted failure")
        self.store[event_id] = payload

    def get(self, event_id: str) -> dict[str, Any] | None:
        """Return the stored event, or None."""
        return self.store.get(event_id)

    def ping(self) -> bool:
        """Report the scripted connectivity."""
        return self.online


class FirebaseCloudClient(CloudClient):
    """The real Firebase Realtime Database replica.

    ``firebase_admin`` is imported lazily so the project runs, and its tests
    pass, on a machine where the cloud library is not installed.
    """

    def __init__(
        self,
        cred_path: str | Path,
        db_url: str,
        *,
        root: str = "events",
        timeout_s: float = HTTP_TIMEOUT_S,
    ) -> None:
        self.cred_path = Path(cred_path)
        self.db_url = db_url
        self.root = root.strip("/")
        self.timeout_s = timeout_s
        self._reference = None

    def _ref(self, path: str = ""):
        """Return a database reference, initialising the app on first use."""
        if self._reference is None:
            import firebase_admin
            from firebase_admin import credentials

            name = "freshguard-sync"
            try:
                app = firebase_admin.get_app(name)
            except ValueError:
                app = firebase_admin.initialize_app(
                    credentials.Certificate(str(self.cred_path)),
                    {"databaseURL": self.db_url, "httpTimeout": self.timeout_s},
                    name=name,
                )
            from firebase_admin import db as firebase_db

            self._reference = firebase_db.reference("/", app=app)

        full = f"/{self.root}/{path}".rstrip("/") if path else f"/{self.root}"
        return self._reference.child(full.lstrip("/"))

    def put(self, event_id: str, payload: dict[str, Any]) -> None:
        """Write the event under its id. ``set`` never creates a second node."""
        try:
            self._ref(event_id).set(payload)
        except Exception as exc:
            raise CloudError(f"could not write {event_id}: {exc}") from exc

    def get(self, event_id: str) -> dict[str, Any] | None:
        """Read one event back."""
        try:
            return self._ref(event_id).get()
        except Exception as exc:
            raise CloudError(f"could not read {event_id}: {exc}") from exc

    def ping(self) -> bool:
        """Try a cheap read rather than an ICMP ping, which proves nothing.

        The probe reads one key from the events node. It must be a *legal*
        Firebase key: a path segment starting with a dot, or containing any of
        ``$ # [ ] /``, is rejected by the client itself, which would make the
        probe fail even when the network is perfectly healthy.
        """
        try:
            self._ref().order_by_key().limit_to_first(1).get()
        except Exception:
            logger.info("cloud replica is unreachable")
            return False
        return True


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What one cycle achieved."""

    sent: int = 0
    failed: int = 0
    offline: bool = False

    @property
    def made_progress(self) -> bool:
        """True when at least one event reached the replica."""
        return self.sent > 0


def pending_count(conn: sqlite3.Connection) -> int:
    """How many events are still waiting to be sent."""
    placeholders = ", ".join("?" for _ in UNSENT)
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM outbox WHERE sync_status IN ({placeholders})",  # noqa: S608
        UNSENT,
    ).fetchone()
    return int(row["total"])


def unsent_rows(conn: sqlite3.Connection, limit: int = BATCH_SIZE) -> list[sqlite3.Row]:
    """Return the oldest unsent events, so the replica stays in order."""
    placeholders = ", ".join("?" for _ in UNSENT)
    return conn.execute(
        "SELECT event_id, entity, payload_json, attempts FROM outbox "  # noqa: S608
        f"WHERE sync_status IN ({placeholders}) ORDER BY created_at, event_id LIMIT ?",
        (*UNSENT, limit),
    ).fetchall()


def mark_synced(conn: sqlite3.Connection, event_id: str) -> None:
    """Record a confirmed write. Only ever called after the cloud accepted it."""
    with db.transaction(conn):
        conn.execute(
            "UPDATE outbox SET sync_status = 'synced', synced_at = ? WHERE event_id = ?",
            (utc_now_iso(), event_id),
        )


def mark_failed(conn: sqlite3.Connection, event_id: str) -> None:
    """Count an attempt. The row stays and will be retried; nothing is lost."""
    with db.transaction(conn):
        conn.execute(
            "UPDATE outbox SET sync_status = 'failed', attempts = attempts + 1 "
            "WHERE event_id = ?",
            (event_id,),
        )


class SyncWorker:
    """Drains the outbox to a cloud replica, on its own thread.

    Backoff is per cycle rather than per row, because the frozen schema has
    nowhere to record a per-row retry time and the failure being backed off
    from is nearly always the link itself rather than one event.
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        client: CloudClient | None = None,
        *,
        db_path: str | Path | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        if conn is None and db_path is None:
            raise ValueError("SyncWorker needs either a connection or a database path")
        if client is None:
            raise ValueError("SyncWorker needs a cloud client")

        # A SQLite connection belongs to the thread that opened it. When the
        # worker is given a path it opens its own connection on whichever
        # thread it runs on, which is what lets it share the database with the
        # sensor loop safely in WAL mode.
        self._shared_conn = conn
        self.db_path = Path(db_path) if db_path is not None else None
        self._local = threading.local()
        self.client = client
        self.interval_s = interval_s
        self.batch_size = batch_size
        self.consecutive_failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection."""
        if self.db_path is None:
            assert self._shared_conn is not None  # noqa: S101 - guaranteed by __init__
            return self._shared_conn
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = db.connect(self.db_path)
            self._local.conn = existing
        return existing

    def close(self) -> None:
        """Close this thread's connection, if the worker opened one."""
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    @property
    def backoff_s(self) -> float:
        """How long to wait before the next cycle."""
        if self.consecutive_failures == 0:
            return self.interval_s
        return min(self.interval_s * (2**self.consecutive_failures), MAX_BACKOFF_S)

    def sync_once(self) -> SyncResult:
        """Send one batch. Never raises: a failed cycle is a result, not a crash."""
        rows = unsent_rows(self.conn, self.batch_size)
        if not rows:
            self.consecutive_failures = 0
            return SyncResult()

        if not self.client.ping():
            self.consecutive_failures += 1
            logger.info("cloud replica offline; %d event(s) still buffered", len(rows))
            return SyncResult(offline=True)

        sent = failed = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                # Unparseable payloads would fail forever; count the attempt and
                # move on rather than blocking every later event behind them.
                logger.exception("outbox row %s holds invalid JSON", row["event_id"])
                mark_failed(self.conn, row["event_id"])
                failed += 1
                continue

            try:
                self.client.put(row["event_id"], payload)
            except CloudError as exc:
                logger.warning("could not send %s: %s", row["event_id"], exc)
                mark_failed(self.conn, row["event_id"])
                failed += 1
                continue

            mark_synced(self.conn, row["event_id"])
            sent += 1

        self.consecutive_failures = 0 if failed == 0 else self.consecutive_failures + 1
        if sent:
            logger.info("synced %d event(s)", sent)
        return SyncResult(sent=sent, failed=failed)

    def drain(self, max_cycles: int = 100) -> SyncResult:
        """Send everything currently buffered. Used by tests and on reconnect.

        The offline flag is carried through: a caller that cannot tell "nothing
        to send" from "could not reach the replica" will report a successful
        sync that never happened.
        """
        total_sent = total_failed = 0
        offline = False
        for _ in range(max_cycles):
            result = self.sync_once()
            total_sent += result.sent
            total_failed += result.failed
            offline = result.offline
            if result.offline or (result.sent == 0 and result.failed == 0):
                break
        return SyncResult(sent=total_sent, failed=total_failed, offline=offline)

    # -- background thread -------------------------------------------------
    def stop(self) -> None:
        """Ask the worker to finish after the current cycle."""
        self._stop.set()

    def run(self, max_cycles: int | None = None) -> int:
        """Loop until stopped. Returns the number of cycles completed."""
        self._stop.clear()
        cycles = 0
        try:
            while not self._stop.is_set():
                self.sync_once()
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                # wait() rather than sleep() so stop() takes effect immediately.
                self._stop.wait(self.backoff_s)
        finally:
            if self.db_path is not None:
                self.close()
        return cycles

    def start(self) -> threading.Thread:
        """Run the worker on a daemon thread, off the sensor loop."""
        self._thread = threading.Thread(target=self.run, name="freshguard-sync", daemon=True)
        self._thread.start()
        return self._thread


def build_client(settings: Any, *, fake: bool = False) -> CloudClient:
    """Return the cloud client for these settings.

    Falls back to the in-memory client when Firebase is not configured, so a
    demo machine with no cloud project still buffers and drains correctly.
    """
    if fake or not settings.firebase.configured:
        if not fake:
            logger.warning("Firebase is not configured; events stay buffered locally")
        return FakeCloudClient()
    return FirebaseCloudClient(settings.firebase.cred_path, settings.firebase.db_url)
