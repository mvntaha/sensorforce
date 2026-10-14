"""On-demand fridge snapshots: the request, the worker's bridge, and the page."""

from __future__ import annotations

import base64
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from core import db, paths
from core import snapshots as store
from core.config import Settings
from core.users import create_user
from scripts.seed_thresholds import seed

DEVICE = "pi-01"
JPEG = b"\xff\xd8\xff\xe0" + b"fake jpeg body" * 10
PHOTO = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
NOW = datetime(2026, 10, 13, 8, 30, 0, tzinfo=UTC)
PASSWORD = "correct-horse-battery"  # noqa: S105 - test fixture


# ------------------------------------------------------------- a fake cloud
class FakeCloud:
    """Just enough of firebase_admin.db.Reference for the bridge."""

    def __init__(self) -> None:
        self.tree: dict[str, Any] = {}

    def child(self, path: str) -> FakeRef:
        return FakeRef(self, [p for p in path.split("/") if p])


class FakeRef:
    def __init__(self, cloud: FakeCloud, parts: list[str]) -> None:
        self.cloud = cloud
        self.parts = parts
        self.query: tuple[str, Any] | None = None
        self.limit: tuple[str, int] | None = None

    def _parent(self, create: bool) -> dict | None:
        node = self.cloud.tree
        for part in self.parts[:-1]:
            if part not in node:
                if not create:
                    return None
                node[part] = {}
            node = node[part]
        return node

    def get(self) -> Any:
        parent = self._parent(create=False)
        value = None if parent is None else parent.get(self.parts[-1])
        if isinstance(value, dict) and (self.query or self.limit):
            keys = sorted(value)
            if self.query:
                keys = [k for k in keys if k >= self.query[1]]
            if self.limit:
                kind, n = self.limit
                keys = keys[-n:] if kind == "last" else keys[:n]
            return {k: value[k] for k in keys}
        return value

    def set(self, value: Any) -> None:
        self._parent(create=True)[self.parts[-1]] = value

    def delete(self) -> None:
        parent = self._parent(create=False)
        if parent is not None:
            parent.pop(self.parts[-1], None)

    def push_as(self, key: str, value: Any) -> None:
        node = self._parent(create=True).setdefault(self.parts[-1], {})
        node[key] = value

    def order_by_key(self) -> FakeRef:
        return self

    def start_at(self, key: str) -> FakeRef:
        self.query = ("start", key)
        return self

    def limit_to_first(self, n: int) -> FakeRef:
        self.limit = ("first", n)
        return self

    def limit_to_last(self, n: int) -> FakeRef:
        self.limit = ("last", n)
        return self


def trigger(cloud: FakeCloud) -> Any:
    return cloud.child(f"pi_monitor/live/{DEVICE}/trigger_snapshot").get()


def device_answers(cloud: FakeCloud, key: str, ts: str = "2026-10-13T08:30:02Z") -> None:
    """What the Pi does: push the photo, then lower the flag."""
    cloud.child(f"pi_monitor/captures/{DEVICE}").push_as(key, {"ts": ts, "image_base64": PHOTO})
    cloud.child(f"pi_monitor/live/{DEVICE}/trigger_snapshot").set(False)


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "captures"
    monkeypatch.setattr(paths, "CAPTURE_DIR", target)
    return target


@pytest.fixture
def clock() -> list[datetime]:
    return [NOW]


@pytest.fixture
def bridge(conn: sqlite3.Connection, folder: Path, clock: list[datetime]):
    cloud = FakeCloud()
    return store.SnapshotBridge(conn, cloud, DEVICE, folder=folder, clock=lambda: clock[0]), cloud


def status(conn: sqlite3.Connection, request_id: int) -> str:
    return store.request_status(conn, request_id)["status"]


# ------------------------------------------------------------------ requests
class TestRequests:
    def test_a_request_starts_pending(self, conn):
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        assert request["status"] == "pending"

    def test_asking_twice_returns_the_same_open_request(self, conn):
        first = store.request_capture(conn, DEVICE, None, now=NOW)
        second = store.request_capture(conn, DEVICE, None, now=NOW + timedelta(seconds=1))
        assert second["id"] == first["id"]

    def test_a_photo_just_taken_blocks_another_for_a_moment(self, conn, bridge):
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        b.cycle()
        device_answers(cloud, "-K1")
        b.cycle()
        assert status(conn, request["id"]) == "done"

        with pytest.raises(store.SnapshotError, match="Try again"):
            store.request_capture(conn, DEVICE, None, now=NOW + timedelta(seconds=1))
        later = NOW + timedelta(seconds=store.COOLDOWN_S + 1)
        assert store.request_capture(conn, DEVICE, None, now=later)["status"] == "pending"

    def test_a_request_nobody_served_does_not_block_the_next(self, conn):
        """With the worker stopped, an old request must not hold the button forever."""
        old = store.request_capture(conn, DEVICE, None, now=NOW)
        later = NOW + timedelta(seconds=store.ABANDONED_AFTER_S + 1)

        fresh = store.request_capture(conn, DEVICE, None, now=later)

        assert fresh["id"] != old["id"]
        assert status(conn, old["id"]) == "failed"


