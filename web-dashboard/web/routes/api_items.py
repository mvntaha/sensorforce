"""Item CRUD over JSON.

Every write validates server-side and commits the change together with its
outbox row. Deletion is limited to an administrator or the person who created
the item.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, request
from werkzeug.wrappers import Response

from core import items as item_store
from core.items import ItemError
from core.status import item_statuses
from web.api import fail, ok
from web.database import get_db
from web.security import current_user, login_required

logger = logging.getLogger(__name__)

bp = Blueprint("api_items", __name__, url_prefix="/api/items")


def _device_id() -> str:
    return current_app.config.get("FG_DEVICE_ID", "pi-01")


def _payload() -> dict:
    """Return the request body as a mapping, accepting JSON or a form post."""
    if request.is_json:
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}
    return request.form.to_dict()


@bp.get("")
@login_required
def list_items() -> tuple[Response, int]:
    """List items, optionally filtered by zone or category."""
    zone_raw = request.args.get("zone")
    zone_id = None
    if zone_raw:
        try:
            zone_id = int(zone_raw)
        except ValueError:
            return fail("zone must be a whole number.", 400)
    category = request.args.get("category") or None
    return ok(item_store.list_items(get_db(), zone_id=zone_id, category=category))


@bp.get("/<qr_id>")
def get_item(qr_id: str) -> tuple[Response, int]:
    """Return one item by its QR identifier.

    Deliberately open, because the scanner beside the fridge is open: someone
    holding a container has to be told what it is. It answers for exactly one
    identifier that the caller already has in their hand, and the identifier
    space is small, so the listing endpoint above stays behind the sign-in
    rather than letting this one be walked to enumerate the kitchen.
    """
    item = item_store.get(get_db(), qr_id)
    if item is None:
        return fail(f"{qr_id} was not found.", 404)

    status = next((s for s in item_statuses(get_db()) if s.item["qr_id"] == item["qr_id"]), None)
    if status is not None:
        return ok(status.as_dict())
    return ok(item)


@bp.post("")
def create_item() -> tuple[Response, int]:
    """Create an item.

    Open, for the same reason the consume endpoint is: the screen beside the
    fridge has to be able to register the carton someone just put in, and a
    kitchen shares one fridge rather than one account. An anonymous creation
    stores no author, exactly as an anonymous consumption stores none.

    Openness costs less here than it does on the consume endpoint, because
    this destroys nothing. Every field is still validated server-side, the
    identifier must be free, and the session's CSRF token is still required.
    """
    conn = get_db()
    user = current_user()
    try:
        data = item_store.validate(conn, _payload())
        created = item_store.create(
            conn, data, created_by=user.id if user else None, device_id=_device_id()
        )
    except ItemError as exc:
        return fail(str(exc), 422)
    return ok(created, 201)


@bp.put("/<qr_id>")
@login_required
def update_item(qr_id: str) -> tuple[Response, int]:
    """Replace an item's details. The identifier comes from the URL."""
    conn = get_db()
    if item_store.get(conn, qr_id) is None:
        return fail(f"{qr_id} was not found.", 404)
    try:
        data = item_store.validate(conn, _payload(), qr_id=qr_id)
        updated = item_store.update(conn, data, device_id=_device_id())
    except ItemError as exc:
        return fail(str(exc), 422)
    return ok(updated)


@bp.delete("/<qr_id>")
@login_required
def delete_item(qr_id: str) -> tuple[Response, int]:
    """Delete an item. Restricted to an administrator or its creator."""
    conn = get_db()
    user = current_user()
    existing = item_store.get(conn, qr_id)
    if existing is None:
        return fail(f"{qr_id} was not found.", 404)

    if user is None or not (user.is_admin or existing["created_by"] == user.id):
        logger.warning("user %s was refused deletion of %s", user.username if user else "?", qr_id)
        return fail("Only an administrator or the item's creator may delete it.", 403)

    try:
        removed = item_store.delete(conn, qr_id, device_id=_device_id())
    except ItemError as exc:
        return fail(str(exc), 404)
    return ok(removed)


@bp.post("/<qr_id>/consume")
def consume_item(qr_id: str) -> tuple[Response, int]:
    """Record that an item was eaten or thrown away, and remove it.

    Open to anyone at the fridge, because that is the point of the kiosk: a
    person who has just taken the last of the milk should be able to say so
    without finding an account. An anonymous record stores no user, and the
    history shows it as coming from the kiosk rather than inventing an author.

    It is still a destructive, unauthenticated endpoint, so it is narrow on
    purpose: it acts on one identifier the caller already holds, it writes a
    history row before removing anything, and it still requires the session's
    CSRF token, which stops another site from firing it at a browser that
    happens to have FreshGuard open.

    The history keeps its own copy, so the container's identifier is free to
    be printed on something else. This is a record of what the kitchen did,
    never FreshGuard judging that the food was unsafe.
    """
    conn = get_db()
    payload = _payload()
    outcome = str(payload.get("outcome", "consumed")).strip().lower()

    if item_store.get(conn, qr_id) is None:
        return fail(f"{qr_id} was not found.", 404)

    # The verdict is frozen into the log, because the readings that produced
    # it will have rolled off by the time anyone reads the history.
    identifier = qr_id.strip().upper()
    current = next((s for s in item_statuses(conn) if s.item["qr_id"] == identifier), None)
    user = current_user()

    try:
        entry = item_store.consume(
            conn,
            identifier,
            outcome,
            device_id=_device_id(),
            status=str(current.assessment.status) if current else "",
            days_stored=current.days_stored if current else None,
            created_by=user.id if user else None,
        )
    except ItemError as exc:
        return fail(str(exc), 422)
    return ok(entry)
