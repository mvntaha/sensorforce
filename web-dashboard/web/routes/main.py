"""Signed-in pages.

Every page is rendered server-side with just enough context to be readable
without JavaScript; the live figures are then refreshed by ``app.js`` against
the JSON API.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime

from flask import Blueprint, Response, abort, redirect, render_template, request, send_file, url_for
from werkzeug.http import http_date as format_datetime

from core import items as item_store
from core import rfid as rfid_store
from core.config import SIM_SCENARIOS, current_scenario, get_settings
from core.engine import DISPOSAL_RECOMMENDATION, days_remaining
from core.items import QR_PATTERN
from core.outputs import FAULT_TEXT, fit, format_reading_line
from core.paths import snapshot_path
from core.status import (
    ZoneStatus,
    item_statuses,
    overall_status,
    to_engine_item,
    use_first,
    zone_statuses,
)
from core.thresholds import door_open_timeout_s, load_all
from web.database import get_db
from web.security import login_required

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

#: Shown as a dash so a missing measurement is never mistaken for a zero.
NO_VALUE = "—"

#: Offered as tap targets on the kiosk's amount step. Anything else can still
#: be typed: this is the short list that covers most of a kitchen, not a
#: restriction on what a unit may be.
UNITS = ("pcs", "kg", "g", "L", "ml", "pack")


def _greeting(hour: int) -> str:
    """A time-appropriate greeting for the dashboard hero."""
    if hour < 12:  # noqa: PLR2004 - clock boundaries
        return "Good morning"
    if hour < 18:  # noqa: PLR2004 - clock boundaries
        return "Good afternoon"
    return "Good evening"


def _tile(value, low, high, faulty, breached, unit_range):
    """Describe one measurement for the sensor tile macro.

    ``fill`` is where the value sits inside its acceptable range, as a
    percentage, so the bar shows headroom rather than an absolute scale.
    """
    if faulty or value is None:
        return {"value": NO_VALUE, "range": unit_range, "state": "fault", "fill": 100}

    span = (high - low) or 1
    fill = max(0, min(100, round((value - low) / span * 100)))
    state = "bad" if breached else "ok"
    return {"value": value, "range": unit_range, "state": state, "fill": fill}


def _zone_tiles(conn, zones: list[ZoneStatus]) -> dict[int, dict]:
    """Build the three sensor tiles for each zone.

    The range shown is the tightest one across the categories **actually
    stored** in that zone. Using every configured category would draw a bar
    against limits nobody is relying on: with produce configured but none in
    the fridge, a perfectly good 53% would be labelled Review because produce
    wants 60% or more. Per-item judgement still uses each item's own category.
    """
    configured = load_all(conn)
    tiles: dict[int, dict] = {}

    for zone in zones:
        stored = {
            row["category"]
            for row in conn.execute(
                "SELECT DISTINCT category FROM items WHERE zone_id = ?", (zone.zone_id,)
            )
        }
        thresholds = {name: limits for name, limits in configured.items() if name in stored}
        if not thresholds:
            thresholds = configured
        tiles[zone.zone_id] = _tiles_for_zone(conn, zone, thresholds, stored)
    return tiles


def _tiles_for_zone(conn, zone: ZoneStatus, thresholds: dict, stored: set[str]) -> dict:
    """The three tiles for one zone, against the limits that apply to it."""
    if thresholds:
        temp_low = max(t.temp_min for t in thresholds.values())
        temp_high = min(t.temp_max for t in thresholds.values())
        hum_low = max(t.hum_min for t in thresholds.values())
        hum_high = min(t.hum_max for t in thresholds.values())
    else:
        temp_low, temp_high, hum_low, hum_high = 0.0, 10.0, 0.0, 100.0

    latest = zone.latest or {}
    # Only a category that is actually stored here can put a tile into Review.
    relevant = [breach for name, breach in zone.breaches.items() if not stored or name in stored]
    temp_breach = any(temp for temp, _hum in relevant)
    hum_breach = any(hum for _temp, hum in relevant)

    gas_row = conn.execute(
        "SELECT gas_baseline, gas_abnormal FROM zones WHERE id = ?", (zone.zone_id,)
    ).fetchone()
    gas_low = float(gas_row["gas_baseline"] or 0)
    gas_high = float(gas_row["gas_abnormal"] or gas_low + 1)

    return {
        "temp": _tile(
            latest.get("temp_c"),
            temp_low,
            temp_high,
            "temp" in zone.faults,
            temp_breach,
            f"{temp_low:g} to {temp_high:g} °C",
        ),
        "hum": _tile(
            latest.get("humidity"),
            hum_low,
            hum_high,
            "humidity" in zone.faults,
            hum_breach,
            f"{hum_low:g} to {hum_high:g} %",
        ),
        "gas": _tile(
            latest.get("gas_raw"),
            gas_low,
            gas_high,
            "gas" in zone.faults,
            zone.gas_breach,
            f"Below {gas_high:g} (relative)",
        ),
    }


def _status_counts(statuses: list) -> dict:
    """How many items sit at each status, for the summary tiles."""
    return {
        "total": len(statuses),
        "fresh": sum(1 for s in statuses if str(s.assessment.status) == "Fresh/Normal"),
        "soon": sum(1 for s in statuses if str(s.assessment.status) == "Use Soon"),
        "check": sum(1 for s in statuses if str(s.assessment.status) == "Check Food"),
        "fault": sum(
            1 for s in statuses if str(s.assessment.status) == "Sensor Fault/Data Unavailable"
        ),
    }


@bp.get("/dashboard")
@login_required
def dashboard() -> str:
    """Live readings, zone state, alerts and the use-first list."""
    conn = get_db()
    statuses = item_statuses(conn)
    zones = zone_statuses(conn)
    alerts = conn.execute(
        "SELECT a.id, a.type, a.severity, a.message, a.ts, z.name AS zone_name "
        "FROM alerts a JOIN zones z ON z.id = a.zone_id "
        "WHERE a.acknowledged = 0 ORDER BY a.ts DESC LIMIT 5"
    ).fetchall()

    zone_list = list(zones.values())
    counts = _status_counts(statuses)

    return render_template(
        "dashboard.html",
        active_page="main.dashboard",
        page_title="Overview",
        greeting=_greeting(datetime.now(UTC).hour),
        zones=zone_list,
        zone_tiles=_zone_tiles(conn, zone_list),
        door_open_timeout_s=door_open_timeout_s(conn),
        counts=counts,
        overall=str(overall_status(statuses, zones)),
        statuses=statuses,
        use_first=use_first(statuses),
        alerts=alerts,
        item_count=len(statuses),
        zone_names={zone.zone_id: zone.name for zone in zone_list},
        device_id=get_settings().device_id,
        history=item_store.history(conn, limit=8),
        outcomes=item_store.outcome_counts(conn),
    )


@bp.get("/inventory")
@login_required
def inventory() -> str:
    """Every stored item, searchable and filterable by status, zone and category."""
    conn = get_db()
    statuses = item_statuses(conn)
    zones = list(zone_statuses(conn).values())
    return render_template(
        "inventory.html",
        active_page="main.inventory",
        page_title="Food inventory",
        statuses=statuses,
        zones=zones,
        zone_names={zone.zone_id: zone.name for zone in zones},
        categories=[
            row["category"]
            for row in conn.execute("SELECT category FROM thresholds ORDER BY category")
        ],
        counts=_status_counts(statuses),
    )


#: How many queued events the activity log shows. Enough to demonstrate an
#: offline period and its catch-up without turning the page into a database
#: dump.
LOG_EVENTS = 40


def _activity_log(conn) -> list[dict]:
    """The outbox as a readable feed: what changed, and whether it was sent."""
    events = []
    for row in conn.execute(
        "SELECT event_id, entity, payload_json, created_at, sync_status FROM outbox "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (LOG_EVENTS,),
    ):
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            # A row we cannot read is still a row that happened; say so rather
            # than dropping it from the record.
            logger.warning("outbox row %s is not readable JSON", row["event_id"])
            payload = {}
        events.append(
            {
                "created_at": row["created_at"],
                "entity": row["entity"],
                "sync_status": row["sync_status"],
                "summary": _summarise(row["entity"], payload),
            }
        )
    return events


def _summarise(entity: str, payload: dict) -> str:
    """One line describing a queued event, without dumping its JSON."""
    operation = payload.get("op", "recorded")
    if entity == "item":
        item = payload.get("item") or {}
        return f"{operation} {item.get('name', 'an item')} ({item.get('qr_id', '?')})"
    if entity == "alert":
        return payload.get("message") or f"{operation} an alert"
    if entity == "reading":
        return "a downsampled sensor reading"
    return f"{operation} {entity}"


@bp.get("/alerts")
@login_required
def alerts() -> str:
    """Alert history, what is still waiting to leave, and the activity log."""
    conn = get_db()
    rows = conn.execute(
        "SELECT a.id, a.type, a.severity, a.message, a.ts, a.acknowledged, "
        "z.name AS zone_name FROM alerts a JOIN zones z ON z.id = a.zone_id "
        "ORDER BY a.ts DESC, a.id DESC LIMIT 200"
    ).fetchall()

    return render_template(
        "alerts.html",
        active_page="main.alerts",
        page_title="Alerts & notifications",
        alerts=rows,
        open_count=sum(1 for row in rows if not row["acknowledged"]),
        notify_pending=conn.execute(
            "SELECT COUNT(*) AS total FROM alerts WHERE notify_pending = 1"
        ).fetchone()["total"],
        sync_pending=conn.execute(
            "SELECT COUNT(*) AS total FROM outbox WHERE sync_status <> 'synced'"
        ).fetchone()["total"],
        events=_activity_log(conn),
    )


@bp.get("/")
def scan() -> str:
    """The front door, and the one page that needs no sign-in.

    This is a shared screen beside the fridge. Anyone in the kitchen can hold a
    container up to it, be told what the system knows about it, and record that
    they have eaten it. That is how a kitchen works, and it is what the SRS
    means by users or authorised staff.

    What that deliberately allows: an anonymous person can look one identifier
    up and remove that item. What it does not: reading the inventory as a
    whole, the readings, the alerts, the thresholds or the accounts. Every one
    of those stays behind the sign-in, and nothing on this page reaches them.

    The camera needs a secure context, so the manual identifier box beside it is
    the guaranteed path and works everywhere. Either route ends in the same
    place: ``?qr_id=`` renders the record server-side, so a person with no
    working camera and no JavaScript still gets the whole flow.
    """
    conn = get_db()
    statuses = item_statuses(conn)
    zones = zone_statuses(conn)

    requested = (request.args.get("qr_id") or "").strip().upper()
    scanned = None
    if requested and QR_PATTERN.match(requested):
        scanned = next((s for s in statuses if s.item["qr_id"] == requested), None)

    return render_template(
        "index.html",
        active_page="main.scan",
        page_title="Scan",
        full_bleed=True,
        counts=_status_counts(statuses),
        overall=str(overall_status(statuses, zones)),
        requested=requested,
        scanned=scanned,
        zone_names={zone.zone_id: zone.name for zone in zones.values()},
        disposal_recommendation=DISPOSAL_RECOMMENDATION,
        open_alert_count=conn.execute(
            "SELECT COUNT(*) AS total FROM alerts WHERE acknowledged = 0"
        ).fetchone()["total"],
    )


@bp.get("/item/new")
def new_item() -> str:
    """Register a container, one group of fields at a time.

    A single long form suits editing, where someone is changing one thing they
    already know. Registering is different: the person is holding a carton and
    reading dates off the packaging, so the fields arrive in that order and the
    last step shows everything before anything is written.

    Open, because it is the other half of the scanner: a code the system has
    never seen is exactly the moment someone needs to add it, and sending them
    to a login there would mean the kiosk could only ever remove food. It
    offers no way to read the inventory, and every field is validated again by
    the API before anything is written.
    """
    conn = get_db()
    requested = (request.args.get("qr_id") or "").strip().upper()

    # Arriving from the "new item tapped in" banner: the tag is carried to the
    # form, shown but not editable, and the day it was tapped in becomes the
    # storage date. Only a tag actually waiting for details is honoured.
    tag = rfid_store.get_pending(conn, (request.args.get("rfid") or "").strip())
    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    put_in = tag["tapped_in_at"][:10] if tag else today
    try:
        put_in_offset = (date.fromisoformat(put_in) - today_date).days
    except ValueError:
        put_in, put_in_offset = today, 0
    categories = [
        row["category"] for row in conn.execute("SELECT category FROM thresholds ORDER BY category")
    ]
    zones = [dict(row) for row in conn.execute("SELECT id, name FROM zones ORDER BY id")]

    return render_template(
        "item_new.html",
        active_page="main.inventory",
        page_title="Add an item",
        # The other half of the kiosk, so it gets the kiosk shell: no sidebar
        # even for a signed-in administrator, and a screen that does not
        # scroll. Someone standing at the fridge is using this the same way
        # whether or not they happen to have an account.
        full_bleed=True,
        qr_id=requested if QR_PATTERN.match(requested) else "",
        suggested_qr=item_store.next_free_identifier(conn),
        categories=categories,
        # Every category's grid is rendered at once and the browser shows the
        # one that matches. It is a few dozen buttons, and it means choosing a
        # category costs no round trip -- which matters on a screen stuck to a
        # fridge on a LAN that may have nothing behind it.
        suggestions={name: item_store.name_suggestions(conn, name) for name in categories},
        units=UNITS,
        zones=zones,
        today=today,
        put_in=put_in,
        put_in_offset=put_in_offset,
        rfid_tag=tag["tag_id"] if tag else "",
    )


@bp.get("/item/<qr_id>")
@login_required
def item_page(qr_id: str) -> str | Response:
    """Show an item, or the creation form when the identifier is unknown."""
    identifier = qr_id.strip().upper()
    if not QR_PATTERN.match(identifier):
        abort(404, description=f"{qr_id} is not a FreshGuard identifier.")

    conn = get_db()
    item = item_store.get(conn, identifier)
    categories = [
        row["category"] for row in conn.execute("SELECT category FROM thresholds ORDER BY category")
    ]
    zones = [dict(row) for row in conn.execute("SELECT id, name FROM zones ORDER BY id")]

    if not categories or not zones:
        logger.warning("item page reached before any category or zone was configured")

    # The verdict is looked up rather than recomputed, so this page and the
    # inventory can never disagree about the same item.
    status = next((s for s in item_statuses(conn) if s.item["qr_id"] == identifier), None)
    verdict = status.assessment if status else None

    return render_template(
        "item.html",
        active_page="main.inventory",
        page_title=identifier,
        qr_id=identifier,
        item=item,
        assessment=verdict,
        days_stored=status.days_stored if status else 0,
        disposal_recommendation=DISPOSAL_RECOMMENDATION,
        categories=categories,
        zones=zones,
    )


@bp.get("/item")
@login_required
def item_lookup() -> Response:
    """Redirect a manual identifier entry to its item page."""
    identifier = (request.args.get("qr_id") or "").strip().upper()
    if not QR_PATTERN.match(identifier):
        return redirect(url_for("main.scan", error="1"))
    return redirect(url_for("main.item_page", qr_id=identifier))


@bp.get("/scanned")
def scanned_lookup() -> Response:
    """Send a typed identifier back to the scanner, which shows the record.

    The signed-in lookup above goes to the full item page. This one stays on
    the kiosk, because the person typing here may have no account and the
    scanner is the only page they can reach.
    """
    identifier = (request.args.get("qr_id") or "").strip().upper()
    if not QR_PATTERN.match(identifier):
        return redirect(url_for("main.scan", error="1"))
    return redirect(url_for("main.scan", qr_id=identifier))


#: What is fitted, how it is wired, and which reading proves it is alive.
#: The health column is derived from the last reading the monitor wrote; an
#: output device has no readback, so it is reported as unverified rather than
#: healthy. Pin numbers are the proposal in docs/pin_map.md and are confirmed
#: against the real wiring there, not here.
DEVICES = (
    ("DHT11", "Temperature", "GPIO4", "temp"),
    ("DHT11", "Humidity", "GPIO4", "humidity"),
    ("MQ-135 via ADS1115", "Gas (relative)", "I2C 0x48, channel A0", "gas"),
    ("Reed switch", "Door", "GPIO17", "door"),
)

#: Fitted but with nothing to read back. Saying "healthy" here would be a
#: guess, and the test matrix is where these are actually verified.
OUTPUT_DEVICES = (
    ("16x2 LCD", "Local display", "I2C 0x27"),
    ("LED indicators", "Green / yellow / red", "GPIO22 / GPIO23 / GPIO24"),
    ("Buzzer", "Local alarm", "GPIO27"),
    ("DS3231 RTC", "Timekeeping", "I2C 0x68"),
)


def _device_rows(zone: ZoneStatus) -> list[dict]:
    """One row per fitted sensor in a zone, with what the last reading proves."""
    latest = zone.latest or {}
    rows = []
    for part, measures, wiring, key in DEVICES:
        if not latest:
            state, detail = "unknown", "No readings recorded yet"
        elif key == "door":
            # The reed switch has no health flag: a closed contact and a broken
            # wire look the same, which is why it is not a required sensor.
            state = "ok"
            detail = "Open" if latest.get("door_open") else "Closed"
        elif key in zone.faults:
            state, detail = "fault", "Sensor Fault/Data Unavailable"
        else:
            column = {"temp": "temp_c", "humidity": "humidity", "gas": "gas_raw"}[key]
            value = latest.get(column)
            state, detail = "ok", NO_VALUE if value is None else str(value)
        rows.append(
            {
                "part": part,
                "measures": measures,
                "wiring": wiring,
                "state": state,
                "detail": detail,
            }
        )
    return rows


def _lcd_lines(zone: ZoneStatus, status: str) -> tuple[str, str]:
    """What the 16x2 display is showing for this zone right now.

    Rendered with the same helpers ``monitor.py`` drives the real display
    with, so the page mirrors the hardware rather than describing it.
    """
    if zone.has_required_fault:
        return fit(zone.name), fit(FAULT_TEXT)
    latest = zone.latest or {}
    return (
        format_reading_line(latest.get("temp_c"), latest.get("humidity"), latest.get("gas_raw")),
        fit(status),
    )


def _sync_counts(conn) -> dict:
    """How much is waiting to reach the cloud, and when the last row left."""
    counts = {
        row["sync_status"]: row["total"]
        for row in conn.execute(
            "SELECT sync_status, COUNT(*) AS total FROM outbox GROUP BY sync_status"
        )
    }
    last = conn.execute("SELECT MAX(synced_at) AS ts FROM outbox").fetchone()["ts"]
    oldest = conn.execute(
        "SELECT MIN(created_at) AS ts FROM outbox WHERE sync_status <> 'synced'"
    ).fetchone()["ts"]
    return {
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
        "synced": counts.get("synced", 0),
        "last_synced_at": last,
        "oldest_unsent_at": oldest,
    }


@bp.get("/sensors")
@login_required
def sensors() -> str:
    """What each sensor is reporting, and what the local outputs are showing."""
    conn = get_db()
    settings = get_settings()
    zones = list(zone_statuses(conn).values())
    statuses = item_statuses(conn)
    overall = str(overall_status(statuses, {zone.zone_id: zone for zone in zones}))

    return render_template(
        "sensors.html",
        active_page="main.sensors",
        page_title="Sensors & devices",
        zones=zones,
        zone_tiles=_zone_tiles(conn, zones),
        devices={zone.zone_id: _device_rows(zone) for zone in zones},
        lcd={zone.zone_id: _lcd_lines(zone, overall) for zone in zones},
        output_devices=OUTPUT_DEVICES,
        door_open_timeout_s=door_open_timeout_s(conn),
        sync=_sync_counts(conn),
        sensor_mode=settings.sensor_mode,
        scenario=current_scenario(settings),
        scenarios=SIM_SCENARIOS,
    )


@bp.get("/planner")
@login_required
def planner() -> str:
    """Everything in store, ordered by what runs out first.

    The order is the whole point: cooking from the top of this list is what
    actually stops food being thrown away.
    """
    conn = get_db()
    thresholds = load_all(conn)
    today = datetime.now(UTC).date()

    plan = []
    for status in item_statuses(conn):
        limits = thresholds.get(status.item["category"])
        plan.append(
            {
                "status": status,
                "days": days_remaining(to_engine_item(status.item), limits, today)
                if limits
                else None,
            }
        )
    plan.sort(key=lambda entry: (entry["days"] is None, entry["days"]))

    return render_template(
        "planner.html",
        active_page="main.planner",
        page_title="Consumption planner",
        plan=plan,
    )


@bp.get("/camera/<device_id>.jpg")
@login_required
def camera_frame(device_id: str) -> Response:
    """Serve the newest camera frame the Pi has sent for this device.

    Served from disk rather than fetched from the cloud per request: the page
    refreshes this every few seconds, and a dashboard that stalls whenever the
    internet is slow is worse than one showing a picture a moment old. It also
    keeps the LAN-only requirement intact.

    204 rather than 404 when there is no frame: the camera not having sent one
    is an ordinary state, and the page treats it as "nothing to show" instead of
    logging an error.
    """
    try:
        path = snapshot_path(device_id)
    except ValueError:
        abort(404)

    if not path.is_file():
        return Response(status=204)

    response = send_file(path, mimetype="image/jpeg")
    # The frame changes every few seconds under a fixed name, so it must never
    # be cached: a cached photograph of a fridge is worse than none.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Last-Modified"] = format_datetime(
        datetime.fromtimestamp(path.stat().st_mtime, UTC)
    )
    return response
