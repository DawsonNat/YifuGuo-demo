-- world.db: group_member (relocated from OASIS, table singularised, FKs dropped)
-- Original FKs to user(agent_id) and chat_group(group_id) removed: world.db has no user table
-- and we avoid cascade behaviour mismatches. Consistency is enforced by GroupMessageBus.
CREATE TABLE IF NOT EXISTS group_member (
    group_id     INTEGER NOT NULL,
    agent_id     INTEGER NOT NULL,
    joined_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_group_member_agent ON group_member(agent_id);
