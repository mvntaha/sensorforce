# IoT Test Matrix

Owner: SF-SensorForce  ·  Last reviewed: 2026-09-29  ·  Build: Raspberry Pi 4B prototype, branch `feat/d-srs-pages`

Every mandatory scenario from the SRS, what was done, what happened, and where
the proof is.

**FreshGuard does not certify food safety.** A passing row means the system
behaved as specified. It does not mean any food was safe to eat.

## How to read this

| Column | What goes in it |
|---|---|
| Test ID | Stable. Referenced by the decision-rule matrix and by the report. |
| Test Scenario | What is being checked. |
| Input/Condition | What was done to provoke it. |
| Expected Result | What the specification requires. |
| Actual Result | What was observed. Blank until someone observes it. |
| Pass/Fail | Blank until Actual Result is filled in. |
| Evidence | The artefact anyone can re-open: a test, a screenshot, a log. |

### Verification methods

| Method | Meaning |
|---|---|
| **A** | Automated. A pytest case drives the sensor simulator and asserts the outcome. Re-runnable by anyone: `pytest`. |
| **S** | Simulated end-to-end. The running application, the real engine and the real database, with simulated sensor readings. |
| **H** | Hardware. Requires the wired prototype. **Cannot be satisfied by the simulator.** |
| **M** | Manual observation, recorded by a person. |

A row verified by **A** or **S** proves the software path. It does **not**
prove the wiring, the sensors or the audible buzzer. Rows marked **H** are the
only evidence that the physical prototype works, and they are filled in by the
person who runs the hardware.

### Environment

| Item | Value |
|---|---|
| Sensor mode | `sim` for A and S rows, `real` for H rows |
| Confirmation rule | 3 consecutive out-of-range readings |
| Poll interval | 1 s |
| Automated suite at time of writing | 843 tests, all passing (re-run 2026-09-29) |
| Test runner | `pytest` |
| Hardware build | Raspberry Pi 4B, wired per `docs/pin_map.md` |
| Tester | SF-SensorForce |
| Date run | A and S rows: 2026-09-29. **H rows: not yet run against the built board.** |

---

## 1. Sensor reading and freshness status

