"""The bridge from the Pi's Firebase node into SQLite.

The payloads used here are taken from a real export of the device's node, so
the awkward shapes are the ones the hardware actually produces rather than ones
imagined for the test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.cloud_ingest import (
    DEVICE_GAS_ALERT_DELTA,
    CloudSensor,
    IngestError,
    device_gas_limit,
    fetch_payloads,
    ingest,
    ingest_live,
    save_snapshot,
    to_reading,
)
from core.notify import FakeNotifier
from core.outputs import fake_outputs
from monitor import Monitor

DEVICE = "pi-01"


def payload(**overrides):
    """A healthy device payload, before the test spoils part of it."""
    base = {
        "components": {"dht22": "OK", "lcd": "OK", "mq135": "OK"},
        "device_id": DEVICE,
        "gas_raw": 9722,
        "humidity": 46.5,
        "system_status": "HEALTHY",
        "temp_c": 24.5,
        "ts": "2026-09-29T05:21:50Z",
    }
    base.update(overrides)
    return base


class TestTranslation:
    def test_a_healthy_payload_keeps_every_value(self):
        reading = to_reading(payload())
        assert reading.temp_c == 24.5
        assert reading.humidity == 46.5
        assert reading.gas_raw == 9722
        assert reading.healthy

    def test_a_disconnected_gas_sensor_discards_its_stale_value(self):
        """The device keeps sending its last good number while unplugged.

        This is the one that matters: storing 5793 would show a steady, healthy
        gas level for a sensor that is not connected.
        """
        spoiled = payload(
            components={"dht22": "OK", "lcd": "OK", "mq135": "I2C_DISCONNECTED"},
            gas_raw=5793,
            system_status="NEEDS_ATTENTION",
        )
        reading = to_reading(spoiled)

        assert reading.gas_raw is None
        assert reading.gas_ok is False
        # The working sensors are untouched by the gas fault.
        assert reading.temp_c == 24.5
        assert reading.humidity == 46.5

    def test_a_missing_key_is_a_fault_not_a_zero(self):
        """Firebase drops null values, so a failed read arrives as no key."""
        without_temp = payload()
        del without_temp["temp_c"]
        reading = to_reading(without_temp)

        assert reading.temp_c is None
        assert reading.temp_ok is False
        assert reading.humidity == 46.5

    def test_the_earliest_firmware_without_components_is_trusted(self):
        """The first readings carried no components block and honest values."""
        early = {
            "device_id": DEVICE,
            "humidity": 45.8,
            "temp_c": 24.7,
            "ts": "2026-09-29T04:54:51Z",
        }
        reading = to_reading(early)

        assert reading.temp_c == 24.7
        assert reading.humidity == 45.8
        # No gas key at all, so the gas sensor is faulted rather than invented.
        assert reading.gas_raw is None
        assert reading.gas_ok is False

    def test_a_payload_with_only_gas_still_translates(self):
        sparse = {"device_id": DEVICE, "gas_raw": 5056, "ts": "2026-09-29T05:05:19Z"}
        reading = to_reading(sparse)

        assert reading.gas_raw == 5056
        assert reading.temp_ok is False
        assert reading.humidity_ok is False

    def test_an_implausible_value_is_refused(self):
        reading = to_reading(payload(temp_c=999))
        assert reading.temp_c is None
        assert reading.temp_ok is False

    def test_a_payload_without_a_timestamp_is_rejected(self):
        broken = payload()
        del broken["ts"]
        with pytest.raises(ValueError, match="timestamp"):
            to_reading(broken)

    def test_the_door_is_recorded_closed_because_there_is_no_reed_switch(self):
        assert to_reading(payload()).door_open is False


class TestIngest:
    def test_readings_reach_the_database(self, conn, zone_id):
        payloads = {
            "-P2fbAQFS": payload(ts="2026-09-29T05:21:50Z"),
            "-P2fbEJEZ": payload(ts="2026-09-29T05:22:06Z", humidity=46.6),
        }
        result = ingest(conn, payloads, zone_id)

        assert result.stored == 2
        rows = conn.execute("SELECT ts, temp_c, gas_raw FROM readings ORDER BY ts").fetchall()
        assert [r["ts"] for r in rows] == ["2026-09-29T05:21:50Z", "2026-09-29T05:22:06Z"]

    def test_running_twice_stores_nothing_the_second_time(self, conn, zone_id):
        payloads = {"-P2fbAQFS": payload()}

        first = ingest(conn, payloads, zone_id)
        second = ingest(conn, payloads, zone_id)

        assert first.stored == 1
        assert second.stored == 0
        assert second.skipped == 1
        total = conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        assert total == 1

    def test_a_faulted_reading_is_stored_as_a_fault(self, conn, zone_id):
        payloads = {
            "-P2fbEJEZ": payload(
                components={"dht22": "OK", "lcd": "OK", "mq135": "I2C_DISCONNECTED"},
                gas_raw=5793,
            )
        }
        ingest(conn, payloads, zone_id)

        row = conn.execute("SELECT gas_raw, gas_ok, temp_ok FROM readings").fetchone()
        assert row["gas_raw"] is None
        assert row["gas_ok"] == 0
        assert row["temp_ok"] == 1

    def test_an_unusable_payload_does_not_stop_the_others(self, conn, zone_id):
        payloads = {
            "-P2fbAQFS": payload(),
            "-P2fbEJEZ": {"device_id": DEVICE},  # no timestamp
        }
        result = ingest(conn, payloads, zone_id)

        assert result.stored == 1
        assert result.rejected == 1

    def test_nothing_to_do_is_not_an_error(self, conn, zone_id):
        assert ingest(conn, {}, zone_id).stored == 0

    def test_the_outbox_is_left_alone_unless_asked(self, conn, zone_id):
        ingest(conn, {"-P2fbAQFS": payload()}, zone_id)
        queued = conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"]
        assert queued == 0

    def test_mirroring_queues_the_reading_for_the_cloud(self, conn, zone_id):
        ingest(conn, {"-P2fbAQFS": payload()}, zone_id, queue_event=True)
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE entity = 'reading'"
        ).fetchone()["n"]
        assert queued == 1


class TestFetch:
    def test_a_cloud_failure_is_reported_not_raised_raw(self):
        class Broken:
            def child(self, _path):
                raise RuntimeError("network down")

        with pytest.raises(IngestError, match="could not read"):
            fetch_payloads(Broken(), DEVICE)

    def test_an_empty_node_returns_an_empty_mapping(self):
        class Empty:
            def child(self, _path):
                return self

            def order_by_key(self):
                return self

            def limit_to_last(self, _n):
                return self

            def get(self):
                return None

        assert fetch_payloads(Empty(), DEVICE) == {}

    def test_after_a_key_only_newer_readings_are_returned(self):
        class Node:
            def __init__(self):
                self.calls = []

            def child(self, _path):
                return self

            def order_by_key(self):
                return self

            def start_at(self, key):
                self.calls.append(("start_at", key))
                return self

            def limit_to_first(self, n):
                self.calls.append(("limit_to_first", n))
                return self

            def get(self):
                # start_at is inclusive, so the known key comes back too.
                return {"-A": payload(), "-B": payload(ts="2026-09-29T05:22:06Z")}

        node = Node()
        result = fetch_payloads(node, DEVICE, limit=10, after="-A")

        assert list(result) == ["-B"]
        assert node.calls == [("start_at", "-A"), ("limit_to_first", 11)]


NOW = datetime(2026, 9, 29, 5, 30, 0, tzinfo=UTC)


def recent(seconds_ago: int, **overrides):
    """A payload stamped ``seconds_ago`` before NOW."""
    ts = datetime.fromtimestamp(NOW.timestamp() - seconds_ago, UTC)
    return payload(ts=ts.strftime("%Y-%m-%dT%H:%M:%SZ"), **overrides)


@pytest.fixture
def live(conn, zone_id, category, item_id):
    """A monitor fed by a CloudSensor, with a dairy item in the zone (0-5 C)."""
    sensor = CloudSensor()
    monitor = Monitor(
        conn,
        sensor=sensor,
        outputs=fake_outputs(),
        notifier=FakeNotifier(),
        zone_id=zone_id,
        zone_name="Main shelf",
        device_id=DEVICE,
    )
    return monitor, sensor


class TestLive:
    def test_a_confirmed_breach_notifies_once(self, conn, live):
        monitor, sensor = live
        # 24.5 C breaches dairy's 5 C limit; 9722 breaches the zone's 2500 gas limit.
        payloads = {f"-K{i}": recent(60 - i * 5) for i in range(5)}

        result = ingest_live(monitor, sensor, payloads, now=NOW)

        assert result.stored == 5
        types = sorted(n.subject for n in monitor.notifier.sent)
        assert len(types) == 2
        stored = sorted(r["type"] for r in conn.execute("SELECT type FROM alerts"))
        assert stored == ["gas_abnormal", "temp_high"]

    def test_a_breach_carries_over_between_cycles(self, conn, live):
        """Confirmation needs three readings; they may arrive in separate polls."""
        monitor, sensor = live
        for i in range(3):
            ingest_live(monitor, sensor, {f"-K{i}": recent(30 - i * 5)}, now=NOW)

        stored = {r["type"] for r in conn.execute("SELECT type FROM alerts")}
        assert "temp_high" in stored

    def test_two_readings_are_not_enough_to_alert(self, conn, live):
        monitor, sensor = live
        ingest_live(monitor, sensor, {"-K0": recent(10), "-K1": recent(5)}, now=NOW)

        assert monitor.notifier.sent == []

    def test_old_readings_are_stored_but_never_notified(self, conn, live):
        monitor, sensor = live
        payloads = {f"-K{i}": recent(3600 - i * 5) for i in range(5)}

        result = ingest_live(monitor, sensor, payloads, now=NOW)

        assert result.stored == 5
        assert monitor.notifier.sent == []
        total = conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        assert total == 5

    def test_a_reading_already_stored_is_not_evaluated_again(self, conn, live):
        monitor, sensor = live
        payloads = {f"-K{i}": recent(30 - i * 5) for i in range(3)}
        ingest_live(monitor, sensor, payloads, now=NOW)
        sent = len(monitor.notifier.sent)

        again = ingest_live(monitor, sensor, payloads, now=NOW)

        assert again.stored == 0
        assert again.skipped == 3
        assert len(monitor.notifier.sent) == sent


class TestSnapshot:
    def test_a_frame_held_open_briefly_is_still_replaced(self, tmp_path, monkeypatch):
        """Windows refuses the move while the dashboard is serving the old frame."""
        target = tmp_path / "pi-01.jpg"
        target.write_bytes(b"old")
        real_replace = Path.replace
        refusals = iter([True, True])

        def flaky_replace(self, dest):
            if next(refusals, False):
                raise PermissionError("[WinError 5] Access is denied")
            return real_replace(self, dest)

        monkeypatch.setattr(Path, "replace", flaky_replace)
        monkeypatch.setattr("core.cloud_ingest.time.sleep", lambda _s: None)

        save_snapshot(b"new", target)

        assert target.read_bytes() == b"new"

    def test_a_frame_that_stays_locked_raises(self, tmp_path, monkeypatch):
        def always_refuse(self, dest):
            raise PermissionError("[WinError 5] Access is denied")

        monkeypatch.setattr(Path, "replace", always_refuse)
        monkeypatch.setattr("core.cloud_ingest.time.sleep", lambda _s: None)

        with pytest.raises(PermissionError):
            save_snapshot(b"new", tmp_path / "pi-01.jpg")


def gas_types(conn):
    return [r["type"] for r in conn.execute("SELECT type FROM alerts WHERE type = 'gas_abnormal'")]


class TestDeviceGasRule:
    """Notify on the same gas condition that sounds the Pi's buzzer."""

    BASELINE = 3229

    def feed(self, monitor, sensor, gas_values, start=0):
        payloads = {
            f"-K{start + i:03d}": recent(
                100 - (start + i), gas_raw=gas, gas_baseline=self.BASELINE, temp_c=3.0
            )
            for i, gas in enumerate(gas_values)
        }
        ingest_live(monitor, sensor, payloads, now=NOW)

    def test_a_level_above_the_zone_limit_but_normal_for_the_device_is_quiet(self, conn, live):
        """3100 is above the seeded 2500, but within 350 of the device's baseline."""
        monitor, sensor = live
        self.feed(monitor, sensor, [3100, 3120, 3110, 3105])

        assert gas_types(conn) == []

    def test_a_rise_past_baseline_plus_delta_notifies(self, conn, live):
        monitor, sensor = live
        spike = self.BASELINE + DEVICE_GAS_ALERT_DELTA + 200
        self.feed(monitor, sensor, [3100, spike, spike, spike])

        assert gas_types(conn) == ["gas_abnormal"]
        assert any("gas abnormal" in n.subject for n in monitor.notifier.sent)

    def test_exactly_baseline_plus_delta_alarms_as_the_device_does(self):
        limit = device_gas_limit(payload(gas_baseline=self.BASELINE))
        assert limit.breaches(self.BASELINE + DEVICE_GAS_ALERT_DELTA)
        assert not limit.breaches(self.BASELINE + DEVICE_GAS_ALERT_DELTA - 1)

    def test_a_cleared_spike_can_notify_again(self, conn, live):
        monitor, sensor = live
        spike = self.BASELINE + DEVICE_GAS_ALERT_DELTA + 200
        self.feed(monitor, sensor, [spike] * 3 + [3100] + [spike] * 3)

        assert gas_types(conn) == ["gas_abnormal", "gas_abnormal"]

    def test_firmware_without_a_baseline_keeps_the_zone_limit(self):
        assert device_gas_limit(payload()) is None
