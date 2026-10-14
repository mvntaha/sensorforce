"""The sensing loop.

The file is split deliberately:

* ``TestLoopResilience``, ``TestReadingStorage`` and ``TestLoopTiming`` exercise
  only monitor.py and pass today. They cover the promise that matters most: the
  loop keeps reading and storing whatever else breaks.
* ``TestLocalAlertPath`` and ``TestAlertStateChanges`` drive the simulator
  scenarios end to end through validation and the engine. They stay **red**
  until the NotImplementedError bodies in core/validation.py and core/engine.py
  are written, because an alert can only be raised once a breach can be
  confirmed and an item assessed.

Nothing here imports those bodies directly; the loop calls them through the
frozen signatures.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from core.engine import Status
from core.notify import FakeNotifier
from core.outputs import BuzzerPattern, Led, fake_outputs
from core.sensors.base import Reading, Sensor
from core.sensors.sim import SimulatedSensor, stepped_clock
from core.thresholds import set_setting
from monitor import Monitor, TickResult, load_items, store_reading

TODAY = date(2026, 9, 24)


def stepping_seconds(step: float = 1.0):
    """A monotonic clock that advances one step per call."""
    state = {"now": -step}

    def _now() -> float:
        state["now"] += step
        return state["now"]

    return _now


class ExplodingSensor(Sensor):
    """A driver that raises on every read, as a wiring fault would."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self) -> Reading:
        self.reads += 1
        raise OSError("I2C bus error")


class FlakySensor(Sensor):
    """Raises on every second read and otherwise reports normally."""

    def __init__(self) -> None:
        self.reads = 0
        self._inner = SimulatedSensor(scenario="normal", clock=stepped_clock())

    def read(self) -> Reading:
        self.reads += 1
        if self.reads % 2 == 0:
            raise TimeoutError("sensor did not respond")
        return self._inner.read()


@pytest.fixture
def monitor_factory(conn: sqlite3.Connection, zone_id: int, category: str):
    """Build a monitor wired to recording outputs and a recording notifier."""

    def build(scenario: str = "normal", sensor: Sensor | None = None, **kwargs: object) -> Monitor:
        return Monitor(
            conn,
            sensor=sensor or SimulatedSensor(scenario=scenario, clock=stepped_clock()),
            outputs=fake_outputs(),
            notifier=FakeNotifier(),
            zone_id=zone_id,
            zone_name="Main shelf",
            device_id="pi-test",
            today=lambda: TODAY,
            **kwargs,  # type: ignore[arg-type]
        )

    return build


def alert_types(conn: sqlite3.Connection) -> list[str]:
    return [row["type"] for row in conn.execute("SELECT type FROM alerts ORDER BY id")]


# ---------------------------------------------------------------- green today
class TestReadingStorage:
    """Storing a reading does not depend on the rules, so these pass now."""

    def test_a_tick_stores_one_reading(self, monitor_factory, conn: sqlite3.Connection) -> None:
        monitor_factory().tick()
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1

    def test_each_tick_stores_another(self, monitor_factory, conn: sqlite3.Connection) -> None:
        monitor = monitor_factory()
        for _ in range(5):
            monitor.tick()
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 5

    def test_a_disconnected_sensor_is_stored_as_a_fault_not_a_zero(
        self, monitor_factory, conn: sqlite3.Connection
    ) -> None:
        """The row must record that the value is missing, never a plausible 0."""
        monitor_factory(scenario="sensor_disconnected").tick()
        row = conn.execute("SELECT temp_c, temp_ok, gas_ok FROM readings").fetchone()
        assert row["temp_c"] is None
        assert row["temp_ok"] == 0
        assert row["gas_ok"] == 1, "the gas sensor is on another bus and still reports"

    def test_a_sensor_error_still_stores_a_fault_row(
        self, monitor_factory, conn: sqlite3.Connection
    ) -> None:
        monitor_factory(sensor=ExplodingSensor()).tick()
        row = conn.execute("SELECT temp_ok, hum_ok, gas_ok FROM readings").fetchone()
        assert (row["temp_ok"], row["hum_ok"], row["gas_ok"]) == (0, 0, 0)

    def test_the_door_state_is_recorded(self, monitor_factory, conn: sqlite3.Connection) -> None:
        monitor_factory(scenario="door_open").tick()
        assert conn.execute("SELECT door_open FROM readings").fetchone()[0] == 1

    def test_store_reading_returns_the_row_id(self, conn: sqlite3.Connection, zone_id: int) -> None:
        reading = Reading(temp_c=4.0, humidity=55.0, gas_raw=1200)
        assert store_reading(conn, reading, zone_id) > 0


