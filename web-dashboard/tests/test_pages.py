"""The rendered pages, the QR stickers and the sticker generator."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from core import db
from core.config import Settings
from core.users import create_user
from scripts.generate_qr import identifier, target_url, write_sheet, write_singles
from scripts.seed_thresholds import seed
from web.security import throttle

ADMIN_PASSWORD = "correct-horse-battery"  # noqa: S105 - test fixture
STAFF_PASSWORD = "staff-password-1234"  # noqa: S105 - test fixture

DISCLAIMER = "FreshGuard does not certify food safety."

#: The pages behind the sign-in. The kiosk pages below are deliberately not
#: in this list; the boundary between the two has its own tests in test_api.py.
PAGES = [
    "/dashboard",
    "/inventory",
    "/sensors",
    "/alerts",
    "/planner",
    "/reports",
    "/item/FG-0001",
]

#: The kiosk: the scanner and the wizard it hands an unknown label to. Open,
#: so they are rendered and checked like every other page but never asserted
#: to redirect.
OPEN_PAGES = ["/", "/item/new"]

#: Everything that renders a page, whoever is asking.
ALL_PAGES = [*PAGES, *OPEN_PAGES, "/admin/"]


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Flask]:
    database = tmp_path / "pages.db"
    connection = db.bootstrap(database)
    seed(connection)
    create_user(connection, "admin", ADMIN_PASSWORD, "admin")
    create_user(connection, "cook", STAFF_PASSWORD, "staff")
    connection.close()

    settings = Settings.from_env(
        {
            "FG_SECRET_KEY": "test-secret-key-that-is-long-enough",
            # Keep the simulator's control file inside the test's own directory;
            # it defaults to sitting beside the database, which here is the repo.
            "FG_SIM_CONTROL_FILE": str(tmp_path / "sim_scenario"),
        }
    )
    application = create_app(settings, db_path=database)
    application.config.update(TESTING=True, FG_LOGIN_FAILURE_DELAY_S=0)
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


def sign_in(client: FlaskClient, username: str, password: str) -> str:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    client.post("/login", data={"username": username, "password": password, "csrf_token": token})
    home = client.get("/dashboard")
    match = re.search(r'name="csrf_token" value="([^"]+)"', home.get_data(as_text=True))
    return match.group(1) if match else token


@pytest.fixture
def client(app: Flask) -> tuple[FlaskClient, str]:
    session = app.test_client()
    return session, sign_in(session, "admin", ADMIN_PASSWORD)


def add_item(client: FlaskClient, token: str, conn: sqlite3.Connection, **overrides) -> None:
    zone = conn.execute("SELECT id FROM zones LIMIT 1").fetchone()["id"]
    payload = {
        "qr_id": "FG-0001",
        "name": "Milk",
        "category": "dairy",
        "quantity": 2,
        "unit": "L",
        "zone_id": zone,
        "storage_date": "2026-09-20",
        "expiry_date": "2099-01-01",
    }
    payload.update(overrides)
    client.post("/api/items", json=payload, headers={"X-CSRF-Token": token})


def add_reading(conn: sqlite3.Connection, **overrides) -> None:
    zone = conn.execute("SELECT id FROM zones LIMIT 1").fetchone()["id"]
    row = {
        "zone_id": zone,
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "temp_c": 4.0,
        "humidity": 55.0,
        "gas_raw": 1200,
        "door_open": 0,
        "temp_ok": 1,
        "hum_ok": 1,
        "gas_ok": 1,
    }
    row.update(overrides)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO readings (zone_id, ts, temp_c, humidity, gas_raw, door_open, "
            "temp_ok, hum_ok, gas_ok) VALUES (:zone_id, :ts, :temp_c, :humidity, :gas_raw, "
            ":door_open, :temp_ok, :hum_ok, :gas_ok)",
            row,
        )


class TestAccess:
    @pytest.mark.parametrize("url", [*PAGES, "/admin/"])
    def test_anonymous_visitors_are_redirected(self, app: Flask, url: str) -> None:
        response = app.test_client().get(url)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    @pytest.mark.parametrize("url", [*PAGES, *OPEN_PAGES])
    def test_a_signed_in_user_can_open_every_page(self, client, url: str) -> None:
        session, _token = client
        assert session.get(url).status_code == 200

    def test_staff_cannot_open_the_admin_page(self, app: Flask) -> None:
        session = app.test_client()
        sign_in(session, "cook", STAFF_PASSWORD)
        assert session.get("/admin/").status_code == 403


class TestDisclaimer:
    @pytest.mark.parametrize("url", ALL_PAGES)
    def test_every_page_carries_the_disclaimer(self, client, url: str) -> None:
        """SRS rule 2: the dashboard must never imply a safety verdict."""
        session, _token = client
        assert DISCLAIMER in session.get(url).get_data(as_text=True)

    def test_the_sign_in_page_carries_it_too(self, app: Flask) -> None:
        assert DISCLAIMER in app.test_client().get("/login").get_data(as_text=True)

    @pytest.mark.parametrize("url", [*PAGES, *OPEN_PAGES])
    def test_no_page_claims_food_is_safe(self, client, url: str) -> None:
        session, _token = client
        body = session.get(url).get_data(as_text=True).lower()
        assert "is safe" not in body
        assert "unsafe" not in body


class TestDashboard:
    def test_it_shows_the_latest_reading(self, client, conn) -> None:
        session, _token = client
        add_reading(conn, temp_c=6.5)
        assert "6.5" in session.get("/dashboard").get_data(as_text=True)

    def test_a_missing_value_is_shown_as_a_dash_not_a_zero(self, client, conn) -> None:
        session, _token = client
        add_reading(conn, temp_c=None, temp_ok=0)
        body = session.get("/dashboard").get_data(as_text=True)
        assert "—" in body

    def test_gas_is_labelled_relative_and_never_ppm(self, client, conn) -> None:
        """SRS rule 3: no ppm unless the sensor is calibrated."""
        session, _token = client
        add_reading(conn)
        body = session.get("/dashboard").get_data(as_text=True)
        assert "Gas (relative)" in body
        assert "ppm" not in body.lower()

    def test_a_faulty_sensor_is_reported_rather_than_hidden(self, client, conn) -> None:
        session, _token = client
        add_item(session, client[1], conn)
        for _ in range(4):
            add_reading(conn, temp_c=None, temp_ok=0)
        body = session.get("/dashboard").get_data(as_text=True)
        assert "Sensor Fault/Data Unavailable" in body

    def test_the_use_first_list_appears(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn, expiry_date="2026-09-25")
        assert "Use these first" in session.get("/dashboard").get_data(as_text=True)

    def test_it_loads_chart_js_from_the_vendor_folder(self, client) -> None:
        """Nothing may be fetched from a CDN; the LAN has no internet."""
        session, _token = client
        body = session.get("/dashboard").get_data(as_text=True)
        assert "vendor/chart.umd.min.js" in body
        assert "cdn." not in body


class TestInventory:
    def test_stored_items_are_listed(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)
        body = session.get("/inventory").get_data(as_text=True)
        assert "Milk" in body
        assert "FG-0001" in body

    def test_each_row_carries_its_status_and_zone_for_filtering(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)
        add_reading(conn)
        body = session.get("/inventory").get_data(as_text=True)
        assert 'data-status="' in body
        assert 'data-zone="' in body

    def test_status_is_shown_as_text_not_only_colour(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)
        for _ in range(4):
            add_reading(conn)
        assert "Fresh/Normal" in session.get("/inventory").get_data(as_text=True)


class TestItemPage:
    def test_an_unknown_identifier_offers_the_creation_form(self, client) -> None:
        session, _token = client
        body = session.get("/item/FG-0042").get_data(as_text=True)
        assert "Store something here" in body
        assert 'data-mode="create"' in body

    def test_a_known_identifier_shows_the_item(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)
        body = session.get("/item/FG-0001").get_data(as_text=True)
        assert 'data-mode="update"' in body
        assert "Remove this item" in body

    def test_deletion_needs_a_confirmation_step(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)
        body = session.get("/item/FG-0001").get_data(as_text=True)
        assert 'id="fg-delete-confirm"' in body

    def test_a_malformed_identifier_is_refused(self, client) -> None:
        session, _token = client
        assert session.get("/item/NOPE").status_code == 404

    def test_lower_case_identifiers_are_accepted(self, client) -> None:
        session, _token = client
        assert "FG-0042" in session.get("/item/fg-0042").get_data(as_text=True)

    def test_manual_lookup_redirects_to_the_item(self, client) -> None:
        session, _token = client
        response = session.get("/item?qr_id=fg-0009")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/item/FG-0009")

    def test_manual_lookup_rejects_rubbish(self, client) -> None:
        session, _token = client
        response = session.get("/item?qr_id=drop-tables")
        assert response.headers["Location"].startswith("/?error=")


class TestScanPage:
    """The scanner is the front door, so it is what "/" serves."""

    def test_the_manual_box_is_always_present(self, client) -> None:
        """The camera needs HTTPS, so typing the identifier must always work."""
        session, _token = client
        body = session.get("/").get_data(as_text=True)
        assert 'id="field-qr_id"' in body
        assert "type the identifier" in body.lower()

    def test_it_loads_the_scanner_from_the_vendor_folder(self, client) -> None:
        session, _token = client
        body = session.get("/").get_data(as_text=True)
        assert "vendor/html5-qrcode.min.js" in body
        assert "https://" not in body

    def test_it_carries_a_csrf_token(self, client) -> None:
        """Consuming from the scan dialog is a write, and api() lifts the token
        from a form on the page. Without one every write here is refused."""
        session, _token = client
        assert 'name="csrf_token"' in session.get("/").get_data(as_text=True)

    def test_the_dashboard_is_one_button_away(self, client) -> None:
        session, _token = client
        body = session.get("/").get_data(as_text=True)
        assert 'href="/dashboard"' in body
        assert "Dashboard" in body

    def test_it_offers_the_wizard_for_something_new(self, client) -> None:
        session, _token = client
        assert 'href="/item/new"' in session.get("/").get_data(as_text=True)


class TestQrStickers:
    def test_a_sticker_is_returned_as_a_png(self, client) -> None:
        session, _token = client
        response = session.get("/qr/FG-0001.png")
        assert response.status_code == 200
        assert response.mimetype == "image/png"
        assert response.data.startswith(b"\x89PNG")

    def test_a_malformed_identifier_has_no_sticker(self, client) -> None:
        session, _token = client
        assert session.get("/qr/NOPE.png").status_code == 404

    def test_stickers_need_a_signed_in_user(self, app: Flask) -> None:
        assert app.test_client().get("/qr/FG-0001.png").status_code == 302

    def test_the_item_page_shows_its_sticker(self, client) -> None:
        session, _token = client
        assert "/qr/FG-0001.png" in session.get("/item/FG-0001").get_data(as_text=True)


class TestQrGenerator:
    def test_identifiers_are_four_digits(self) -> None:
        assert identifier(7) == "FG-0007"
        assert identifier(1234) == "FG-1234"

    def test_out_of_range_numbers_are_refused(self) -> None:
        with pytest.raises(ValueError, match="FG-0000"):
            identifier(10000)

    def test_a_sticker_encodes_the_full_item_url(self) -> None:
        """Scanning with any camera app should open FreshGuard, not show a code."""
        assert target_url("http://pi.local:5000/", "FG-0001") == "http://pi.local:5000/item/FG-0001"

    def test_single_stickers_are_written(self, tmp_path: Path) -> None:
        written = write_singles(["FG-0001", "FG-0002"], "http://pi.local", tmp_path)
        assert len(written) == 2
        assert all(path.is_file() and path.stat().st_size > 0 for path in written)

    def test_a_printable_sheet_is_written(self, tmp_path: Path) -> None:
        sheets = write_sheet([identifier(n) for n in range(1, 30)], "http://pi.local", tmp_path)
        assert len(sheets) == 2, "24 to a sheet, so 29 identifiers need two"


class TestTheme:
    def test_the_theme_script_runs_before_the_stylesheet_paints(self, client) -> None:
        """Loaded in head so a dark-mode user never sees a light flash."""
        session, _token = client
        body = session.get("/dashboard").get_data(as_text=True)
        head = body.split("</head>")[0]
        assert "js/theme.js" in head

    def test_every_page_offers_the_toggle(self, client) -> None:
        session, _token = client
        for url in [*PAGES, *OPEN_PAGES]:
            assert 'id="fg-theme-toggle"' in session.get(url).get_data(as_text=True)

    def test_the_toggle_is_labelled_for_screen_readers(self, client) -> None:
        """Each glyph names the action it performs, not the control it is."""
        session, _token = client
        body = session.get("/dashboard").get_data(as_text=True)
        assert "Switch to dark mode" in body
        assert "Switch to light mode" in body


class TestAdminPage:
    def test_thresholds_are_editable(self, client) -> None:
        session, _token = client
        body = session.get("/admin/").get_data(as_text=True)
        assert 'data-save-threshold="dairy"' in body
        assert 'name="source_note"' in body

    def test_the_source_note_rule_is_stated(self, client) -> None:
        session, _token = client
        assert "prototype/demo assumption" in session.get("/admin/").get_data(as_text=True)

    def test_gas_limits_are_shown_per_zone(self, client) -> None:
        session, _token = client
        body = session.get("/admin/").get_data(as_text=True)
        assert "Zone gas limits" in body
        assert "never as ppm" in body

    def test_accounts_are_listed(self, client) -> None:
        session, _token = client
        body = session.get("/admin/").get_data(as_text=True)
        assert "admin" in body
        assert "cook" in body


class TestHeadings:
    """Each page needs exactly one top-level heading for screen readers."""

    @pytest.mark.parametrize("url", ALL_PAGES)
    def test_every_page_has_exactly_one_h1(self, client, url: str) -> None:
        session, _token = client
        body = session.get(url).get_data(as_text=True)
        assert body.count("<h1") == 1, f"{url} should have one h1, found {body.count('<h1')}"

    def test_the_dashboard_heading_names_the_page(self, client) -> None:
        """The one h1 is the page's own title; the greeting is the hero below it."""
        session, _token = client
        body = session.get("/dashboard").get_data(as_text=True)
        assert re.search(r"<h1>\s*A fresher kitchen starts here\.", body)
        assert re.search(r"<h2>\s*Good (morning|afternoon|evening)", body)


