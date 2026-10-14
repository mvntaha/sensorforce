-- Migration 003 -- consumption log.
--
-- Recording what became of an item now removes the item. A kitchen reuses its
-- printed QR stickers, and an item that has been eaten is not occupying a
-- shelf, so holding the row forever would leave the inventory full of things
-- that are not there and the identifier permanently spent.
--
-- The history therefore cannot point at the item: it has to carry its own
-- copy of what was consumed. Every column below that duplicates an item field
-- is duplicated deliberately, because the row it came from is gone by the time
-- anyone reads this table.
--
-- This supersedes the lifecycle state added in migration 002. That column
-- stays -- SQLite makes dropping one awkward, and an unused column with a
-- default is harmless -- but nothing writes anything other than 'stored' to it
-- any more. The history is here.
--
-- Recording a disposal is a record of a decision a person already made after
-- looking at the food. It is never FreshGuard concluding that food was unsafe.

CREATE TABLE IF NOT EXISTS consumption_log (
    id                    INTEGER PRIMARY KEY,
    -- UUID4, and the outbox key, so the cloud replica sees this exactly once.
    event_id              TEXT    NOT NULL UNIQUE,

    -- The item as it was. No foreign key: the row is deleted in the same
    -- transaction that writes this one.
    item_id               INTEGER,
    qr_id                 TEXT    NOT NULL,
    item_name             TEXT    NOT NULL,
    category              TEXT    NOT NULL,
    zone_id               INTEGER,
    zone_name             TEXT,
    quantity              REAL,
    unit                  TEXT,
    storage_date          TEXT,
    expiry_date           TEXT,
    days_stored           INTEGER,

    -- What happened, and what the engine thought of it at that moment. The
    -- verdict is frozen here: the readings that produced it will roll off.
    outcome               TEXT    NOT NULL CHECK (outcome IN ('consumed', 'discarded')),
    status_at_consumption TEXT    NOT NULL,

    consumed_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_by            INTEGER REFERENCES users (id)
                                  ON UPDATE CASCADE ON DELETE SET NULL,

    CHECK (qr_id GLOB 'FG-[0-9][0-9][0-9][0-9]'),
    CHECK (length(trim(item_name)) > 0),
    CHECK (length(trim(category)) > 0),
    CHECK (quantity IS NULL OR quantity > 0),
    CHECK (days_stored IS NULL OR days_stored >= 0),
    CHECK (datetime(consumed_at) IS NOT NULL)
);

-- The reports page reads this newest-first and counts by outcome.
CREATE INDEX IF NOT EXISTS idx_consumption_at      ON consumption_log (consumed_at);
CREATE INDEX IF NOT EXISTS idx_consumption_outcome ON consumption_log (outcome);
-- A sticker is reused, so one identifier legitimately appears many times.
CREATE INDEX IF NOT EXISTS idx_consumption_qr      ON consumption_log (qr_id);
