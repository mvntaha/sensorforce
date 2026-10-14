"""Zones, readings, freshness status, alerts and the health check.

All read-only. The web app reports what ``monitor.py`` wrote; it never reads a
sensor and never decides an alert for itself.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from flask import Blueprint, request
from werkzeug.wrappers import Response

from core.status import item_statuses, overall_status, use_first, zone_statuses
from web.api import fail, ok
from web.database import get_db
from web.security import login_required

logger = logging.getLogger(__name__)

bp = Blueprint("api_zones", __name__, url_prefix="/api")

#: Readings older than this mean the monitor has stopped writing.
STALE_READING_S = 30

MAX_READINGS = 2000
DEFAULT_READINGS = 200


@bp.get("/zones")
@login_required
def zones() -> tuple[Response, int]:
    """List the storage zones and their gas configuration."""
    rows = (
        get_db()
        .execute(
            "SELECT id, name, gas_baseline, gas_abnormal, gas_source_note FROM zones ORDER BY id"
        )
        .fetchall()
    )
    return ok([dict(row) for row in rows])


@bp.get("/readings/latest")
@login_required
def latest_readings() -> tuple[Response, int]:
    """The most recent reading for each zone, with its confirmed state."""
    statuses = zone_statuses(get_db())
    return ok(
        [
            {
                "zone_id": zone.zone_id,
                "name": zone.name,
                "latest": zone.latest,
                "faults": sorted(zone.faults),
                "gas_breach": zone.gas_breach,
                "has_required_fault": zone.has_required_fault,
            }
            for zone in statuses.values()
        ]
    )


@bp.get("/readings")
@login_required
def readings() -> tuple[Response, int]:
    """Readings for charting, filtered by zone and time range."""
    conn = get_db()
    clauses, params = [], []

    zone_raw = request.args.get("zone")
    if zone_raw:
        try:
            params.append(int(zone_raw))
        except ValueError:
            return fail("zone must be a whole number.", 400)
        clauses.append("zone_id = ?")

    for field, comparison in (("from", ">="), ("to", "<=")):
        value = request.args.get(field)
        if value:
            clauses.append(f"ts {comparison} ?")
            params.append(value)

    try:
        limit = min(int(request.args.get("limit", DEFAULT_READINGS)), MAX_READINGS)
    except ValueError:
        return fail("limit must be a whole number.", 400)
    if limit <= 0:
        return fail("limit must be greater than zero.", 400)

    # Clause text is fixed; every value is bound.
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT zone_id, ts, temp_c, humidity, gas_raw, door_open, temp_ok, hum_ok, gas_ok "  # noqa: S608
        f"FROM readings{where} ORDER BY ts DESC, id DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return ok([dict(row) for row in reversed(rows)])


@bp.get("/status")
@login_required
def status() -> tuple[Response, int]:
    """Per-item freshness plus per-zone sensor state."""
    conn = get_db()
    statuses = item_statuses(conn)
    zones_by_id = zone_statuses(conn)
    return ok(
        {
            "overall": str(overall_status(statuses, zones_by_id)),
            "items": [status.as_dict() for status in statuses],
            "use_first": [status.as_dict() for status in use_first(statuses)],
            "zones": [
                {
                    "zone_id": zone.zone_id,
                    "name": zone.name,
                    "faults": sorted(zone.faults),
                    "gas_breach": zone.gas_breach,
                    "has_required_fault": zone.has_required_fault,
                }
                for zone in zones_by_id.values()
            ],
            "disclaimer": "FreshGuard does not certify food safety.",
        }
    )


@bp.get("/alerts")
@login_required
def alerts() -> tuple[Response, int]:
    """Alert history, newest first."""
    conn = get_db()
    only_open = request.args.get("open") == "1"
    where = " WHERE a.acknowledged = 0" if only_open else ""
    rows = conn.execute(
        "SELECT a.id, a.event_id, a.zone_id, a.item_id, a.type, a.severity, a.message, a.ts, "  # noqa: S608
        "a.acknowledged, a.notify_pending, z.name AS zone_name "
        f"FROM alerts a JOIN zones z ON z.id = a.zone_id{where} "
        "ORDER BY a.ts DESC, a.id DESC LIMIT 200"
    ).fetchall()
    return ok([dict(row) for row in rows])


@bp.post("/alerts/<int:alert_id>/ack")
@login_required
def acknowledge(alert_id: int) -> tuple[Response, int]:
    """Acknowledge an alert so it leaves the active list."""
    from core import db

    conn = get_db()
    if conn.execute("SELECT 1 FROM alerts WHERE id = ?", (alert_id,)).fetchone() is None:
        return fail(f"Alert {alert_id} was not found.", 404)
    with db.transaction(conn):
        conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
    return ok({"id": alert_id, "acknowledged": True})


@bp.get("/health")
@login_required
def health() -> tuple[Response, int]:
    """Sync backlog and monitor liveness, used by the dashboard's fault banner."""
    conn = get_db()
    pending = conn.execute(
        "SELECT COUNT(*) AS total FROM outbox WHERE sync_status IN ('pending', 'failed')"
    ).fetchone()["total"]
    unsent = conn.execute(
        "SELECT COUNT(*) AS total FROM alerts WHERE notify_pending = 1"
    ).fetchone()["total"]

    row = conn.execute("SELECT MAX(ts) AS latest FROM readings").fetchone()
    age_s: float | None = None
    if row["latest"]:
        try:
            last = datetime.strptime(row["latest"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            age_s = max((datetime.now(UTC) - last).total_seconds(), 0.0)
        except ValueError:
            logger.warning("unparseable reading timestamp %r", row["latest"])

    return ok(
        {
            "sync_pending": pending,
            "notifications_pending": unsent,
            "last_reading_age_s": age_s,
            "monitor_running": age_s is not None and age_s <= STALE_READING_S,
        }
    )
