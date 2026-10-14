"""The outbox drain: idempotency, retry, reconnect and no duplicates.

Everything here runs against the in-memory cloud client. The whole point of
the design is that the replica is written by event id with ``set``, so these
tests can prove "exactly once" without a network.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from core import db
from core.sync import (
    MAX_BACKOFF_S,
    CloudError,
    FakeCloudClient,
    SyncWorker,
    mark_failed,
    mark_synced,
    pending_count,
    unsent_rows,
)

DEVICE = "pi-test"


def queue(conn: sqlite3.Connection, entity: str = "alert", **payload: object) -> str:
    """Put one event in the outbox and return its id."""
    event_id = str(uuid.uuid4())
    body = {
        "event_id": event_id,
        "entity": entity,
        "op": "create",
        "ts": "2026-09-24T10:00:00Z",
        "device_id": DEVICE,
        **payload,
    }
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO outbox (event_id, entity, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (event_id, entity, json.dumps(body, sort_keys=True), "2026-09-24T10:00:00Z"),
        )
    return event_id


@pytest.fixture
def client() -> FakeCloudClient:
    return FakeCloudClient()


@pytest.fixture
def worker(conn: sqlite3.Connection, client: FakeCloudClient) -> SyncWorker:
    return SyncWorker(conn, client, interval_s=0.01)


class TestDraining:
    def test_a_queued_event_reaches_the_replica(self, conn, client, worker) -> None:
        event_id = queue(conn)
        result = worker.sync_once()
        assert result.sent == 1
        assert client.get(event_id) is not None

    def test_a_synced_row_records_when(self, conn, worker) -> None:
        queue(conn)
        worker.sync_once()
        row = conn.execute("SELECT sync_status, synced_at FROM outbox").fetchone()
        assert row["sync_status"] == "synced"
        assert row["synced_at"]

    def test_nothing_queued_is_not_an_error(self, worker) -> None:
        result = worker.sync_once()
        assert result.sent == 0
        assert result.failed == 0

    def test_a_synced_event_is_not_sent_again(self, conn, client, worker) -> None:
        queue(conn)
        worker.sync_once()
        worker.sync_once()
        assert client.writes == 1

    def test_events_are_sent_oldest_first(self, conn, client, worker) -> None:
        first, second = queue(conn), queue(conn)
        conn.execute("UPDATE outbox SET created_at = ? WHERE event_id = ?", ("2026-01-01", first))
        worker.sync_once()
        assert next(iter(client.store)) == first
        assert second in client.store

    def test_a_batch_is_limited(self, conn, client) -> None:
        for _ in range(5):
            queue(conn)
        worker = SyncWorker(conn, client, batch_size=2)
        assert worker.sync_once().sent == 2
        assert pending_count(conn) == 3

    def test_drain_empties_the_outbox(self, conn, worker) -> None:
        for _ in range(7):
            queue(conn)
        worker.drain()
        assert pending_count(conn) == 0


class TestIdempotency:
    def test_sending_the_same_event_twice_leaves_one_record(self, conn, client, worker) -> None:
        """The property the whole design exists to guarantee."""
        event_id = queue(conn)
        worker.sync_once()

        # Simulate a lost acknowledgement: the cloud took the write, but the
        # worker never learned that, so the row is still marked unsent.
        with db.transaction(conn):
            conn.execute(
                "UPDATE outbox SET sync_status = 'pending', synced_at = NULL WHERE event_id = ?",
                (event_id,),
            )
        worker.sync_once()

        assert client.writes == 2, "the event really was sent twice"
        assert len(client.store) == 1, "but the replica holds one record"

    def test_every_event_id_appears_exactly_once(self, conn, client, worker) -> None:
        ids = [queue(conn) for _ in range(10)]
        worker.drain()
        assert sorted(client.store) == sorted(ids)
        assert len(client.store) == len(set(client.store))

    def test_the_replica_count_matches_the_outbox_count(self, conn, client, worker) -> None:
        """The check the IoT Test Matrix asks for after a reconnect."""
        for _ in range(12):
            queue(conn)
        worker.drain()
        outbox_total = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        assert len(client.store) == outbox_total

    def test_the_stored_payload_carries_the_sync_fields(self, conn, client, worker) -> None:
        event_id = queue(conn)
        worker.sync_once()
        stored = client.get(event_id)
        assert stored["event_id"] == event_id
        assert stored["entity"] == "alert"
        assert stored["op"] == "create"
        assert stored["device_id"] == DEVICE
        assert stored["ts"]

    def test_a_deletion_is_an_event_too(self, conn, client, worker) -> None:
        """Otherwise the replica keeps food the kitchen has thrown away."""
        event_id = queue(conn, entity="item", op="delete")
        worker.sync_once()
        assert client.get(event_id)["op"] == "delete"


class TestFailureHandling:
    def test_a_failed_write_keeps_the_row(self, conn, worker, client) -> None:
        queue(conn)
        client.fail_times = -1
        assert worker.sync_once().failed == 1
        row = conn.execute("SELECT sync_status, attempts, synced_at FROM outbox").fetchone()
        assert row["sync_status"] == "failed"
        assert row["attempts"] == 1
        assert row["synced_at"] is None

    def test_a_failed_row_is_retried(self, conn, worker, client) -> None:
        queue(conn)
        client.fail_times = 2
        worker.sync_once()
        worker.sync_once()
        assert worker.sync_once().sent == 1
        assert pending_count(conn) == 0

    def test_attempts_accumulate(self, conn, worker, client) -> None:
        queue(conn)
        client.fail_times = -1
        worker.sync_once()
        worker.sync_once()
        assert conn.execute("SELECT attempts FROM outbox").fetchone()[0] == 2

    def test_an_unsent_row_is_never_deleted(self, conn, worker, client) -> None:
        queue(conn)
        client.fail_times = -1
        for _ in range(5):
            worker.sync_once()
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1

    def test_an_unparseable_payload_does_not_block_later_events(self, conn, client, worker) -> None:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO outbox (event_id, entity, payload_json, created_at) "
                "VALUES ('broken', 'alert', 'not json', '2026-01-01')"
            )
        good = queue(conn)
        worker.sync_once()
        assert client.get(good) is not None

    def test_a_partial_batch_keeps_what_failed(self, conn, client) -> None:
        """Killing the worker mid-batch must not lose or duplicate anything."""
        first, second = queue(conn), queue(conn)
        conn.execute("UPDATE outbox SET created_at = ? WHERE event_id = ?", ("2026-01-01", first))
        client.fail_times = 1  # the first write fails, the second succeeds
        worker = SyncWorker(conn, client, interval_s=0.01)
        result = worker.sync_once()

        assert result.sent == 1
        assert result.failed == 1
        assert client.get(second) is not None
        assert client.get(first) is None

        client.fail_times = 0
        worker.sync_once()
        assert len(client.store) == 2, "the retry completes the pair, without duplicating"


class TestOfflineAndReconnect:
    def test_nothing_is_sent_while_offline(self, conn, client, worker) -> None:
        for _ in range(3):
            queue(conn)
        client.online = False
        result = worker.sync_once()
        assert result.offline is True
        assert result.sent == 0
        assert client.writes == 0

    def test_events_keep_buffering_while_offline(self, conn, client, worker) -> None:
        client.online = False
        for _ in range(5):
            queue(conn)
            worker.sync_once()
        assert pending_count(conn) == 5

    def test_reconnecting_drains_the_backlog(self, conn, client, worker) -> None:
        """The scenario the IoT Test Matrix requires."""
        client.online = False
        ids = [queue(conn) for _ in range(6)]
        worker.sync_once()
        assert pending_count(conn) == 6

        client.online = True
        worker.drain()

        assert pending_count(conn) == 0
        assert sorted(client.store) == sorted(ids)
        assert len(client.store) == 6, "no duplicates after the reconnect"

    def test_a_second_disconnection_behaves_the_same(self, conn, client, worker) -> None:
        client.online = False
        queue(conn)
        worker.sync_once()
        client.online = True
        worker.drain()

        client.online = False
        queue(conn)
        worker.sync_once()
        client.online = True
        worker.drain()

        assert pending_count(conn) == 0
        assert len(client.store) == 2


class TestBackoff:
    def test_a_healthy_worker_uses_the_plain_interval(self, worker) -> None:
        assert worker.backoff_s == worker.interval_s

    def test_failures_lengthen_the_wait(self, conn, client, worker) -> None:
        queue(conn)
        client.online = False
        worker.sync_once()
        first = worker.backoff_s
        worker.sync_once()
        assert worker.backoff_s > first

    def test_the_wait_is_capped(self, conn, client, worker) -> None:
        queue(conn)
        client.online = False
        for _ in range(40):
            worker.sync_once()
        assert worker.backoff_s == MAX_BACKOFF_S

    def test_success_resets_the_wait(self, conn, client, worker) -> None:
        queue(conn)
        client.online = False
        worker.sync_once()
        client.online = True
        worker.sync_once()
        assert worker.backoff_s == worker.interval_s


class TestWorkerLoop:
    def test_it_runs_for_a_bounded_number_of_cycles(self, conn, client) -> None:
        queue(conn)
        worker = SyncWorker(conn, client, interval_s=0.001)
        assert worker.run(max_cycles=3) == 3

    def test_stop_ends_the_loop(self, conn, client) -> None:
        worker = SyncWorker(conn, client, interval_s=0.001)
        worker.stop()
        assert worker.run(max_cycles=5) >= 1

    def test_it_can_run_on_its_own_thread(self, conn, client, db_path) -> None:
        """The sensor loop must never wait on the network.

        The worker is given a path rather than a connection, because a SQLite
        connection belongs to the thread that opened it.
        """
        event_id = queue(conn)
        worker = SyncWorker(client=client, db_path=db_path, interval_s=0.001)
        thread = worker.start()
        try:
            for _ in range(200):
                if client.get(event_id):
                    break
                import time

                time.sleep(0.01)
        finally:
            worker.stop()
            thread.join(timeout=2)
        assert client.get(event_id) is not None
        assert thread.is_alive() is False


class TestThreadSafety:
    def test_a_worker_needs_a_connection_or_a_path(self, client) -> None:
        with pytest.raises(ValueError, match="connection or a database path"):
            SyncWorker(client=client)

    def test_a_worker_given_a_path_opens_its_own_connection(self, conn, client, db_path) -> None:
        event_id = queue(conn)
        worker = SyncWorker(client=client, db_path=db_path)
        try:
            assert worker.sync_once().sent == 1
            assert client.get(event_id) is not None
        finally:
            worker.close()


class TestHelpers:
    def test_pending_counts_both_unsent_states(self, conn) -> None:
        first, second = queue(conn), queue(conn)
        mark_failed(conn, first)
        assert pending_count(conn) == 2
        mark_synced(conn, second)
        assert pending_count(conn) == 1

    def test_unsent_rows_exclude_synced_events(self, conn) -> None:
        event_id = queue(conn)
        mark_synced(conn, event_id)
        assert unsent_rows(conn) == []

    def test_the_fake_client_reports_its_failures(self) -> None:
        client = FakeCloudClient(fail_times=1)
        with pytest.raises(CloudError):
            client.put("x", {})
        client.put("x", {})
        assert client.get("x") == {}


class TestOfflineReporting:
    def test_drain_reports_that_it_could_not_reach_the_replica(self, conn, client, worker) -> None:
        """Otherwise a caller cannot tell 'nothing to send' from 'no network'."""
        queue(conn)
        client.online = False
        result = worker.drain()
        assert result.offline is True
        assert result.sent == 0

    def test_drain_reports_online_when_it_finishes(self, conn, client, worker) -> None:
        queue(conn)
        assert worker.drain().offline is False
