"""Authentication, CSRF, route guards and the first-administrator seed."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from core import db
from core.config import Settings
from core.users import UserError, create_user, seed_admin, verify_credentials
from web.security import throttle

ADMIN_PASSWORD = "correct-horse-battery"  # noqa: S105 - test fixture, not a real credential
STAFF_PASSWORD = "staff-password-1234"  # noqa: S105 - test fixture, not a real credential

CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Flask]:
    """An application backed by an empty database, with login delays disabled."""
    database = tmp_path / "auth.db"
    db.bootstrap(database).close()

    settings = Settings.from_env({"FG_SECRET_KEY": "test-secret-key-that-is-long-enough"})
    application = create_app(settings, db_path=database)
    application.config.update(TESTING=True, FG_LOGIN_FAILURE_DELAY_S=0)

    throttle.reset("")
    throttle._failures.clear()

    with application.app_context():
        yield application


@pytest.fixture
def conn(app: Flask) -> Iterator[sqlite3.Connection]:
    connection = db.connect(app.config["FG_DB_PATH"])
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def admin(conn: sqlite3.Connection) -> None:
    create_user(conn, "admin", ADMIN_PASSWORD, "admin")


@pytest.fixture
def staff(conn: sqlite3.Connection) -> None:
    create_user(conn, "cook", STAFF_PASSWORD, "staff")


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def csrf_from(client: FlaskClient, path: str = "/login") -> str:
    """Fetch a page and return the CSRF token embedded in its form.

    Signed out, the token comes from the sign-in form; signed in, from the
    sign-out form in the navigation bar.
    """
    page = client.get(path)
    match = CSRF_INPUT.search(page.get_data(as_text=True))
    assert match, f"{path} did not carry a CSRF token"
    return match.group(1)


def csrf_from_login_page(client: FlaskClient) -> str:
    """Fetch the sign-in page and return the token it embedded."""
    return csrf_from(client, "/login")


def sign_in(client: FlaskClient, username: str, password: str):
    token = csrf_from_login_page(client)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
    )


class TestLogin:
    def test_the_form_renders_for_an_anonymous_visitor(self, client: FlaskClient) -> None:
        page = client.get("/login")
        assert page.status_code == 200
        assert "Sign in" in page.get_data(as_text=True)

    def test_correct_credentials_sign_the_user_in(self, client: FlaskClient, admin: None) -> None:
        response = sign_in(client, "admin", ADMIN_PASSWORD)
        assert response.status_code == 302
        assert response.headers["Location"] == "/dashboard"

        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        body = dashboard.get_data(as_text=True)
        assert "<strong>Overview</strong>" in body, "the breadcrumb names the page"
        assert "admin" in body, "the signed-in account is named in the sidebar"

    def test_a_wrong_password_is_refused(self, client: FlaskClient, admin: None) -> None:
        response = sign_in(client, "admin", "not-the-password")
        assert response.status_code == 401
        body = response.get_data(as_text=True)
        assert "was not recognised" in body
        # Still anonymous.
        assert client.get("/dashboard").status_code == 302

    def test_an_unknown_username_gives_the_same_message(self, client: FlaskClient) -> None:
        """The form must not reveal which usernames exist."""
        unknown = sign_in(client, "nobody", "whatever-password")
        assert unknown.status_code == 401
        assert "was not recognised" in unknown.get_data(as_text=True)

    def test_an_empty_password_is_refused(self, client: FlaskClient, admin: None) -> None:
        assert sign_in(client, "admin", "").status_code == 401

    def test_usernames_are_matched_without_case(self, client: FlaskClient, admin: None) -> None:
        assert sign_in(client, "ADMIN", ADMIN_PASSWORD).status_code == 302

    def test_a_signed_in_user_is_sent_to_the_dashboard(
        self, client: FlaskClient, admin: None
    ) -> None:
        sign_in(client, "admin", ADMIN_PASSWORD)
        assert client.get("/login").status_code == 302

    def test_the_next_target_must_be_a_local_path(self, client: FlaskClient, admin: None) -> None:
        token = csrf_from_login_page(client)
        response = client.post(
            "/login?next=https://evil.example.com/",
            data={"username": "admin", "password": ADMIN_PASSWORD, "csrf_token": token},
        )
        assert response.headers["Location"] == "/dashboard"


class TestCsrf:
    def test_a_post_without_a_token_is_rejected(self, client: FlaskClient, admin: None) -> None:
        client.get("/login")  # establish a session so only the token is missing
        response = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
        assert response.status_code == 400
        assert client.get("/dashboard").status_code == 302, "a CSRF failure must not sign anyone in"

    def test_a_wrong_token_is_rejected(self, client: FlaskClient, admin: None) -> None:
        client.get("/login")
        response = client.post(
            "/login",
            data={"username": "admin", "password": ADMIN_PASSWORD, "csrf_token": "forged"},
        )
        assert response.status_code == 400

    def test_logout_is_protected_too(self, client: FlaskClient, admin: None) -> None:
        sign_in(client, "admin", ADMIN_PASSWORD)
        assert client.post("/logout").status_code == 400
        assert (
            client.get("/dashboard").status_code == 200
        ), "the session must survive a rejected logout"

    def test_a_token_from_a_header_is_accepted(self, client: FlaskClient, admin: None) -> None:
        token = csrf_from_login_page(client)
        response = client.post(
            "/login",
            data={"username": "admin", "password": ADMIN_PASSWORD},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 302

    def test_get_requests_need_no_token(self, client: FlaskClient) -> None:
        assert client.get("/login").status_code == 200


class TestGuards:
    def test_anonymous_visitors_are_sent_to_the_sign_in_page(self, client: FlaskClient) -> None:
        response = client.get("/dashboard")
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_api_paths_answer_json_rather_than_redirecting(self, client: FlaskClient) -> None:
        response = client.get("/api/anything")
        assert response.status_code == 404
        assert response.is_json

    def test_an_admin_reaches_the_admin_page(self, client: FlaskClient, admin: None) -> None:
        sign_in(client, "admin", ADMIN_PASSWORD)
        assert client.get("/admin/").status_code == 200

    def test_staff_are_blocked_from_the_admin_page(self, client: FlaskClient, staff: None) -> None:
        sign_in(client, "cook", STAFF_PASSWORD)
        assert client.get("/dashboard").status_code == 200, "staff may still use the dashboard"
        assert client.get("/admin/").status_code == 403

    def test_anonymous_visitors_are_not_told_the_admin_page_exists(
        self, client: FlaskClient
    ) -> None:
        assert client.get("/admin/").status_code == 302

    def test_the_admin_link_is_hidden_from_staff(self, client: FlaskClient, staff: None) -> None:
        sign_in(client, "cook", STAFF_PASSWORD)
        assert "/admin/" not in client.get("/dashboard").get_data(as_text=True)


class TestSession:
    def test_the_session_cookie_is_http_only(self, app: Flask) -> None:
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_the_cookie_carries_the_httponly_flag_in_practice(
        self, client: FlaskClient, admin: None
    ) -> None:
        response = sign_in(client, "admin", ADMIN_PASSWORD)
        cookies = response.headers.getlist("Set-Cookie")
        assert any("HttpOnly" in cookie for cookie in cookies)

    def test_signing_in_replaces_the_session_identifier(
        self, client: FlaskClient, admin: None
    ) -> None:
        """Guards against session fixation."""
        before = csrf_from_login_page(client)
        client.post(
            "/login",
            data={"username": "admin", "password": ADMIN_PASSWORD, "csrf_token": before},
        )
        # Signed in, the sign-out form carries the session's current token.
        assert csrf_from(client, "/") != before

    def test_logout_ends_the_session(self, client: FlaskClient, admin: None) -> None:
        sign_in(client, "admin", ADMIN_PASSWORD)
        token = csrf_from(client, "/")
        assert client.post("/logout", data={"csrf_token": token}).status_code == 302
        assert client.get("/dashboard").status_code == 302


class TestThrottle:
    def test_repeated_failures_increase_the_delay(self) -> None:
        from web.security import LoginThrottle

        local = LoginThrottle()
        assert local.delay_for("k", 0.5, now=0.0) == 0.0
        local.record_failure("k", now=0.0)
        first = local.delay_for("k", 0.5, now=0.0)
        local.record_failure("k", now=0.0)
        second = local.delay_for("k", 0.5, now=0.0)
        assert second > first > 0

    def test_the_delay_is_capped(self) -> None:
        from web.security import MAX_FAILURE_DELAY_S, LoginThrottle

        local = LoginThrottle()
        for _ in range(20):
            local.record_failure("k", now=0.0)
        assert local.delay_for("k", 0.5, now=0.0) == MAX_FAILURE_DELAY_S

    def test_a_successful_sign_in_clears_the_count(self) -> None:
        from web.security import LoginThrottle

        local = LoginThrottle()
        local.record_failure("k", now=0.0)
        local.reset("k")
        assert local.delay_for("k", 0.5, now=0.0) == 0.0

    def test_old_failures_stop_counting(self) -> None:
        from web.security import LoginThrottle

        local = LoginThrottle(window_s=10.0)
        local.record_failure("k", now=0.0)
        assert local.delay_for("k", 0.5, now=100.0) == 0.0

    def test_no_delay_is_applied_when_disabled(self) -> None:
        from web.security import LoginThrottle

        local = LoginThrottle()
        local.record_failure("k", now=0.0)
        assert local.delay_for("k", 0.0, now=0.0) == 0.0


class TestSeedAdmin:
    def test_seeds_the_first_administrator(self, conn: sqlite3.Connection) -> None:
        user = seed_admin(conn, "admin", ADMIN_PASSWORD)
        assert user is not None
        assert user.is_admin
        assert verify_credentials(conn, "admin", ADMIN_PASSWORD) is not None

    @pytest.mark.parametrize("password", ["", "   ", "\t\n"])
    def test_refuses_an_empty_password(self, conn: sqlite3.Connection, password: str) -> None:
        with pytest.raises(UserError, match="FG_ADMIN_PASSWORD"):
            seed_admin(conn, "admin", password)
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    def test_refuses_an_empty_username(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(UserError, match="FG_ADMIN_USERNAME"):
            seed_admin(conn, "  ", ADMIN_PASSWORD)

    def test_is_safe_to_run_twice(self, conn: sqlite3.Connection) -> None:
        assert seed_admin(conn, "admin", ADMIN_PASSWORD) is not None
        assert seed_admin(conn, "admin", "a-different-password") is None
        # The original password still works; reseeding did not overwrite it.
        assert verify_credentials(conn, "admin", ADMIN_PASSWORD) is not None

    def test_the_password_is_stored_only_as_a_hash(self, conn: sqlite3.Connection) -> None:
        seed_admin(conn, "admin", ADMIN_PASSWORD)
        stored = conn.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
        assert ADMIN_PASSWORD not in stored
        assert stored.startswith(("scrypt:", "pbkdf2:"))

    def test_the_user_object_carries_no_hash(self, conn: sqlite3.Connection) -> None:
        user = seed_admin(conn, "admin", ADMIN_PASSWORD)
        assert "password" not in repr(user).lower()


class TestUsers:
    def test_a_duplicate_username_is_refused(self, conn: sqlite3.Connection) -> None:
        create_user(conn, "sam", STAFF_PASSWORD, "staff")
        with pytest.raises(UserError, match="already taken"):
            create_user(conn, "sam", STAFF_PASSWORD, "staff")

    def test_an_unknown_role_is_refused(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(UserError, match="Role must be"):
            create_user(conn, "sam", STAFF_PASSWORD, "superuser")

    def test_a_blank_password_is_refused(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(UserError, match="password is required"):
            create_user(conn, "sam", "   ", "staff")
