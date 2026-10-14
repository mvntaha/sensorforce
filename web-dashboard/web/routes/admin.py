"""Administrator pages: thresholds, the door timeout, categories and users."""

from __future__ import annotations

from flask import Blueprint, render_template

from core.thresholds import door_open_timeout_s
from web.database import get_db
from web.security import admin_required

bp = Blueprint("admin", __name__, url_prefix="/admin")

#: The order core.engine.assess applies, written out for the administrator who
#: sets the limits above. Kept here as text rather than derived from the engine:
#: the engine's job is to decide, not to describe itself, and the wording is
#: reviewed against docs/decision_rules.md.
DECISION_RULES = (
    {
        "condition": "The item is past its expiry date or its storage limit",
        "outcome": "Check Food. A known problem outranks an unknown one, so this is tested first.",
    },
    {
        "condition": "A required sensor in the zone is faulty",
        "outcome": (
            "Sensor Fault/Data Unavailable. Never Fresh: an unknown condition is not a good one."
        ),
    },
    {
        "condition": "Zone temperature or humidity is confirmed outside the category range",
        "outcome": "Check Food, naming the zone rather than blaming the item.",
    },
    {
        "condition": "Zone air quality is confirmed above its normal reading",
        "outcome": (
            "Check Food for the zone. One MQ-135 serves the whole zone, so no item is named and "
            "no ppm figure is shown."
        ),
    },
    {
        "condition": "The item is within its use-soon window",
        "outcome": "Use Soon. Plan a meal around it.",
    },
    {
        "condition": "Nothing above matched",
        "outcome": (
            "Fresh/Normal. Conditions are within the configured range, which is not a "
            "verdict on whether the food may be eaten."
        ),
    },
)


@bp.get("/")
@admin_required
def settings() -> str:
    """Thresholds, the door-open timeout and the account list."""
    conn = get_db()
    thresholds = conn.execute(
        "SELECT category, temp_min, temp_max, hum_min, hum_max, storage_limit_days, "
        "use_soon_days, source_note FROM thresholds ORDER BY category"
    ).fetchall()
    zones = conn.execute(
        "SELECT id, name, gas_baseline, gas_abnormal, gas_source_note FROM zones ORDER BY id"
    ).fetchall()
    users = conn.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY username"
    ).fetchall()

    return render_template(
        "admin.html",
        active_page="admin.settings",
        page_title="Settings",
        thresholds=thresholds,
        zones=zones,
        users=users,
        door_open_timeout_s=door_open_timeout_s(conn),
        decision_rules=DECISION_RULES,
    )
