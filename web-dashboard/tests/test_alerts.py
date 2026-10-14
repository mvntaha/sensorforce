"""Local outputs, the alert path, remote notification and threshold loading."""

from __future__ import annotations

import sqlite3
import urllib.error

import pytest

from core.alerts import (
    Alert,
    pending_notifications,
    persist,
    raise_alert,
    retry_pending,
)
from core.notify import (
    DISCLAIMER,
    CompositeNotifier,
    FakeNotifier,
    Notification,
    NtfyNotifier,
    SmtpNotifier,
    build_alert_notification,
)
from core.outputs import (
    BuzzerPattern,
    FakeBuzzer,
    FakeDisplay,
    FakeLedArray,
    Led,
    fake_outputs,
    fit,
    format_reading_line,
    get_outputs,
)
from core.thresholds import (
    DEFAULT_DOOR_OPEN_TIMEOUT_S,
    ThresholdError,
    door_open_timeout_s,
    gas_limit,
    humidity_limit,
    load_all,
    load_for_category,
    set_setting,
    temperature_limit,
)

DEVICE = "pi-test"


@pytest.fixture
def outputs():
    return fake_outputs()


def make_alert(**overrides: object) -> Alert:
    defaults: dict[str, object] = {
        "zone_id": 1,
        "zone_name": "Main shelf",
        "type": "temp_high",
        "severity": "critical",
        "message": "Zone temperature above the configured range",
    }
    return Alert(**(defaults | overrides))  # type: ignore[arg-type]


class TestOutputs:
    def test_fit_truncates_and_pads_to_the_display_width(self) -> None:
        assert fit("short") == "short".ljust(16)
        assert len(fit("a very long line indeed")) == 16

    def test_a_missing_value_shows_as_dashes_not_zero(self) -> None:
        """A fault must never be displayed as a plausible reading."""
        line = format_reading_line(None, None, None)
        assert "--C" in line
        assert "0.0C" not in line

    def test_values_are_shown_when_present(self) -> None:
        line = format_reading_line(4.2, 55.0, 1200)
        assert "4.2C" in line
        assert "55%" in line

    def test_the_display_never_reports_ppm(self) -> None:
        assert "ppm" not in format_reading_line(4.2, 55.0, 1200).lower()

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("Fresh/Normal", Led.GREEN),
            ("Use Soon", Led.YELLOW),
            ("Check Food", Led.RED),
            ("Sensor Fault/Data Unavailable", Led.RED),
        ],
    )
    def test_each_status_lights_its_lamp(self, status: str, expected: Led) -> None:
        leds = FakeLedArray()
        leds.show_status(status)
        assert leds.current is expected

    def test_a_fault_never_lights_the_green_lamp(self) -> None:
        leds = FakeLedArray()
        leds.show_status("Sensor Fault/Data Unavailable")
        assert leds.current is not Led.GREEN

    def test_an_unknown_status_lights_red_rather_than_green(self) -> None:
        """A bug must never present as everything being fine."""
        leds = FakeLedArray()
        leds.show_status("something unexpected")
        assert leds.current is Led.RED

    def test_the_display_truncates_to_two_lines_of_sixteen(self) -> None:
        display = FakeDisplay()
        display.write("a" * 40, "b" * 40)
        assert [len(line) for line in display.lines] == [16, 16]

    def test_the_buzzer_distinguishes_door_from_threshold(self) -> None:
        buzzer = FakeBuzzer()
        buzzer.sound(BuzzerPattern.DOOR)
        buzzer.sound(BuzzerPattern.THRESHOLD)
        assert buzzer.patterns == [BuzzerPattern.DOOR, BuzzerPattern.THRESHOLD]

    def test_the_buzzer_can_be_silenced(self) -> None:
        buzzer = FakeBuzzer()
        buzzer.sound(BuzzerPattern.THRESHOLD)
        buzzer.silence()
        assert buzzer.silenced is True

    def test_sim_mode_returns_recording_outputs(self) -> None:
        assert isinstance(get_outputs("sim").leds, FakeLedArray)

    def test_an_unknown_output_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown output mode"):
            get_outputs("semaphore")


