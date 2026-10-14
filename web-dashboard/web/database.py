"""Per-request SQLite connection handling.

Kept out of ``core/db.py`` so that ``monitor.py`` never has to import Flask.
"""

from __future__ import annotations

import sqlite3

from flask import Flask, current_app, g

from core import db


def get_db() -> sqlite3.Connection:
    """Return this request's connection, opening one on first use."""
    if "db" not in g:
        g.db = db.connect(current_app.config["FG_DB_PATH"])
    return g.db


def close_db(_exception: BaseException | None = None) -> None:
    """Close the request's connection if one was opened."""
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_app(app: Flask) -> None:
    """Close the connection at the end of every request."""
    app.teardown_appcontext(close_db)
