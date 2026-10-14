"""User accounts and password handling.

Passwords are only ever stored as a werkzeug hash. Nothing in this module
returns a hash to its caller, so a hash cannot leak into a template, a log line
or a JSON response by accident.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

ROLES = ("admin", "staff")

#: Passwords shorter than this are accepted but logged as a warning. Only an
#: empty password is refused outright.
RECOMMENDED_PASSWORD_LENGTH = 12

#: Compared against when the username is unknown, so a failed login takes about
#: as long either way and cannot be used to enumerate accounts.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


class UserError(ValueError):
    """Raised when an account cannot be created as requested."""


@dataclass(frozen=True, slots=True)
class User:
    """An authenticated account. Deliberately carries no password hash."""

    id: int
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        """True when this account may reach admin-only routes."""
        return self.role == "admin"


def _row_to_user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(id=row["id"], username=row["username"], role=row["role"])


def hash_password(password: str) -> str:
    """Return a salted hash. Refuses an empty or whitespace-only password."""
    if not password or not password.strip():
        raise UserError("A password is required.")
    if len(password) < RECOMMENDED_PASSWORD_LENGTH:
        logger.warning("A password shorter than %d characters was set", RECOMMENDED_PASSWORD_LENGTH)
    return generate_password_hash(password)


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str) -> User:
    """Create an account and return it.

    Raises :class:`UserError` for a blank username, a blank password, an
    unknown role or a username that is already taken.
    """
    username = username.strip()
    if not username:
        raise UserError("A username is required.")
    if role not in ROLES:
        raise UserError(f"Role must be one of {', '.join(ROLES)}.")

    password_hash = hash_password(password)
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, password_hash, role),
        )
    except sqlite3.IntegrityError as exc:
        raise UserError(f"The username {username!r} is already taken.") from exc
    return User(id=int(cursor.lastrowid), username=username, role=role)


def get_by_id(conn: sqlite3.Connection, user_id: int) -> User | None:
    """Return the account with this id, or None."""
    row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row)


def get_by_username(conn: sqlite3.Connection, username: str) -> User | None:
    """Return the account with this username, or None. Case-insensitive."""
    row = conn.execute(
        "SELECT id, username, role FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()
    return _row_to_user(row)


def verify_credentials(conn: sqlite3.Connection, username: str, password: str) -> User | None:
    """Return the account when the password matches, otherwise None.

    An unknown username still runs a hash comparison so that the response time
    does not reveal whether the account exists.
    """
    row = conn.execute(
        "SELECT id, username, role, password_hash FROM users WHERE username = ?",
        (username.strip(),),
    ).fetchone()

    if row is None:
        check_password_hash(_DUMMY_HASH, password)
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return _row_to_user(row)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    """Replace an account's password."""
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id)
    )


def count_users(conn: sqlite3.Connection) -> int:
    """Return how many accounts exist."""
    return int(conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"])


def seed_admin(conn: sqlite3.Connection, username: str, password: str) -> User | None:
    """Create the first administrator from configuration.

    Refuses to seed an empty password, so a misconfigured deployment fails
    loudly instead of creating an account that anyone could sign in to. Doing
    nothing and returning None when the account already exists makes this safe
    to run on every start.
    """
    username = (username or "").strip()
    if not username:
        raise UserError("FG_ADMIN_USERNAME must be set to seed an administrator.")
    if not password or not password.strip():
        raise UserError(
            "FG_ADMIN_PASSWORD must be set to a non-empty value to seed an administrator."
        )

    existing = get_by_username(conn, username)
    if existing is not None:
        logger.info("administrator %r already exists; not reseeding", username)
        return None

    user = create_user(conn, username, password, "admin")
    logger.info("seeded administrator %r", username)
    return user
