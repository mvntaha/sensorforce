"""The JSON API: item CRUD, readings, status, alerts, health and admin."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from core import db
from core import items as item_store
from core.config import Settings, get_settings
from core.users import create_user
from scripts.seed_thresholds import seed
from web.security import throttle

ADMIN_PASSWORD = "correct-horse-battery"  # noqa: S105 - test fixture
STAFF_PASSWORD = "staff-password-1234"  # noqa: S105 - test fixture


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Flask]:
    database = tmp_path / "api.db"
    connection = db.bootstrap(database)
    seed(connection)
    create_user(connection, "admin", ADMIN_PASSWORD, "admin")
    create_user(connection, "cook", STAFF_PASSWORD, "staff")
    connection.close()

    settings = Settings.from_env(
        {
            "FG_SECRET_KEY": "test-secret-key-that-is-long-enough",
            # Keep the simulator's control file inside the test's own directory;
            # it defaults to sitting beside the database, which here is the repo.
            "FG_SIM_CONTROL_FILE": str(tmp_path / "sim_scenario"),
        }
    )
    application = create_app(settings, db_path=database)
    application.config.update(TESTING=True, FG_LOGIN_FAILURE_DELAY_S=0)
    throttle._failures.clear()

    with application.app_context():
        yield application


@pytest.fixture
def conn(app: Flask) -> Iterator[sqlite3.Connection]:
    connection = db.connect(app.config["FG_DB_PATH"])
    try:
        yield connection
    finally:
        connection.close()


def sign_in(client: FlaskClient, username: str, password: str) -> str:
    """Sign in and return the session's CSRF token."""
    import re

    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    client.post("/login", data={"username": username, "password": password, "csrf_token": token})
    home = client.get("/dashboard")
    return re.search(r'name="csrf_token" value="([^"]+)"', home.get_data(as_text=True)).group(1)


@pytest.fixture
def admin_client(app: Flask) -> tuple[FlaskClient, str]:
    client = app.test_client()
    return client, sign_in(client, "admin", ADMIN_PASSWORD)


@pytest.fixture
def staff_client(app: Flask) -> tuple[FlaskClient, str]:
    client = app.test_client()
    return client, sign_in(client, "cook", STAFF_PASSWORD)


def zone_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT id FROM zones LIMIT 1").fetchone()["id"])


def new_item(conn: sqlite3.Connection, **overrides: object) -> dict:
    payload = {
        "qr_id": "FG-0001",
        "name": "Milk",
        "category": "dairy",
        "quantity": 2,
        "unit": "L",
        "zone_id": zone_id(conn),
        "storage_date": "2026-09-20",
        "expiry_date": "2026-12-01",
    }
    payload.update(overrides)
    return payload


def post_json(client: FlaskClient, token: str, url: str, body: dict, method: str = "post"):
    return getattr(client, method)(url, json=body, headers={"X-CSRF-Token": token})