| Test ID | Test Scenario | Input/Condition | Expected Result | Actual Result | Pass/Fail | Evidence |
|---|---|---|---|---|---|---|
| TM-01 | Normal temperature, humidity and gas readings | `normal` scenario, readings inside every configured range | No alert raised. Items read Fresh/Normal. Green lamp lit. | No alert raised; green lamp lit (A) | Pass (A) | `test_monitor.py::TestLocalAlertPath::test_the_normal_scenario_raises_nothing`, `::test_the_normal_scenario_lights_green` |
| TM-02 | Readings are stored, not just displayed | `normal` scenario, one poll | One row written to `readings` with its timestamp | One row per tick (A) | Pass (A) | `test_monitor.py::TestReadingStorage::test_a_tick_stores_one_reading` |
| TM-03 | High temperature | `high_temp` scenario | After 3 consecutive bad readings: Check Food, temperature alert, buzzer, red lamp | Alert `temp_high` raised on the third reading; threshold buzzer pattern; red lamp (A) | Pass (A) | `test_monitor.py::TestLocalAlertPath::test_high_temp_raises_a_temperature_alert`, `::test_high_temp_sounds_the_threshold_pattern`, `::test_high_temp_lights_the_red_lamp` |
| TM-04 | A single bad reading does not alert | `high_temp` scenario, stopped after 2 readings | No alert. One or two bad readings are not a confirmed violation. | No alert after 2 readings (A) | Pass (A) | `test_monitor.py::TestLocalAlertPath::test_high_temp_does_not_alert_before_the_third_reading` |
| TM-05 | Abnormal humidity | `high_humidity` scenario | After 3 consecutive bad readings: Check Food, humidity alert, buzzer | Alert `humidity_high` raised on the third reading, not before; threshold buzzer pattern (A) | Pass (A) | `test_monitor.py::TestLocalAlertPath::test_high_humidity_raises_a_humidity_alert`, `::test_high_humidity_does_not_alert_before_the_third_reading`, `::test_high_humidity_sounds_the_threshold_pattern`, `test_engine.py::TestRule3TemperatureAndHumidity::test_a_confirmed_humidity_breach_is_check_food` |
| TM-06 | Abnormal gas reading | `gas_spike` scenario | Confirmed breach → Check Food **for the zone**. Message names the zone, never an item. No ppm figure. | Zone-level alert raised; no item referenced; no ppm in the message (A) | Pass (A) | `test_monitor.py::TestLocalAlertPath::test_gas_spike_raises_a_zone_level_alert`, `::test_the_gas_alert_never_names_an_item`, `test_engine.py::TestRule4Gas::test_the_gas_message_never_reports_ppm` |
| TM-07 | Gas is never attributed to one item | Gas alert raised while several items share the zone | The alert carries no item reference, in the database and in the email | Refused at the database and in the notification (A) | Pass (A) | `test_alerts.py::TestAlertConstruction::test_a_gas_alert_may_not_name_an_item`, `test_alerts.py::TestNotificationContent::test_a_gas_notification_refuses_to_name_an_item` |
| TM-08 | Door left open beyond the timeout | `door_open` scenario, door held open past the configured timeout | Door alert, buzzer sounds its own pattern, "Close the refrigerator door" | Door alert raised with its own buzzer pattern (A) | Pass (A) | `test_monitor.py::TestLocalAlertPath::test_door_open_raises_a_door_alert`, `::test_the_door_alert_uses_its_own_buzzer_pattern` |
| TM-09 | Door timeout is administrator-configurable | Timeout changed through the admin page | The new value takes effect; a nonsensical value is refused | Configured value used; negative value refused (A) | Pass (A) | `test_alerts.py::TestSettings::test_a_configured_door_timeout_is_used`, `test_api.py::TestAdminApi::test_a_negative_door_timeout_is_refused` |
| TM-10 | Use Soon condition | Item inside its use-soon window, zone healthy | Status Use Soon, yellow lamp, "Use the food soon" | Use Soon returned with the SRS wording (A) | Pass (A) | `test_engine.py::TestRule5UseSoon::test_an_item_inside_the_use_soon_window_is_use_soon`, `::test_the_use_soon_recommendation_is_the_srs_wording` |
| TM-11 | Check Food from an exceeded limit | Item past its expiry date, and separately past its storage limit | Status Check Food, red lamp, "Inspect the food before use" | Check Food in both cases (A) | Pass (A) | `test_engine.py::TestRule1Expiry::test_an_expired_item_is_check_food`, `::test_an_exceeded_storage_limit_is_check_food` |
| TM-12 | Disconnected sensor → Sensor Fault/Data Unavailable | `sensor_disconnected` scenario | Separate fault state shown. **Never Fresh/Normal.** Fault recorded. Red lamp, not green. | Fault status returned; never Fresh; green lamp never lit (A) | Pass (A) | `test_monitor.py::TestLocalAlertPath::test_sensor_disconnected_never_reports_fresh`, `test_engine.py::TestRule2SensorFault::test_the_disconnected_sensor_scenario_is_never_fresh`, `test_alerts.py::TestOutputs::test_a_fault_never_lights_the_green_lamp` |
| TM-13 | A fault is stored as a fault, not as a zero | Sensor returns nothing | The value column is NULL and the health flag is 0. No plausible-looking zero is stored. | Stored as NULL with the flag clear; the database refuses any other shape (A) | Pass (A) | `test_monitor.py::TestReadingStorage::test_a_disconnected_sensor_is_stored_as_a_fault_not_a_zero`, `test_schema.py` reading-table constraints |
| TM-14 | An intermittent sensor | `dht_intermittent` scenario | Faults handled without crashing; the loop keeps running on the remaining valid inputs | Loop continued; fault rows stored (A) | Pass (A) | `test_sensors.py::test_every_scenario_produces_storable_rows[dht_intermittent]`, `test_monitor.py::TestLoopResilience` |
| TM-15 | Freshness decision rules, in order | Each of the six rules, and each precedence pair | First match wins, as documented in the decision-rule matrix | All six rules and every precedence pair behave as documented (A) | Pass (A) | `test_engine.py::TestRule1Expiry` through `::TestRule6Fresh`, and `::TestRulePrecedence`; see `docs/decision_rules.md` |

