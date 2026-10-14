"""RFID taps: tapped in asks for details, details link the tag, tapped out is eaten."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from core import db, rfid
from core import items as item_store
from core.config import Settings
from core.users import create_user
from scripts.seed_thresholds import seed
from tests.test_snapshots import FakeCloud

DEVICE = "pi-01"
TAG = "584607310456"
OTHER = "465775560315"
NOW = datetime(2026, 10, 13, 10, 0, 0, tzinfo=UTC)
PASSWORD = "correct-horse-battery"  # noqa: S105 - test fixture


def tap(cloud: FakeCloud, key: str, tag: str, action: str, ts: str = "2026-10-13T10:00:05Z"):
    """What the Pi logs for one tap."""
    cloud.child(f"pi_monitor/rfid_history/{DEVICE}").push_as(
        key, {"tag_id": tag, "action": action, "item_name": f"Item-{tag[-4:]}", "ts": ts}
    )


def started(
    conn: sqlite3.Connection, inside: dict | None = None
) -> tuple[rfid.RfidBridge, FakeCloud]:
    """A bridge past its first run, with the Pi's list as given."""
    cloud = FakeCloud()
    if inside is not None:
        cloud.child(f"pi_monitor/live/{DEVICE}/items_inside_tags").set(inside)
    bridge = rfid.RfidBridge(conn, cloud, DEVICE, clock=lambda: NOW)
    bridge.cycle()
    return bridge, cloud


def add_item(conn: sqlite3.Connection, zone_id: int, category: str, tag: str | None) -> dict:
    data = item_store.validate(
        conn,
        {
            "qr_id": item_store.next_free_identifier(conn),
            "name": "Milk",
            "category": category,
            "quantity": 1,
            "zone_id": zone_id,
            "storage_date": "2026-10-13",
            "rfid_tag": tag,
        },
    )
    return item_store.create(conn, data, created_by=None, device_id=DEVICE)


class TestFirstRun:
    def test_tags_already_inside_wait_for_details(self, conn):
        started(conn, {TAG: "Item-0456"})
        assert [row["tag_id"] for row in rfid.list_pending(conn)] == [TAG]

    def test_older_taps_are_not_replayed(self, conn):
        cloud = FakeCloud()
        tap(cloud, "-K1", OTHER, "ADDED")
        rfid.RfidBridge(conn, cloud, DEVICE, clock=lambda: NOW).cycle()
        assert rfid.list_pending(conn) == []

    def test_a_tag_whose_item_exists_is_not_pending(self, conn, zone_id, category):
        add_item(conn, zone_id, category, TAG)
        started(conn, {TAG: "Item-0456"})
        assert rfid.list_pending(conn) == []


