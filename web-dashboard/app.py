"""Flask application factory.

The web app only ever reads what ``monitor.py`` wrote to SQLite; it never
touches a sensor itself. Both processes share one database file in WAL mode.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import click
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from core import db
from core import rfid as rfid_store
from core.config import Settings, get_settings
from core.users import UserError, seed_admin
from web import database, security
from web.routes import admin as admin_routes
from web.routes import api_admin, api_items, api_zones
from web.routes import auth as auth_routes
from web.routes import main as main_routes
from web.routes import qr as qr_routes
from web.routes import reports as report_routes
from web.routes import rfid as rfid_routes
from web.routes import snapshots as snapshot_routes

logger = logging.getLogger(__name__)

#: How long a signed-in session survives without activity.
SESSION_LIFETIME = timedelta(hours=12)

#: Links shown in the signed-in navigation bar.
#: endpoint, label, icon name
#: The workspace sections, in the order they appear in the sidebar. Scanning is
#: reached from a button on the pages that need it rather than a section of its
#: own, because it is an action, not a place.
NAV_ITEMS = [
    ("main.scan", "Scan", "scan"),
    ("main.dashboard", "Overview", "grid"),
    ("main.inventory", "Food inventory", "box"),
    ("main.sensors", "Sensors & devices", "sensor"),
    ("snapshots.snapshots", "Snapshots", "camera"),
    ("main.alerts", "Alerts & notifications", "bell"),
    ("main.planner", "Consumption planner", "calendar"),
    ("reports.reports", "Reports & insights", "chart"),
]

ADMIN_NAV_ITEMS = [
    ("admin.settings", "Settings", "settings"),
]


def create_app(settings: Settings | None = None, *, db_path: str | Path | None = None) -> Flask:
    """Build and configure the application."""
    settings = settings or get_settings()

    app = Flask(
        __name__,
        template_folder="web/templates",
        static_folder="web/static",
    )

    app.config.update(
        SECRET_KEY=settings.secret_key,
        FG_DB_PATH=Path(db_path) if db_path is not None else settings.db_path,
        FG_DEVICE_ID=settings.device_id,
        FG_LOGIN_FAILURE_DELAY_S=security.DEFAULT_FAILURE_DELAY_S,
        # The session cookie is not readable from JavaScript and is not sent
        # on cross-site requests; Secure is added once TLS is configured.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.tls_enabled,
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    database.init_app(app)
    security.init_app(app)

    app.register_blueprint(auth_routes.bp)
    app.register_blueprint(main_routes.bp)
    app.register_blueprint(admin_routes.bp)
    app.register_blueprint(api_items.bp)
    app.register_blueprint(api_zones.bp)
    app.register_blueprint(api_admin.bp)
    app.register_blueprint(report_routes.bp)
    app.register_blueprint(qr_routes.bp)
    app.register_blueprint(snapshot_routes.bp)
    app.register_blueprint(rfid_routes.bp)

    _register_navigation(app)
    _register_stickers(app)
    _register_error_handlers(app)
    _register_cli(app)

    return app


#: Where the category stickers live, relative to the static folder.
STICKER_DIR = Path("web/static/img/stickers")


#: Image formats a sticker may be supplied in, best first. A category that has
#: more than one file takes the earliest of these, so dropping a .jpg beside an
#: existing .png changes nothing until the .png is removed. PNG and WebP lead
#: because they carry transparency, and the sticker sits on a tinted pill that
#: an opaque rectangle would show through.
STICKER_FORMATS = (".png", ".webp", ".jpg", ".jpeg")


def _register_stickers(app: Flask) -> None:
    """Expose which category stickers actually exist on disk.

    Resolved once at start-up rather than per render, and checked rather than
    assumed: a category an administrator adds later has no illustration yet,
    and a broken image is worse than the generic one.

    The whole filename is resolved here, extension included, so a new set can
    be dropped in as .png, .jpg or .webp without the template caring which.
    """
    folder = Path(app.static_folder or "") / "img" / "stickers"
    available: dict[str, str] = {}
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            suffix = path.suffix.lower()
            if not path.is_file() or suffix not in STICKER_FORMATS:
                continue
            held = available.get(path.stem)
            if held is None or STICKER_FORMATS.index(suffix) < STICKER_FORMATS.index(
                Path(held).suffix.lower()
            ):
                available[path.stem] = path.name

    if "default" not in available:
        logger.warning("no default category sticker found in %s", folder)

    @app.template_global()
    def sticker_name(category: str) -> str:
        """The sticker filename for a category, or the fallback."""
        return available.get(category) or available.get("default") or "default.png"


def _register_navigation(app: Flask) -> None:
    """Expose the navigation to templates, hiding admin links from staff."""

    @app.context_processor
    def _navigation() -> dict[str, object]:
        user = security.current_user()
        items = list(NAV_ITEMS)
        if user is not None and user.is_admin:
            items += ADMIN_NAV_ITEMS
        if user is None:
            return {"nav_items": items}

        # The sidebar and header need these on every signed-in page, so they
        # are gathered once here rather than in each view.
        conn = database.get_db()
        zones = conn.execute("SELECT COUNT(*) AS total FROM zones").fetchone()["total"]
        open_alerts = conn.execute(
            "SELECT COUNT(*) AS total FROM alerts WHERE acknowledged = 0"
        ).fetchone()["total"]
        # Tags tapped in with no details: a banner on every page, and part of
        # the bell's count until someone fills them in.
        pending_tags = [rfid_routes.public(row) for row in rfid_store.list_pending(conn)]
        return {
            "nav_items": items,
            "zone_count": zones,
            "open_alert_count": open_alerts,
            "rfid_pending": pending_tags,
            "bell_count": open_alerts + len(pending_tags),
            "mode_label": "Simulator" if get_settings().simulated else "Live sensors",
        }


def _register_error_handlers(app: Flask) -> None:
    """Answer errors as JSON under /api and as a page everywhere else."""

    @app.errorhandler(HTTPException)
    def _handle(error: HTTPException):
        if request.path.startswith("/api/"):
            response = jsonify({"ok": False, "error": error.description})
            response.status_code = error.code or 500
            return response
        return (
            render_template("error.html", code=error.code, message=error.description),
            error.code or 500,
        )


def _register_cli(app: Flask) -> None:
    """Register management commands."""

    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create the schema and apply any pending migrations."""
        connection = db.bootstrap(app.config["FG_DB_PATH"])
        try:
            click.echo(f"database ready at version {db.schema_version(connection)}")
        finally:
            connection.close()

    @app.cli.command("seed-admin")
    def seed_admin_command() -> None:
        """Create the first administrator from FG_ADMIN_USERNAME / FG_ADMIN_PASSWORD."""
        settings = get_settings()
        connection = db.bootstrap(app.config["FG_DB_PATH"])
        try:
            with db.transaction(connection):
                user = seed_admin(connection, settings.admin_username, settings.admin_password)
        except UserError as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            connection.close()

        if user is None:
            click.echo("administrator already exists; nothing to do")
        else:
            click.echo(f"seeded administrator {user.username!r}")


