# Sensor Calibration and Baselines

Owner: SF-SensorForce  ·  Last reviewed: 2026-09-29  ·  Ambient during calibration: indoor room, not recorded

Recorded values for the fitted sensors. Every number here is measured on real
hardware, not copied from a datasheet. Where a figure is still the configured
demo value rather than a measurement, it says so.

**FreshGuard does not certify food safety.** These figures describe the
instruments, not the food.

## MQ-135 air quality

The MQ-135 has a heater that must warm up before its readings mean anything,
and it reports a **relative** value. This prototype does not convert to ppm:
that would need a reference gas the team does not have, so the dashboard shows
raw ADC counts and the label says so.

Wiring note: the analog output is connected **directly** to ADS1115 A0 with no
voltage divider. See the safety note in `docs/pin_map.md`.

### Warm-up

The software reports a gas fault until the warm-up has elapsed, so a cold
sensor can never produce a misleading number.

| Item | Value | Notes |
|---|---|---|
| Configured warm-up | 180 s | `MQ135_WARMUP_S` in `core/sensors/real.py` |
| Observed time to settle | not recorded | outstanding |
| Adjusted warm-up, if changed | unchanged | |

### Clean-air baseline — measured

Measured on an **ESP32 bench rig** (Arduino IDE, COM3, `Adafruit_ADS1X15.h`),
not on the Pi. The serial monitor reports volts, not raw counts.

| Item | Value |
|---|---|
| Clean-air baseline | **1.9933 V** |
| Observed clean-air spread | ≈1.9896 – 2.0056 V |
| Value during a detection event | ≈2.06 – 2.53 V |
| Rig | ESP32 Dev Module, ADS1115, `Adafruit_ADS1X15.h` |
| Evidence | serial monitor capture, lines labelled `Clean Air` and `GAS/ALCOHOL DETECTED` |

### Abnormal threshold — configured

The values the running system uses are stored on the zone, in **raw ADC counts**:

| Item | Value | How it was chosen |
|---|---|---|
| `gas_baseline` | 1200 | configured demo value |
| `gas_abnormal` | 2500 | configured demo value |
| Source note | `prototype/demo assumption` | |

**These two figures are not derived from the 1.9933 V measurement above, and the
two have not been reconciled.** The bench rig measured volts; the database
stores raw counts; no conversion has been recorded, and the gain setting used on
the bench rig is not noted in the capture. The raw values the Pi has actually
reported (5056 – 10123) match neither. Converting the measured baseline into the
counts this hardware produces, and reseeding the zone from it, is outstanding
work.

The device firmware uses a third, independent rule: `CHANGE_DELTA = 300` raw
counts of drift from a baseline captured at startup
(`reference/hardware/pi_monitor.py`). This is not the database threshold and
does not feed the freshness engine.

Record these on the **zone**, not on a food category: one sensor serves the
whole zone, and a gas reading is never attributed to a single item.

### Known fault

The MQ-135 reports intermittently on the built board. Firebase payloads show
`components.mq135` flipping between `OK` and `I2C_DISCONNECTED` within minutes,
and while disconnected the firmware continues to publish its **last good value**
unchanged for minutes at a time. The ingest layer detects this and discards the
stale value rather than storing it, so no fabricated gas reading reaches the
database. The underlying I²C fault has not been diagnosed.

## DHT22 temperature and humidity

The fitted sensor is a **DHT22** (the SRS and `core/sensors/real.py` both say
DHT11; the board has a DHT22). Roughly ±0.5 °C and ±2 – 5 % RH, one decimal
place, and about one reading every two seconds.

| Check | Reference instrument | DHT22 | Difference |
|---|---|---|---|
| Room temperature | none used | 31.1 °C (LCD, photographed) | not compared |
| Room temperature | none used | 24.5 – 24.9 °C (Firebase, later session) | not compared |
| Room humidity | none used | 71 % (LCD, photographed) | not compared |
| Room humidity | none used | 45.2 – 47.5 % (Firebase, later session) | not compared |
| Fridge temperature | not taken | | outstanding |

| Item | Value |
|---|---|
| Reference instrument used | **none** — no calibrated thermometer or hygrometer was used |
| Failed reads observed in 100 polls | not counted; occasional missing values seen in the cloud payloads |
| Offset applied | none |

No comparison against a reference instrument has been made, so the sensor's
accuracy is unverified. The readings above are what it reports, not proof that
it is correct.

A failed read is recorded as a fault, never as a value, so an occasional
failure is expected and harmless. A **persistent** failure shows as
Sensor Fault/Data Unavailable.

## Reed switch

| Item | Value | Notes |
|---|---|---|
| Configured debounce | 50 ms | `REED_DEBOUNCE_S` in `core/sensors/real.py` |
| Bounce observed on closing | not measured | outstanding |
| Magnet gap at which it releases | not measured | outstanding |
| Reads HIGH when open? | not verified | polarity untested on this board |

The switch is wired (GPIO17, pin 11) and reported working, but the firmware
publishing to Firebase sends no door state, so no door reading has reached the
database. `door_open` is recorded as 0 on every ingested row — a default, not a
measurement.

## DS3231 real-time clock

| Check | Result |
|---|---|
| Module keeps and displays time | yes — LCD photographed showing `27/09/2026 15:33:52` |
| Time correct after a power cycle with no network | not tested |
| Drift observed over the project | not measured |
| Kernel RTC overlay enabled on the Pi | not confirmed |

The module works as a clock. It is not yet confirmed as the Pi's system clock
via `dtoverlay=i2c-rtc,ds3231`.

## RC522 RFID reader

| Item | Value |
|---|---|
| Tag UID read | `09 FE 01 07`, read repeatedly |
| Rig | **ESP32 Dev Module, COM3** — not the Pi |
| Read range | not measured |
| Pi-side verification | reported working; no capture on file |

No RC522 driver exists in this repository, so the reader is not integrated with
FreshGuard on either board.

## Pi Camera

| Item | Value |
|---|---|
| Physically connected | yes, CSI ribbon, photographed |
| Capture verified | reported working; no capture on file |
| Software in this repository | none |

## Re-calibration

Redo the MQ-135 baseline if the sensor is replaced, the zone changes, or the
readings drift noticeably. Record each run:

| Date | Reason | New baseline | New threshold | By |
|---|---|---|---|---|
| | | | | |