class TestTaps:
    def test_a_new_tag_tapped_in_waits_for_details(self, conn):
        bridge, cloud = started(conn)
        tap(cloud, "-K1", TAG, "ADDED")

        assert bridge.cycle() == 1

        pending = rfid.get_pending(conn, TAG)
        assert pending["tapped_in_at"] == "2026-10-13T10:00:05Z"

    def test_adding_details_links_the_tag_and_clears_the_wait(self, conn, zone_id, category):
        bridge, cloud = started(conn)
        tap(cloud, "-K1", TAG, "ADDED")
        bridge.cycle()

        item = add_item(conn, zone_id, category, TAG)

        assert item["rfid_tag"] == TAG
        assert item["qr_id"].startswith("FG-")
        assert rfid.list_pending(conn) == []

    def test_tapping_out_removes_the_item_and_logs_it_as_consumed(self, conn, zone_id, category):
        bridge, cloud = started(conn)
        item = add_item(conn, zone_id, category, TAG)
        tap(cloud, "-K1", TAG, "REMOVED")

        bridge.cycle()

        assert item_store.get(conn, item["qr_id"]) is None
        log = conn.execute("SELECT qr_id, outcome FROM consumption_log").fetchall()
        assert [(row["qr_id"], row["outcome"]) for row in log] == [(item["qr_id"], "consumed")]

    def test_a_tag_tapped_out_before_its_details_just_goes(self, conn):
        bridge, cloud = started(conn)
        tap(cloud, "-K1", TAG, "ADDED")
        tap(cloud, "-K2", TAG, "REMOVED", ts="2026-10-13T10:01:00Z")

        bridge.cycle()

        assert rfid.list_pending(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM consumption_log").fetchone()[0] == 0

    def test_after_a_pi_restart_an_added_for_a_known_item_is_ignored(self, conn, zone_id, category):
        """The Pi forgets its list on restart, so its next tap says ADDED again."""
        bridge, cloud = started(conn)
        item = add_item(conn, zone_id, category, TAG)
        tap(cloud, "-K1", TAG, "ADDED")

        bridge.cycle()

        assert item_store.get(conn, item["qr_id"]) is not None
        assert rfid.list_pending(conn) == []

    def test_a_tap_is_applied_once_even_by_a_restarted_worker(self, conn, zone_id, category):
        bridge, cloud = started(conn)
        add_item(conn, zone_id, category, TAG)
        tap(cloud, "-K1", TAG, "REMOVED")
        bridge.cycle()

        again = rfid.RfidBridge(conn, cloud, DEVICE, clock=lambda: NOW)
        assert again.cycle() == 0
        assert conn.execute("SELECT COUNT(*) FROM consumption_log").fetchone()[0] == 1


class TestValidation:
    def test_a_tag_must_be_digits(self, conn, zone_id, category):
        with pytest.raises(item_store.ItemError, match="digits"):
            add_item(conn, zone_id, category, "ab12; DROP")

    def test_one_tag_cannot_be_on_two_items(self, conn, zone_id, category):
        add_item(conn, zone_id, category, TAG)
        with pytest.raises(item_store.ItemError, match="already linked"):
            add_item(conn, zone_id, category, TAG)

    def test_an_item_without_a_tag_still_works(self, conn, zone_id, category):
        assert add_item(conn, zone_id, category, None)["rfid_tag"] is None


# ---------------------------------------------------------------- the pages
@pytest.fixture
def app(tmp_path: Path) -> Iterator[Flask]:
    database = tmp_path / "rfid.db"
    connection = db.bootstrap(database)
    seed(connection)
    create_user(connection, "admin", PASSWORD, "admin")
    connection.execute(
        "INSERT INTO rfid_pending (tag_id, device_id, tapped_in_at, device_label) "
        "VALUES (?, ?, ?, ?)",
        (TAG, DEVICE, "2026-10-12T18:30:00Z", "Item-0456"),
    )
    connection.commit()
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
    home = session.get("/dashboard")
    token = re.search(r'name="csrf_token" value="([^"]+)"', home.get_data(as_text=True)).group(1)
    return session, token


class TestPages:
    def test_every_page_shows_the_banner_with_a_link_to_the_form(self, client):
        session, _ = client
        for page in ("/dashboard", "/inventory", "/alerts", "/snapshots"):
            html = session.get(page).get_data(as_text=True)
            assert "New item tapped in" in html, page
            assert f"/item/new?rfid={TAG}" in html, page

    def test_the_bell_counts_the_waiting_tag(self, client):
        session, _ = client
        html = session.get("/dashboard").get_data(as_text=True)
        assert re.search(r'id="fg-bell-count"\s*>1<', html)

    def test_inventory_and_alerts_list_it_as_details_needed(self, client):
        session, _ = client
        for page in ("/inventory", "/alerts"):
            assert "Unnamed tag &hellip;0456" in session.get(page).get_data(as_text=True)

    def test_the_api_lists_waiting_tags(self, client):
        session, _ = client
        data = session.get("/api/rfid/pending").get_json()["data"]
        assert [(t["tag_id"], t["short"]) for t in data] == [(TAG, "0456")]

    def test_the_form_shows_the_tag_and_the_day_it_was_tapped_in(self, client):
        session, _ = client
        html = session.get(f"/item/new?rfid={TAG}").get_data(as_text=True)
        assert f'name="rfid_tag" id="field-rfid_tag" value="{TAG}"' in html
        assert 'value="2026-10-12" required' in html

    def test_an_unknown_tag_in_the_address_is_not_trusted(self, client):
        session, _ = client
        html = session.get("/item/new?rfid=999999999999").get_data(as_text=True)
        assert 'name="rfid_tag"' not in html

    def test_saving_the_form_links_the_tag_and_the_banner_goes(self, client):
        session, token = client
        response = session.post(
            "/api/items",
            json={
                "qr_id": "FG-0001",
                "name": "Yoghurt",
                "category": "dairy",
                "quantity": 1,
                "zone_id": 1,
                "storage_date": "2026-10-12",
                "rfid_tag": TAG,
            },
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 201
        assert response.get_json()["data"]["rfid_tag"] == TAG
        assert session.get("/api/rfid/pending").get_json()["data"] == []
        # The phrase also lives in the banner's <template>; the tag's link must not.
        assert f"/item/new?rfid={TAG}" not in session.get("/dashboard").get_data(as_text=True)
