"""Administrator API: thresholds, global settings and user accounts.

Every route here is admin-only, enforced on the server. A threshold cannot be
saved without a source note, which is the SRS rule that every limit is either
cited or openly labelled an assumption.
"""

from __future__ import annotations

import logging

from flask import Blueprint, request
from werkzeug.wrappers import Response

from core import db
from core.config import SIM_SCENARIOS, current_scenario, get_settings
from core.thresholds import DOOR_OPEN_TIMEOUT_KEY, door_open_timeout_s, set_setting
from core.users import UserError, create_user, get_by_id
from web.api import fail, ok
from web.database import get_db
from web.security import admin_required, current_user

logger = logging.getLogger(__name__)

bp = Blueprint("api_admin", __name__, url_prefix="/api")

HUMIDITY_MIN = 0.0
HUMIDITY_MAX = 100.0

THRESHOLD_FIELDS = (
    "temp_min",
    "temp_max",
    "hum_min",
    "hum_max",
    "storage_limit_days",
    "use_soon_days",
)


def _range_problem(values: dict[str, float]) -> str | None:
    """Return why these limits are unusable, or None when they are fine."""
    if values["temp_min"] >= values["temp_max"]:
        return "temp_min must be below temp_max."
    if values["hum_min"] >= values["hum_max"]:
        return "hum_min must be below hum_max."
    if values["hum_min"] < HUMIDITY_MIN or values["hum_max"] > HUMIDITY_MAX:
        return "Humidity limits must lie between 0 and 100."
    if values["storage_limit_days"] <= 0:
        return "storage_limit_days must be greater than zero."
    if not (0 <= values["use_soon_days"] <= values["storage_limit_days"]):
        return "use_soon_days must be between zero and storage_limit_days."
    return None


def _payload() -> dict:
    if request.is_json:
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}
    return request.form.to_dict()


@bp.get("/thresholds")
@admin_required
def list_thresholds() -> tuple[Response, int]:
    """Every configured food category and its limits."""
    rows = (
        get_db()
        .execute(
            "SELECT category, temp_min, temp_max, hum_min, hum_max, storage_limit_days, "
            "use_soon_days, source_note, updated_at FROM thresholds ORDER BY category"
        )
        .fetchall()
    )
    return ok([dict(row) for row in rows])


@bp.put("/thresholds/<category>")
@admin_required
def save_threshold(category: str) -> tuple[Response, int]:
    """Create or replace one category's limits."""
    payload = _payload()
    values: dict[str, float] = {}
    for field in THRESHOLD_FIELDS:
        raw = payload.get(field)
        if raw is None or raw == "":
            return fail(f"{field} is required.", 422)
        try:
            values[field] = float(raw)
        except (TypeError, ValueError):
            return fail(f"{field} must be a number.", 422)

    source_note = str(payload.get("source_note", "")).strip()
    if not source_note:
        return fail(
            "source_note is required: cite a source or write 'prototype/demo assumption'.", 422
        )

    problem = _range_problem(values)
    if problem:
        return fail(problem, 422)

    conn = get_db()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO thresholds (category, temp_min, temp_max, hum_min, hum_max, "
            "storage_limit_days, use_soon_days, source_note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "ON CONFLICT(category) DO UPDATE SET temp_min = excluded.temp_min, "
            "temp_max = excluded.temp_max, hum_min = excluded.hum_min, "
            "hum_max = excluded.hum_max, storage_limit_days = excluded.storage_limit_days, "
            "use_soon_days = excluded.use_soon_days, source_note = excluded.source_note, "
            "updated_at = excluded.updated_at",
            (
                category.strip(),
                values["temp_min"],
                values["temp_max"],
                values["hum_min"],
                values["hum_max"],
                int(values["storage_limit_days"]),
                int(values["use_soon_days"]),
                source_note,
            ),
        )
    logger.info("threshold for %s updated", category)
    return ok({"category": category.strip(), **values, "source_note": source_note})


@bp.get("/settings")
@admin_required
def get_settings_route() -> tuple[Response, int]:
    """Global settings an administrator may change."""
    return ok({DOOR_OPEN_TIMEOUT_KEY: door_open_timeout_s(get_db())})


