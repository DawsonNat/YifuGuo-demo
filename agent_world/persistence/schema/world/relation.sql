-- world.db: relation
-- Directed agent-agent relations; symmetric/exclusive semantics enforced in RelationGraph.
CREATE TABLE IF NOT EXISTS relation (
    src_agent       INTEGER NOT NULL,
    dst_agent       INTEGER NOT NULL,
    relation_type   TEXT NOT NULL,                -- conscribe-registered relation_type
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER,                      -- NULL = permanent
    metadata        TEXT,                         -- JSON, optional
    PRIMARY KEY (src_agent, dst_agent, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_relation_src ON relation(src_agent, relation_type);
CREATE INDEX IF NOT EXISTS idx_relation_dst ON relation(dst_agent, relation_type);
