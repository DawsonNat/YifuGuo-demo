-- world.db: chat_group (relocated from OASIS social_platform/schema/chat_group.sql)
-- group_id is globally unique across places/pools.
CREATE TABLE IF NOT EXISTS chat_group (
    group_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
