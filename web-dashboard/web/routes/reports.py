"""Reports and insights.

Everything on this page is counted from the local database. Nothing is
estimated: FreshGuard has no weight, no price and no measured environmental
data, so no money saved and no carbon figure is shown. Saying otherwise would
be inventing evidence.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime

from flask import Blueprint, render_template
from werkzeug.wrappers import Response

from core import items as item_store
from core.status import item_statuses
from web.database import get_db
from web.security import login_required

logger = logging.getLogger(__name__)

bp = Blueprint("reports", __name__)

#: The columns of the exported inventory, in a fixed order. Built from a
#: tuple rather than from whatever the database happens to return, so the
#: export a team member opens next week has the shape they expect.
CSV_COLUMNS = (
    "qr_id",
    "name",
    "category",
    "quantity",
    "unit",
    "zone",
    "manufacture_date",
    "storage_date",
    "expiry_date",
    "storage_limit_days",
    "state",
    "status",
    "rule",
)


def _counts(conn) -> dict:
    """Totals for the summary tiles, counted from the database."""
    # Consumed and discarded items no longer exist as items: the history is
    # the record, and it carries its own copy of what was removed.
    stored = conn.execute("SELECT COUNT(*) AS total FROM items").fetchone()["total"]
    outcomes = item_store.outcome_counts(conn)
    readings = conn.execute("SELECT COUNT(*) AS total FROM readings").fetchone()["total"]
    alerts = conn.execute("SELECT COUNT(*) AS total FROM alerts").fetchone()["total"]
    events = conn.execute("SELECT COUNT(*) AS total FROM outbox").fetchone()["total"]
    pending = conn.execute(
        "SELECT COUNT(*) AS total FROM outbox WHERE sync_status <> 'synced'"
    ).fetchone()["total"]
    return {
        "stored": stored,
        "consumed": outcomes["consumed"],
        "discarded": outcomes["discarded"],
        "readings": readings,
        "alerts": alerts,
        "events": events,
        "pending": pending,
    }


def _shares(rows: list[tuple[str, int]]) -> list[dict]:
    """Turn counts into bar widths, as a percentage of the largest one."""
    highest = max((count for _label, count in rows), default=0)
    return [
        {"label": label, "count": count, "share": round(count / highest * 100) if highest else 0}
        for label, count in rows
    ]


@bp.get("/reports")
@login_required
def reports() -> str:
    """Counts by category and by status, plus what the kitchen has done."""
    conn = get_db()
    statuses = item_statuses(conn)

    by_category = _shares(
        [
            (row["category"], row["total"])
            for row in conn.execute(
                "SELECT category, COUNT(*) AS total FROM items "
                "GROUP BY category ORDER BY total DESC, category"
            )
        ]
    )

    labels = ["Fresh/Normal", "Use Soon", "Check Food", "Sensor Fault/Data Unavailable"]
    by_status = _shares(
        [(label, sum(1 for s in statuses if str(s.assessment.status) == label)) for label in labels]
    )

    by_zone = _shares(
        [
            (row["name"], row["total"])
            for row in conn.execute(
                "SELECT z.name, COUNT(i.id) AS total FROM zones z "
                "LEFT JOIN items i ON i.zone_id = z.id "
                "GROUP BY z.id ORDER BY z.name"
            )
        ]
    )

    alerts_by_type = _shares(
        [
            (row["type"].replace("_", " "), row["total"])
            for row in conn.execute(
                "SELECT type, COUNT(*) AS total FROM alerts GROUP BY type ORDER BY total DESC"
            )
        ]
    )

    return render_template(
        "reports.html",
        active_page="reports.reports",
        page_title="Reports & insights",
        counts=_counts(conn),
        by_category=by_category,
        by_status=by_status,
        by_zone=by_zone,
        alerts_by_type=alerts_by_type,
    )


@bp.get("/reports/inventory.csv")
@login_required
def inventory_csv() -> Response:
    """The inventory as a spreadsheet, for the report's test-data appendix."""
    conn = get_db()
    zones = {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM zones")}
    verdicts = {status.item["qr_id"]: status.assessment for status in item_statuses(conn)}

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in conn.execute(
        "SELECT qr_id, name, category, quantity, unit, zone_id, manufacture_date, "
        "storage_date, expiry_date, storage_limit_days, state FROM items ORDER BY qr_id"
    ):
        verdict = verdicts.get(row["qr_id"])
        writer.writerow(
            [
                row["qr_id"],
                row["name"],
                row["category"],
                row["quantity"],
                row["unit"],
                zones.get(row["zone_id"], ""),
                row["manufacture_date"] or "",
                row["storage_date"],
                row["expiry_date"] or "",
                row["storage_limit_days"] or "",
                row["state"],
                str(verdict.status) if verdict else "",
                verdict.rule if verdict else "",
            ]
        )

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    logger.info("exported the inventory as CSV")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="freshguard-inventory-{stamp}.csv"'},
    )
