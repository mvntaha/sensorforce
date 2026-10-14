-- Migration 005 -- RFID tags.
--
-- The Pi reads RFID tags stuck on containers. A tap toggles the tag in or out
-- of the fridge, and the Pi logs each tap to pi_monitor/rfid_history with the
-- tag's id and ADDED or REMOVED. The ingest worker replays those taps here:
--
--   * a tag tapped in that no item carries waits in rfid_pending until someone
--     fills in what the container holds; the form links the tag to the item;
--   * a tag tapped out removes its item, recorded as consumed, exactly as if
--     someone had pressed "eaten" on the dashboard.
--
-- The item keeps its FG-nnnn identifier. The tag is a second way to find it,
-- so QR labels, links and every page that names an item by FG-nnnn still work.

-- No UNIQUE in ADD COLUMN; the partial index below enforces one item per tag.
ALTER TABLE items ADD COLUMN rfid_tag TEXT
    CHECK (rfid_tag IS NULL OR (length(rfid_tag) BETWEEN 4 AND 32 AND rfid_tag NOT GLOB '*[^0-9]*'));

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_rfid_tag ON items (rfid_tag) WHERE rfid_tag IS NOT NULL;

-- Tags tapped in that no item carries yet: "details needed".
CREATE TABLE IF NOT EXISTS rfid_pending (
    tag_id        TEXT    PRIMARY KEY
                          CHECK (length(tag_id) BETWEEN 4 AND 32 AND tag_id NOT GLOB '*[^0-9]*'),
    device_id     TEXT    NOT NULL,
    -- When the tag was tapped in, from the Pi. Becomes the item's storage date.
    tapped_in_at  TEXT    NOT NULL,
    -- The name the Pi gave the tag, e.g. Item-0456. A hint only.
    device_label  TEXT,

    CHECK (datetime(tapped_in_at) IS NOT NULL)
);

-- Every tap the worker has applied, in order. The cloud key makes replaying a
-- tap harmless.
CREATE TABLE IF NOT EXISTS rfid_events (
    id            INTEGER PRIMARY KEY,
    device_id     TEXT    NOT NULL,
    cloud_key     TEXT    NOT NULL UNIQUE,
    tag_id        TEXT    NOT NULL,
    action        TEXT    NOT NULL CHECK (action IN ('ADDED', 'REMOVED', 'OTHER')),
    tapped_at     TEXT    NOT NULL,
    -- What the dashboard did about it:
    --   pending   -- tag waits for details
    --   consumed  -- its item was removed and logged as consumed
    --   cleared   -- a tag still waiting for details was tapped out again
    --   ignored   -- nothing to do (tag already known, or already gone)
    result        TEXT    NOT NULL CHECK (result IN ('pending', 'consumed', 'cleared', 'ignored')),
    qr_id         TEXT,
    applied_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_rfid_events_device ON rfid_events (device_id, cloud_key);

-- Where in pi_monitor/rfid_history the worker has read up to, per device. Kept
-- apart from rfid_events so the first run can start from "now" without
-- inventing events for the taps that came before it.
CREATE TABLE IF NOT EXISTS rfid_cursor (
    device_id     TEXT    PRIMARY KEY,
    last_key      TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
