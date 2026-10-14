-- FreshGuard schema, version 1.
--
-- Conventions
--   * All timestamps are UTC ISO-8601 text: 'YYYY-MM-DDTHH:MM:SSZ'.
--   * All calendar dates are text: 'YYYY-MM-DD'.
--   * Booleans are INTEGER 0 or 1, constrained by CHECK.
--   * PRAGMA foreign_keys is per connection and is set in core/db.py.
--
-- This file is frozen at the end of Phase 1. Later changes go into
-- core/migrations/NNN_name.sql and bump schema_version.

PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------------------
-- schema_version
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    -- werkzeug.security hash only. A plaintext password is never stored.
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('admin', 'staff')),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    CHECK (length(trim(username)) > 0),
    CHECK (length(password_hash) > 0)
);

-- ---------------------------------------------------------------------------
-- zones -- one zone shares one DHT11, one MQ-135 and one reed switch
--
-- Gas limits live here, not on thresholds: one MQ-135 serves a whole zone, and
-- SRS rule 3 forbids attributing a gas reading to a single item. Values are
-- raw ADC counts or a relative index recorded after MQ-135 warm-up; they are
-- never ppm unless the sensor has been calibrated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zones (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    gas_baseline INTEGER,
    gas_abnormal INTEGER,
    -- SRS rule 4: cite a source or say 'prototype/demo assumption'.
    gas_source_note TEXT,

    CHECK (length(trim(name)) > 0),
    CHECK (gas_baseline IS NULL OR gas_baseline >= 0),
    CHECK (gas_abnormal IS NULL OR gas_baseline IS NULL OR gas_abnormal > gas_baseline),
    -- A usable gas limit must be documented. Spelled out rather than relying on
    -- length(trim(NULL)), which is NULL and would let a CHECK pass silently.
    CHECK (gas_abnormal IS NULL
           OR (gas_source_note IS NOT NULL AND length(trim(gas_source_note)) > 0))
);

-- ---------------------------------------------------------------------------
-- thresholds -- one row per food category; doubles as the category list
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS thresholds (
    category           TEXT    PRIMARY KEY,
    temp_min           REAL    NOT NULL,
    temp_max           REAL    NOT NULL,
    hum_min            REAL    NOT NULL,
    hum_max            REAL    NOT NULL,
    storage_limit_days INTEGER NOT NULL,
    use_soon_days      INTEGER NOT NULL,
    -- Gas limits are deliberately NOT here: gas is zone-level (see zones).
    -- SRS rule 4: every threshold cites a source or says
    -- 'prototype/demo assumption'. Enforced as non-empty here.
    source_note        TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    CHECK (length(trim(category)) > 0),
    CHECK (temp_min < temp_max),
    CHECK (hum_min < hum_max),
    CHECK (hum_min >= 0 AND hum_max <= 100),
    CHECK (storage_limit_days > 0),
    CHECK (use_soon_days >= 0 AND use_soon_days <= storage_limit_days),
    CHECK (length(trim(source_note)) > 0)
);

-- ---------------------------------------------------------------------------
-- settings -- global admin-configurable values, e.g. door_open_timeout_s
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT NOT NULL PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    CHECK (length(trim(key)) > 0)
);

-- ---------------------------------------------------------------------------
-- items
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    id                 INTEGER PRIMARY KEY,
    -- QR identifiers are exactly FG-0000 .. FG-9999.
    qr_id              TEXT    NOT NULL UNIQUE,
    name               TEXT    NOT NULL,
    category           TEXT    NOT NULL REFERENCES thresholds (category)
                               ON UPDATE CASCADE ON DELETE RESTRICT,
    quantity           REAL    NOT NULL,
    -- Free-text measure for the quantity above: pcs, kg, g, L, ml, pack ...
    unit               TEXT    NOT NULL DEFAULT 'pcs',
    zone_id            INTEGER NOT NULL REFERENCES zones (id)
                               ON UPDATE CASCADE ON DELETE RESTRICT,
    manufacture_date   TEXT,
    storage_date       TEXT    NOT NULL,
    -- A NULL expiry_date falls back to storage_limit_days, and a NULL
    -- storage_limit_days falls back to the category default.
    expiry_date        TEXT,
    storage_limit_days INTEGER,
    created_by         INTEGER REFERENCES users (id)
                               ON UPDATE CASCADE ON DELETE SET NULL,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    CHECK (qr_id GLOB 'FG-[0-9][0-9][0-9][0-9]'),
    CHECK (length(trim(name)) > 0),
    CHECK (quantity > 0),
    CHECK (length(trim(unit)) > 0),
    CHECK (storage_limit_days IS NULL OR storage_limit_days > 0),
    -- Dates must be real calendar dates in YYYY-MM-DD form.
    CHECK (date(storage_date) IS storage_date),
    CHECK (manufacture_date IS NULL OR date(manufacture_date) IS manufacture_date),
    CHECK (expiry_date IS NULL OR date(expiry_date) IS expiry_date),
    -- Expiry may not precede manufacture or storage.
    CHECK (expiry_date IS NULL OR manufacture_date IS NULL OR expiry_date >= manufacture_date),
    CHECK (expiry_date IS NULL OR expiry_date >= storage_date)
);