class TestSensorsPage:
    """The page that shows what the hardware is doing, not what it should do."""

    def test_it_lists_the_fitted_sensors(self, client, conn) -> None:
        session, _token = client
        body = session.get("/sensors").get_data(as_text=True)
        assert "DHT11" in body
        assert "MQ-135 via ADS1115" in body
        assert "Reed switch" in body

    def test_it_mirrors_the_character_display(self, client, conn) -> None:
        session, _token = client
        add_reading(conn, temp_c=4.2, humidity=55.0, gas_raw=120)
        body = session.get("/sensors").get_data(as_text=True)
        assert "fg-lcd" in body
        assert "4.2C" in body

    def test_a_faulty_sensor_shows_the_fault_wording(self, client, conn) -> None:
        """SRS rule 1: never a plausible reading in place of a broken sensor."""
        session, _token = client
        for _ in range(3):
            add_reading(conn, temp_c=None, temp_ok=0)
        body = session.get("/sensors").get_data(as_text=True)
        assert "Sensor Fault/Data Unavailable" in body

    def test_gas_is_never_presented_as_ppm(self, client, conn) -> None:
        session, _token = client
        add_reading(conn, gas_raw=300)
        body = session.get("/sensors").get_data(as_text=True)
        assert "ppm" not in body.replace("never shown as ppm", "")

    def test_it_reports_what_is_waiting_to_sync(self, client) -> None:
        session, _token = client
        body = session.get("/sensors").get_data(as_text=True)
        assert "Waiting to send" in body
        assert "Cloud synchronisation" in body

    def test_output_devices_are_not_claimed_healthy(self, client) -> None:
        """A buzzer has no readback, so the page must not invent a state for it."""
        session, _token = client
        body = session.get("/sensors").get_data(as_text=True)
        assert "16x2 LCD" in body
        assert "no readback, so no health is claimed" in body


