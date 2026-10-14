"""The Snapshots page: ask the fridge for a photograph, and keep the ones taken.

Like every other page, this only touches SQLite. Asking records a request; the
ingest worker passes it to the Pi and stores the photograph that comes back.
The page polls the request until it settles.
"""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.wrappers import Response as WerkzeugResponse

from core import snapshots as store
from web.api import fail, ok
from web.database import get_db
from web.security import current_user, login_required

logger = logging.getLogger(__name__)

bp = Blueprint("snapshots", __name__)


def _device() -> str:
    return current_app.config["FG_DEVICE_ID"]


def _public(row: dict) -> dict:
    """A snapshot as the page needs it, with the addresses to show and save it."""
    return {
        "id": row["id"],
        "taken_at": row["taken_at"],
        "taken_label": store.taken_label(row),
        "size_bytes": row["size_bytes"],
        "url": url_for("snapshots.snapshot_image", snapshot_id=row["id"]),
        "download_url": url_for("snapshots.snapshot_image", snapshot_id=row["id"], download=1),
        "download_name": store.download_name(row),
    }


def _public_request(row: dict | None) -> dict | None:
    if row is None:
        return None
    snapshot = None
    if row.get("snapshot_id") and row.get("snapshot_taken_at"):
        found = store.get_snapshot(get_db(), row["snapshot_id"])
        snapshot = _public(found) if found else None
    return {
        "id": row["id"],
        "status": row["status"],
        "requested_at": row["requested_at"],
        "finished_at": row.get("finished_at"),
        "error": row.get("error"),
        "snapshot": snapshot,
    }


# ------------------------------------------------------------------- the page
@bp.get("/snapshots")
@login_required
def snapshots() -> str:
    """The capture button and every photograph kept."""
    conn = get_db()
    rows = store.list_snapshots(conn, _device())
    return render_template(
        "snapshots.html",
        active_page="snapshots.snapshots",
        page_title="Snapshots",
        device_id=_device(),
        snapshots=[_public(row) for row in rows],
        open_request=_public_request(store.open_request(conn, _device())),
        timeout_s=store.REQUEST_TIMEOUT_S,
    )


@bp.get("/snapshots/<int:snapshot_id>.jpg")
@login_required
def snapshot_image(snapshot_id: int) -> WerkzeugResponse:
    """One kept photograph, shown in the page or, with ?download=1, saved."""
    row = store.get_snapshot(get_db(), snapshot_id)
    if row is None:
        abort(404)
    try:
        path = store.snapshot_file(row)
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)

    download = request.args.get("download") == "1"
    response = send_file(
        path,
        mimetype="image/jpeg",
        as_attachment=download,
        download_name=store.download_name(row),
        max_age=0,
    )
    # A photograph of the inside of a home: kept out of shared caches.
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


# ----------------------------------------------------------------------- API
@bp.get("/api/snapshots")
@login_required
def list_api() -> tuple[Response, int]:
    """Every kept photograph, newest first."""
    rows = store.list_snapshots(get_db(), _device())
    return ok([_public(row) for row in rows])


@bp.post("/api/snapshots/capture")
@login_required
def capture() -> tuple[Response, int]:
    """Ask the fridge for a photograph. Returns the request to poll."""
    user = current_user()
    try:
        row = store.request_capture(get_db(), _device(), user.id if user else None)
    except store.SnapshotError as exc:
        return fail(str(exc), 429)
    return ok(_public_request(row), 202)


@bp.get("/api/snapshots/requests/<int:request_id>")
@login_required
def request_state(request_id: int) -> tuple[Response, int]:
    """Where one request has got to: pending, sent, done or failed."""
    row = store.request_status(get_db(), request_id)
    if row is None or row["device_id"] != _device():
        return fail(f"Snapshot request {request_id} was not found.", 404)
    return ok(_public_request(row))


@bp.delete("/api/snapshots/<int:snapshot_id>")
@login_required
def delete(snapshot_id: int) -> tuple[Response, int]:
    """Remove a photograph from the page, the disk and, shortly, the cloud."""
    if not store.delete_snapshot(get_db(), snapshot_id):
        return fail(f"Snapshot {snapshot_id} was not found.", 404)
    return ok({"id": snapshot_id, "deleted": True})