## 2. Local alert timing

The SRS requires a local alert within **2–5 seconds** of a confirmed violation.
Three consecutive bad readings at a 1 s poll interval put the floor at about
two seconds by design.

Measured with `scripts/measure_alert_path.py`, wall-clock, from the first
reading to the buzzer sounding. The sensor and the siren are simulated; every
step between them is the shipped code.

| Test ID | Test Scenario | Input/Condition | Expected Result | Actual Result | Pass/Fail | Evidence |
|---|---|---|---|---|---|---|
| TM-16 | Local alert timing, high temperature | `high_temp`, 20 runs | Local output within 2–5 s | min 2.002 s · median 2.005 s · max 2.018 s · 20 of 20 inside the window (S) | Pass (S) | `scripts/measure_alert_path.py --runs 20` |
| TM-17 | Local alert timing, abnormal humidity | `high_humidity`, 10 runs | Local output within 2–5 s | min 2.004 s · median 2.006 s · max 2.009 s · 10 of 10 inside the window (S) | Pass (S) | `scripts/measure_alert_path.py --runs 10 --scenario high_humidity` |
| TM-18 | Local alert timing, abnormal gas | `gas_spike`, 10 runs | Local output within 2–5 s | min 4.006 s · median 4.010 s · max 4.012 s · 10 of 10 inside the window (S) | Pass (S) | `scripts/measure_alert_path.py --runs 10 --scenario gas_spike` |
| TM-19 | Local alert timing on the hardware | Wired prototype, real DHT22, provoked high temperature | Buzzer audible and LCD updated within 2–5 s, measured with a stopwatch | | | **H** — _______________ |

The gas figure is longer because the `gas_spike` scenario ramps for two
readings before it goes abnormal; the confirmation itself still takes three
readings. It remains inside the required window.

## 3. Local operation without a network

| Test ID | Test Scenario | Input/Condition | Expected Result | Actual Result | Pass/Fail | Evidence |
|---|---|---|---|---|---|---|
| TM-20 | Sensing and alerting continue offline | Network unavailable, monitor running | Readings, freshness analysis and local alerts continue unaffected | The sync worker runs on its own thread with its own database connection, so a slow or failed network cannot delay a reading or an alert. Every timing run in section 2 was made with no cloud client and no mail server configured. (A, S) | Pass (A) | `test_sync.py::TestOfflineAndReconnect::test_nothing_is_sent_while_offline`, `test_monitor.py::TestLocalAlertPath`, and the TM-16 to TM-18 measurements |
| TM-21 | Temporary network failure | Cloud made unreachable mid-run | Nothing sent. Events buffer locally. No data lost. No crash. | Nothing sent; events buffered; the drain reports it could not reach the replica (A) | Pass (A) | `test_sync.py::TestOfflineAndReconnect::test_events_keep_buffering_while_offline`, `test_sync.py::TestOfflineReporting::test_drain_reports_that_it_could_not_reach_the_replica` |
| TM-22 | The dashboard works on the LAN with no internet | Browser on the LAN, internet disconnected | Every page renders. Charts and the QR scanner work: no asset is fetched from the internet. | Chart.js and html5-qrcode are served from the Pi; no external URL appears in any template or stylesheet (A) | Pass (A) | `test_design_system.py::TestComponents::test_no_external_resources_are_fetched`, `::test_no_external_assets` |
| TM-23 | Offline operation on the hardware | Pi with Wi-Fi switched off, food in the zone | Buzzer, LEDs and LCD continue to work with no network | | | **H** — _______________ |