class TestPlannerPage:
    def test_it_orders_by_what_runs_out_first(self, client, conn) -> None:
        session, _token = client
        add_item(client[0], _token, conn, qr_id="FG-0001", name="Later", expiry_date="2099-01-01")
        add_item(client[0], _token, conn, qr_id="FG-0002", name="Sooner", expiry_date="2026-09-25")

        body = session.get("/planner").get_data(as_text=True)

        assert body.index("Sooner") < body.index("Later")

    def test_each_item_can_be_consumed_or_recorded_as_waste(self, client, conn) -> None:
        session, _token = client
        add_item(client[0], _token, conn)
        body = session.get("/planner").get_data(as_text=True)
        assert 'data-state="consumed"' in body
        assert 'data-state="discarded"' in body

    def test_a_consumed_item_leaves_the_plan(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)
        session.post(
            "/api/items/FG-0001/consume",
            json={"outcome": "consumed"},
            headers={"X-CSRF-Token": token},
        )

        assert "Milk" not in session.get("/planner").get_data(as_text=True)

    def test_it_says_the_decision_belongs_to_the_user(self, client) -> None:
        session, _token = client
        assert "your decision" in session.get("/planner").get_data(as_text=True)


class TestReportsPage:
    def test_it_counts_what_is_stored_by_category(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)
        body = session.get("/reports").get_data(as_text=True)
        assert "What is in store, by category" in body
        assert "dairy" in body

    def test_it_separates_consumed_from_discarded(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn, qr_id="FG-0001")
        add_item(session, token, conn, qr_id="FG-0002")
        headers = {"X-CSRF-Token": token}
        session.post("/api/items/FG-0001/consume", json={"outcome": "consumed"}, headers=headers)
        session.post("/api/items/FG-0002/consume", json={"outcome": "discarded"}, headers=headers)

        body = session.get("/reports").get_data(as_text=True)

        assert "Consumed" in body
        assert "Recorded as thrown away" in body

    def test_it_refuses_to_invent_a_saving(self, client) -> None:
        """No money and no carbon figure: nothing here measures either."""
        session, _token = client
        body = session.get("/reports").get_data(as_text=True)
        assert "No money saved and no carbon figure is shown" in body

    def test_the_inventory_exports_as_csv(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)

        response = session.get("/reports/inventory.csv")

        assert response.status_code == 200
        assert response.mimetype == "text/csv"
        body = response.get_data(as_text=True)
        assert "qr_id,name,category" in body
        assert "FG-0001,Milk,dairy" in body

    def test_the_export_needs_a_signed_in_user(self, app: Flask) -> None:
        response = app.test_client().get("/reports/inventory.csv")
        assert response.status_code == 302


