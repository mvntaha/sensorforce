"""Bringing the Raspberry Pi's cloud readings into SQLite.

The Pi writes straight to Firebase under ``pi_monitor/readings/<device>``. That
is a useful uplink but it is not the dashboard: Flask reads SQLite, the
freshness engine reads SQLite, and the alert path reads SQLite. This module is
the bridge, so the Pi's measurements arrive in the one place everything else
already looks.

SQLite stays the source of truth. Firebase is the transport the device happens
to use, nothing more, and an ingested reading is an ordinary row indistinguish-
able from one the local sensor loop wrote.

Two things about the device payload force real work here rather than a copy:

* **Firebase drops keys whose value is null.** A failed DHT read arrives as a
  payload with no ``temp_c`` key at all, not as ``temp_c: null``. A missing key
  therefore means a faulted sensor, and is translated into one.
* **The device reports a stale gas value while the MQ-135 is disconnected.**
  Readings carry ``gas_raw: 5793`` unchanged for minutes with
  ``components.mq135 = "I2C_DISCONNECTED"``. That number is the last good
  sample, not a measurement, and storing it would put a steady, healthy-looking
  gas level on the dashboard from a sensor that is not plugged in. When the
  component says the sensor is unhealthy the value is discarded and the reading
  is marked faulty, which is what SRS rule 1 requires.

The device has no reed switch, so ``door_open`` is recorded as closed. That is
a statement about this device's wiring, not a measurement, and it is why the
door-open alert cannot be driven from an ingested reading.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.cloud_time import trusted_ts
from core.sensors.base import GAS_RAW_RANGE, Reading, Sensor, is_plausible
from core.snapshots import LIVE_PATH, decode_jpeg_data_uri
from core.validation import Limit

if TYPE_CHECKING:
    from monitor import Monitor

logger = logging.getLogger(__name__)

#: Where the device publishes, under the database root.
READINGS_PATH = "pi_monitor/readings"

#: The component name the device uses for the gas sensor, and the value it
#: reports when that sensor is healthy.
GAS_COMPONENT = "mq135"
COMPONENT_OK = "OK"

#: How far above its own baseline the device's gas reading must rise before the
#: device sounds its buzzer. Mirrors GAS_ALERT_DELTA in the Pi's fresh.guard.py,
#: so a notification goes out for exactly the condition that sounds the buzzer.
DEVICE_GAS_ALERT_DELTA = 350

#: Rows pulled per cycle. Bounded so a long backlog makes steady progress
#: instead of loading the whole history into memory at once.
BATCH_SIZE = 200


class IngestError(RuntimeError):
    """Raised when the device's readings could not be read from the cloud."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What one ingest cycle achieved."""

    stored: int = 0
    skipped: int = 0
    rejected: int = 0

    @property
    def made_progress(self) -> bool:
        """True when at least one new reading reached SQLite."""
        return self.stored > 0


def _component_healthy(payload: dict[str, Any], name: str) -> bool:
    """True when the device did not report this component as faulty.

    A payload with no ``components`` block at all is treated as healthy: the
    earliest firmware did not send one, and those readings carry their values
    honestly. Only an explicit non-OK status means a fault.
    """
    components = payload.get("components")
    if not isinstance(components, dict):
        return True
    status = components.get(name)
    if status is None:
        return True
    return str(status) == COMPONENT_OK


def _number(payload: dict[str, Any], key: str) -> float | None:
    """Return a numeric field, or None when it is absent or unusable."""
    value = payload.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.debug("ignoring non-numeric %s=%r", key, value)
        return None


def device_gas_limit(payload: dict[str, Any]) -> Limit | None:
    """The gas limit the device itself is applying, or None when it sent none.

    The device measures its clean-air baseline each time it starts, so the
    figure moves from run to run. Taking it from the payload keeps the alert in
    step with the buzzer instead of with a number fixed in the database. The
    earliest firmware sends no baseline, and then the zone's limit stands.
    """
    baseline = _number(payload, "gas_baseline")
    if baseline is None or baseline <= 0:
        return None
    # The device alarms at baseline + delta inclusive; a Limit is breached only
    # beyond its high end.
    return Limit(low=float("-inf"), high=baseline + DEVICE_GAS_ALERT_DELTA - 1)