# -------------------------------------------------------------------- bridge
class TestBridge:
    def test_a_pending_request_raises_the_trigger(self, conn, bridge):
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)

        b.cycle()

        assert trigger(cloud) is True
        assert status(conn, request["id"]) == "sent"

    def test_the_photo_is_stored_and_settles_the_request(self, conn, bridge, folder):
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        b.cycle()
        device_answers(cloud, "-K1")

        assert b.cycle() == 1

        done = store.request_status(conn, request["id"])
        assert done["status"] == "done"
        snapshot = store.get_snapshot(conn, done["snapshot_id"])
        assert snapshot["taken_at"] == "2026-10-13T08:30:02Z"
        assert store.snapshot_file(snapshot, folder).read_bytes() == JPEG

    def test_a_photo_is_never_stored_twice(self, conn, bridge, folder):
        b, cloud = bridge
        device_answers(cloud, "-K1")
        b.cycle()
        # A restarted worker starts from what the database already holds.
        again = store.SnapshotBridge(conn, cloud, DEVICE, folder=folder, clock=lambda: NOW)
        assert again.cycle() == 0
        assert len(store.list_snapshots(conn, DEVICE)) == 1

    def test_a_lowered_flag_without_a_photo_fails_the_request(self, conn, bridge):
        """The Pi lowers the flag after a failed capture, or on boot."""
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        b.cycle()
        cloud.child(f"pi_monitor/live/{DEVICE}/trigger_snapshot").set(False)

        b.cycle()

        failed = store.request_status(conn, request["id"])
        assert failed["status"] == "failed"
        assert "could not take" in failed["error"]

    def test_an_unanswered_request_times_out_and_lowers_the_flag(self, conn, bridge, clock):
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        b.cycle()
        clock[0] = NOW + timedelta(seconds=store.REQUEST_TIMEOUT_S + 1)

        b.cycle()

        assert status(conn, request["id"]) == "failed"
        assert trigger(cloud) is False

    def test_a_request_gone_stale_while_the_worker_was_off_is_never_sent(self, conn, bridge, clock):
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        clock[0] = NOW + timedelta(seconds=store.REQUEST_TIMEOUT_S + 60)

        b.cycle()

        assert status(conn, request["id"]) == "failed"
        assert trigger(cloud) is not True

    def test_firmware_that_only_updates_the_live_node_is_understood(self, conn, bridge, folder):
        """The Pi script in use writes the photo to live/ and never to captures/."""
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        b.cycle()
        live = f"pi_monitor/live/{DEVICE}"
        cloud.child(f"{live}/image_base64").set(PHOTO)
        cloud.child(f"{live}/last_snapshot_ts").set("2026-10-13T08:30:04Z")
        cloud.child(f"{live}/trigger_snapshot").set(False)

        assert b.cycle() == 1

        done = store.request_status(conn, request["id"])
        assert done["status"] == "done"
        snapshot = store.get_snapshot(conn, done["snapshot_id"])
        assert store.snapshot_file(snapshot, folder).read_bytes() == JPEG

    def test_a_live_photo_from_a_reset_clock_is_still_the_answer(self, conn, bridge):
        """A Pi that believes it is 2000 stamps its photo then; it still just answered."""
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        b.cycle()
        live = f"pi_monitor/live/{DEVICE}"
        cloud.child(f"{live}/image_base64").set(PHOTO)
        cloud.child(f"{live}/last_snapshot_ts").set("2000-01-15T20:05:00Z")
        cloud.child(f"{live}/trigger_snapshot").set(False)

        assert b.cycle() == 1

        done = store.request_status(conn, request["id"])
        assert done["status"] == "done"
        assert store.get_snapshot(conn, done["snapshot_id"])["taken_at"].startswith("2026-")

    def test_an_old_live_photo_is_not_taken_as_the_answer(self, conn, bridge):
        b, cloud = bridge
        request = store.request_capture(conn, DEVICE, None, now=NOW)
        b.cycle()
        live = f"pi_monitor/live/{DEVICE}"
        cloud.child(f"{live}/image_base64").set(PHOTO)
        cloud.child(f"{live}/last_snapshot_ts").set("2026-10-13T07:00:00Z")
        cloud.child(f"{live}/trigger_snapshot").set(False)

        b.cycle()

        assert status(conn, request["id"]) == "failed"

    def test_something_that_is_not_a_jpeg_is_refused(self, conn, bridge):
        b, cloud = bridge
        cloud.child(f"pi_monitor/captures/{DEVICE}").push_as(
            "-K1", {"ts": "2026-10-13T08:30:02Z", "image_base64": "data:text/html;base64,PGI+"}
        )
        assert b.cycle() == 0

    def test_deleting_removes_the_file_now_and_the_cloud_copy_next_cycle(
        self, conn, bridge, folder
    ):
        b, cloud = bridge
        device_answers(cloud, "-K1")
        b.cycle()
        snapshot = store.list_snapshots(conn, DEVICE)[0]
        path = store.snapshot_file(store.get_snapshot(conn, snapshot["id"]), folder)

        assert store.delete_snapshot(conn, snapshot["id"], folder)
        assert not path.exists()
        assert store.list_snapshots(conn, DEVICE) == []

        b.cycle()
        assert cloud.child(f"pi_monitor/captures/{DEVICE}/-K1").get() is None
        # And it is never collected again.
        assert store.SnapshotBridge(conn, cloud, DEVICE, folder=folder).cycle() == 0


