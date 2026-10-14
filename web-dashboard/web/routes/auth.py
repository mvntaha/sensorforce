"""Sign in and sign out."""

from __future__ import annotations

import logging
import time

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from core.users import verify_credentials
from web.database import get_db
from web.security import current_user, login_user, logout_user, throttle, throttle_key

logger = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)

#: Shown for every failed attempt. Never says which half was wrong, so the form
#: cannot be used to discover which usernames exist.
INVALID_CREDENTIALS = "That username and password combination was not recognised."


def _safe_next(target: str | None) -> str:
    """Return a redirect target, refusing anything pointing off this site."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return url_for("main.dashboard")
    return target


@bp.route("/login", methods=["GET", "POST"])
def login() -> Response | str:
    """Render the sign-in form and check submitted credentials."""
    if current_user() is not None:
        return redirect(url_for("main.dashboard"))

    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    key = throttle_key(username)

    delay = throttle.delay_for(key, current_app.config["FG_LOGIN_FAILURE_DELAY_S"])
    if delay:
        time.sleep(delay)

    user = verify_credentials(get_db(), username, password) if username and password else None
    if user is None:
        throttle.record_failure(key)
        logger.warning("failed sign-in for %r from %s", username, request.remote_addr)
        return render_template("login.html", error=INVALID_CREDENTIALS, username=username), 401

    throttle.reset(key)
    login_user(user)
    logger.info("user %r signed in", user.username)
    return redirect(_safe_next(request.args.get("next")))


@bp.post("/logout")
def logout() -> Response:
    """End the session and return to the sign-in page."""
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))
