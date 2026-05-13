-- world.db: place
-- Hierarchical place registry; loaded into PlaceStore at startup.
CREATE TABLE IF NOT EXISTS place (
    place_id     TEXT PRIMARY KEY,
    parent_id    TEXT,                       -- NULL = top-level
    place_type   TEXT NOT NULL,              -- conscribe-registered place_type name
    capacity     INTEGER,                    -- NULL = unbounded
    attrs        TEXT NOT NULL DEFAULT '{}', -- JSON: timezone / behavior_hint / custom
    created_at   INTEGER NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES place(place_id)
);

CREATE INDEX IF NOT EXISTS idx_place_parent ON place(parent_id);