class TestSeed:
    def test_every_seeded_threshold_is_labelled_an_assumption(
        self, conn: sqlite3.Connection
    ) -> None:
        """SRS rule 4: a limit is cited or openly labelled an assumption."""
        rows = conn.execute("SELECT category, source_note FROM thresholds").fetchall()
        assert rows
        for row in rows:
            assert row["source_note"] == "prototype/demo assumption"

    def test_the_seeded_zone_documents_its_gas_limit(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT gas_abnormal, gas_source_note FROM zones").fetchone()
        assert row["gas_abnormal"] > 0
        assert row["gas_source_note"]

    def test_seeding_twice_changes_nothing(self, conn: sqlite3.Connection) -> None:
        before = conn.execute("SELECT COUNT(*) FROM thresholds").fetchone()[0]
        added = seed(conn)
        assert added["categories"] == 0
        assert conn.execute("SELECT COUNT(*) FROM thresholds").fetchone()[0] == before


class TestAuthorisation:
    @pytest.mark.parametrize(
        "url", ["/api/items", "/api/zones", "/api/status", "/api/alerts", "/api/health"]
    )
    def test_anonymous_requests_are_refused(self, app: Flask, url: str) -> None:
        response = app.test_client().get(url)
        assert response.status_code == 401
        assert response.get_json()["ok"] is False

    @pytest.mark.parametrize("url", ["/api/thresholds", "/api/settings", "/api/users"])
    def test_staff_cannot_reach_admin_routes(self, staff_client, url: str) -> None:
        client, _token = staff_client
        assert client.get(url).status_code == 403

    def test_a_write_without_a_csrf_token_is_refused(self, admin_client, conn) -> None:
        client, _token = admin_client
        assert client.post("/api/items", json=new_item(conn)).status_code == 400


class TestItemCrud:
    def test_create_read_update_delete(self, admin_client, conn: sqlite3.Connection) -> None:
        client, token = admin_client

        created = post_json(client, token, "/api/items", new_item(conn))
        assert created.status_code == 201
        assert created.get_json()["data"]["qr_id"] == "FG-0001"

        fetched = client.get("/api/items/FG-0001")
        assert fetched.get_json()["data"]["name"] == "Milk"

        updated = post_json(
            client, token, "/api/items/FG-0001", new_item(conn, name="Whole milk"), method="put"
        )
        assert updated.get_json()["data"]["name"] == "Whole milk"

        removed = client.delete("/api/items/FG-0001", headers={"X-CSRF-Token": token})
        assert removed.status_code == 200
        assert client.get("/api/items/FG-0001").status_code == 404

    def test_every_write_queues_exactly_one_outbox_row(
        self, admin_client, conn: sqlite3.Connection
    ) -> None:
        """Create, update and delete are all events the cloud copy must see."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        post_json(client, token, "/api/items/FG-0001", new_item(conn, name="X"), method="put")
        client.delete("/api/items/FG-0001", headers={"X-CSRF-Token": token})

        rows = conn.execute(
            "SELECT payload_json FROM outbox WHERE entity = 'item' ORDER BY created_at, event_id"
        ).fetchall()
        assert len(rows) == 3
        import json

        operations = sorted(json.loads(row["payload_json"])["op"] for row in rows)
        assert operations == ["create", "delete", "update"]

    def test_outbox_event_ids_are_unique(self, admin_client, conn: sqlite3.Connection) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        post_json(client, token, "/api/items/FG-0001", new_item(conn, name="X"), method="put")
        ids = [row["event_id"] for row in conn.execute("SELECT event_id FROM outbox")]
        assert len(ids) == len(set(ids))

    def test_a_duplicate_identifier_is_refused(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        again = post_json(client, token, "/api/items", new_item(conn))
        assert again.status_code == 422
        assert "already in use" in again.get_json()["error"]

    @pytest.mark.parametrize(
        ("field", "value", "fragment"),
        [
            ("qr_id", "FG-1", "FG-0001"),
            ("quantity", 0, "greater than zero"),
            ("quantity", "lots", "must be a number"),
            ("category", "unicorn", "Unknown category"),
            ("zone_id", 999, "Unknown zone"),
            ("storage_date", "2026-02-31", "real date"),
            ("expiry_date", "2026-09-19", "before storage_date"),
            ("name", "", "required"),
        ],
    )
    def test_invalid_input_is_refused_with_a_reason(
        self, admin_client, conn, field: str, value: object, fragment: str
    ) -> None:
        client, token = admin_client
        response = post_json(client, token, "/api/items", new_item(conn, **{field: value}))
        assert response.status_code == 422
        assert fragment in response.get_json()["error"]

    def test_nothing_is_written_when_validation_fails(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn, quantity=0))
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0

    def test_identifiers_are_normalised_to_upper_case(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn, qr_id="fg-0007"))
        assert client.get("/api/items/FG-0007").status_code == 200

    def test_updating_a_missing_item_is_a_404(self, admin_client, conn) -> None:
        client, token = admin_client
        response = post_json(client, token, "/api/items/FG-9999", new_item(conn), method="put")
        assert response.status_code == 404

    def test_items_can_be_filtered_by_zone(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        assert len(client.get(f"/api/items?zone={zone_id(conn)}").get_json()["data"]) == 1
        assert client.get("/api/items?zone=999").get_json()["data"] == []


class TestDeletePermission:
    def test_staff_may_delete_their_own_item(self, staff_client, conn) -> None:
        client, token = staff_client
        post_json(client, token, "/api/items", new_item(conn))
        assert (
            client.delete("/api/items/FG-0001", headers={"X-CSRF-Token": token}).status_code == 200
        )

    def test_staff_may_not_delete_someone_elses_item(self, app: Flask, admin_client, conn) -> None:
        admin, admin_token = admin_client
        post_json(admin, admin_token, "/api/items", new_item(conn))

        cook = app.test_client()
        cook_token = sign_in(cook, "cook", STAFF_PASSWORD)
        refused = cook.delete("/api/items/FG-0001", headers={"X-CSRF-Token": cook_token})
        assert refused.status_code == 403
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1

    def test_an_admin_may_delete_anyones_item(self, app: Flask, staff_client, conn) -> None:
        cook, cook_token = staff_client
        post_json(cook, cook_token, "/api/items", new_item(conn))

        admin = app.test_client()
        admin_token = sign_in(admin, "admin", ADMIN_PASSWORD)
        assert (
            admin.delete("/api/items/FG-0001", headers={"X-CSRF-Token": admin_token}).status_code
            == 200
        )


def add_reading(conn: sqlite3.Connection, zid: int, **overrides: object) -> None:
    row = {
        "zone_id": zid,
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "temp_c": 4.0,
        "humidity": 55.0,
        "gas_raw": 1200,
        "door_open": 0,
        "temp_ok": 1,
        "hum_ok": 1,
        "gas_ok": 1,
    }
    row.update(overrides)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO readings (zone_id, ts, temp_c, humidity, gas_raw, door_open, "
            "temp_ok, hum_ok, gas_ok) VALUES (:zone_id, :ts, :temp_c, :humidity, :gas_raw, "
            ":door_open, :temp_ok, :hum_ok, :gas_ok)",
            row,
        )


class TestStatus:
    def test_a_healthy_item_reads_fresh(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        for _ in range(4):
            add_reading(conn, zone_id(conn))

        data = client.get("/api/status").get_json()["data"]
        assert data["overall"] == "Fresh/Normal"
        assert data["items"][0]["status"] == "Fresh/Normal"

    def test_a_confirmed_high_temperature_reads_check_food(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        for _ in range(4):
            add_reading(conn, zone_id(conn), temp_c=14.0)

        data = client.get("/api/status").get_json()["data"]
        assert data["items"][0]["status"] == "Check Food"
        assert data["overall"] == "Check Food"

    def test_a_single_bad_reading_does_not_change_the_status(self, admin_client, conn) -> None:
        """The three-consecutive rule applies to the dashboard as well."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        for _ in range(4):
            add_reading(conn, zone_id(conn))
        add_reading(conn, zone_id(conn), temp_c=14.0)

        assert client.get("/api/status").get_json()["data"]["overall"] == "Fresh/Normal"

    def test_a_faulty_sensor_is_never_fresh(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        for _ in range(4):
            add_reading(conn, zone_id(conn), temp_c=None, temp_ok=0)

        data = client.get("/api/status").get_json()["data"]
        assert data["items"][0]["status"] == "Sensor Fault/Data Unavailable"
        assert data["overall"] != "Fresh/Normal"

    def test_a_zone_with_no_readings_is_not_reported_as_fresh(self, admin_client, conn) -> None:
        """No data at all is not the same as everything being fine."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        data = client.get("/api/status").get_json()["data"]
        assert data["overall"] != "Fresh/Normal"

    def test_each_category_is_judged_against_its_own_limits(self, admin_client, conn) -> None:
        """8 C is too warm for dairy and fine for root vegetables."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        post_json(
            client,
            token,
            "/api/items",
            new_item(conn, qr_id="FG-0002", name="Carrots", category="rootveg"),
        )
        for _ in range(4):
            add_reading(conn, zone_id(conn), temp_c=8.0)

        by_id = {
            i["qr_id"]: i["status"] for i in client.get("/api/status").get_json()["data"]["items"]
        }
        assert by_id["FG-0001"] == "Check Food"
        assert by_id["FG-0002"] == "Fresh/Normal"

    def test_the_response_carries_the_safety_disclaimer(self, admin_client, conn) -> None:
        client, _token = admin_client
        assert "does not certify" in client.get("/api/status").get_json()["data"]["disclaimer"]

    def test_the_use_first_list_puts_the_worst_first(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn, expiry_date="2099-01-01"))
        post_json(
            client,
            token,
            "/api/items",
            new_item(conn, qr_id="FG-0002", name="Old milk", expiry_date="2026-09-21"),
        )
        for _ in range(4):
            add_reading(conn, zone_id(conn))

        first = client.get("/api/status").get_json()["data"]["use_first"][0]
        assert first["qr_id"] == "FG-0002"


