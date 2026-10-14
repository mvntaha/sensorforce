"""The command the server publishes for the Pi's lamps."""

from __future__ import annotations

from datetime import date

import pytest

from core.engine import Status
from core.pi_commands import COMMANDS_PATH, LED_FOR_STATUS, build_command, publish

TODAY = date(2026, 9, 29)


@pytest.fixture
def healthy_zone(conn, zone_id):
    """Recent, in-range readings, so the zone is not reported as faulty.

    Without these the engine reaches Sensor Fault before it ever looks at an
    expiry date, and every lamp would be red for the wrong reason.
    """
    for minute in range(5):
        conn.execute(
            "INSERT INTO readings (zone_id, ts, temp_c, humidity, gas_raw, door_open, "
            "temp_ok, hum_ok, gas_ok) VALUES (?, ?, ?, ?, ?, 0, 1, 1, 1)",
            (zone_id, f"2026-09-29T09:{minute:02d}:00Z", 4.0, 50.0, 1200),
        )
    conn.commit()
    return zone_id


@pytest.fixture
def stored_item(conn, zone_id, category, healthy_zone):
    """Insert one item and return a function to adjust its dates."""

    def _store(expiry: str | None = None, storage: str = "2026-09-27"):
        conn.execute(
            "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date, "
            "expiry_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("FG-0001", "Yoghurt", category, 1, zone_id, storage, expiry),
        )
        conn.commit()

    return _store


class TestLampMapping:
    def test_every_status_has_a_lamp(self):
        assert set(LED_FOR_STATUS) == set(Status)

    def test_a_fault_is_never_green(self):
        """SRS rule 1: a faulty sensor must not produce a healthy signal."""
        assert LED_FOR_STATUS[Status.FAULT] != "green"

    def test_use_soon_is_yellow(self):
        assert LED_FOR_STATUS[Status.USE_SOON] == "yellow"


class TestBuildCommand:
    def test_an_empty_kitchen_is_green_and_silent(self, conn):
        command = build_command(conn, today=TODAY)

        assert command["led"] == "green"
        assert command["status"] == str(Status.FRESH)
        assert command["buzzer"] is False
        assert command["use_soon_count"] == 0
        assert command["attention"] == []

    def test_an_item_near_its_expiry_turns_the_lamp_yellow(self, conn, stored_item):
        stored_item(expiry="2026-09-30")  # tomorrow

        command = build_command(conn, today=TODAY)

        assert command["led"] == "yellow"
        assert command["status"] == str(Status.USE_SOON)
        assert command["use_soon_count"] == 1
        # Silent on purpose: a lamp reminds, a buzzer alarms.
        assert command["buzzer"] is False

    def test_an_expired_item_turns_the_lamp_red_and_sounds(self, conn, stored_item):
        stored_item(expiry="2026-09-20", storage="2026-09-10")  # well past

        command = build_command(conn, today=TODAY)

        assert command["led"] == "red"
        assert command["status"] == str(Status.CHECK)
        assert command["check_count"] == 1
        assert command["buzzer"] is True

    def test_the_named_items_carry_what_a_small_screen_needs(self, conn, stored_item):
        stored_item(expiry="2026-09-30")

        command = build_command(conn, today=TODAY)

        assert len(command["attention"]) == 1
        named = command["attention"][0]
        assert named["name"] == "Yoghurt"
        assert named["qr_id"] == "FG-0001"
        assert named["status"] == str(Status.USE_SOON)
        assert "days_stored" in named

    def test_the_command_says_which_device_and_when(self, conn):
        command = build_command(conn, device_id="pi-07", today=TODAY)

        assert command["device_id"] == "pi-07"
        assert command["ts"].endswith("Z")
        assert command["source"] == "freshguard-server"

    def test_the_worst_item_decides_the_lamp(self, conn, zone_id, category, healthy_zone):
        """One expired item outranks any number of merely-soon ones."""
        conn.execute(
            "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date, "
            "expiry_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("FG-0001", "Yoghurt", category, 1, zone_id, "2026-09-27", "2026-09-30"),
        )
        conn.execute(
            "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date, "
            "expiry_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("FG-0002", "Milk", category, 1, zone_id, "2026-09-10", "2026-09-20"),
        )
        conn.commit()

        command = build_command(conn, today=TODAY)

        assert command["led"] == "red"
        assert command["use_soon_count"] == 1
        assert command["check_count"] == 1


class TestPublish:
    def test_it_writes_to_the_device_node(self, conn):
        written = {}

        class FakeRef:
            def __init__(self, path=""):
                self.path = path

            def child(self, path):
                return FakeRef(path)

            def set(self, value):
                written[self.path] = value

        command = build_command(conn, device_id="pi-01", today=TODAY)
        publish(FakeRef(), command)

        assert f"{COMMANDS_PATH}/pi-01" in written
        assert written[f"{COMMANDS_PATH}/pi-01"]["led"] == "green"

    def test_a_cloud_failure_is_reported_clearly(self, conn):
        class Broken:
            def child(self, _path):
                raise RuntimeError("network down")

        command = build_command(conn, today=TODAY)
        with pytest.raises(RuntimeError, match="could not publish"):
            publish(Broken(), command)