## 4. Synchronisation and duplicate prevention

| Test ID | Test Scenario | Input/Condition | Expected Result | Actual Result | Pass/Fail | Evidence |
|---|---|---|---|---|---|---|
| TM-24 | Every buffered event carries its identity | Any change that queues an event | Unique event ID, timestamp and sync status on every row | Enforced by the outbox table; every event carries all three (A) | Pass (A) | `test_sync.py::TestIdempotency::test_the_stored_payload_carries_the_sync_fields`, `test_schema.py` outbox constraints |
| TM-25 | Reconnection drains the backlog | Cloud restored after an offline period | Every unsent event is transferred | Backlog drained on reconnection (A) | Pass (A) | `test_sync.py::TestOfflineAndReconnect::test_reconnecting_drains_the_backlog` |
| TM-26 | No duplicates on retry | The same event sent twice | One record in the replica. The event ID is the key, so a repeat overwrites rather than appends. | Sending twice leaves one record; every event ID appears exactly once; replica count equals outbox count (A) | Pass (A) | `test_sync.py::TestIdempotency::test_sending_the_same_event_twice_leaves_one_record`, `::test_every_event_id_appears_exactly_once`, `::test_the_replica_count_matches_the_outbox_count` |
| TM-27 | No duplicates against the live cloud | Real Firebase project, events synced then deliberately resent | Node count unchanged after the resend | 3 events synced, all 3 resent, node count unchanged, then cleaned to 0 (M, Phase 4) | Pass (M) | Phase 4 verification run against the team's Firebase project; re-runnable — record the date and project here: _______________ |
| TM-28 | A second disconnection behaves the same | Offline, reconnect, offline, reconnect | Identical behaviour both times. No state left behind. | Second cycle identical to the first (A) | Pass (A) | `test_sync.py::TestOfflineAndReconnect::test_a_second_disconnection_behaves_the_same` |
| TM-29 | An alert always queues an event | Any alert raised | The alert row and its outbox row are written in one transaction | Every alert writes an outbox row (A) | Pass (A) | `test_monitor.py::TestAlertStateChanges::test_every_alert_writes_an_outbox_row` |

## 5. Remote notification

The SRS makes a proactive remote notification **mandatory** on a confirmed
alert, through "e-mail, Blynk/app push notification or another approved
remote-notification service".

Two channels are wired: email over SMTP, and push through ntfy. Both are tried
on every alert and one success is enough. Push needs only a topic name, with no
account and no credentials, so it is the channel most likely to be working
during a demonstration.

| Test ID | Test Scenario | Input/Condition | Expected Result | Actual Result | Pass/Fail | Evidence |
|---|---|---|---|---|---|---|
| TM-30 | A confirmed alert attempts a notification | Alert raised with a notifier configured | The notification is attempted once, and the alert is marked pending until it is delivered | Attempted on every alert; the pending flag clears only on delivery (A) | Pass (A) | `test_monitor.py::TestAlertStateChanges::test_the_remote_notification_is_attempted`, `test_alerts.py::TestRaiseAlert::test_a_delivered_notification_clears_the_pending_flag` |
| TM-31 | A failed send is retried, not lost | Notifier offline when the alert is raised | The alert stays pending and is retried; the failure is reported, not raised | Alert left pending; connection failure reported rather than raised (A) | Pass (A) | `test_monitor.py::TestAlertStateChanges::test_an_offline_notification_leaves_the_alert_pending`, `test_alerts.py::TestSmtpNotifier::test_a_connection_failure_is_reported_not_raised` |
| TM-32 | Notification content | Alert of each kind | Names the zone, carries the safety disclaimer, and never attributes gas to an item | Subject names the zone; disclaimer present; gas notification refuses to name an item (A) | Pass (A) | `test_alerts.py::TestNotificationContent` (5 cases) |
| TM-33 | **A notification actually arrives** | Both channels configured, then the `high_temp` scenario run until the alert is confirmed | The message leaves the system and is retrievable at its destination, with the expected title and body | Alert `temp_high` raised on the third reading. Both channels reported delivery and the alert's pending flag cleared. The push was then read back off the topic with the expected title "FreshGuard critical: temp high in Main shelf". The email was accepted by the mail server; its arrival in the mailbox is not verified here. (S) | Pass, push. Partial, email | `scripts/check_notify.py --send`, topic poll `https://ntfy.sh/<topic>/json?poll=1` — _______________ screenshot of the push on the phone |
| TM-66 | Push notification content and transport | Alert pushed through ntfy | Posted to the topic URL, subject as the title, body carrying the safety disclaimer, token sent only when configured | Topic becomes the URL; subject travels as the title; disclaimer in the body; a non-http server is refused outright (A) | Pass (A) | `test_alerts.py::TestNtfyNotifier` (10 cases) |
| TM-67 | Two channels, one alert | Alert raised with both channels configured | Both are attempted; one success means the alert was delivered; an unconfigured channel is skipped rather than counted as a failure | Every configured channel attempted; one success is enough; unconfigured channels skipped (A) | Pass (A) | `test_alerts.py::TestCompositeNotifier` (5 cases) |