def test_download_name_is_readable():
    row = {"id": 3, "device_id": DEVICE, "taken_at": "2026-10-13T08:35:12Z"}
    assert store.download_name(row) == "fridge-pi-01-2026-10-13-083512.jpg"


def test_a_capture_file_name_cannot_escape_the_folder():
    with pytest.raises(ValueError):
        paths.capture_path("../../app.py")


# ---------------------------------------------------------------- the pages
@pytest.fixture
def app(tmp_path: Path, folder: Path) -> Iterator[Flask]:
    database = tmp_path / "snap.db"
    connection = db.bootstrap(database)
    seed(connection)
    create_user(connection, "admin", PASSWORD, "admin")
    connection.close()
    settings = Settings.from_env(
        {
            "FG_SECRET_KEY": "test-secret-key-that-is-long-enough",
            "FG_SIM_CONTROL_FILE": str(tmp_path / "sim_scenario"),
        }
    )
    application = create_app(settings, db_path=database)
    application.config.update(TESTING=True, FG_LOGIN_FAILURE_DELAY_S=0)
    yield application


@pytest.fixture
def client(app: Flask) -> tuple[FlaskClient, str]:
    session = app.test_client()
    page = session.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    session.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token})
    home = session.get("/snapshots")
    token = re.search(r'name="csrf_token" value="([^"]+)"', home.get_data(as_text=True)).group(1)
    return session, token


def stored_photo(app: Flask, folder: Path) -> int:
    """Run the bridge once against a fake cloud holding one photograph."""
    conn = db.connect(app.config["FG_DB_PATH"])
    try:
        cloud = FakeCloud()
        device_answers(cloud, "-K1")
        store.SnapshotBridge(conn, cloud, DEVICE, folder=folder).cycle()
        return store.list_snapshots(conn, DEVICE)[0]["id"]
    finally:
        conn.close()


class TestPage:
    def test_the_page_is_in_the_navigation_and_renders(self, client):
        session, _ = client
        page = session.get("/snapshots").get_data(as_text=True)
        assert "Take snapshot" in page
        assert "No snapshots yet" in page
        assert 'href="/snapshots"' in page

    def test_the_page_needs_a_sign_in(self, app):
        assert app.test_client().get("/snapshots").status_code in (302, 401)

    def test_the_dashboard_has_a_shortcut(self, client):
        session, _ = client
        assert "fg-dashboard-capture" in session.get("/dashboard").get_data(as_text=True)

    def test_capture_records_a_request(self, client):
        session, token = client
        response = session.post("/api/snapshots/capture", headers={"X-CSRF-Token": token})
        assert response.status_code == 202
        request_id = response.get_json()["data"]["id"]

        state = session.get(f"/api/snapshots/requests/{request_id}").get_json()["data"]
        assert state["status"] == "pending"

    def test_capture_without_the_csrf_token_is_refused(self, client):
        session, _ = client
        assert session.post("/api/snapshots/capture").status_code in (400, 403)

    def test_a_stored_photo_can_be_viewed_and_downloaded(self, app, client, folder):
        session, _ = client
        snapshot_id = stored_photo(app, folder)

        shown = session.get(f"/snapshots/{snapshot_id}.jpg")
        assert shown.status_code == 200
        assert shown.data == JPEG
        assert "attachment" not in shown.headers.get("Content-Disposition", "")

        saved = session.get(f"/snapshots/{snapshot_id}.jpg?download=1")
        assert "attachment" in saved.headers["Content-Disposition"]
        assert "fridge-pi-01-2026-10-13-083002.jpg" in saved.headers["Content-Disposition"]

        listed = session.get("/api/snapshots").get_json()["data"]
        assert [s["id"] for s in listed] == [snapshot_id]

    def test_a_photo_can_be_deleted(self, app, client, folder):
        session, token = client
        snapshot_id = stored_photo(app, folder)

        response = session.delete(f"/api/snapshots/{snapshot_id}", headers={"X-CSRF-Token": token})

        assert response.status_code == 200
        assert session.get(f"/snapshots/{snapshot_id}.jpg").status_code == 404
        assert session.get("/api/snapshots").get_json()["data"] == []