def to_reading(payload: dict[str, Any]) -> Reading:
    """Translate one device payload into a Reading.

    Every sensor that did not produce a usable value becomes a fault, so the
    resulting row can never show a number for a sensor that was not working.
    """
    ts = payload.get("ts")
    if not isinstance(ts, str) or not ts:
        raise ValueError("payload has no timestamp")

    faults: set[str] = set()

    temp = _number(payload, "temp_c")
    if temp is None or not is_plausible("temp", temp):
        if temp is not None:
            logger.warning("discarding implausible temperature %r at %s", temp, ts)
        temp = None
        faults.add("temp")

    humidity = _number(payload, "humidity")
    if humidity is None or not is_plausible("humidity", humidity):
        if humidity is not None:
            logger.warning("discarding implausible humidity %r at %s", humidity, ts)
        humidity = None
        faults.add("humidity")

    # The component status is checked before the value, because a disconnected
    # MQ-135 still ships its last good number.
    gas: int | None = None
    if not _component_healthy(payload, GAS_COMPONENT):
        faults.add("gas")
    else:
        raw = _number(payload, "gas_raw")
        if raw is None:
            faults.add("gas")
        else:
            gas = int(raw)
            low, high = GAS_RAW_RANGE
            if not low <= gas <= high:
                logger.warning("discarding out-of-range gas value %r at %s", gas, ts)
                gas = None
                faults.add("gas")

    return Reading(
        ts=ts,
        temp_c=temp,
        humidity=humidity,
        gas_raw=gas,
        # The device has no reed switch. Recorded as closed, never measured.
        door_open=False,
        faults=frozenset(faults),
    )


def existing_timestamps(conn: sqlite3.Connection, zone_id: int, since: str) -> set[str]:
    """Return the timestamps this zone already holds at or after ``since``.

    Used to make ingest idempotent without touching the frozen schema: running
    the bridge twice over the same window stores nothing the second time.
    """
    rows = conn.execute(
        "SELECT ts FROM readings WHERE zone_id = ? AND ts >= ?",
        (zone_id, since),
    ).fetchall()
    return {row["ts"] for row in rows}


def ingest(
    conn: sqlite3.Connection,
    payloads: dict[str, dict[str, Any]],
    zone_id: int,
    *,
    device_id: str = "pi-01",
    queue_event: bool = False,
) -> IngestResult:
    """Store the device payloads that are not already in SQLite.

    ``payloads`` is the node as Firebase returns it: push key -> payload. The
    keys are only used for ordering; identity is the timestamp, because that is
    what the readings table records.
    """
    # Imported here rather than at module scope: monitor.py imports this module
    # in some deployments, and a top-level import would be circular.
    from monitor import store_reading

    readings, skipped, rejected = new_readings(conn, payloads, zone_id)
    for reading, _payload in readings:
        store_reading(conn, reading, zone_id, queue_event=queue_event, device_id=device_id)

    stored = len(readings)
    if stored:
        logger.info("ingested %d reading(s) from %s into zone %d", stored, device_id, zone_id)
    return IngestResult(stored=stored, skipped=skipped, rejected=rejected)


def new_readings(
    conn: sqlite3.Connection,
    payloads: dict[str, dict[str, Any]],
    zone_id: int,
) -> tuple[list[tuple[Reading, dict[str, Any]]], int, int]:
    """Translate the payloads SQLite does not already hold, oldest first.

    Returns each new reading beside the payload it came from, with the counts
    of payloads skipped as already stored and rejected as unusable. Nothing is
    written.
    """
    if not payloads:
        return [], 0, 0

    # A Pi whose clock has reset sends timestamps from 2000; Firebase's receipt
    # time, carried in the push key, stands in for them.
    ordered = sorted(
        (key, {**p, "ts": trusted_ts(key, p.get("ts"))} if isinstance(p, dict) else p)
        for key, p in payloads.items()
    )
    earliest = min(
        (p.get("ts") for _, p in ordered if isinstance(p, dict) and isinstance(p.get("ts"), str)),
        default=None,
    )
    if earliest is None:
        return [], 0, len(ordered)

    seen = existing_timestamps(conn, zone_id, earliest)

    readings: list[tuple[Reading, dict[str, Any]]] = []
    skipped = rejected = 0
    for key, payload in ordered:
        if not isinstance(payload, dict):
            rejected += 1
            continue
        try:
            reading = to_reading(payload)
        except ValueError as exc:
            logger.warning("skipping unusable payload %s: %s", key, exc)
            rejected += 1
            continue

        if reading.ts in seen:
            skipped += 1
            continue

        seen.add(reading.ts)
        readings.append((reading, payload))
    return readings, skipped, rejected


#: A reading older than this when it arrives is stored but not evaluated. After
#: the worker has been down, the backlog still reaches the charts, but nobody is
#: emailed at 09:00 about a temperature that was high at 07:30.
LIVE_WINDOW_S = 300


