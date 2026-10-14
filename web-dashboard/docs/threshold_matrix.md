# Food Threshold Matrix

Owner: SF-SensorForce  ·  Last reviewed: 2026-09-29

Storage limits per food category, and gas limits per zone.

**FreshGuard does not certify food safety.** These are monitoring thresholds for
a student prototype, not food-safety standards.

## Rule for every row

Every threshold must carry a **source note**: either a citation, or the literal
words `prototype/demo assumption`. A row with an empty source note is rejected
by the database and must not be demonstrated.

Leave a value blank until it has been agreed and its source recorded. Do not
copy values from another team's report.

## Category thresholds

One row per food category. A category with no row cannot have items assigned to
it.

| Field | Meaning | Unit |
|---|---|---|
| Category | Name shown in the inventory and on the item form | text |
| Min temperature | Below this is a breach | °C |
| Max temperature | Above this is a breach | °C |
| Min humidity | Below this is a breach | % RH |
| Max humidity | Above this is a breach | % RH |
| Storage limit | Days from the storage date before the item is over its limit | whole days |
| Use soon | Days before the deadline at which the item becomes Use Soon | whole days |
| Source note | Citation, or `prototype/demo assumption` | text, required |

Constraints enforced by the database: minimum below maximum, humidity within
0–100, storage limit greater than zero, use-soon not longer than the storage
limit, source note not empty.

These are the values the system is **currently configured with**, read from the
live database on 2026-09-29. Every one is labelled a prototype/demo assumption
because that is exactly what it is: none has been checked against a
food-storage authority.

Replacing a row means changing the value in the running system and citing a
source here. Both have to happen: the dashboard reads the database, not this
file.

| Category | Min temp (°C) | Max temp (°C) | Min humidity (%) | Max humidity (%) | Storage limit (days) | Use soon (days) | Source note |
|---|---|---|---|---|---|---|---|
| bakery | 0 | 25 | 20 | 70 | 7 | 2 | prototype/demo assumption |
| beverage | 0 | 8 | 20 | 80 | 21 | 4 | prototype/demo assumption |
| cooked | 0 | 5 | 30 | 70 | 3 | 1 | prototype/demo assumption |
| dairy | 0 | 5 | 30 | 70 | 7 | 2 | prototype/demo assumption |
| fish | 0 | 4 | 50 | 90 | 2 | 1 | prototype/demo assumption |
| meat | 0 | 4 | 50 | 85 | 3 | 1 | prototype/demo assumption |
| pantry | 0 | 25 | 20 | 70 | 180 | 14 | prototype/demo assumption |
| produce | 2 | 10 | 60 | 95 | 10 | 3 | prototype/demo assumption |
| rootveg | 2 | 12 | 50 | 90 | 30 | 5 | prototype/demo assumption |

`bakery` and `pantry` deliberately allow up to 25 °C: they hold shelf-stable
goods (biscuits, jarred spreads, sauces, protein bars) that do not need
refrigeration, so a fridge-only range would report them as breaching whenever
the zone is at room temperature. The other seven are refrigerated ranges.

Note that `scripts/seed_thresholds.py` seeds only the original five categories.
`bakery`, `pantry`, `fish` and `meat` were added to the database directly on
2026-09-29 and are not yet in the seed script, so a fresh install will not have
them.

An administrator can change any of these from the Settings page, and a row
cannot be saved without a source note.

## Zone gas thresholds

Gas limits belong to the **zone**, not to a food category: one air-quality
sensor serves the whole zone, and a gas reading is never attributed to a single
item.

| Field | Meaning | Unit |
|---|---|---|
| Zone | Storage area sharing one sensor set | text |
| Clean-air baseline | Reading recorded in clean air after the sensor has warmed up | raw / relative |
| Abnormal threshold | Above this, and confirmed, the zone is reported as abnormal | raw / relative |
| Source note | Citation, or `prototype/demo assumption` | text, required |

Readings are **raw or relative values, never ppm**, unless the sensor has been
calibrated against a known reference and that calibration is recorded in
`docs/calibration.md`. The abnormal threshold must be above the baseline, and a
zone that has an abnormal threshold must have a source note.

| Zone | Clean-air baseline | Abnormal threshold | Warm-up time observed | Source note |
|---|---|---|---|---|
| Main shelf | 1200 | 2500 | not recorded | prototype/demo assumption |

The baseline and threshold above are the configured demo values in raw ADC
counts, **not** measured ones. A clean-air baseline of **1.9933 V** has since
been measured on an ESP32 bench rig (see `docs/calibration.md`), but it is in
volts, the gain used was not recorded, and it has not been converted into the
counts this hardware produces. The raw values the Pi has reported (5056 – 10123)
match neither figure. Reconciling the measurement with the configured values is
outstanding, and until it is done the zone gas threshold should be treated as an
untuned demo setting.

## Global settings

| Setting | Value | Unit | Source note |
|---|---|---|---|
| Door-open timeout | 80 | seconds | prototype/demo assumption, administrator-configurable; live value read from the database 2026-09-29 |
| Polling interval | 1 | seconds | `FG_POLL_INTERVAL_S`; the DHT22 answers about once every two seconds |
| Consecutive readings to confirm a breach | 3 | readings | prototype/demo assumption, to suppress single-reading false alarms |
| Hysteresis margin to clear a breach | not recorded | same unit as the measurement | outstanding |

## Where these values are used

| Value | Used by |
|---|---|
| Temperature and humidity ranges | Decision rule 3 |
| Storage limit | Decision rule 1 |
| Use soon | Decision rule 5 |
| Zone gas abnormal threshold | Decision rule 4 |
| Door-open timeout | Door-open alert |

Thresholds are stored in the database and edited by an administrator in the app.
Nothing here is fixed in the program, so changing a value does not require a code
change. Record any change below and in the app on the same day.

## Change record

| Date | Field changed | Old value | New value | Reason | Changed by |
|---|---|---|---|---|---|
| | | | | | |
