"""Session handling, CSRF protection, route guards and login throttling.

CSRF is checked centrally in a ``before_request`` hook rather than per view, so
a new state-changing route cannot forget it. Authorisation is enforced here on
the server; the templates only decide what to display.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections import defaultdict
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from werkzeug.wrappers import Response

from core.users import User, get_by_id
from web.database import get_db

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "user_id"
CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

#: Methods that do not change state and therefore need no CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Failed sign-ins are slowed down: base * 2^(failures-1), capped.
DEFAULT_FAILURE_DELAY_S = 0.5
MAX_FAILURE_DELAY_S = 4.0
#: Failures older than this stop counting.
FAILURE_WINDOW_S = 900.0

F = TypeVar("F", bound=Callable[..., Any])


class LoginThrottle:
    """Counts recent failed sign-ins and turns them into a delay.

    Held in memory: FreshGuard runs as a single process on one Pi, and a
    restart clearing the counters is an acceptable trade for not writing a
    failed-login row on every attempt.
    """

    def __init__(self, window_s: float = FAILURE_WINDOW_S) -> None:
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._window_s = window_s

    def _recent(self, key: str, now: float) -> list[float]:
        kept = [at for at in self._failures[key] if now - at < self._window_s]
        self._failures[key] = kept
        return kept

    def record_failure(self, key: str, now: float | None = None) -> None:
        """Note one failed attempt for this key."""
        now = time.monotonic() if now is None else now
        self._recent(key, now).append(now)

    def reset(self, key: str) -> None:
        """Forget this key's failures, called after a successful sign-in."""
        self._failures.pop(key, None)

    def delay_for(self, key: str, base_s: float, now: float | None = None) -> float:
        """Return how long to wait before answering a failed attempt."""
        if base_s <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        failures = len(self._recent(key, now))
        if failures == 0:
            return 0.0
        return min(base_s * (2 ** (failures - 1)), MAX_FAILURE_DELAY_S)


throttle = LoginThrottle()


def throttle_key(username: str) -> str:
    """Identify an attempt by username and client address together."""
    return f"{username.strip().lower()}|{request.remote_addr or '-'}"


# --------------------------------------------------------------------- CSRF
def csrf_token() -> str:
    """Return this session's CSRF token, creating one on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def submitted_csrf_token() -> str:
    """Return the token supplied by the request, from the form or a header."""
    return request.form.get(CSRF_FORM_FIELD) or request.headers.get(CSRF_HEADER) or ""


def csrf_is_valid() -> bool:
    """True when the request carries this session's token."""
    expected = session.get(CSRF_SESSION_KEY)
    supplied = submitted_csrf_token()
    if not expected or not supplied:
        return False
    return hmac.compare_digest(str(expected), str(supplied))


# ------------------------------------------------------------------ session
def current_user() -> User | None:
    """Return the signed-in account for this request, or None."""
    if "current_user" not in g:
        user_id = session.get(SESSION_USER_KEY)
        g.current_user = get_by_id(get_db(), int(user_id)) if user_id is not None else None
    return g.current_user


def login_user(user: User) -> None:
    """Start a session for this account.

    The session is cleared first so a pre-authentication session identifier
    cannot be reused after sign-in.
    """
    session.clear()
    session[SESSION_USER_KEY] = user.id
    session.permanent = True
    g.current_user = user


def logout_user() -> None:
    """End the session and drop the cached user."""
    session.clear()
    g.pop("current_user", None)


# ------------------------------------------------------------------- guards
def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def _deny_anonymous() -> Response:
    if _wants_json():
        response = jsonify({"ok": False, "error": "Authentication required."})
        response.status_code = 401
        return response
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


def login_required(view: F) -> F:
    """Refuse the route unless somebody is signed in."""

    @wraps(view)
    def wrapper(*args: object, **kwargs: object) -> object:
        if current_user() is None:
            return _deny_anonymous()
        return view(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def admin_required(view: F) -> F:
    """Refuse the route unless an administrator is signed in."""

    @wraps(view)
    def wrapper(*args: object, **kwargs: object) -> object:
        user = current_user()
        if user is None:
            return _deny_anonymous()
        if not user.is_admin:
            logger.warning("user %r was refused an admin route: %s", user.username, request.path)
            abort(403)
        return view(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------- app hooks
def init_app(app: Flask) -> None:
    """Install the CSRF check and expose helpers to templates."""

    @app.before_request
    def _reset_request_state() -> None:
        """Drop the cached user at the start of every request.

        current_user() memoises on flask.g, which lives on the application
        context, not the request. Flask reuses an existing application context
        when one is already pushed, so without this the identity from one
        request could be seen by the next.
        """
        g.pop("current_user", None)

    @app.before_request
    def _enforce_csrf() -> None:
        if request.method in SAFE_METHODS or request.endpoint == "static":
            return
        if not csrf_is_valid():
            logger.warning(
                "rejected %s %s: missing or invalid CSRF token", request.method, request.path
            )
            abort(400, description="Missing or invalid CSRF token.")

    @app.context_processor
    def _template_helpers() -> dict[str, object]:
        return {"current_user": current_user(), "csrf_token": csrf_token()}