class CloudSensor(Sensor):
    """A sensor whose readings are handed to it from the Firebase feed.

    Lets the monitor evaluate the Pi's cloud readings through exactly the path
    it uses for local hardware: confirmation, per-category limits, alert
    de-duplication and the email and ntfy channels.
    """

    def __init__(self) -> None:
        self._pending: deque[Reading] = deque()

    def feed(self, reading: Reading) -> None:
        """Queue one reading for the next :meth:`read`."""
        self._pending.append(reading)

    def read(self) -> Reading:
        """Return the next queued reading.

        Only called after :meth:`feed`. An empty queue is reported as a total
        fault rather than raised, as the Sensor contract requires.
        """
        if not self._pending:
            logger.error("cloud sensor read with no reading queued")
            return Reading.faulted()
        return self._pending.popleft()


def _age_s(ts: str, now: datetime) -> float:
    """Seconds between a reading's timestamp and ``now``. Unparseable is ancient."""
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return float("inf")
    return (now - then).total_seconds()


def ingest_live(
    monitor: Monitor,
    sensor: CloudSensor,
    payloads: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
    max_age_s: float = LIVE_WINDOW_S,
) -> IngestResult:
    """Store the new payloads and evaluate the recent ones for alerts.

    ``sensor`` must be the monitor's own sensor. Each recent reading is fed to it
    and the monitor ticks once, so the reading is stored, assessed and, when a
    condition has just become true, notified, all in the order the device took
    them. Readings older than ``max_age_s`` are stored without evaluation.
    """
    from monitor import store_reading

    now = now or datetime.now(UTC)
    readings, skipped, rejected = new_readings(monitor.conn, payloads, monitor.zone_id)

    late = 0
    for reading, payload in readings:
        if _age_s(reading.ts, now) > max_age_s:
            store_reading(monitor.conn, reading, monitor.zone_id, device_id=monitor.device_id)
            late += 1
            continue
        limit = device_gas_limit(payload)
        if limit is not None:
            monitor.set_gas_limit(limit)
        sensor.feed(reading)
        monitor.tick()

    if late:
        logger.info("stored %d late reading(s) without evaluating them", late)
    return IngestResult(stored=len(readings), skipped=skipped, rejected=rejected)


#: Tries at moving a new frame into place while a reader holds the old one.
SNAPSHOT_REPLACE_ATTEMPTS = 5


def fetch_snapshot(reference: Any, device_id: str) -> bytes | None:
    """Return the device's latest camera frame as JPEG bytes, or None.

    Returns None rather than raising when the device has sent no frame: a
    missing photograph is an ordinary state, not a failure.
    """
    try:
        raw = reference.child(f"{LIVE_PATH}/{device_id}/image_base64").get()
    except Exception as exc:
        raise IngestError(f"could not read the camera frame: {exc}") from exc
    return decode_jpeg_data_uri(raw)


def save_snapshot(data: bytes, path: Path) -> Path:
    """Write the frame, replacing any previous one.

    Written to a temporary name and moved into place, so a page loading the
    picture at the moment it is replaced never sees a half-written file.

    On Windows the move is refused while another process has the old frame
    open, which happens whenever the dashboard is serving it. That lock lasts
    milliseconds, so the move is retried briefly before giving up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    for attempt in range(SNAPSHOT_REPLACE_ATTEMPTS):
        try:
            temporary.replace(path)
            return path
        except PermissionError:
            if attempt == SNAPSHOT_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.1)
    return path


def fetch_payloads(
    reference: Any,
    device_id: str,
    limit: int = BATCH_SIZE,
    *,
    after: str | None = None,
) -> dict[str, Any]:
    """Return device readings from the cloud.

    Without ``after``, the ``limit`` most recent. With it, up to ``limit``
    readings pushed after that key, oldest first, which keeps a fast poll down
    to the one or two new readings instead of re-downloading the whole batch.

    ``reference`` is a Firebase database reference for the root, kept as a
    parameter so the caller owns the connection and tests can pass a fake.
    """
    try:
        node = reference.child(f"{READINGS_PATH}/{device_id}").order_by_key()
        if after is None:
            payloads = node.limit_to_last(limit).get()
        else:
            # start_at is inclusive, so one extra is asked for and the known key
            # dropped. Push keys sort by time, so this is exactly "what is new".
            payloads = node.start_at(after).limit_to_first(limit + 1).get()
            if payloads:
                payloads.pop(after, None)
    except Exception as exc:
        raise IngestError(f"could not read the device readings: {exc}") from exc
    return payloads or {}