class TestInventoryPage:
    def test_it_offers_a_search_and_a_category_filter(self, client) -> None:
        session, _token = client
        body = session.get("/inventory").get_data(as_text=True)
        assert 'id="fg-inventory-search"' in body
        assert 'id="fg-inventory-category"' in body

    def test_each_row_carries_what_a_search_matches(self, client, conn) -> None:
        """Filtering happens in the page, so the text to match is rendered with the row."""
        session, token = client
        add_item(session, token, conn)
        body = session.get("/inventory").get_data(as_text=True)
        assert 'data-search="milk dairy fg-0001"' in body
        assert 'data-category="dairy"' in body


class TestAlertsPage:
    def test_it_offers_acknowledge_all(self, client) -> None:
        session, _token = client
        assert 'id="fg-ack-all"' in session.get("/alerts").get_data(as_text=True)

    def test_acknowledge_all_is_disabled_when_nothing_is_open(self, client) -> None:
        session, _token = client
        body = session.get("/alerts").get_data(as_text=True)
        assert re.search(r'id="fg-ack-all"[^>]*\n?[^>]*disabled', body)

    def test_the_activity_log_shows_queued_events(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)

        body = session.get("/alerts").get_data(as_text=True)

        assert "Activity log" in body
        assert "create Milk (FG-0001)" in body
        assert "pending" in body

    def test_it_counts_what_is_still_waiting_to_be_sent(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn)
        body = session.get("/alerts").get_data(as_text=True)
        assert "Queued for the cloud" in body
        assert "Email waiting" in body