class TestLoopResilience:
    """The loop must survive anything. These pass now and must keep passing."""

    def test_a_raising_sensor_does_not_crash_the_tick(self, monitor_factory) -> None:
        result = monitor_factory(sensor=ExplodingSensor()).tick()
        assert isinstance(result, TickResult)
        assert any("read" in error for error in result.errors)

    def test_a_raising_sensor_is_recorded_as_a_total_fault(self, monitor_factory) -> None:
        result = monitor_factory(sensor=ExplodingSensor()).tick()
        assert result.reading is not None
        assert result.reading.healthy is False

    def test_the_loop_keeps_running_through_repeated_errors(
        self, monitor_factory, conn: sqlite3.Connection
    ) -> None:
        sensor = ExplodingSensor()
        monitor = monitor_factory(sensor=sensor)
        assert monitor.run(max_ticks=5, sleeper=lambda _s: None) == 5
        assert sensor.reads == 5
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 5

    def test_an_intermittent_sensor_keeps_the_good_readings(
        self, monitor_factory, conn: sqlite3.Connection
    ) -> None:
        monitor = monitor_factory(sensor=FlakySensor())
        monitor.run(max_ticks=6, sleeper=lambda _s: None)
        rows = conn.execute("SELECT temp_ok FROM readings ORDER BY id").fetchall()
        assert [row["temp_ok"] for row in rows] == [1, 0, 1, 0, 1, 0]

    def test_a_failure_in_the_rules_never_costs_the_reading(
        self, monitor_factory, conn: sqlite3.Connection
    ) -> None:
        """The reading is stored before the rules run, so it survives them.

        This is what makes the loop useful while the engine is unimplemented.
        """
        result = monitor_factory().tick()
        assert result.reading_id is not None
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1

    def test_a_rules_failure_never_shows_fresh(
        self, monitor_factory, item_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken engine must not present as everything being fine."""

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the rules are broken")

        monkeypatch.setattr("monitor.assess", explode)
        monitor = monitor_factory()
        result = monitor.tick()
        assert result.errors, "the failure must be recorded, not swallowed silently"
        assert monitor.outputs.leds.current is not Led.GREEN

    def test_a_rules_failure_reports_a_fault_on_the_display(
        self, monitor_factory, item_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The status line says Sensor Fault, truncated to the 16-column LCD."""

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the rules are broken")

        monkeypatch.setattr("monitor.assess", explode)
        monitor = monitor_factory()
        monitor.tick()
        assert "Sensor Fault" in monitor.outputs.display.text
        assert "Fresh" not in monitor.outputs.display.text

    def test_a_zone_fault_shows_even_with_nothing_stored(self, monitor_factory) -> None:
        """An empty zone with dead sensors must not light green.

        The assessment list is empty when no items are stored, and an empty
        list is Fresh, so the zone fault has to reach the lamps on its own.
        """
        monitor = monitor_factory(scenario="sensor_disconnected")
        monitor.tick()
        assert monitor.outputs.leds.current is Led.RED
        assert "Sensor Fault" in monitor.outputs.display.text


class TestLoopTiming:
    def test_the_loop_sleeps_the_remainder_of_the_interval(self, monitor_factory) -> None:
        """A slow pass must not push every later reading further behind."""
        slept: list[float] = []
        elapsed = iter([0.0, 0.3, 0.3, 0.6, 0.6, 0.9])
        monitor = monitor_factory(poll_interval_s=1.0)
        monitor.run(max_ticks=3, sleeper=slept.append, clock=lambda: next(elapsed, 99.0))
        assert all(duration <= 1.0 for duration in slept)

    def test_no_sleep_when_the_pass_overran(self, monitor_factory) -> None:
        slept: list[float] = []
        ticks = iter([0.0, 5.0, 5.0, 10.0])
        monitor = monitor_factory(poll_interval_s=1.0)
        monitor.run(max_ticks=2, sleeper=slept.append, clock=lambda: next(ticks, 99.0))
        assert slept == []

    def test_stop_ends_the_loop(self, monitor_factory) -> None:
        monitor = monitor_factory()
        monitor.stop()
        assert monitor.run(max_ticks=3, sleeper=lambda _s: None) >= 1

    def test_the_tick_counter_advances(self, monitor_factory) -> None:
        monitor = monitor_factory()
        monitor.run(max_ticks=4, sleeper=lambda _s: None)
        assert monitor.ticks == 4


class TestItemLoading:
    def test_items_load_from_the_zone(
        self, conn: sqlite3.Connection, zone_id: int, item_id: int
    ) -> None:
        items = load_items(conn, zone_id)
        assert [item.qr_id for item in items] == ["FG-0001"]
        assert items[0].expiry_date == date(2026, 9, 27)

    def test_an_item_without_an_expiry_loads(
        self, conn: sqlite3.Connection, zone_id: int, category: str
    ) -> None:
        conn.execute(
            "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("FG-0002", "Eggs", category, 6, zone_id, "2026-09-21"),
        )
        item = next(i for i in load_items(conn, zone_id) if i.qr_id == "FG-0002")
        assert item.expiry_date is None

    def test_another_zones_items_are_not_loaded(
        self, conn: sqlite3.Connection, zone_id: int, item_id: int
    ) -> None:
        other = conn.execute("INSERT INTO zones (name) VALUES (?)", ("Crisper",))
        assert load_items(conn, int(other.lastrowid)) == []


# ------------------------------------------- red until the engine bodies exist
@pytest.mark.engine
class TestLocalAlertPath:
    """End-to-end scenarios. Red until validation.py and engine.py are written."""

    def test_high_temp_raises_a_temperature_alert(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        """Three consecutive bad readings confirm, then one alert is raised."""
        monitor = monitor_factory(scenario="high_temp")
        monitor.run(max_ticks=4, sleeper=lambda _s: None)
        assert "temp_high" in alert_types(conn)

    def test_high_temp_does_not_alert_before_the_third_reading(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="high_temp")
        monitor.run(max_ticks=2, sleeper=lambda _s: None)
        assert alert_types(conn) == []

    def test_high_temp_sounds_the_threshold_pattern(self, monitor_factory, item_id: int) -> None:
        monitor = monitor_factory(scenario="high_temp")
        monitor.run(max_ticks=4, sleeper=lambda _s: None)
        assert BuzzerPattern.THRESHOLD in monitor.outputs.buzzer.patterns

    def test_high_temp_lights_the_red_lamp(self, monitor_factory, item_id: int) -> None:
        monitor = monitor_factory(scenario="high_temp")
        monitor.run(max_ticks=4, sleeper=lambda _s: None)
        assert monitor.outputs.leds.current is Led.RED

    def test_high_humidity_raises_a_humidity_alert(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        """Abnormal humidity is a mandatory SRS scenario in its own right.

        The engine's humidity rule and the simulator's humidity scenario were
        both covered, but nothing checked that the two meet and actually raise
        an alert, which is what the test matrix claims.
        """
        monitor = monitor_factory(scenario="high_humidity")
        monitor.run(max_ticks=4, sleeper=lambda _s: None)
        assert "humidity_high" in alert_types(conn)

    def test_high_humidity_sounds_the_threshold_pattern(
        self, monitor_factory, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="high_humidity")
        monitor.run(max_ticks=4, sleeper=lambda _s: None)
        assert BuzzerPattern.THRESHOLD in monitor.outputs.buzzer.patterns

    def test_high_humidity_does_not_alert_before_the_third_reading(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="high_humidity")
        monitor.run(max_ticks=2, sleeper=lambda _s: None)
        assert alert_types(conn) == []

    def test_gas_spike_raises_a_zone_level_alert(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="gas_spike")
        monitor.run(max_ticks=6, sleeper=lambda _s: None)
        assert "gas_abnormal" in alert_types(conn)

    def test_the_gas_alert_never_names_an_item(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        """SRS rule 3: gas is zone-level and is never attributed to an item."""
        monitor = monitor_factory(scenario="gas_spike")
        monitor.run(max_ticks=6, sleeper=lambda _s: None)
        rows = conn.execute(
            "SELECT item_id, message FROM alerts WHERE type = 'gas_abnormal'"
        ).fetchall()
        assert rows, "no gas alert was raised"
        for row in rows:
            assert row["item_id"] is None
            assert "Milk" not in row["message"]
            assert "ppm" not in row["message"].lower()

    def test_door_open_raises_a_door_alert(self, monitor_factory, conn: sqlite3.Connection) -> None:
        set_setting(conn, "door_open_timeout_s", "1")
        monitor = monitor_factory(scenario="door_open", monotonic=stepping_seconds())
        monitor.run(max_ticks=5, sleeper=lambda _s: None)
        assert "door_open" in alert_types(conn)

    def test_the_door_alert_uses_its_own_buzzer_pattern(
        self, monitor_factory, conn: sqlite3.Connection
    ) -> None:
        set_setting(conn, "door_open_timeout_s", "1")
        monitor = monitor_factory(scenario="door_open", monotonic=stepping_seconds())
        monitor.run(max_ticks=5, sleeper=lambda _s: None)
        assert BuzzerPattern.DOOR in monitor.outputs.buzzer.patterns

    def test_sensor_disconnected_raises_a_fault_alert(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="sensor_disconnected")
        monitor.run(max_ticks=3, sleeper=lambda _s: None)
        assert "sensor_fault" in alert_types(conn)

    def test_sensor_disconnected_never_reports_fresh(self, monitor_factory, item_id: int) -> None:
        """The scenario the SRS singles out: a fault must never look fine."""
        monitor = monitor_factory(scenario="sensor_disconnected")
        monitor.run(max_ticks=3, sleeper=lambda _s: None)
        assert monitor.outputs.leds.current is not Led.GREEN
        assert "Fresh" not in monitor.outputs.display.text

    def test_the_normal_scenario_raises_nothing(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="normal")
        monitor.run(max_ticks=10, sleeper=lambda _s: None)
        assert alert_types(conn) == []

    def test_the_normal_scenario_lights_green(self, monitor_factory, item_id: int) -> None:
        monitor = monitor_factory(scenario="normal")
        monitor.run(max_ticks=5, sleeper=lambda _s: None)
        assert monitor.outputs.leds.current is Led.GREEN


@pytest.mark.engine
class TestAlertStateChanges:
    """An alert fires on a transition, not on every poll."""

    def test_a_persisting_condition_alerts_only_once(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="high_temp")
        monitor.run(max_ticks=20, sleeper=lambda _s: None)
        assert alert_types(conn).count("temp_high") == 1

    def test_every_alert_writes_an_outbox_row(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="high_temp")
        monitor.run(max_ticks=6, sleeper=lambda _s: None)
        alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        queued = conn.execute("SELECT COUNT(*) FROM outbox WHERE entity = 'alert'").fetchone()[0]
        assert alerts > 0
        assert alerts == queued

    def test_the_remote_notification_is_attempted(self, monitor_factory, item_id: int) -> None:
        monitor = monitor_factory(scenario="high_temp")
        monitor.run(max_ticks=6, sleeper=lambda _s: None)
        assert monitor.notifier.sent, "a confirmed alert must notify remotely"

    def test_an_offline_notification_leaves_the_alert_pending(
        self, monitor_factory, conn: sqlite3.Connection, item_id: int
    ) -> None:
        monitor = monitor_factory(scenario="high_temp")
        monitor.notifier = FakeNotifier(fail=True)
        monitor.run(max_ticks=6, sleeper=lambda _s: None)
        pending = conn.execute("SELECT COUNT(*) FROM alerts WHERE notify_pending = 1").fetchone()[0]
        assert pending > 0
        assert monitor.outputs.buzzer.patterns, "the buzzer fires with no network"


class ConstantSensor(Sensor):
    """Holds the zone at a fixed temperature, so limits can be compared."""

    def __init__(self, temp_c: float = 8.0, humidity: float = 55.0) -> None:
        self.temp_c = temp_c
        self.humidity = humidity
        self._clock = stepped_clock()

    def read(self) -> Reading:
        from datetime import UTC

        return Reading(
            ts=self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            temp_c=self.temp_c,
            humidity=self.humidity,
            gas_raw=1200,
            door_open=False,
        )


# The tolerant category is named so it sorts either before or after the strict
# one, because the original defect picked whichever category sorted first.
CATEGORY_ORDERS = [
    pytest.param("aa_rootveg", "zz_dairy", id="tolerant-sorts-first"),
    pytest.param("zz_rootveg", "aa_dairy", id="tolerant-sorts-last"),
]


@pytest.fixture
def two_category_zone(conn: sqlite3.Connection, zone_id: int):
    """Set up a zone holding one strict and one tolerant category."""

    def build(tolerant: str, strict: str) -> tuple[str, str]:
        conn.execute(
            "INSERT INTO thresholds (category, temp_min, temp_max, hum_min, hum_max, "
            "storage_limit_days, use_soon_days, source_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tolerant, 2.0, 12.0, 30.0, 70.0, 30, 5, "prototype/demo assumption"),
        )
        conn.execute(
            "INSERT INTO thresholds (category, temp_min, temp_max, hum_min, hum_max, "
            "storage_limit_days, use_soon_days, source_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (strict, 0.0, 5.0, 30.0, 70.0, 30, 5, "prototype/demo assumption"),
        )
        for qr, category, name in [
            ("FG-0101", tolerant, "Carrots"),
            ("FG-0102", strict, "Milk"),
        ]:
            conn.execute(
                "INSERT INTO items (qr_id, name, category, quantity, zone_id, storage_date, "
                "expiry_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (qr, name, category, 1, zone_id, "2026-09-20", "2026-12-01"),
            )
        return tolerant, strict

    return build


class TestMultipleCategories:
    """A zone has one temperature; what counts as a breach depends on the food.

    The defect these cover judged every item against whichever category sorted
    first, so a tolerant item was flagged for a strict item's limit, and worse,
    a strict item could read Fresh under a tolerant category's limit.
    """

    def run_at_8c(self, monitor_factory, ticks: int = 4):
        monitor = monitor_factory(sensor=ConstantSensor(temp_c=8.0))
        result = None
        for _ in range(ticks):
            result = monitor.tick()
        return monitor, result

    @pytest.mark.parametrize(("tolerant", "strict"), CATEGORY_ORDERS)
    def test_each_item_is_judged_against_its_own_category(
        self, monitor_factory, two_category_zone, tolerant: str, strict: str
    ) -> None:
        two_category_zone(tolerant, strict)
        _monitor, result = self.run_at_8c(monitor_factory)

        by_rule = {a.rule for a in result.assessments}
        statuses = sorted(str(a.status) for a in result.assessments)
        assert statuses == [
            "Check Food",
            "Fresh/Normal",
        ], f"at 8 C the strict item must be Check Food and the tolerant one Fresh, got {statuses}"
        assert by_rule == {3, 6}

    @pytest.mark.parametrize(("tolerant", "strict"), CATEGORY_ORDERS)
    def test_a_strict_item_never_reads_fresh_when_too_warm(
        self, monitor_factory, two_category_zone, tolerant: str, strict: str
    ) -> None:
        """The dangerous direction: a tolerant limit must not excuse dairy."""
        two_category_zone(tolerant, strict)
        _monitor, result = self.run_at_8c(monitor_factory)

        milk = next(a for a in result.assessments if "Milk" in a.message or a.rule == 3)
        assert milk.status is not Status.FRESH

    @pytest.mark.parametrize(("tolerant", "strict"), CATEGORY_ORDERS)
    def test_the_worst_status_drives_the_lamps(
        self, monitor_factory, two_category_zone, tolerant: str, strict: str
    ) -> None:
        two_category_zone(tolerant, strict)
        monitor, _result = self.run_at_8c(monitor_factory)
        assert monitor.outputs.leds.current is Led.RED

    @pytest.mark.parametrize(("tolerant", "strict"), CATEGORY_ORDERS)
    def test_the_alert_names_the_category_not_an_item(
        self,
        monitor_factory,
        two_category_zone,
        conn: sqlite3.Connection,
        tolerant: str,
        strict: str,
    ) -> None:
        """One measurement affects every item of a category, so it names the category."""
        two_category_zone(tolerant, strict)
        self.run_at_8c(monitor_factory)

        rows = conn.execute(
            "SELECT item_id, message FROM alerts WHERE type IN ('temp_high', 'temp_low')"
        ).fetchall()
        assert rows, "the strict category should have alerted"
        for row in rows:
            assert row["item_id"] is None, "a category-level alert must not name one item"
            assert strict in row["message"]
            assert "Milk" not in row["message"]
            assert "Carrots" not in row["message"]

    @pytest.mark.parametrize(("tolerant", "strict"), CATEGORY_ORDERS)
    def test_only_the_breached_category_alerts(
        self,
        monitor_factory,
        two_category_zone,
        conn: sqlite3.Connection,
        tolerant: str,
        strict: str,
    ) -> None:
        two_category_zone(tolerant, strict)
        self.run_at_8c(monitor_factory)

        messages = [
            row["message"]
            for row in conn.execute("SELECT message FROM alerts WHERE type LIKE 'temp%'")
        ]
        assert len(messages) == 1, f"only the strict category should alert, got {messages}"
        assert tolerant not in messages[0]

    @pytest.mark.parametrize(("tolerant", "strict"), CATEGORY_ORDERS)
    def test_a_temperature_suiting_both_alerts_for_neither(
        self,
        monitor_factory,
        two_category_zone,
        conn: sqlite3.Connection,
        tolerant: str,
        strict: str,
    ) -> None:
        two_category_zone(tolerant, strict)
        monitor = monitor_factory(sensor=ConstantSensor(temp_c=4.0))
        for _ in range(4):
            result = monitor.tick()
        assert alert_types(conn) == []
        assert all(a.status is Status.FRESH for a in result.assessments)
        assert monitor.outputs.leds.current is Led.GREEN


class TestReadingDownsampling:
    """Readings are mirrored to the cloud sparsely, not on every poll."""

    def test_the_first_reading_is_queued(self, monitor_factory, conn: sqlite3.Connection) -> None:
        monitor_factory().tick()
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE entity='reading'").fetchone()[0] == 1

    def test_most_polls_are_not_mirrored(self, monitor_factory, conn: sqlite3.Connection) -> None:
        """Sending every poll would fill the replica with noise."""
        monitor = monitor_factory(downsample_every=10)
        monitor.run(max_ticks=10, sleeper=lambda _s: None)
        queued = conn.execute("SELECT COUNT(*) FROM outbox WHERE entity='reading'").fetchone()[0]
        stored = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        assert stored == 10
        assert queued == 1

    def test_the_rate_is_configurable(self, monitor_factory, conn: sqlite3.Connection) -> None:
        monitor = monitor_factory(downsample_every=2)
        monitor.run(max_ticks=6, sleeper=lambda _s: None)
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE entity='reading'").fetchone()[0] == 3

    def test_a_queued_reading_carries_the_sync_fields(
        self, monitor_factory, conn: sqlite3.Connection
    ) -> None:
        import json

        monitor_factory().tick()
        row = conn.execute("SELECT payload_json FROM outbox WHERE entity='reading'").fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["entity"] == "reading"
        assert payload["op"] == "create"
        assert payload["device_id"] == "pi-test"
        assert payload["event_id"]

    def test_the_reading_and_its_outbox_row_commit_together(
        self, monitor_factory, conn: sqlite3.Connection
    ) -> None:
        monitor_factory().tick()
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE entity='reading'").fetchone()[0] == 1
