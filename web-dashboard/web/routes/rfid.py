"""RFID tags tapped in that still need their details.

The ingest worker writes them; these routes only read. Adding the details goes
through the ordinary item form with the tag prefilled (``/item/new?rfid=...``).
"""

from __future__ import annotations

from flask import Blueprint, Response, url_for

from core import rfid as rfid_store
from web.api import ok
from web.database import get_db
from web.security import login_required

bp = Blueprint("rfid", __name__)


def public(row: dict) -> dict:
    """A waiting tag as the page needs it, with where to add its details."""
    return {
        "tag_id": row["tag_id"],
        "short": rfid_store.short_tag(row["tag_id"]),
        "tapped_in_at": row["tapped_in_at"],
        "add_url": url_for("main.new_item", rfid=row["tag_id"]),
    }


@bp.get("/api/rfid/pending")
@login_required
def pending() -> tuple[Response, int]:
    """Tags tapped in with no details yet, oldest first."""
    return ok([public(row) for row in rfid_store.list_pending(get_db())])
