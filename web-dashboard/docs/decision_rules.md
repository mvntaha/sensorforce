# Freshness Decision Rule Matrix

Owner: SF-SensorForce  ·  Last reviewed: 2026-09-29

How FreshGuard turns an item's dates and its zone's confirmed sensor state into
one of four statuses.

**FreshGuard does not certify food safety.** No rule below declares food safe or
unsafe. A status is a prompt for a person to look, not a verdict.

## How to read this

Rules are evaluated in order and **the first match wins**. A later rule is only
reached when every earlier rule did not match. Rule numbers are stable and are
referenced by the dashboard, the IoT Test Matrix and the automated tests.

"Confirmed" means the condition survived the signal rules in the next section:
a single out-of-range reading never reaches this table.

## Statuses

| Status | Shown as | Meaning |
|---|---|---|
| Fresh/Normal | `Fresh/Normal` | No rule matched. |
| Use Soon | `Use Soon` | Approaching its deadline. |
| Check Food | `Check Food` | A person should inspect the item. |
| Sensor Fault/Data Unavailable | `Sensor Fault/Data Unavailable` | A required measurement is missing, so freshness cannot be assessed. |

Status is never communicated by colour alone; every badge carries its text.

## The rules

| # | Condition | Status | Message describes | Applies to |
|---|---|---|---|---|
| 1 | Item is past its deadline, or its storage limit has been exceeded | Check Food | The item | One item |
| 2 | A required zone sensor is faulty or its data is unavailable | Sensor Fault/Data Unavailable | The zone's sensor | Every item in the zone |
| 3 | Zone temperature or humidity is outside the category range (confirmed) | Check Food | The zone's conditions | Every item in the zone |
| 4 | Zone gas reading is above the zone's abnormal threshold (confirmed) | Check Food | **The zone** — never a single item | Every item in the zone |
| 5 | Item is within its "use soon" window of the deadline | Use Soon | The item | One item |
| 6 | None of the above | Fresh/Normal | — | One item |

### Notes on precedence

| Pair | Which wins | Why |
|---|---|---|
| Rule 1 before rule 2 | Expiry | A known problem outranks an unknown one: an expired item is Check Food even when a sensor is down. |
| Rule 2 before rule 3 | Sensor fault | With a failed sensor the out-of-range reading is not trustworthy. |
| Rule 2 before rules 5 and 6 | Sensor fault | A faulty required sensor must never produce Fresh/Normal. |
| Rule 3 before rule 4 | Temperature / humidity | Reported ahead of gas when both are breached. |

### Deadline precedence

| Order | Source of the deadline |
|---|---|
| 1 | The item's own expiry date |
| 2 | The item's storage date plus the item's storage limit |
| 3 | The item's storage date plus the category's storage limit |

Where both an expiry date and a storage limit apply, **the earlier of the two**
is the deadline.

### Required sensors

| Sensor | Required for an item's status? | Reason |
|---|---|---|
| Temperature | Yes | Rule 3 depends on it. |
| Humidity | Yes | Rule 3 depends on it. |
| Gas | No | Gas describes the zone, not an item. A failed gas sensor makes the zone's gas state unknown; it does not make an item's status unknown. |
| Door (reed switch) | No | Drives the door-open alert, not an item's status. |

## Gas is zone-level

One air-quality sensor serves a whole zone, so a gas reading cannot be
attributed to any single item. Rule 4 therefore:

- names the **zone** in its message and never an item;
- raises a zone-level alert that carries no item reference;
- reports a **relative or raw** reading, never a ppm figure, unless the sensor
  has been calibrated and that calibration is recorded.

## Signal rules applied before this table

| Rule | Value | Source |
|---|---|---|
| Consecutive out-of-range readings before a breach is confirmed | 3 | prototype/demo assumption |
| Smoothing | Moving average over the last N readings | prototype/demo assumption |
| Hysteresis margin required to clear a confirmed breach | not implemented | outstanding |
| Reed switch debounce | ~50 ms | prototype/demo assumption |
| Polling interval | 1 s | prototype/demo assumption |
| Identical consecutive readings treated as a stuck sensor | not implemented | outstanding |
| Door-open timeout | Configured by an administrator | Admin setting |

A reading counts as a fault when the driver reports an error, returns no value,
or returns a physically impossible value. A faulty reading neither confirms a
breach nor clears one: an unknown value is not evidence in either direction.

## Recommendations shown with each status

Wording is fixed by the SRS. The disposal line appears **only after a person has
inspected the food and found it unsuitable** — never as an automated conclusion.

| Status | Recommendation |
|---|---|
| Use Soon | Use the food soon |
| Check Food | Inspect the food before use |
| Check Food (temperature) | Check the storage temperature |
| Door open too long | Close the refrigerator door |
| After human inspection only | Follow the appropriate disposal procedure |

## Verification

Each rule is exercised by automated tests driven by the sensor simulator, and
by a row in the IoT Test Matrix (`docs/test_matrix.md`).

| # | Covered by automated test | Test Matrix ID | Evidence |
|---|---|---|---|
| 1 | Expired item, and exceeded storage limit | TM-11, TM-15 | `test_engine.py::TestRule1Expiry` |
| 2 | Faulty temperature sensor, faulty humidity sensor, never Fresh | TM-12, TM-15 | `test_engine.py::TestRule2SensorFault` |
| 3 | Confirmed temperature breach, confirmed humidity breach | TM-03, TM-05, TM-15 | `test_engine.py::TestRule3TemperatureAndHumidity` |
| 4 | Confirmed gas breach, message names the zone, no ppm | TM-06, TM-07, TM-15 | `test_engine.py::TestRule4Gas` |
| 5 | Inside the use-soon window, and at its edge | TM-10, TM-15 | `test_engine.py::TestRule5UseSoon` |
| 6 | Healthy item in a healthy zone | TM-01, TM-15 | `test_engine.py::TestRule6Fresh` |
| Precedence | Expiry over fault, fault over breach, fault over use-soon | TM-15 | `test_engine.py::TestRulePrecedence` |

The hysteresis margin and the stuck-sensor rule above are still blank because
nobody has chosen those values yet. They are settings, not findings: fill them
in, then re-run `pytest -m engine`.

## Review record

| Date | Reviewer | Change |
|---|---|---|
| | | |