def check_database(db_path: Path) -> str | None:
    """Return why the database cannot be served, or None when it is ready.

    The web app deliberately does not create the schema: ``monitor.py`` and the
    init-db command own that, and a web process quietly creating tables would
    hide a misconfigured FG_DB_PATH. Opening SQLite creates an empty file
    though, so without this check the first request fails with "no such table"
    and nothing says why.

    An out-of-date database fails the same way and is the more likely of the
    two, because it happens to a database that has been working: a migration
    lands, the file stays where it is, and the next request for a page that
    touches the new table returns 500. So being behind is refused here, at
    start-up, where the message can say which migrations are missing and how
    to apply them.
    """
    if not db_path.is_file():
        return f"{db_path} does not exist."
    try:
        connection = db.connect(db_path)
    except sqlite3.Error as exc:
        return f"{db_path} could not be opened: {exc}"
    try:
        version = db.schema_version(connection)
        if version < 1:
            return f"{db_path} has no FreshGuard schema."
        pending = db.pending_migrations(connection)
        if pending:
            return (
                f"{db_path} is at schema version {version}, but version "
                f"{db.latest_version()} is required. Not applied: {', '.join(pending)}."
            )
    except sqlite3.DatabaseError:
        return f"{db_path} has no FreshGuard schema."
    finally:
        connection.close()
    return None


SETUP_HELP = """
Set the database up, or bring it up to date. init-db does both: it creates
what is missing and applies any migration the file has not had yet, and it
leaves existing data alone.

    python -m flask --app app:create_app init-db

A brand new database also needs its thresholds and its first account:

    python scripts/seed_thresholds.py
    python -m flask --app app:create_app seed-admin

Then start the dashboard again. Run monitor.py alongside it for live readings.
"""


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    logging.basicConfig(level=logging.INFO)
    config = get_settings()

    problem = check_database(config.db_path)
    if problem:
        sys.stderr.write(f"FreshGuard cannot start: {problem}\n{SETUP_HELP}")
        raise SystemExit(1)

    # A browser refuses camera access unless the page came over HTTPS or from
    # localhost, and the scanner is the front door, so TLS is wired up here
    # rather than left as configuration nothing reads. Without a certificate
    # the app still serves plain HTTP and the scanner falls back to the manual
    # identifier box.
    ssl_context = None
    if config.tls_enabled:
        ssl_context = (str(config.tls_cert), str(config.tls_key))
        logging.getLogger(__name__).info("serving HTTPS; the camera scanner will work on the LAN")
    else:
        logging.getLogger(__name__).info(
            "serving plain HTTP; the camera scanner will only work on this machine. "
            "Run scripts/make_cert.py to enable it for other devices."
        )

    create_app(config).run(
        host=config.host,
        port=config.port,
        debug=config.debug,
        ssl_context=ssl_context,
    )
