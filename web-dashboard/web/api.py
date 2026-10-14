"""JSON response helpers.

Every JSON endpoint answers with the same envelope, so the browser can treat
success and failure uniformly:

    {"ok": true,  "data": ...}
    {"ok": false, "error": "message"}
"""

from __future__ import annotations

from typing import Any

from flask import jsonify
from werkzeug.wrappers import Response


def ok(data: Any = None, status: int = 200) -> tuple[Response, int]:
    """Return a successful JSON response."""
    return jsonify({"ok": True, "data": data}), status


def fail(error: str, status: int = 400) -> tuple[Response, int]:
    """Return a failed JSON response with a human-readable message."""
    return jsonify({"ok": False, "error": error}), status
