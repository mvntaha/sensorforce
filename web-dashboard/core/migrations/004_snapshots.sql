-- Migration 004 -- on-demand fridge snapshots.
--
-- A person away from home asks the Pi for a photograph of the inside of the
-- fridge. The request is recorded here first; the ingest worker, which holds
-- the cloud connection, raises the device's trigger_snapshot flag, collects
-- the photograph the device uploads, and stores it beside these rows.
--
-- The web app never talks to the cloud. It writes a request and reads back
-- what the worker recorded, exactly as it reads readings and alerts.
--
-- A photograph is a picture, not a measurement: nothing here feeds the
-- freshness engine.

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY,
    device_id     TEXT    NOT NULL,
    -- The device's push key for this capture. Unique, so a capture is stored
    -- once however many times the worker sees it, and kept after deletion so
    -- a deleted photograph is never collected again.
    cloud_key     TEXT    NOT NULL UNIQUE,
    taken_at      TEXT    NOT NULL,
    received_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- Name of the JPEG under var/captures. NULL once deleted.
    file_name     TEXT,
    size_bytes    INTEGER,
    -- Deleting hides the row and removes the file at once; the worker then
    -- removes the cloud copy and sets cloud_deleted.
    deleted       INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    cloud_deleted INTEGER NOT NULL DEFAULT 0 CHECK (cloud_deleted IN (0, 1)),

    CHECK (datetime(taken_at) IS NOT NULL),
    CHECK (size_bytes IS NULL OR size_bytes > 0),
    CHECK (deleted = 1 OR file_name IS NOT NULL)
);

-- The page lists a device's photographs newest first.
CREATE INDEX IF NOT EXISTS idx_snapshots_device_taken ON snapshots (device_id, taken_at);

CREATE TABLE IF NOT EXISTS snapshot_requests (
    id            INTEGER PRIMARY KEY,
    device_id     TEXT    NOT NULL,
    requested_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    requested_by  INTEGER REFERENCES users (id)
                          ON UPDATE CASCADE ON DELETE SET NULL,
    -- pending: recorded, not yet passed to the device
    -- sent:    trigger_snapshot raised, waiting for the photograph
    -- done:    the photograph arrived; snapshot_id names it
    -- failed:  the device did not answer in time, or reported it could not
    status        TEXT    NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'sent', 'done', 'failed')),
    sent_at       TEXT,
    finished_at   TEXT,
    snapshot_id   INTEGER REFERENCES snapshots (id) ON DELETE SET NULL,
    error         TEXT,

    CHECK (datetime(requested_at) IS NOT NULL),
    CHECK (status <> 'done' OR finished_at IS NOT NULL)
);

-- The worker looks up the open requests every cycle.
CREATE INDEX IF NOT EXISTS idx_snapshot_requests_open ON snapshot_requests (device_id, status);