class TestSettingsPage:
    def test_the_decision_rule_order_is_shown(self, client) -> None:
        session, _token = client
        body = session.get("/admin/").get_data(as_text=True)
        assert "Decision rule order" in body
        assert "past its expiry date or its storage limit" in body

    def test_it_states_that_gas_is_never_attributed_to_an_item(self, client) -> None:
        """SRS rule 3, stated where the limits are actually configured."""
        session, _token = client
        body = session.get("/admin/").get_data(as_text=True)
        assert "never attributed to" in body

    def test_a_fault_never_reads_fresh_in_the_rules(self, client) -> None:
        session, _token = client
        body = session.get("/admin/").get_data(as_text=True)
        assert "Never Fresh" in body


class TestChartControls:
    def test_the_chart_can_be_narrowed_to_one_zone_or_metric(self, client) -> None:
        session, _token = client
        body = session.get("/dashboard").get_data(as_text=True)
        assert 'id="fg-chart-zone"' in body
        assert 'id="fg-chart-metric"' in body
        assert 'value="gas"' in body


class TestScenarioControl:
    """The simulator controls on the sensors page."""

    def test_an_administrator_sees_the_scenario_buttons(self, client) -> None:
        session, _token = client
        body = session.get("/sensors").get_data(as_text=True)
        assert 'data-scenario="high_temp"' in body
        assert "Disconnected sensor" in body

    def test_staff_do_not_see_them(self, app: Flask) -> None:
        """Changing what the hardware reports is an administrator's decision."""
        session = app.test_client()
        sign_in(session, "cook", STAFF_PASSWORD)
        assert 'data-scenario="high_temp"' not in session.get("/sensors").get_data(as_text=True)

    def test_the_page_says_only_the_readings_are_simulated(self, client) -> None:
        session, _token = client
        body = session.get("/sensors").get_data(as_text=True)
        assert "only the" in body
        assert "readings are simulated" in body

    def test_the_running_scenario_is_shown(self, client) -> None:
        session, token = client
        session.post(
            "/api/simulator", json={"scenario": "gas_spike"}, headers={"X-CSRF-Token": token}
        )

        body = session.get("/sensors").get_data(as_text=True)

        assert "Abnormal gas" in body


