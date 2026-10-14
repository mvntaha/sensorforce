"""One test class per table in the frozen schema.

Each class checks that the table accepts a valid row and that the constraints
which encode an SRS rule actually reject the rows they are meant to reject. A
constraint nobody tests is a comment, not a guarantee.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.test_db import latest_migration_version

EXPECTED_TABLES = {
    "alerts",
    "consumption_log",
    "items",
    "outbox",
    "readings",
    "rfid_cursor",
    "rfid_events",
    "rfid_pending",
    "schema_version",
    "settings",
    "snapshot_requests",
    "snapshots",
    "thresholds",
    "users",
    "zones",
}


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows if not row["name"].startswith("sqlite_")}


def test_schema_creates_every_table(conn: sqlite3.Connection) -> None:
    assert table_names(conn) == EXPECTED_TABLES


class TestSchemaVersion:
    def test_records_the_base_schema_and_every_migration(self, conn: sqlite3.Connection) -> None:
        versions = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
        # Version 1 is schema.sql itself; each migration file adds its own row.
        assert 1 in versions
        assert max(versions) == latest_migration_version()

    def test_version_is_unique(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")


class TestUsers:
    def test_accepts_a_hashed_user(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("admin", "scrypt:32768:8:1$fake$hash", "admin"),
        )
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1

    def test_rejects_an_unknown_role(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                ("mallory", "hash", "superuser"),
            )

    def test_usernames_are_unique_ignoring_case(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("Admin", "hash", "admin"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                ("admin", "hash", "staff"),
            )

    def test_rejects_an_empty_password_hash(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                ("bob", "", "staff"),
            )


class TestZones:
    def test_accepts_a_zone_without_gas_limits(self, conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO zones (name) VALUES (?)", ("Crisper",))
        assert conn.execute("SELECT gas_abnormal FROM zones").fetchone()[0] is None

    def test_gas_limits_live_on_the_zone(self, conn: sqlite3.Connection, zone_id: int) -> None:
        row = conn.execute(
            "SELECT gas_baseline, gas_abnormal FROM zones WHERE id = ?", (zone_id,)
        ).fetchone()
        assert (row["gas_baseline"], row["gas_abnormal"]) == (1200, 2500)

    def test_rejects_a_gas_limit_without_a_source_note(self, conn: sqlite3.Connection) -> None:
        """SRS rule 4: a threshold must cite a source or be labelled an assumption."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO zones (name, gas_baseline, gas_abnormal) VALUES (?, ?, ?)",
                ("Undocumented", 100, 900),
            )

    def test_rejects_an_abnormal_limit_below_the_baseline(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO zones (name, gas_baseline, gas_abnormal, gas_source_note) "
                "VALUES (?, ?, ?, ?)",
                ("Backwards", 900, 100, "note"),
            )