TM-33 is now demonstrated for push: a provoked alert was read back off the
ntfy topic, which proves the message left the system and reached its
destination. The email leg is accepted by the mail server but its arrival in
the mailbox has not been checked, and the screenshot of the push arriving on
the phone is still to be taken.

A topic on a public ntfy server is readable by anyone who knows its name, and an
alert names a storage zone and can name a food item. Use a long, unguessable
topic.

## 6. Food registration, QR and the dashboard

| Test ID | Test Scenario | Input/Condition | Expected Result | Actual Result | Pass/Fail | Evidence |
|---|---|---|---|---|---|---|
| TM-34 | Register a food item | Item submitted with name, category, quantity, dates and zone | Stored and visible in the inventory. Invalid input refused with a reason. | Item stored; every invalid field refused server-side (A) | Pass (A) | `test_api.py::TestItemCrud` |
| TM-35 | Each container has a unique identifier | Item created | A unique `FG-nnnn` identifier, enforced by the database | Format and uniqueness enforced by constraint (A) | Pass (A) | `test_schema.py` items constraints, `test_pages.py` QR sticker tests |
| TM-36 | QR code generation | Identifier requested as a sticker | A scannable PNG containing the item's URL | Sticker generated and served (A) | Pass (A) | `test_pages.py` QR sticker and sheet tests |
| TM-37 | QR scanning identifies an item | Label scanned, or the identifier typed | The item's page opens. The manual box works with no camera. | Both routes reach the item page (A) | Pass (A) | `test_pages.py` scan and item-lookup tests |
| TM-38 | Scanning on a real device | Printed label, phone camera on the LAN | The camera reads the label and the item page opens | | | **H/M** — _______________ |
| TM-39 | Dashboard shows everything the SRS lists | Signed-in dashboard | Sensor readings, inventory, expiry information, storage duration, freshness status, fault state, active alerts | All seven present. Storage duration is shown as "Stored for N days" in the inventory and on the item page, and inline in the planner. | Pass (A, S) | `test_pages.py::TestSrsDisplayRequirements`, `docs/evidence/dashboard-desktop.png` |
| TM-40 | Sensor fault is visible on the dashboard | Required sensor faulty | "Sensor Fault/Data Unavailable" shown in place of a status. Never Fresh. | Fault wording shown on the sensors page and the dashboard banner (A) | Pass (A) | `test_pages.py::TestSensorsPage::test_a_faulty_sensor_shows_the_fault_wording`, `test_api.py::TestStatus::test_a_faulty_sensor_is_never_fresh` |
| TM-41 | Data visualisation | Dashboard and reports pages | Readings and records shown as charts, tables and summaries | Chart with zone and metric selectors; counts by category, status, zone and alert type | Pass (S) | `docs/evidence/dashboard-desktop.png`, `docs/evidence/reports-desktop.png` |
| TM-42 | Consumption planning | Items in store | A use-first order, and a way to record what was eaten or thrown away | Ordered by earliest deadline; consumed and discarded both recorded (A) | Pass (A) | `test_pages.py::TestPlannerPage` (4 cases), `docs/evidence/planner-desktop.png` |
| TM-43 | The dashboard is responsive | 360 px and desktop widths | Usable at both, no horizontal scrolling | Every page captured at both widths | Pass (S) | `docs/evidence/*-360px.png` and `*-desktop.png` (10 pages) |
| TM-44 | The safety disclaimer is always present | Every page, including sign-in | "FreshGuard does not certify food safety." | Present on every page | Pass (A) | `test_pages.py::TestDisclaimer` (all pages parametrised) |
| TM-45 | No page claims food is safe or unsafe | Every page | Neither claim appears anywhere | Neither phrase appears on any page | Pass (A) | `test_pages.py::TestDisclaimer::test_no_page_claims_food_is_safe` |
| TM-65 | The SRS recommendations are the ones shown | Each status and the door alert | "Use the food soon", "Check the storage temperature", "Close the refrigerator door", "Inspect the food before use", and the disposal line after human inspection | All five present. The temperature wording belongs to a conditions breach, not to a sensor fault. The disposal line appears only on a Check Food item and only as conditional on what a person finds. | Pass (A) | `test_engine.py::TestSrsRecommendations`, `test_pages.py::TestSrsDisplayRequirements::test_the_disposal_wording_appears_only_for_check_food` |