class TestSrsDisplayRequirements:
    """Fields and wordings the SRS names explicitly."""

    def stored_days_ago(self, days: int) -> tuple[str, str]:
        """A storage date N days back, and how the page should word it.

        Computed rather than written down: a fixed date turns into a failing
        test the next morning, which is how this one first broke.
        """
        day = datetime.now(UTC).date() - timedelta(days=days)
        return day.isoformat(), f"{days} day{'' if days == 1 else 's'}"

    def test_the_inventory_shows_storage_duration(self, client, conn) -> None:
        """SRS 1.6 xvii lists storage duration separately from expiry."""
        session, token = client
        stored, wording = self.stored_days_ago(4)
        add_item(session, token, conn, storage_date=stored)
        body = session.get("/inventory").get_data(as_text=True)
        assert "Stored for" in body
        assert wording in body

    def test_the_planner_shows_storage_duration(self, client, conn) -> None:
        session, token = client
        stored, wording = self.stored_days_ago(1)
        add_item(session, token, conn, storage_date=stored)
        assert f"stored {wording}" in session.get("/planner").get_data(as_text=True)

    def test_the_item_page_shows_storage_duration(self, client, conn) -> None:
        session, token = client
        stored, wording = self.stored_days_ago(4)
        add_item(session, token, conn, storage_date=stored)
        body = session.get("/item/FG-0001").get_data(as_text=True)
        assert f"Stored for {wording}" in body

    def test_the_disposal_wording_appears_only_for_check_food(self, client, conn) -> None:
        """A Fresh item must never be offered a disposal procedure."""
        session, token = client
        add_item(session, token, conn, expiry_date="2099-01-01")
        assert "disposal procedure" not in session.get("/item/FG-0001").get_data(as_text=True)

    def test_an_expired_item_is_offered_the_disposal_wording(self, client, conn) -> None:
        session, token = client
        add_item(session, token, conn, qr_id="FG-0002", expiry_date="2026-09-21")
        body = session.get("/item/FG-0002").get_data(as_text=True)
        assert "disposal procedure" in body
        assert "If inspection finds" in body
