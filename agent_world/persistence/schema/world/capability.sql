-- world.db: capability
-- Granted/revoked timestamps (no row deletion) for auditability.
CREATE TABLE IF NOT EXISTS capability (
    agent_id     INTEGER NOT NULL,
    capability   TEXT NOT NULL,
    granted_at   INTEGER NOT NULL,
    revoked_at   INTEGER,                         -- NULL = currently active
    metadata     TEXT,                            -- JSON, optional
    PRIMARY KEY (agent_id, capability, granted_at)
);

-- Partial index over currently-active grants
CREATE INDEX IF NOT EXISTS idx_capability_active
    ON capability(agent_id, capability) WHERE revoked_at IS NULL;
