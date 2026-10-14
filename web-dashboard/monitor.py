"""The sensing loop: read, validate, store, evaluate, alert.

Runs as its own process alongside the Flask app, sharing one SQLite file in WAL
mode. It is the only writer of readings and alerts; the web app only reads.

Two properties matter more than anything else here:

* **It never crashes.** A sensor error, a database hiccup or a bug in the
  freshness rules is logged and the loop continues. A monitor that dies takes
  the buzzer with it, which is worse than a monitor that reports a fault.
* **Storing a reading is independent of evaluating it.** The reading is written
  before the rules run, so a failure in the rules can never cost us the data,
  and the dashboard keeps showing live measurements either way.

Alerts fire on *state changes*, not on every poll. A condition that is still
true one second later is the same alert, not a new one.
"""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from core import db
from core.alerts import Alert, raise_alert
from core.config import Settings, get_settings
from core.engine import (
    DOOR_RECOMMENDATION,
    Assessment,
    CategoryThresholds,
    Item,
    Status,
    ZoneState,
    assess,
    worst_status,
)
from core.notify import Notifier, get_notifier
from core.outputs import OutputSet, format_reading_line, get_outputs
from core.sensors.base import Reading, Sensor, get_sensor
from core.sync import SyncWorker, build_client
from core.thresholds import (
    door_open_timeout_s,
    gas_limit,
    humidity_limit,
    load_all,
    temperature_limit,
)
from core.validation import ConfirmationTracker, DoorTracker, Limit, detect_faults

logger = logging.getLogger(__name__)

#: Keys used to remember which conditions are already alerting, so a condition
#: that persists does not re-alert on every poll.
TEMP_KEY = "temp"
HUMIDITY_KEY = "humidity"
GAS_KEY = "gas"
FAULT_KEY = "fault"

#: Mirror one reading per minute at the default 1 Hz poll. Sending every poll
#: would fill the replica with noise and the link with traffic.
DOWNSAMPLE_EVERY = 60


def _as_float(value: float | int | None) -> float | None:
    """Return the value as a float, preserving a missing reading as None."""
    return None if value is None else float(value)


@dataclass(slots=True)
class CategoryTrackers:
    """The confirmation trackers for one food category's limits."""

    temperature: ConfirmationTracker
    humidity: ConfirmationTracker


def load_items(conn: sqlite3.Connection, zone_id: int) -> list[Item]:
    """Return the items stored in this zone."""
    rows = conn.execute(
        "SELECT id, qr_id, name, category, zone_id, storage_date, expiry_date, "
        "storage_limit_days FROM items WHERE zone_id = ? ORDER BY id",
        (zone_id,),
    ).fetchall()
    return [
        Item(
            id=row["id"],
            qr_id=row["qr_id"],
            name=row["name"],
            category=row["category"],
            zone_id=row["zone_id"],
            storage_date=date.fromisoformat(row["storage_date"]),
            expiry_date=date.fromisoformat(row["expiry_date"]) if row["expiry_date"] else None,
            storage_limit_days=row["storage_limit_days"],
        )
        for row in rows
    ]


def store_reading(
    conn: sqlite3.Connection,
    reading: Reading,
    zone_id: int,
    *,
    queue_event: bool = False,
    device_id: str = "pi-01",
) -> int:
    """Write one reading and return its row id.

    When ``queue_event`` is set the outbox row is written in the same
    transaction, so the cloud copy of a reading is exactly as durable as the
    reading itself. Readings are downsampled before this point: mirroring every
    poll would fill the replica with noise.
    """
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO readings (zone_id, ts, temp_c, humidity, gas_raw, door_open, "
            "temp_ok, hum_ok, gas_ok) VALUES (:zone_id, :ts, :temp_c, :humidity, :gas_raw, "
            ":door_open, :temp_ok, :hum_ok, :gas_ok)",
            reading.to_row(zone_id),
        )
        if queue_event:
            event_id = str(uuid.uuid4())
            payload = {
                "event_id": event_id,
                "entity": "reading",
                "op": "create",
                "ts": reading.ts,
                "device_id": device_id,
                "zone_id": zone_id,
                **reading.to_row(zone_id),
            }
            conn.execute(
                "INSERT INTO outbox (event_id, entity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (event_id, "reading", json.dumps(payload, sort_keys=True), reading.ts),
            )
        return int(cursor.lastrowid)


@dataclass
class TickResult:
    """What one pass of the loop did. Returned so tests can inspect it."""

    reading: Reading | None = None
    reading_id: int | None = None
    assessments: list[Assessment] = field(default_factory=list)
    status: Status = Status.FRESH
    alerts: list[Alert] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def alert_types(self) -> list[str]:
        """The types of alert raised on this tick."""
        return [alert.type for alert in self.alerts]


