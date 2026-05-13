-- world.db: group_message (relocated from OASIS, table singularised, FKs dropped)
-- Source-of-truth for group content; per-member fan-out copies live in direct_message
-- (channel_type='GRP', group_id=?) so PerceptionBuilder uses one path for incoming.
CREATE TABLE IF NOT EXISTS group_message (
    message_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    sender_id    INTEGER NOT NULL,
    content      TEXT NOT NULL,
    sent_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_group_message_group ON group_message(group_id, sent_at);
