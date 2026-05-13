-- world.db: script_event_log
-- Append-only log of YAML script events that fired (trigger + effect snapshot in JSON payload).
CREATE TABLE IF NOT EXISTS script_event_log (
    event_id       TEXT NOT NULL,                 -- author-supplied id (C2; reload-dedup key)
    triggered_at   INTEGER NOT NULL,
    payload        TEXT NOT NULL,                 -- JSON: { trigger: ..., effect: ... }
    PRIMARY KEY (event_id, triggered_at)
);

CREATE INDEX IF NOT EXISTS idx_script_log_time
    ON script_event_log(triggered_at);