class TestReadings:
    def test_latest_readings_are_reported_per_zone(self, admin_client, conn) -> None:
        client, _token = admin_client
        add_reading(conn, zone_id(conn), temp_c=6.5)
        data = client.get("/api/readings/latest").get_json()["data"]
        assert data[0]["latest"]["temp_c"] == 6.5

    def test_readings_can_be_limited(self, admin_client, conn) -> None:
        client, _token = admin_client
        for _ in range(5):
            add_reading(conn, zone_id(conn))
        assert len(client.get("/api/readings?limit=2").get_json()["data"]) == 2

    def test_a_bad_limit_is_refused(self, admin_client) -> None:
        client, _token = admin_client
        assert client.get("/api/readings?limit=nope").status_code == 400

    def test_readings_are_returned_oldest_first_for_charting(self, admin_client, conn) -> None:
        client, _token = admin_client
        base = datetime.now(UTC)
        for offset in range(3):
            add_reading(
                conn,
                zone_id(conn),
                ts=(base + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                temp_c=float(offset),
            )
        data = client.get("/api/readings").get_json()["data"]
        assert [row["temp_c"] for row in data] == [0.0, 1.0, 2.0]


class TestAlertsApi:
    def _alert(self, conn: sqlite3.Connection, **overrides: object) -> int:
        from core.alerts import Alert, persist

        alert = Alert(
            zone_id=zone_id(conn),
            zone_name="Main shelf",
            type="temp_high",
            severity="critical",
            message="Too warm",
            **overrides,  # type: ignore[arg-type]
        )
        return persist(conn, alert, "pi-test")

    def test_alerts_are_listed_newest_first(self, admin_client, conn) -> None:
        client, _token = admin_client
        self._alert(conn)
        data = client.get("/api/alerts").get_json()["data"]
        assert data[0]["type"] == "temp_high"
        assert data[0]["zone_name"] == "Main shelf"

    def test_an_alert_can_be_acknowledged(self, admin_client, conn) -> None:
        client, token = admin_client
        alert_id = self._alert(conn)
        response = client.post(f"/api/alerts/{alert_id}/ack", headers={"X-CSRF-Token": token})
        assert response.status_code == 200
        assert conn.execute("SELECT acknowledged FROM alerts").fetchone()[0] == 1

    def test_acknowledging_a_missing_alert_is_a_404(self, admin_client) -> None:
        client, token = admin_client
        assert (
            client.post("/api/alerts/999/ack", headers={"X-CSRF-Token": token}).status_code == 404
        )

    def test_open_alerts_can_be_filtered(self, admin_client, conn) -> None:
        client, token = admin_client
        alert_id = self._alert(conn)
        client.post(f"/api/alerts/{alert_id}/ack", headers={"X-CSRF-Token": token})
        assert client.get("/api/alerts?open=1").get_json()["data"] == []


class TestHealth:
    def test_it_reports_the_sync_backlog(self, admin_client, conn) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        data = client.get("/api/health").get_json()["data"]
        assert data["sync_pending"] == 1

    def test_a_fresh_reading_means_the_monitor_is_running(self, admin_client, conn) -> None:
        client, _token = admin_client
        add_reading(conn, zone_id(conn))
        data = client.get("/api/health").get_json()["data"]
        assert data["monitor_running"] is True
        assert data["last_reading_age_s"] < 5

    def test_no_readings_means_the_monitor_is_not_running(self, admin_client) -> None:
        client, _token = admin_client
        data = client.get("/api/health").get_json()["data"]
        assert data["monitor_running"] is False
        assert data["last_reading_age_s"] is None

    def test_an_old_reading_means_the_monitor_has_stopped(self, admin_client, conn) -> None:
        client, _token = admin_client
        stale = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        add_reading(conn, zone_id(conn), ts=stale)
        assert client.get("/api/health").get_json()["data"]["monitor_running"] is False


class TestAdminApi:
    def test_thresholds_are_listed(self, admin_client) -> None:
        client, _token = admin_client
        data = client.get("/api/thresholds").get_json()["data"]
        assert {row["category"] for row in data} >= {"dairy", "rootveg"}

    def test_a_threshold_can_be_saved(self, admin_client, conn) -> None:
        client, token = admin_client
        response = post_json(
            client,
            token,
            "/api/thresholds/dairy",
            {
                "temp_min": 1,
                "temp_max": 4,
                "hum_min": 30,
                "hum_max": 70,
                "storage_limit_days": 5,
                "use_soon_days": 1,
                "source_note": "FSA guidance",
            },
            method="put",
        )
        assert response.status_code == 200
        assert (
            conn.execute("SELECT temp_max FROM thresholds WHERE category = 'dairy'").fetchone()[0]
            == 4
        )

    def test_a_threshold_without_a_source_note_is_refused(self, admin_client) -> None:
        """SRS rule 4, enforced at the boundary as well as in the schema."""
        client, token = admin_client
        response = post_json(
            client,
            token,
            "/api/thresholds/dairy",
            {
                "temp_min": 1,
                "temp_max": 4,
                "hum_min": 30,
                "hum_max": 70,
                "storage_limit_days": 5,
                "use_soon_days": 1,
                "source_note": "   ",
            },
            method="put",
        )
        assert response.status_code == 422
        assert "source_note" in response.get_json()["error"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("temp_min", 9), ("hum_max", 200), ("storage_limit_days", 0), ("use_soon_days", 99)],
    )
    def test_nonsensical_limits_are_refused(self, admin_client, field: str, value: float) -> None:
        client, token = admin_client
        body = {
            "temp_min": 1,
            "temp_max": 4,
            "hum_min": 30,
            "hum_max": 70,
            "storage_limit_days": 5,
            "use_soon_days": 1,
            "source_note": "note",
        }
        body[field] = value
        assert post_json(client, token, "/api/thresholds/x", body, method="put").status_code == 422

    def test_the_door_timeout_can_be_changed(self, admin_client) -> None:
        client, token = admin_client
        post_json(client, token, "/api/settings", {"door_open_timeout_s": 45}, method="put")
        assert client.get("/api/settings").get_json()["data"]["door_open_timeout_s"] == 45

    def test_a_negative_door_timeout_is_refused(self, admin_client) -> None:
        client, token = admin_client
        assert (
            post_json(
                client, token, "/api/settings", {"door_open_timeout_s": -1}, method="put"
            ).status_code
            == 422
        )

    def test_users_are_listed_without_password_material(self, admin_client) -> None:
        client, _token = admin_client
        data = client.get("/api/users").get_json()["data"]
        assert {row["username"] for row in data} == {"admin", "cook"}
        assert all("password" not in key for row in data for key in row)

    def test_a_user_can_be_added(self, admin_client) -> None:
        client, token = admin_client
        response = post_json(
            client,
            token,
            "/api/users",
            {"username": "newcook", "password": "a-long-enough-password", "role": "staff"},
        )
        assert response.status_code == 201
        assert response.get_json()["data"]["role"] == "staff"

    def test_a_user_without_a_password_is_refused(self, admin_client) -> None:
        client, token = admin_client
        response = post_json(
            client, token, "/api/users", {"username": "ghost", "password": "", "role": "staff"}
        )
        assert response.status_code == 422

    def test_the_last_administrator_cannot_be_deleted(self, admin_client, conn) -> None:
        client, token = admin_client
        cook_id = conn.execute("SELECT id FROM users WHERE username = 'cook'").fetchone()[0]
        client.delete(f"/api/users/{cook_id}", headers={"X-CSRF-Token": token})

        admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
        response = client.delete(f"/api/users/{admin_id}", headers={"X-CSRF-Token": token})
        assert response.status_code == 422
        assert "last administrator" in response.get_json()["error"]

    def test_you_cannot_delete_your_own_account(self, admin_client, conn) -> None:
        client, token = admin_client
        admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
        response = client.delete(f"/api/users/{admin_id}", headers={"X-CSRF-Token": token})
        assert response.status_code == 422

    def test_categories_are_listed_for_the_item_form(self, admin_client) -> None:
        client, _token = admin_client
        assert "dairy" in client.get("/api/categories").get_json()["data"]