class TestThresholds:
    def test_accepts_a_documented_category(self, conn: sqlite3.Connection, category: str) -> None:
        row = conn.execute(
            "SELECT source_note FROM thresholds WHERE category = ?", (category,)
        ).fetchone()
        assert row["source_note"] == "prototype/demo assumption"

    def test_holds_no_gas_columns(self, conn: sqlite3.Connection) -> None:
        """Gas is zone-level (SRS rule 3), so it must not be per food category."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(thresholds)")}
        assert not any("gas" in column for column in columns)

    def test_rejects_an_undocumented_threshold(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO thresholds (category, temp_min, temp_max, hum_min, hum_max, "
                "storage_limit_days, use_soon_days, source_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("mystery", 0.0, 5.0, 30.0, 70.0, 7, 2, "   "),
            )

    def test_rejects_an_inverted_temperature_range(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO thresholds (category, temp_min, temp_max, hum_min, hum_max, "
                "storage_limit_days, use_soon_days, source_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("inverted", 9.0, 2.0, 30.0, 70.0, 7, 2, "note"),
            )

    def test_rejects_use_soon_longer_than_the_storage_limit(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO thresholds (category, temp_min, temp_max, hum_min, hum_max, "
                "storage_limit_days, use_soon_days, source_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("silly", 0.0, 5.0, 30.0, 70.0, 3, 9, "note"),
            )


class TestSettings:
    def test_stores_the_door_open_timeout(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)", ("door_open_timeout_s", "60")
        )
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("door_open_timeout_s",)
        ).fetchone()
        assert row["value"] == "60"

    def test_keys_are_unique(self, conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("k", "1"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("k", "2"))


class TestItems:
    def test_accepts_a_valid_item(self, conn: sqlite3.Connection, item_id: int) -> None:
        row = conn.execute("SELECT qr_id, unit FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["qr_id"] == "FG-0001"
        assert row["unit"] == "L"

    def test_unit_defaults_to_pieces(
        self, conn: sqlite3.Connection, zone_id: int, category: str
    ) -> None:
        conn.execute(
            "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("FG-0002", "Eggs", category, 6, zone_id, "2026-09-21"),
        )
        row = conn.execute("SELECT unit FROM items WHERE qr_id = ?", ("FG-0002",)).fetchone()
        assert row["unit"] == "pcs"

    @pytest.mark.parametrize("qr_id", ["FG-1", "FG-00001", "fg-0001", "XX-0001", "FG-00A1"])
    def test_rejects_a_malformed_qr_id(
        self, conn: sqlite3.Connection, zone_id: int, category: str, qr_id: str
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (qr_id, "Milk", category, 1, zone_id, "2026-09-20"),
            )

    def test_rejects_a_duplicate_qr_id(
        self, conn: sqlite3.Connection, item_id: int, zone_id: int, category: str
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("FG-0001", "Other milk", category, 1, zone_id, "2026-09-20"),
            )

    def test_rejects_a_non_positive_quantity(
        self, conn: sqlite3.Connection, zone_id: int, category: str
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("FG-0003", "Ghost", category, 0, zone_id, "2026-09-20"),
            )

    def test_rejects_expiry_before_manufacture(
        self, conn: sqlite3.Connection, zone_id: int, category: str
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date, "
                "manufacture_date, expiry_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("FG-0004", "Milk", category, 1, zone_id, "2026-09-20", "2026-09-10", "2026-09-05"),
            )

    def test_rejects_an_impossible_calendar_date(
        self, conn: sqlite3.Connection, zone_id: int, category: str
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("FG-0005", "Milk", category, 1, zone_id, "2026-02-31"),
            )

    def test_rejects_an_unknown_category(self, conn: sqlite3.Connection, zone_id: int) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("FG-0006", "Milk", "unicorn", 1, zone_id, "2026-09-20"),
            )

    def test_category_in_use_cannot_be_deleted(
        self, conn: sqlite3.Connection, item_id: int, category: str
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM thresholds WHERE category = ?", (category,))


class TestReadings:
    def _insert(self, conn: sqlite3.Connection, zone_id: int, **overrides: object) -> None:
        row = {
            "zone_id": zone_id,
            "ts": "2026-09-24T10:00:00Z",
            "temp_c": 4.0,
            "humidity": 55.0,
            "gas_raw": 1200,
            "door_open": 0,
            "temp_ok": 1,
            "hum_ok": 1,
            "gas_ok": 1,
        }
        row.update(overrides)
        conn.execute(
            "INSERT INTO readings (zone_id, ts, temp_c, humidity, gas_raw, door_open, "
            "temp_ok, hum_ok, gas_ok) VALUES (:zone_id, :ts, :temp_c, :humidity, :gas_raw, "
            ":door_open, :temp_ok, :hum_ok, :gas_ok)",
            row,
        )

    def test_accepts_a_healthy_reading(self, conn: sqlite3.Connection, zone_id: int) -> None:
        self._insert(conn, zone_id)
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1

    def test_accepts_a_fault_row(self, conn: sqlite3.Connection, zone_id: int) -> None:
        """A disconnected sensor stores NULL alongside its ok flag set to 0."""
        self._insert(conn, zone_id, temp_c=None, temp_ok=0, humidity=None, hum_ok=0)
        row = conn.execute("SELECT temp_c, temp_ok FROM readings").fetchone()
        assert row["temp_c"] is None
        assert row["temp_ok"] == 0

    def test_rejects_a_healthy_flag_without_a_value(
        self, conn: sqlite3.Connection, zone_id: int
    ) -> None:
        """The rule that stops a faulty sensor being reported as Fresh."""
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, zone_id, temp_c=None, temp_ok=1)

    def test_rejects_a_fault_flag_carrying_a_value(
        self, conn: sqlite3.Connection, zone_id: int
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, zone_id, temp_c=4.0, temp_ok=0)

    @pytest.mark.parametrize(
        ("column", "value"),
        [("temp_c", 999.0), ("temp_c", -80.0), ("humidity", 150.0), ("gas_raw", 99999)],
    )
    def test_rejects_physically_impossible_values(
        self, conn: sqlite3.Connection, zone_id: int, column: str, value: float
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, zone_id, **{column: value})

    def test_rejects_an_unparseable_timestamp(self, conn: sqlite3.Connection, zone_id: int) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, zone_id, ts="last tuesday")

    def test_readings_follow_their_zone(self, conn: sqlite3.Connection, zone_id: int) -> None:
        self._insert(conn, zone_id)
        conn.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


class TestAlerts:
    def _insert(self, conn: sqlite3.Connection, zone_id: int, **overrides: object) -> None:
        row = {
            "event_id": "11111111-1111-4111-8111-111111111111",
            "zone_id": zone_id,
            "item_id": None,
            "type": "temp_high",
            "severity": "critical",
            "message": "Zone temperature above the configured range",
            "ts": "2026-09-24T10:00:00Z",
        }
        row.update(overrides)
        conn.execute(
            "INSERT INTO alerts (event_id, zone_id, item_id, type, severity, message, ts) "
            "VALUES (:event_id, :zone_id, :item_id, :type, :severity, :message, :ts)",
            row,
        )

    def test_accepts_an_alert(self, conn: sqlite3.Connection, zone_id: int) -> None:
        self._insert(conn, zone_id)
        row = conn.execute("SELECT acknowledged, notify_pending FROM alerts").fetchone()
        assert row["acknowledged"] == 0
        assert row["notify_pending"] == 1

    def test_gas_alerts_are_zone_level_only(
        self, conn: sqlite3.Connection, zone_id: int, item_id: int
    ) -> None:
        """SRS rule 3: a gas reading is never attributed to a specific item."""
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, zone_id, type="gas_abnormal", item_id=item_id)

    def test_gas_alert_without_an_item_is_allowed(
        self, conn: sqlite3.Connection, zone_id: int
    ) -> None:
        self._insert(conn, zone_id, type="gas_abnormal", item_id=None)
        assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1

    def test_other_alert_types_may_name_an_item(
        self, conn: sqlite3.Connection, zone_id: int, item_id: int
    ) -> None:
        self._insert(conn, zone_id, type="expiry", item_id=item_id)
        assert conn.execute("SELECT item_id FROM alerts").fetchone()["item_id"] == item_id

    def test_event_id_is_unique(self, conn: sqlite3.Connection, zone_id: int) -> None:
        self._insert(conn, zone_id)
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, zone_id, type="door_open")

    def test_rejects_an_unknown_type(self, conn: sqlite3.Connection, zone_id: int) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, zone_id, type="spoiled")

    def test_rejects_an_empty_message(self, conn: sqlite3.Connection, zone_id: int) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, zone_id, message="  ")

    def test_deleting_an_item_keeps_its_alert_history(
        self, conn: sqlite3.Connection, zone_id: int, item_id: int
    ) -> None:
        self._insert(conn, zone_id, type="expiry", item_id=item_id)
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        row = conn.execute("SELECT item_id FROM alerts").fetchone()
        assert row["item_id"] is None


class TestOutbox:
    def _insert(self, conn: sqlite3.Connection, **overrides: object) -> None:
        row = {
            "event_id": "22222222-2222-4222-8222-222222222222",
            "entity": "alert",
            "payload_json": '{"op": "create"}',
            "sync_status": "pending",
            "synced_at": None,
        }
        row.update(overrides)
        conn.execute(
            "INSERT INTO outbox (event_id, entity, payload_json, sync_status, synced_at) "
            "VALUES (:event_id, :entity, :payload_json, :sync_status, :synced_at)",
            row,
        )

    def test_accepts_a_pending_row(self, conn: sqlite3.Connection) -> None:
        self._insert(conn)
        row = conn.execute("SELECT sync_status, attempts FROM outbox").fetchone()
        assert row["sync_status"] == "pending"
        assert row["attempts"] == 0

    def test_event_id_is_the_primary_key(self, conn: sqlite3.Connection) -> None:
        """One event id maps to one node in Firebase, so retries cannot duplicate."""
        self._insert(conn)
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, entity="item")

    def test_synced_rows_must_record_when(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, sync_status="synced", synced_at=None)

    def test_unsynced_rows_must_not_record_a_sync_time(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, sync_status="pending", synced_at="2026-09-24T10:00:00Z")

    def test_rejects_an_unknown_entity(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(conn, entity="banana")

    def test_rejects_a_negative_attempt_count(self, conn: sqlite3.Connection) -> None:
        self._insert(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE outbox SET attempts = -1")