class Monitor:
    """One zone's sensing loop."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        sensor: Sensor,
        outputs: OutputSet,
        notifier: Notifier,
        *,
        zone_id: int,
        zone_name: str,
        device_id: str = "pi-01",
        poll_interval_s: float = 1.0,
        today: Callable[[], date] | None = None,
        monotonic: Callable[[], float] | None = None,
        downsample_every: int = DOWNSAMPLE_EVERY,
    ) -> None:
        self.conn = conn
        self.sensor = sensor
        self.outputs = outputs
        self.notifier = notifier
        self.zone_id = zone_id
        self.zone_name = zone_name
        self.device_id = device_id
        self.poll_interval_s = poll_interval_s
        self._today = today or (lambda: datetime.now(UTC).date())
        self._monotonic = monotonic or time.monotonic

        self.thresholds: dict[str, CategoryThresholds] = load_all(conn)
        self._category_trackers: dict[str, CategoryTrackers] = {}
        self._gas_tracker: ConfirmationTracker | None = None
        self._gas_limit_override: Limit | None = None
        self._door_tracker = DoorTracker(timeout_s=float(door_open_timeout_s(conn)))
        self.downsample_every = max(1, downsample_every)
        self._active: set[str] = set()
        self._running = False
        self.ticks = 0

    # -- setup -------------------------------------------------------------
    def _build_trackers(self) -> None:
        """Create one temperature and humidity tracker per food category.

        The zone has a single temperature, but what counts as a breach of it
        depends on what is stored: 8 C is too warm for dairy and perfectly fine
        for root vegetables. Tracking per category is what keeps an item from
        being judged against another category's limits.

        Gas, sensor faults and the door stay zone-wide, because one gas sensor,
        one thermometer and one reed switch serve the whole zone.
        """
        if self._category_trackers:
            return
        for name, thresholds in self.thresholds.items():
            self._category_trackers[name] = CategoryTrackers(
                temperature=ConfirmationTracker(limit=temperature_limit(thresholds)),
                humidity=ConfirmationTracker(limit=humidity_limit(thresholds)),
            )
        gas = self._gas_limit_override or gas_limit(self.conn, self.zone_id)
        if gas is not None:
            self._gas_tracker = ConfirmationTracker(limit=gas)

    def set_gas_limit(self, limit: Limit) -> None:
        """Replace the zone's gas limit with one supplied by the device.

        A streak or confirmed breach in progress is kept: the device moving its
        baseline is not evidence that the gas level changed.
        """
        self._gas_limit_override = limit
        if self._gas_tracker is None:
            self._gas_tracker = ConfirmationTracker(limit=limit)
        else:
            self._gas_tracker.limit = limit

    # -- one pass ----------------------------------------------------------
    def tick(self) -> TickResult:
        """Run one pass. Never raises; failures are recorded in the result."""
        result = TickResult()
        self.ticks += 1

        # 1. Read. A driver that misbehaves is a fault, not a crash.
        try:
            result.reading = self.sensor.read()
        except Exception as exc:
            logger.exception("sensor read failed")
            result.errors.append(f"read: {exc}")
            result.reading = Reading.faulted()

        # 2. Store. Done before evaluation so a rules failure never costs data.
        #    Only every Nth reading is mirrored to the cloud; the rest stay local.
        try:
            result.reading_id = store_reading(
                self.conn,
                result.reading,
                self.zone_id,
                queue_event=(self.ticks % self.downsample_every == 1 or self.downsample_every == 1),
                device_id=self.device_id,
            )
        except Exception as exc:
            logger.exception("could not store reading")
            result.errors.append(f"store: {exc}")

        # 3. Evaluate and alert.
        try:
            self._evaluate(result)
        except Exception as exc:
            logger.exception("evaluation failed")
            result.errors.append(f"evaluate: {exc}")
            self._show_fallback(result.reading)

        return result

    def _evaluate(self, result: TickResult) -> None:
        """Validate the reading, assess every item and raise any new alerts."""
        reading = result.reading
        if reading is None:
            return

        self._build_trackers()
        faults = detect_faults(reading)

        gas_breach = self._update(self._gas_tracker, reading.gas_raw)
        door_alert = self._door_tracker.update(reading.door_open, self._monotonic())

        # Every category is fed on every poll, whether or not anything of that
        # category is stored right now, so a streak is never broken by an item
        # being added or removed part way through.
        breaches: dict[str, tuple[bool, bool]] = {
            name: (
                trackers.temperature.update(_as_float(reading.temp_c)),
                trackers.humidity.update(_as_float(reading.humidity)),
            )
            for name, trackers in self._category_trackers.items()
        }

        def zone_for(category: str) -> ZoneState:
            """The zone as it applies to one category's limits."""
            temp_breach, humidity_breach = breaches.get(category, (False, False))
            return ZoneState(
                zone_id=self.zone_id,
                name=self.zone_name,
                faults=faults,
                temp_breach=temp_breach,
                humidity_breach=humidity_breach,
                gas_breach=gas_breach,
                door_open_too_long=door_alert,
            )

        today = self._today()
        items = load_items(self.conn, self.zone_id)
        result.assessments = [
            assess(item, zone_for(item.category), self.thresholds[item.category], today)
            for item in items
            if item.category in self.thresholds
        ]

        # The conditions that belong to the zone rather than to any category.
        zone_wide = ZoneState(
            zone_id=self.zone_id,
            name=self.zone_name,
            faults=faults,
            gas_breach=gas_breach,
            door_open_too_long=door_alert,
        )

        # A zone-level fault is folded in separately: with no items stored the
        # assessment list is empty, and an empty list is Fresh. Without this a
        # zone with dead sensors and nothing in it would light green.
        statuses = list(result.assessments)
        if zone_wide.has_required_fault:
            statuses.append(
                Assessment(Status.FAULT, 2, f"Sensor data for {self.zone_name} is unavailable")
            )
        result.status = worst_status(statuses)

        self._drive_outputs(reading, result.status)
        stored = {item.category for item in items}
        result.alerts = self._sync_alerts(
            zone_wide, breaches, stored, reading, door_alert, result.status
        )

    @staticmethod
    def _update(tracker: ConfirmationTracker | None, value: float | int | None) -> bool:
        """Feed a tracker, treating an absent tracker as never breaching."""
        if tracker is None:
            return False
        return tracker.update(None if value is None else float(value))

    def _drive_outputs(self, reading: Reading, status: Status) -> None:
        """Show the current measurements and the worst status."""
        self.outputs.leds.show_status(str(status))
        self.outputs.display.write(
            format_reading_line(reading.temp_c, reading.humidity, reading.gas_raw),
            str(status),
        )

    def _show_fallback(self, reading: Reading | None) -> None:
        """Show measurements when the rules could not run.

        The status line reports that data is unavailable rather than guessing,
        so a broken rule engine can never present as Fresh.
        """
        if reading is None:
            return
        self.outputs.leds.show_status(str(Status.FAULT))
        self.outputs.display.write(
            format_reading_line(reading.temp_c, reading.humidity, reading.gas_raw),
            str(Status.FAULT),
        )

    # -- alerting ----------------------------------------------------------
    def _category_conditions(
        self,
        breaches: dict[str, tuple[bool, bool]],
        stored: set[str],
        reading: Reading,
    ) -> list[tuple[str, bool, str, str, str]]:
        """Temperature and humidity conditions, one pair per food category.

        The message names the category whose limits were breached, never a
        single item: the measurement belongs to the zone, and every item of
        that category is affected equally.

        A category with nothing stored is reported as inactive rather than
        skipped. That stops the zone alarming about food nobody is keeping,
        and it also clears a live alert when the last item of a category is
        removed, so the same condition can alert again later.
        """
        conditions: list[tuple[str, bool, str, str, str]] = []
        for category in sorted(breaches):
            temp_breach, humidity_breach = breaches[category]
            if category not in stored:
                temp_breach = humidity_breach = False
            thresholds = self.thresholds[category]

            too_cold = reading.temp_c is not None and reading.temp_c < thresholds.temp_min
            conditions.append(
                (
                    f"{TEMP_KEY}:{category}",
                    temp_breach,
                    "temp_low" if too_cold else "temp_high",
                    "critical",
                    f"Temperature in {self.zone_name} is outside the range for {category}",
                )
            )

            too_dry = reading.humidity is not None and reading.humidity < thresholds.hum_min
            conditions.append(
                (
                    f"{HUMIDITY_KEY}:{category}",
                    humidity_breach,
                    "humidity_low" if too_dry else "humidity_high",
                    "warning",
                    f"Humidity in {self.zone_name} is outside the range for {category}",
                )
            )
        return conditions

    def _sync_alerts(
        self,
        zone: ZoneState,
        breaches: dict[str, tuple[bool, bool]],
        stored: set[str],
        reading: Reading,
        door_alert: bool,
        status: Status,
    ) -> list[Alert]:
        """Raise an alert for each condition that has just become true.

        A condition already alerting is not raised again, and a condition that
        has cleared is forgotten so it can alert afresh next time.
        """
        raised: list[Alert] = []

        conditions: list[tuple[str, bool, str, str, str]] = [
            *self._category_conditions(breaches, stored, reading),
            (
                GAS_KEY,
                zone.gas_breach,
                "gas_abnormal",
                "warning",
                f"Air quality in {self.zone_name} is above its normal reading",
            ),
            (
                FAULT_KEY,
                bool(zone.faults),
                "sensor_fault",
                "warning",
                f"A sensor in {self.zone_name} is not reporting; data unavailable",
            ),
        ]

        for key, active, alert_type, severity, message in conditions:
            if active and key not in self._active:
                self._active.add(key)
                raised.append(self._raise(alert_type, severity, message, status))
            elif not active and key in self._active:
                logger.info("%s cleared in %s", key, self.zone_name)
                self._active.discard(key)

        # The door tracker already reports only the transition, so it is raised
        # directly rather than tracked here.
        if door_alert:
            raised.append(
                self._raise(
                    "door_open",
                    "warning",
                    # The SRS names this recommendation, so it travels with
                    # the alert to the dashboard, the display and the email.
                    f"The door of {self.zone_name} has been open longer than "
                    f"the limit. {DOOR_RECOMMENDATION}.",
                    status,
                )
            )
        return raised

    def _raise(self, alert_type: str, severity: str, message: str, status: Status) -> Alert:
        """Persist, announce and notify one alert."""
        alert = Alert(
            zone_id=self.zone_id,
            zone_name=self.zone_name,
            type=alert_type,
            severity=severity,
            message=message,
        )
        raise_alert(
            self.conn,
            alert,
            outputs=self.outputs,
            notifier=self.notifier,
            device_id=self.device_id,
            worst_status=str(status),
        )
        logger.info("raised %s in %s", alert_type, self.zone_name)
        return alert

    # -- the loop ----------------------------------------------------------
    def stop(self) -> None:
        """Ask the loop to finish after the current pass."""
        self._running = False

    def run(
        self,
        max_ticks: int | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> int:
        """Poll until stopped, keeping to the configured interval.

        Sleeps only for the time left in the interval, so a slow pass does not
        push every later reading further behind. Returns the number of passes.
        """
        self._running = True
        completed = 0
        while self._running:
            started = clock()
            self.tick()
            completed += 1
            if max_ticks is not None and completed >= max_ticks:
                break
            remaining = self.poll_interval_s - (clock() - started)
            if remaining > 0:
                sleeper(remaining)
        self._running = False
        return completed


def build_monitor(
    settings: Settings,
    conn: sqlite3.Connection,
    zone_id: int = 1,
    *,
    sensor: Sensor | None = None,
) -> Monitor:
    """Assemble a monitor from configuration and the database.

    Passing ``sensor`` replaces the configured backend. The cloud ingest worker
    does this to evaluate the Pi's Firebase readings, and since those readings
    come from a device elsewhere, this machine's GPIO outputs are not driven.
    """
    row = conn.execute("SELECT id, name FROM zones WHERE id = ?", (zone_id,)).fetchone()
    if row is None:
        raise LookupError(f"no zone with id {zone_id}")

    if sensor is None:
        # The control file lets the dashboard change the simulated scenario while
        # the loop is running, which is how the test matrix drives a high-temp or
        # gas run without restarting anything. It means nothing to the real backend.
        sensor_options: dict[str, object] = {}
        if settings.simulated:
            sensor_options = {
                "scenario": settings.sim_scenario,
                "control_file": settings.sim_control_file,
            }
        sensor = get_sensor(settings.sensor_mode, **sensor_options)
        outputs = get_outputs(settings.sensor_mode)
    else:
        outputs = get_outputs("sim")

    return Monitor(
        conn,
        sensor=sensor,
        outputs=outputs,
        notifier=get_notifier(settings.smtp, settings.ntfy),
        zone_id=row["id"],
        zone_name=row["name"],
        device_id=settings.device_id,
        poll_interval_s=settings.poll_interval_s,
    )


def main() -> int:  # pragma: no cover - process entry point
    """Run the monitor until interrupted."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    conn = db.bootstrap(settings.db_path)
    monitor = build_monitor(settings, conn)

    # The sync worker gets the database path rather than this connection: a
    # SQLite connection belongs to the thread that opened it, and the worker
    # runs on its own so a slow network never delays a reading or an alert.
    worker = SyncWorker(
        client=build_client(settings),
        db_path=settings.db_path,
        interval_s=float(settings.firebase.sync_interval_s),
    )
    worker.start()

    def _shutdown(*_args: object) -> None:
        logger.info("shutting down")
        monitor.stop()
        worker.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        monitor.run()
    finally:
        worker.stop()
        monitor.outputs.close()
        monitor.sensor.close()
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
