"""QR stickers, rendered on demand.

The sticker encodes the item page's full URL, so scanning it with any camera
app opens FreshGuard directly rather than showing a bare identifier.
"""

from __future__ import annotations

import io
import logging

from flask import Blueprint, abort, request, send_file
from werkzeug.wrappers import Response

from core.items import QR_PATTERN
from web.security import login_required

logger = logging.getLogger(__name__)

bp = Blueprint("qr", __name__, url_prefix="/qr")

#: Cache stickers for a day: the image for an identifier never changes.
CACHE_SECONDS = 86400


def sticker_url(qr_id: str) -> str:
    """The URL a sticker encodes, absolute so any camera app can follow it."""
    return request.url_root.rstrip("/") + f"/item/{qr_id}"


@bp.get("/<qr_id>.png")
@login_required
def sticker(qr_id: str) -> Response:
    """Return the QR code for one identifier as a PNG."""
    identifier = qr_id.strip().upper()
    if not QR_PATTERN.match(identifier):
        abort(404, description=f"{qr_id} is not a FreshGuard identifier.")

    import qrcode

    image = qrcode.make(sticker_url(identifier))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    response = send_file(buffer, mimetype="image/png", download_name=f"{identifier}.png")
    response.headers["Cache-Control"] = f"private, max-age={CACHE_SECONDS}"
    return response
