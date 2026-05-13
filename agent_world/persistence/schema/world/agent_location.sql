-- world.db: agent_location
-- Persistent snapshot of each agent's current place; reverse index lives in PlaceStore memory.
CREATE TABLE IF NOT EXISTS agent_location (
    agent_id     INTEGER PRIMARY KEY,             -- one row per agent
    place_id     TEXT NOT NULL,
    arrived_at   INTEGER NOT NULL,                -- world.t when agent entered this place
    FOREIGN KEY (place_id) REFERENCES place(place_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_location_place ON agent_location(place_id);