class TestAlertConstruction:
    def test_an_event_id_and_timestamp_are_generated(self) -> None:
        alert = make_alert()
        assert alert.event_id
        assert alert.ts.endswith("Z")

    def test_each_alert_gets_its_own_event_id(self) -> None:
        assert make_alert().event_id != make_alert().event_id

    def test_an_unknown_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown alert type"):
            make_alert(type="spoiled")

    def test_an_unknown_severity_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown severity"):
            make_alert(severity="apocalyptic")

    def test_a_gas_alert_may_not_name_an_item(self) -> None:
        """SRS rule 3, enforced before the database ever sees the row."""
        with pytest.raises(ValueError, match="must not name an item"):
            make_alert(type="gas_abnormal", item_id=7)

    def test_a_door_alert_may_not_name_an_item(self) -> None:
        with pytest.raises(ValueError, match="must not name an item"):
            make_alert(type="door_open", item_id=7)

    def test_an_expiry_alert_may_name_an_item(self) -> None:
        assert make_alert(type="expiry", item_id=7).item_id == 7

    def test_the_door_alert_uses_its_own_buzzer_pattern(self) -> None:
        assert make_alert(type="door_open").buzzer_pattern is BuzzerPattern.DOOR
        assert make_alert(type="temp_high").buzzer_pattern is BuzzerPattern.THRESHOLD


class TestPersistence:
    def test_the_alert_and_its_outbox_row_are_written_together(
        self, conn: sqlite3.Connection, zone_id: int
    ) -> None:
        persist(conn, make_alert(zone_id=zone_id), DEVICE)
        assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1

    def test_both_rows_share_the_event_id(self, conn: sqlite3.Connection, zone_id: int) -> None:
        """One id means a retried cloud write overwrites rather than duplicates."""
        alert = make_alert(zone_id=zone_id)
        persist(conn, alert, DEVICE)
        assert conn.execute("SELECT event_id FROM alerts").fetchone()[0] == alert.event_id
        assert conn.execute("SELECT event_id FROM outbox").fetchone()[0] == alert.event_id

    def test_nothing_is_written_when_the_alert_is_invalid(self, conn: sqlite3.Connection) -> None:
        """A bad zone id must not leave a half-written pair behind."""
        with pytest.raises(sqlite3.IntegrityError):
            persist(conn, make_alert(zone_id=999), DEVICE)
        assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0

    def test_the_outbox_payload_carries_the_sync_fields(
        self, conn: sqlite3.Connection, zone_id: int
    ) -> None:
        import json

        persist(conn, make_alert(zone_id=zone_id), DEVICE)
        payload = json.loads(conn.execute("SELECT payload_json FROM outbox").fetchone()[0])
        assert payload["entity"] == "alert"
        assert payload["op"] == "create"
        assert payload["device_id"] == DEVICE
        assert payload["event_id"]
        assert payload["ts"]

    def test_a_new_alert_starts_pending_notification(
        self, conn: sqlite3.Connection, zone_id: int
    ) -> None:
        persist(conn, make_alert(zone_id=zone_id), DEVICE)
        assert conn.execute("SELECT notify_pending FROM alerts").fetchone()[0] == 1


