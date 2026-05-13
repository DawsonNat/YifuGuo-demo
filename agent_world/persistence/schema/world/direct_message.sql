-- world.db: direct_message (B1.1 + B6 + B9 combined)
-- Single table backs F2F / RDC / GRP incoming streams plus failed-attempt feedback.
CREATE TABLE IF NOT EXISTS direct_message (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id     INTEGER,                        -- NULL = system message
    recipient_id  INTEGER NOT NULL,
    group_id      INTEGER,                        -- NULL = F2F/RDC; non-NULL = GRP fan-out copy
    channel_type  TEXT NOT NULL CHECK (channel_type IN ('F2F','RDC','GRP')),
    content       TEXT NOT NULL,
    place_id      TEXT,                           -- sender's place at attempted_at
    attempted_at  INTEGER NOT NULL,               -- world.t when send was invoked (always set)
    arrive_at     INTEGER NOT NULL,               -- attempted_at + channel_delay; == attempted_at on failure
    delivered     INTEGER NOT NULL CHECK (delivered IN (-1, 0, 1))
                  -- -1 cancelled (e.g. leave/kick cleanup) / 0 failed or pending retry / 1 delivered
);

-- Hot path: PerceptionBuilder pulls incoming for a recipient at world.t
CREATE INDEX IF NOT EXISTS idx_dm_recipient_arrive
    ON direct_message(recipient_id, arrive_at) WHERE delivered = 1;

-- Hot path: B9 recent_failed_attempts (sender side, attempted_at == t-1)
CREATE INDEX IF NOT EXISTS idx_dm_failed_attempt
    ON direct_message(sender_id, attempted_at) WHERE delivered = 0;

-- Hot path: B6 group retry sweep over undelivered group fan-outs
CREATE INDEX IF NOT EXISTS idx_dm_undelivered_group
    ON direct_message(group_id, recipient_id) WHERE delivered = 0 AND group_id IS NOT NULL;