## 7. Administration and security

| Test ID | Test Scenario | Input/Condition | Expected Result | Actual Result | Pass/Fail | Evidence |
|---|---|---|---|---|---|---|
| TM-46 | An administrator configures every limit | Temperature, humidity, gas, storage duration, door-open time | All configurable; changes take effect; nonsensical values refused | All seven limit kinds editable; invalid values refused (A) | Pass (A) | `test_api.py::TestAdminApi`, `docs/evidence/admin-desktop.png` |
| TM-47 | A limit cannot be saved without a source | Threshold saved with an empty source note | Refused. Every limit cites a source or says "prototype/demo assumption". | Refused by the database and by the API (A) | Pass (A) | `test_api.py::TestAdminApi`, `test_schema.py` threshold constraints |
| TM-48 | Passwords are hashed | Account created | No plaintext password is stored anywhere | Only a `werkzeug.security` hash is stored (A) | Pass (A) | `test_auth.py::TestSeedAdmin`, `test_auth.py::TestUsers`, `test_schema.py::TestUsers` |
| TM-49 | Protected pages require sign-in | Every page requested anonymously | Redirected to sign-in | All pages redirect (A) | Pass (A) | `test_pages.py::TestAccess::test_anonymous_visitors_are_redirected` |
| TM-50 | Admin routes are enforced server-side | Staff account opens an admin route | Refused with 403, regardless of what the browser shows | Refused (A) | Pass (A) | `test_pages.py::TestAccess::test_staff_cannot_open_the_admin_page`, `test_api.py::TestAuthorisation` |
| TM-51 | Forms are protected against cross-site requests | Write submitted without the token | Refused | Refused on every write path (A) | Pass (A) | `test_auth.py::TestCsrf` |
| TM-52 | Repeated failed sign-ins are slowed | Several wrong passwords | Each failure is delayed | Delay applied per account (A) | Pass (A) | `test_auth.py::TestThrottle` |

## 8. Hardware — to be completed by the person who runs the prototype

Nothing in this section can be satisfied by the simulator. Until these rows are
filled in, the project has not demonstrated a working IoT prototype, only a
working system driven by simulated readings.