class TestRaiseAlert:
    def test_the_local_outputs_fire(self, conn: sqlite3.Connection, zone_id: int, outputs) -> None:
        raise_alert(
            conn,
            make_alert(zone_id=zone_id),
            outputs=outputs,
            notifier=FakeNotifier(),
            device_id=DEVICE,
            worst_status="Check Food",
        )
        assert outputs.buzzer.patterns == [BuzzerPattern.THRESHOLD]
        assert outputs.leds.current is Led.RED
        assert outputs.display.text

    def test_a_delivered_notification_clears_the_pending_flag(
        self, conn: sqlite3.Connection, zone_id: int, outputs
    ) -> None:
        notifier = FakeNotifier()
        raise_alert(
            conn,
            make_alert(zone_id=zone_id),
            outputs=outputs,
            notifier=notifier,
            device_id=DEVICE,
            worst_status="Check Food",
        )
        assert len(notifier.sent) == 1
        assert conn.execute("SELECT notify_pending FROM alerts").fetchone()[0] == 0

    def test_a_failed_notification_leaves_the_alert_pending(
        self, conn: sqlite3.Connection, zone_id: int, outputs
    ) -> None:
        """Offline: the local warning still happens and the mail is retried later."""
        raise_alert(
            conn,
            make_alert(zone_id=zone_id),
            outputs=outputs,
            notifier=FakeNotifier(fail=True),
            device_id=DEVICE,
            worst_status="Check Food",
        )
        assert conn.execute("SELECT notify_pending FROM alerts").fetchone()[0] == 1
        assert outputs.buzzer.patterns, "the buzzer must fire even with no network"

    def test_the_alert_is_stored_even_when_the_network_is_down(
        self, conn: sqlite3.Connection, zone_id: int, outputs
    ) -> None:
        raise_alert(
            conn,
            make_alert(zone_id=zone_id),
            outputs=outputs,
            notifier=FakeNotifier(fail=True),
            device_id=DEVICE,
            worst_status="Check Food",
        )
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


class TestRetryPending:
    def test_pending_alerts_are_listed(
        self, conn: sqlite3.Connection, zone_id: int, outputs
    ) -> None:
        raise_alert(
            conn,
            make_alert(zone_id=zone_id),
            outputs=outputs,
            notifier=FakeNotifier(fail=True),
            device_id=DEVICE,
            worst_status="Check Food",
        )
        assert len(pending_notifications(conn)) == 1

    def test_reconnecting_delivers_them(
        self, conn: sqlite3.Connection, zone_id: int, outputs
    ) -> None:
        for _ in range(3):
            raise_alert(
                conn,
                make_alert(zone_id=zone_id),
                outputs=outputs,
                notifier=FakeNotifier(fail=True),
                device_id=DEVICE,
                worst_status="Check Food",
            )
        working = FakeNotifier()
        assert retry_pending(conn, working) == 3
        assert pending_notifications(conn) == []

    def test_a_still_failing_retry_keeps_them_pending(
        self, conn: sqlite3.Connection, zone_id: int, outputs
    ) -> None:
        raise_alert(
            conn,
            make_alert(zone_id=zone_id),
            outputs=outputs,
            notifier=FakeNotifier(fail=True),
            device_id=DEVICE,
            worst_status="Check Food",
        )
        assert retry_pending(conn, FakeNotifier(fail=True)) == 0
        assert len(pending_notifications(conn)) == 1


class TestNotificationContent:
    def test_the_subject_names_the_zone(self) -> None:
        notification = build_alert_notification(
            zone_name="Main shelf",
            alert_type="temp_high",
            severity="critical",
            message="Zone temperature above the configured range",
            timestamp="2026-09-24T10:00:00Z",
        )
        assert "Main shelf" in notification.subject

    def test_a_gas_notification_refuses_to_name_an_item(self) -> None:
        with pytest.raises(ValueError, match="must not name an item"):
            build_alert_notification(
                zone_name="Main shelf",
                alert_type="gas_abnormal",
                severity="warning",
                message="Zone air quality is abnormal",
                timestamp="2026-09-24T10:00:00Z",
                item_name="Milk",
            )

    def test_an_item_alert_may_name_the_item(self) -> None:
        notification = build_alert_notification(
            zone_name="Main shelf",
            alert_type="expiry",
            severity="warning",
            message="Milk is past its date",
            timestamp="2026-09-24T10:00:00Z",
            item_name="Milk",
        )
        assert "Milk" in notification.body

    def test_the_email_carries_the_safety_disclaimer(self) -> None:
        notification = build_alert_notification(
            zone_name="Main shelf",
            alert_type="temp_high",
            severity="critical",
            message="Zone temperature above the configured range",
            timestamp="2026-09-24T10:00:00Z",
        )
        email = notification.as_email("fg@example.org", "kitchen@example.org")
        assert "does not certify food safety" in email.get_content()

    def test_the_email_has_the_expected_headers(self) -> None:
        email = build_alert_notification(
            zone_name="Z",
            alert_type="door_open",
            severity="info",
            message="The door has been open too long",
            timestamp="2026-09-24T10:00:00Z",
        ).as_email("fg@example.org", "kitchen@example.org")
        assert email["From"] == "fg@example.org"
        assert email["To"] == "kitchen@example.org"


