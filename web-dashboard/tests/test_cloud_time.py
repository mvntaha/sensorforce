"""A Pi whose clock has reset to 2000 must not silence alerts or bury readings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.cloud_ingest import CloudSensor, ingest, ingest_live
from core.cloud_time import MAX_CLOCK_SKEW_S, push_key_time, trusted_ts
from core.notify import FakeNotifier
from core.outputs import fake_outputs
from monitor import Monitor

PUSH_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
RESET = "2000-01-15T20:02:54Z"
FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def push_key(moment: datetime, suffix: str = "abcdefghijkl") -> str:
    """A Firebase push key received at ``moment``."""
    millis = int(moment.timestamp() * 1000)
    chars = ""
    for _ in range(8):
        chars = PUSH_CHARS[millis % 64] + chars
        millis //= 64
    return chars + suffix


def reading(ts: str, **extra) -> dict:
    return {
        "ts": ts,
        "temp_c": 3.0,
        "humidity": 50.0,
        "gas_raw": 1000,
        "components": {"dht22": "OK", "mq135": "OK"},
        **extra,
    }


class TestPushKeys:
    def test_a_push_key_carries_when_firebase_received_it(self):
        moment = datetime(2026, 10, 14, 6, 0, 25, tzinfo=UTC)
        assert push_key_time(push_key(moment)) == moment

    def test_a_healthy_clock_is_believed(self):
        moment = datetime(2026, 10, 14, 6, 0, 0, tzinfo=UTC)
        own = (moment - timedelta(seconds=MAX_CLOCK_SKEW_S - 5)).strftime(FORMAT)
        assert trusted_ts(push_key(moment), own) == own

    def test_a_reset_clock_is_overruled_by_firebase(self):
        moment = datetime(2026, 10, 14, 6, 0, 25, tzinfo=UTC)
        assert trusted_ts(push_key(moment), RESET) == "2026-10-14T06:00:25Z"

    def test_a_key_that_is_not_a_push_key_changes_nothing(self):
        assert trusted_ts("-K1", RESET) == RESET


class TestReadings:
    def test_readings_from_a_reset_clock_are_stored_at_the_real_time(self, conn, zone_id):
        moment = datetime(2026, 10, 14, 6, 0, 25, tzinfo=UTC)
        ingest(conn, {push_key(moment): reading(RESET)}, zone_id)

        stored = conn.execute("SELECT ts FROM readings").fetchone()["ts"]
        assert stored == "2026-10-14T06:00:25Z"

    def test_and_are_evaluated_so_alerts_still_fire(self, conn, zone_id, category, item_id):
        """The bug this exists for: every reading looked 26 years late, so none alerted."""
        sensor = CloudSensor()
        monitor = Monitor(
            conn,
            sensor=sensor,
            outputs=fake_outputs(),
            notifier=FakeNotifier(),
            zone_id=zone_id,
            zone_name="Main shelf",
        )
        now = datetime.now(UTC)
        hot = {
            push_key(now - timedelta(seconds=30 - i * 10)): reading(RESET, temp_c=24.0)
            for i in range(3)
        }

        ingest_live(monitor, sensor, hot, now=now)

        assert any("temp high" in n.subject for n in monitor.notifier.sent)