@bp.put("/settings")
@admin_required
def save_settings() -> tuple[Response, int]:
    """Update the global settings."""
    payload = _payload()
    raw = payload.get(DOOR_OPEN_TIMEOUT_KEY)
    if raw is None or raw == "":
        return fail(f"{DOOR_OPEN_TIMEOUT_KEY} is required.", 422)
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        return fail(f"{DOOR_OPEN_TIMEOUT_KEY} must be a whole number of seconds.", 422)
    if timeout <= 0:
        return fail(f"{DOOR_OPEN_TIMEOUT_KEY} must be greater than zero.", 422)

    conn = get_db()
    with db.transaction(conn):
        set_setting(conn, DOOR_OPEN_TIMEOUT_KEY, str(timeout))
    return ok({DOOR_OPEN_TIMEOUT_KEY: timeout})


@bp.get("/users")
@admin_required
def list_users() -> tuple[Response, int]:
    """Accounts, without any password material."""
    rows = (
        get_db()
        .execute("SELECT id, username, role, created_at FROM users ORDER BY username")
        .fetchall()
    )
    return ok([dict(row) for row in rows])


@bp.post("/users")
@admin_required
def add_user() -> tuple[Response, int]:
    """Create an account."""
    payload = _payload()
    conn = get_db()
    try:
        with db.transaction(conn):
            user = create_user(
                conn,
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                str(payload.get("role", "")),
            )
    except UserError as exc:
        return fail(str(exc), 422)
    return ok({"id": user.id, "username": user.username, "role": user.role}, 201)


@bp.delete("/users/<int:user_id>")
@admin_required
def remove_user(user_id: int) -> tuple[Response, int]:
    """Delete an account, refusing to remove the last administrator."""
    conn = get_db()
    target = get_by_id(conn, user_id)
    if target is None:
        return fail(f"User {user_id} was not found.", 404)

    # Checked before the self-delete rule below: with a single administrator
    # the only way to reach this route is for that administrator to delete
    # themselves, and "the last administrator" is the useful reason to give.
    if target.is_admin:
        admins = conn.execute(
            "SELECT COUNT(*) AS total FROM users WHERE role = 'admin'"
        ).fetchone()["total"]
        if admins <= 1:
            return fail("The last administrator cannot be deleted.", 422)

    actor = current_user()
    if actor is not None and actor.id == user_id:
        return fail("You cannot delete your own account.", 422)

    with db.transaction(conn):
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    logger.info("deleted user %s", target.username)
    return ok({"id": user_id})


@bp.get("/categories")
@admin_required
def categories() -> tuple[Response, int]:
    """Category names, for the item form's dropdown."""
    rows = get_db().execute("SELECT category FROM thresholds ORDER BY category").fetchall()
    return ok([row["category"] for row in rows])


@bp.get("/simulator")
@admin_required
def simulator_state() -> tuple[Response, int]:
    """Which simulated scenario is running, and which ones exist."""
    settings = get_settings()
    return ok(
        {
            "mode": settings.sensor_mode,
            "scenario": current_scenario(settings),
            "scenarios": list(SIM_SCENARIOS),
        }
    )


@bp.post("/simulator")
@admin_required
def set_simulator_scenario() -> tuple[Response, int]:
    """Switch the simulated scenario the running monitor is producing.

    The scenario is written to a control file that ``monitor.py`` reads on its
    next tick, so the loop changes behaviour without being restarted. This is
    how the test matrix drives a high-temperature or gas run on demand.

    It is refused outright on real hardware: there is nothing to simulate, and
    quietly doing nothing would be worse than saying no.
    """
    settings = get_settings()
    if not settings.simulated:
        return fail("The simulator is not in use; FG_SENSOR_MODE is 'real'.", 409)

    scenario = str(_payload().get("scenario", "")).strip()
    if scenario not in SIM_SCENARIOS:
        return fail(f"Scenario must be one of {', '.join(SIM_SCENARIOS)}.", 422)

    control = settings.sim_control_file
    if control is None:
        return fail("No simulator control file is configured.", 409)

    try:
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(scenario, encoding="utf-8")
    except OSError as exc:
        logger.error("could not write the simulator control file: %s", exc)
        return fail("The simulator control file could not be written.", 500)

    logger.info("simulated scenario set to %s", scenario)
    return ok({"scenario": scenario})