class TestSmtpNotifier:
    def test_an_unconfigured_notifier_does_not_attempt_to_send(self) -> None:
        from core.config import SmtpSettings

        notifier = SmtpNotifier(SmtpSettings())
        assert notifier.configured is False
        notification = build_alert_notification(
            zone_name="Z",
            alert_type="temp_high",
            severity="info",
            message="m",
            timestamp="2026-09-24T10:00:00Z",
        )
        # Must return False rather than raising; the alert path cannot break.
        assert notifier.send(notification) is False

    def test_a_connection_failure_is_reported_not_raised(self) -> None:
        from core.config import SmtpSettings

        notifier = SmtpNotifier(
            SmtpSettings(
                host="127.0.0.1",
                port=1,
                user="u",
                password="p",  # noqa: S106 - unreachable server, not a credential
                sender="fg@example.org",
                recipient="kitchen@example.org",
            ),
            timeout_s=0.25,
        )
        notification = build_alert_notification(
            zone_name="Z",
            alert_type="temp_high",
            severity="info",
            message="m",
            timestamp="2026-09-24T10:00:00Z",
        )
        assert notifier.send(notification) is False


class TestThresholdLoading:
    def test_a_configured_category_loads(self, conn: sqlite3.Connection, category: str) -> None:
        thresholds = load_for_category(conn, category)
        assert thresholds.temp_max == 5.0
        assert thresholds.use_soon_days == 2

    def test_an_unknown_category_is_an_error_not_a_guess(self, conn: sqlite3.Connection) -> None:
        """Inventing a limit would put an unsourced number on the dashboard."""
        with pytest.raises(ThresholdError, match="unicorn"):
            load_for_category(conn, "unicorn")

    def test_every_category_can_be_loaded_at_once(
        self, conn: sqlite3.Connection, category: str
    ) -> None:
        assert set(load_all(conn)) == {category}

    def test_the_ranges_become_validation_limits(
        self, conn: sqlite3.Connection, category: str
    ) -> None:
        thresholds = load_for_category(conn, category)
        assert temperature_limit(thresholds).high == 5.0
        assert humidity_limit(thresholds).low == 30.0

    def test_the_gas_limit_comes_from_the_zone(
        self, conn: sqlite3.Connection, zone_id: int
    ) -> None:
        """Gas is zone-level, so it is never read from a food category."""
        limit = gas_limit(conn, zone_id)
        assert limit is not None
        assert limit.high == 2500.0

    def test_a_zone_without_a_gas_threshold_has_no_limit(self, conn: sqlite3.Connection) -> None:
        cursor = conn.execute("INSERT INTO zones (name) VALUES (?)", ("Crisper",))
        assert gas_limit(conn, int(cursor.lastrowid)) is None

    def test_an_unknown_zone_is_an_error(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ThresholdError, match="no zone"):
            gas_limit(conn, 999)


class TestSettings:
    def test_the_door_timeout_falls_back_when_unset(self, conn: sqlite3.Connection) -> None:
        assert door_open_timeout_s(conn) == DEFAULT_DOOR_OPEN_TIMEOUT_S

    def test_a_configured_door_timeout_is_used(self, conn: sqlite3.Connection) -> None:
        set_setting(conn, "door_open_timeout_s", "45")
        assert door_open_timeout_s(conn) == 45

    def test_setting_a_value_twice_replaces_it(self, conn: sqlite3.Connection) -> None:
        set_setting(conn, "door_open_timeout_s", "45")
        set_setting(conn, "door_open_timeout_s", "90")
        assert door_open_timeout_s(conn) == 90

    @pytest.mark.parametrize("bad", ["", "soon", "-5", "0"])
    def test_an_unusable_timeout_falls_back(self, conn: sqlite3.Connection, bad: str) -> None:
        """A bad setting must not stop the door alert working entirely."""
        set_setting(conn, "door_open_timeout_s", bad)
        assert door_open_timeout_s(conn) == DEFAULT_DOOR_OPEN_TIMEOUT_S