class TestConsumption:
    """Recording an outcome removes the item and keeps its history."""

    def test_consuming_removes_the_item(
        self, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        response = post_json(client, token, "/api/items/FG-0001/consume", {"outcome": "consumed"})

        assert response.status_code == 200
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0

    def test_the_history_keeps_its_own_copy(
        self, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """The item row is gone, so the log has to stand on its own."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        post_json(client, token, "/api/items/FG-0001/consume", {"outcome": "consumed"})

        row = conn.execute("SELECT * FROM consumption_log").fetchone()

        assert row["item_name"] == "Milk"
        assert row["category"] == "dairy"
        assert row["qr_id"] == "FG-0001"
        assert row["zone_name"]
        assert row["outcome"] == "consumed"

    def test_the_verdict_at_the_time_is_frozen(
        self, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """The readings behind it roll off; the history must not go blank."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        post_json(client, token, "/api/items/FG-0001/consume", {"outcome": "consumed"})

        row = conn.execute("SELECT status_at_consumption FROM consumption_log").fetchone()

        assert row["status_at_consumption"]
        assert row["status_at_consumption"] != "Not assessed"

    def test_the_identifier_comes_back_into_circulation(
        self, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """A kitchen reuses its printed stickers."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        post_json(client, token, "/api/items/FG-0001/consume", {"outcome": "consumed"})

        again = post_json(client, token, "/api/items", new_item(conn, name="Yoghurt"))

        assert again.status_code == 201
        assert item_store.next_free_identifier(conn) != "FG-0001"

    def test_a_disposal_is_recorded_the_same_way(
        self, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        post_json(client, token, "/api/items/FG-0001/consume", {"outcome": "discarded"})

        counts = item_store.outcome_counts(conn)
        assert counts == {"consumed": 0, "discarded": 1}

    def test_both_rows_are_written_or_neither(
        self, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """The log row, the deletion and the outbox row are one transaction."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))
        before = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

        post_json(client, token, "/api/items/FG-0001/consume", {"outcome": "consumed"})

        assert conn.execute("SELECT COUNT(*) FROM consumption_log").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == before + 1

    def test_an_unknown_outcome_is_refused(
        self, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        response = post_json(client, token, "/api/items/FG-0001/consume", {"outcome": "eaten"})

        assert response.status_code == 422
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1

    def test_an_unknown_item_is_a_404(self, admin_client: tuple[FlaskClient, str]) -> None:
        client, token = admin_client
        response = post_json(client, token, "/api/items/FG-9999/consume", {"outcome": "consumed"})
        assert response.status_code == 404

    def test_it_needs_the_csrf_token(
        self, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        response = client.post("/api/items/FG-0001/consume", json={"outcome": "consumed"})

        assert response.status_code == 400
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


class TestSimulatorControl:
    """Switching the simulated scenario while the monitor is running."""

    @pytest.fixture(autouse=True)
    def _private_control_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The route reads the process settings, not the test app's, so without
        # this it writes the real control file beside the real database, where
        # a running monitor.py would obey it.
        monkeypatch.setenv("FG_SIM_CONTROL_FILE", str(tmp_path / "sim_scenario"))

    def test_an_administrator_can_choose_a_scenario(
        self, admin_client: tuple[FlaskClient, str], app: Flask
    ) -> None:
        client, token = admin_client

        response = post_json(client, token, "/api/simulator", {"scenario": "high_temp"})

        assert response.status_code == 200
        control = get_settings().sim_control_file
        assert control is not None
        assert control.read_text(encoding="utf-8") == "high_temp"

    def test_the_chosen_scenario_is_reported_back(
        self, admin_client: tuple[FlaskClient, str]
    ) -> None:
        client, token = admin_client
        post_json(client, token, "/api/simulator", {"scenario": "gas_spike"})

        body = client.get("/api/simulator").get_json()["data"]

        assert body["scenario"] == "gas_spike"
        assert "sensor_disconnected" in body["scenarios"]

    def test_an_unknown_scenario_is_refused(self, admin_client: tuple[FlaskClient, str]) -> None:
        client, token = admin_client
        response = post_json(client, token, "/api/simulator", {"scenario": "catastrophe"})
        assert response.status_code == 422

    def test_staff_cannot_change_the_scenario(self, staff_client: tuple[FlaskClient, str]) -> None:
        client, token = staff_client
        response = post_json(client, token, "/api/simulator", {"scenario": "high_temp"})
        assert response.status_code == 403

    def test_it_is_refused_on_real_hardware(
        self, admin_client: tuple[FlaskClient, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is nothing to simulate, and silently doing nothing would be worse."""
        import core.config

        client, token = admin_client
        real = replace(get_settings(), sensor_mode="real")
        monkeypatch.setattr(core.config, "get_settings", lambda: real)
        monkeypatch.setattr("web.routes.api_admin.get_settings", lambda: real)

        response = post_json(client, token, "/api/simulator", {"scenario": "high_temp"})

        assert response.status_code == 409


class TestKioskBoundary:
    """The scanner is open. Everything behind it is not.

    These tests exist because an unauthenticated endpoint that deletes data is
    a deliberate decision, and a deliberate decision needs a fence that fails
    loudly if someone widens it later.
    """

    def test_an_anonymous_visitor_can_open_the_scanner(self, app: Flask) -> None:
        assert app.test_client().get("/").status_code == 200

    def test_an_anonymous_visitor_can_look_one_item_up(
        self, app: Flask, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        response = app.test_client().get("/api/items/FG-0001")

        assert response.status_code == 200
        assert response.get_json()["data"]["name"] == "Milk"

    def test_an_anonymous_visitor_can_record_consumption(
        self, app: Flask, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """Someone who just took the last of the milk can say so."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        kiosk = app.test_client()
        page = kiosk.get("/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        assert csrf, "the kiosk must carry a token or no write from it can succeed"

        response = kiosk.post(
            "/api/items/FG-0001/consume",
            json={"outcome": "consumed"},
            headers={"X-CSRF-Token": csrf.group(1)},
        )

        assert response.status_code == 200
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0

    def test_an_anonymous_record_names_no_author(
        self, app: Flask, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """Better an honest blank than a fabricated one."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        kiosk = app.test_client()
        page = kiosk.get("/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        kiosk.post(
            "/api/items/FG-0001/consume",
            json={"outcome": "consumed"},
            headers={"X-CSRF-Token": csrf.group(1)},
        )

        assert conn.execute("SELECT created_by FROM consumption_log").fetchone()[0] is None

    def test_a_kiosk_write_still_needs_the_csrf_token(
        self, app: Flask, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """Open does not mean another site may fire it at an open browser."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        response = app.test_client().post(
            "/api/items/FG-0001/consume", json={"outcome": "consumed"}
        )

        assert response.status_code == 400
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1

    @pytest.mark.parametrize(
        "path",
        [
            "/dashboard",
            "/inventory",
            "/sensors",
            "/alerts",
            "/planner",
            "/reports",
            "/admin/",
        ],
    )
    def test_everything_else_still_needs_a_sign_in(self, app: Flask, path: str) -> None:
        response = app.test_client().get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    @pytest.mark.parametrize(
        "path",
        ["/api/items", "/api/status", "/api/readings", "/api/alerts", "/api/thresholds"],
    )
    def test_the_kitchen_cannot_be_read_as_a_whole(self, app: Flask, path: str) -> None:
        """One identifier you already hold is not the same as the inventory."""
        assert app.test_client().get(path).status_code in (302, 401, 403)

    def test_an_anonymous_visitor_can_reach_the_wizard(self, app: Flask) -> None:
        """A code the system has never seen is the moment someone adds it."""
        assert app.test_client().get("/item/new").status_code == 200

    def test_an_anonymous_visitor_can_store_an_item(
        self, app: Flask, conn: sqlite3.Connection
    ) -> None:
        kiosk = app.test_client()
        page = kiosk.get("/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        assert csrf

        response = kiosk.post(
            "/api/items", json=new_item(conn), headers={"X-CSRF-Token": csrf.group(1)}
        )

        assert response.status_code == 201
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1

    def test_an_anonymous_item_names_no_author(self, app: Flask, conn: sqlite3.Connection) -> None:
        kiosk = app.test_client()
        page = kiosk.get("/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        kiosk.post("/api/items", json=new_item(conn), headers={"X-CSRF-Token": csrf.group(1)})

        assert conn.execute("SELECT created_by FROM items").fetchone()[0] is None

    def test_an_anonymous_creation_still_needs_the_csrf_token(
        self, app: Flask, conn: sqlite3.Connection
    ) -> None:
        response = app.test_client().post("/api/items", json=new_item(conn))

        assert response.status_code == 400
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0

    def test_an_anonymous_creation_is_still_validated(
        self, app: Flask, conn: sqlite3.Connection
    ) -> None:
        """Opening the door does not lower the bar for what comes through it."""
        kiosk = app.test_client()
        page = kiosk.get("/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))

        response = kiosk.post(
            "/api/items", json={"name": "Nothing else"}, headers={"X-CSRF-Token": csrf.group(1)}
        )

        assert response.status_code == 422
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0

    def test_an_anonymous_visitor_still_cannot_delete_an_item(
        self, app: Flask, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """Consuming writes a record first. Deleting leaves nothing behind."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        kiosk = app.test_client()
        page = kiosk.get("/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        response = kiosk.delete("/api/items/FG-0001", headers={"X-CSRF-Token": csrf.group(1)})

        assert response.status_code in (302, 401, 403)
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1

    def test_a_scanned_identifier_renders_its_record_without_javascript(
        self, app: Flask, admin_client: tuple[FlaskClient, str], conn: sqlite3.Connection
    ) -> None:
        """The dialog is server-rendered, so no camera and no JS still works."""
        client, token = admin_client
        post_json(client, token, "/api/items", new_item(conn))

        page = app.test_client().get("/?qr_id=FG-0001").get_data(as_text=True)

        assert "Milk" in page
        assert 'data-outcome="consumed"' in page

    def test_an_unknown_identifier_offers_the_wizard(self, app: Flask) -> None:
        page = app.test_client().get("/?qr_id=FG-0404").get_data(as_text=True)

        assert "Not registered yet" in page
        assert "/item/new?qr_id=FG-0404" in page
