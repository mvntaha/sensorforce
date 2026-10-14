-- Migration 002 -- item lifecycle.
--
-- The consumption planner needs to record what happened to an item: it was
-- eaten, or it was thrown away. Deleting the row would lose exactly the
-- information the food-waste report is built from, so the row stays and
-- carries a state instead.
--
-- 'stored' is the only state the freshness engine looks at. A consumed or
-- discarded item keeps its dates and category so the reports page can count
-- it, but it no longer occupies a place in the kitchen and is never assessed.
--
-- Recording a disposal is a record of a decision the user already made. It is
-- never FreshGuard telling anyone that food was unsafe.

ALTER TABLE items ADD COLUMN state TEXT NOT NULL DEFAULT 'stored'
    CHECK (state IN ('stored', 'consumed', 'discarded'));

ALTER TABLE items ADD COLUMN state_changed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_items_state ON items (state);