class TestNtfyNotifier:
    """Push notification, the channel that needs no account to demonstrate."""

    def settings(self, **overrides):
        from core.config import NtfySettings

        values = {"server": "https://ntfy.example", "topic": "freshguard-test-topic"}
        values.update(overrides)
        return NtfySettings(**values)

    def test_a_topic_is_all_it_needs(self) -> None:
        assert NtfyNotifier(self.settings()).configured

    def test_without_a_topic_it_is_not_configured(self) -> None:
        assert not NtfyNotifier(self.settings(topic="")).configured

    def test_a_non_http_server_is_refused(self) -> None:
        """A mistyped server naming file: would turn a push into a file read."""
        assert not NtfyNotifier(self.settings(server="file:///etc/passwd")).configured

    def test_the_topic_becomes_the_url(self) -> None:
        notifier = NtfyNotifier(self.settings())
        request = notifier._request(Notification(subject="Zone A", body="Too warm"))
        assert request.full_url == "https://ntfy.example/freshguard-test-topic"
        assert request.method == "POST"

    def test_the_subject_travels_as_the_title(self) -> None:
        notifier = NtfyNotifier(self.settings())
        request = notifier._request(Notification(subject="Zone A", body="Too warm"))
        assert request.get_header("Title") == "Zone A"

    def test_the_body_carries_the_safety_disclaimer(self) -> None:
        """SRS rule 2: the wording travels with the alert, wherever it goes."""
        notifier = NtfyNotifier(self.settings())
        request = notifier._request(Notification(subject="Zone A", body="Too warm"))
        assert DISCLAIMER.encode() in request.data

    def test_a_token_is_sent_as_a_bearer_header(self) -> None:
        notifier = NtfyNotifier(self.settings(token="tk_secret"))  # noqa: S106 - test fixture
        request = notifier._request(Notification(subject="Zone A", body="Too warm"))
        assert request.get_header("Authorization") == "Bearer tk_secret"

    def test_no_token_means_no_authorization_header(self) -> None:
        notifier = NtfyNotifier(self.settings())
        request = notifier._request(Notification(subject="Zone A", body="Too warm"))
        assert request.get_header("Authorization") is None

    def test_an_unconfigured_notifier_does_not_attempt_to_send(self) -> None:
        assert NtfyNotifier(self.settings(topic="")).send(Notification("s", "b")) is False

    def test_a_network_failure_is_reported_not_raised(self, monkeypatch) -> None:
        """A push that cannot go out must never interrupt the local alert."""
        import core.notify

        def refuse(*_args, **_kwargs):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(core.notify.urllib.request, "urlopen", refuse)
        assert NtfyNotifier(self.settings()).send(Notification("s", "b")) is False


class TestCompositeNotifier:
    """Email and push together: the kitchen is told twice, not once."""

    def test_one_success_is_enough(self) -> None:
        good, bad = FakeNotifier(), FakeNotifier(fail=True)
        assert CompositeNotifier([bad, good]).send(Notification("s", "b")) is True

    def test_every_channel_is_tried_even_after_one_succeeds(self) -> None:
        first, second = FakeNotifier(), FakeNotifier()
        CompositeNotifier([first, second]).send(Notification("s", "b"))
        assert len(first.sent) == 1
        assert len(second.sent) == 1

    def test_all_failing_reports_failure(self) -> None:
        composite = CompositeNotifier([FakeNotifier(fail=True), FakeNotifier(fail=True)])
        assert composite.send(Notification("s", "b")) is False

    def test_with_nothing_configured_it_is_not_configured(self) -> None:
        from core.config import NtfySettings, SmtpSettings

        composite = CompositeNotifier([SmtpNotifier(SmtpSettings()), NtfyNotifier(NtfySettings())])
        assert not composite.configured
        assert composite.send(Notification("s", "b")) is False

    def test_an_unconfigured_channel_is_skipped_not_failed(self) -> None:
        """An empty SMTP block must not make a working push look like a failure."""
        from core.config import SmtpSettings

        composite = CompositeNotifier([SmtpNotifier(SmtpSettings()), FakeNotifier()])
        assert composite.send(Notification("s", "b")) is True