CREATE INDEX IF NOT EXISTS idx_items_zone     ON items (zone_id);
CREATE INDEX IF NOT EXISTS idx_items_category ON items (category);
CREATE INDEX IF NOT EXISTS idx_items_expiry   ON items (expiry_date);

-- ---------------------------------------------------------------------------
-- readings -- one row per zone per poll, written only by monitor.py
--
-- temp_ok / hum_ok / gas_ok describe SENSOR HEALTH, not threshold compliance:
-- 0 means the read failed or the value was physically impossible, and the
-- matching value column is then NULL. The CHECK constraints below make the
-- "NULL value with *_ok = 0" invariant impossible to break, which is what
-- stops a faulty sensor from ever being reported as Fresh (SRS rule 1).
-- Threshold comparison is the engine's job, not this table's.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY,
    zone_id   INTEGER NOT NULL REFERENCES zones (id)
                      ON UPDATE CASCADE ON DELETE CASCADE,
    ts        TEXT    NOT NULL,
    temp_c    REAL,
    humidity  REAL,
    -- Raw ADS1115 counts, never ppm (SRS rule 3).
    gas_raw   INTEGER,
    door_open INTEGER NOT NULL CHECK (door_open IN (0, 1)),
    temp_ok   INTEGER NOT NULL CHECK (temp_ok IN (0, 1)),
    hum_ok    INTEGER NOT NULL CHECK (hum_ok  IN (0, 1)),
    gas_ok    INTEGER NOT NULL CHECK (gas_ok  IN (0, 1)),

    CHECK (datetime(ts) IS NOT NULL),
    -- A value is present exactly when its sensor is healthy.
    CHECK ((temp_ok = 1) = (temp_c   IS NOT NULL)),
    CHECK ((hum_ok  = 1) = (humidity IS NOT NULL)),
    CHECK ((gas_ok  = 1) = (gas_raw  IS NOT NULL)),
    -- Plausible ranges for the fitted sensors.
    CHECK (temp_c   IS NULL OR temp_c   BETWEEN -20.0 AND 60.0),
    CHECK (humidity IS NULL OR humidity BETWEEN 0.0 AND 100.0),
    CHECK (gas_raw  IS NULL OR gas_raw  BETWEEN -32768 AND 32767)
);

CREATE INDEX IF NOT EXISTS idx_readings_zone_ts ON readings (zone_id, ts);
CREATE INDEX IF NOT EXISTS idx_readings_ts      ON readings (ts);

-- ---------------------------------------------------------------------------
-- alerts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY,
    -- UUID4 generated at the moment of the local change; also the outbox key.
    event_id       TEXT    NOT NULL UNIQUE,
    zone_id        INTEGER NOT NULL REFERENCES zones (id)
                           ON UPDATE CASCADE ON DELETE CASCADE,
    item_id        INTEGER REFERENCES items (id)
                           ON UPDATE CASCADE ON DELETE SET NULL,
    type           TEXT    NOT NULL CHECK (type IN (
                               'temp_high', 'temp_low',
                               'humidity_high', 'humidity_low',
                               'gas_abnormal', 'door_open',
                               'sensor_fault', 'expiry', 'storage_limit')),
    severity       TEXT    NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    message        TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    acknowledged   INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
    -- Set to 0 once the SMTP notification is delivered; retried on reconnect.
    notify_pending INTEGER NOT NULL DEFAULT 1 CHECK (notify_pending IN (0, 1)),

    CHECK (length(trim(message)) > 0),
    CHECK (datetime(ts) IS NOT NULL),
    -- SRS rule 3: a gas reading is zone-level and is never attributed to an item.
    CHECK (type <> 'gas_abnormal' OR item_id IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_alerts_ts     ON alerts (ts);
CREATE INDEX IF NOT EXISTS idx_alerts_open   ON alerts (acknowledged, ts);
CREATE INDEX IF NOT EXISTS idx_alerts_zone   ON alerts (zone_id, ts);
CREATE INDEX IF NOT EXISTS idx_alerts_notify ON alerts (notify_pending);

-- ---------------------------------------------------------------------------
-- outbox -- written in the SAME transaction as the change it describes
--
-- event_id is the primary key here and the Firebase node name, so a retried
-- write overwrites the same node and duplicates cannot occur.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
    event_id     TEXT    PRIMARY KEY,
    entity       TEXT    NOT NULL CHECK (entity IN (
                             'alert', 'item', 'reading', 'threshold_change')),
    -- Full JSON document sent to Firebase: event_id, entity, op, ts, device_id.
    payload_json TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    sync_status  TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (sync_status IN ('pending', 'synced', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    synced_at    TEXT,

    CHECK (length(trim(payload_json)) > 0),
    -- synced_at is set exactly when the row is synced.
    CHECK ((sync_status = 'synced') = (synced_at IS NOT NULL))
);

-- Drives the sync worker's "oldest pending or failed first" query.
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox (sync_status, created_at);

-- ---------------------------------------------------------------------------
-- Initial version row
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