| Test ID | Test Scenario | Input/Condition | Expected Result | Actual Result | Pass/Fail | Evidence |
|---|---|---|---|---|---|---|
| TM-53 | Real sensors drive the same pipeline | `FG_SENSOR_MODE=real`, wired prototype | Readings from the real DHT22, MQ-135 and reed switch flow through the same engine, database and dashboard | | | **H** — _______________ |
| TM-54 | DHT22 readings are plausible | Prototype at room temperature, compared with a reference thermometer | Within the manufacturer's stated tolerance | | | **H** — record both readings: _______________ |
| TM-55 | MQ-135 warm-up and baseline | Sensor powered and left to stabilise per its datasheet | A documented baseline recorded under controlled conditions, before any monitoring | | | **H** — `docs/calibration.md` |
| TM-56 | Gas readings are relative, never ppm | Abnormal gas provoked on the prototype | A raw or relative figure shown. No ppm anywhere, since the sensor is not calibrated for it. | | | **H** — _______________ |
| TM-57 | Reed switch detects the door | Door opened and closed by hand, repeatedly | Every open and close detected. No bounce-induced false events. | | | **H** — _______________ |
| TM-58 | Buzzer is audible | Confirmed violation on the prototype | The buzzer sounds and is audible at the fridge | | | **H** — _______________ |
| TM-59 | LED indicators | Each status provoked in turn | Green Fresh/Normal, yellow Use Soon, red Check Food. Red, never green, on a fault. | | | **H** — _______________ |
| TM-60 | LCD displays the required fields | Prototype running | Temperature, humidity, gas reading, status and warnings. "Sensor Fault/Data Unavailable" on a fault. | | | **H** — photograph the display: _______________ |
| TM-61 | Physically disconnect a sensor | A sensor unplugged while running | Fault state within one poll. Never Fresh. System keeps running on the remaining inputs. | | | **H** — _______________ |
| TM-62 | RTC keeps time without power | Controller powered down, battery fitted, powered up later | Date and time retained; storage durations still correct | | | **H** — record both timestamps: _______________ |
| TM-63 | Safe interfacing | Wiring inspected before first power-on | MQ-135 through a divider; no 5 V on a Pi GPIO pin; common ground | | | **H** — `docs/pin_map.md`, wiring photograph |
| TM-64 | Power and wiring plan matches the build | Built prototype compared with the documented plan | They match, or the document is corrected to match reality | | | **H** — `docs/pin_map.md` |

## Summary

| Section | Rows | Verified | Outstanding |
|---|---|---|---|
| 1. Sensor reading and freshness status | 15 | 15 (A) | — |
| 2. Local alert timing | 4 | 3 (S) | TM-19 (H) |
| 3. Local operation without a network | 4 | 3 (A) | TM-23 (H) |
| 4. Synchronisation and duplicate prevention | 6 | 6 (A, M) | — |
| 5. Remote notification | 6 | 6 (A, S) | TM-33 email leg and phone screenshot |
| 6. Food registration, QR and the dashboard | 13 | 12 (A, S) | TM-38 (H/M) |
| 7. Administration and security | 7 | 7 (A) | — |
| 8. Hardware | 12 | 0 | **All 12 (H)** |
| **Total** | **67** | **53** | **14** |

### What the outstanding rows mean

| | |
|---|---|
| **TM-33** | The push leg is delivered and read back. What is left is a screenshot of it arriving on the phone, and confirmation that the email reached the mailbox rather than only the mail server. |
| **TM-53 to TM-64** | The prototype has not been demonstrated on hardware. The Phase 4 tag records "real sensors drive the same pipeline" as **not met**. |
| **TM-19, TM-23, TM-38** | Timing, offline behaviour and scanning are proven in software but not on the physical build. |


## Re-running the automated rows

| What | Command |
|---|---|
| Every automated row | `pytest` (749 tests) |
| Engine rules only | `pytest -m engine` (19 tests) |
| Pi driver logic, against fakes | `pytest tests/test_hardware.py` (27 tests) |
| Alert-path timing | `python scripts/measure_alert_path.py --runs 20` |
| Dashboard screenshots | `python scripts/capture_evidence.py --base <url> --password <password> --color-scheme both` |

## Review record

| Date | Reviewer | Change |
|---|---|---|
| | | |
