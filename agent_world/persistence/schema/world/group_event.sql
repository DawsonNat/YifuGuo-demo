-- world.db: group_event (B6)
-- join/leave/kick events; PerceptionBuilder pulls occurred_at == t-1 rows into obs.group_events
-- for one-tick passthrough. Rows are retained for post-hoc reporting.
CREATE TABLE IF NOT EXISTS group_event (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    agent_id     INTEGER NOT NULL,                -- subject of the event
    event_type   TEXT NOT NULL CHECK (event_type IN ('join','leave','kick')),
    occurred_at  INTEGER NOT NULL,
    actor_id     INTEGER                          -- initiator (kick: kicker; join/leave: = agent_id)
);

CREATE INDEX IF NOT EXISTS idx_group_event_lookup
    ON group_event(group_id, occurred_at);
