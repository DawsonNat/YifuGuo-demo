-- world.db: overhear
-- F2F co-located bystanders for a given direct_message; joined back to direct_message for content.
CREATE TABLE IF NOT EXISTS overhear (
    message_id     INTEGER NOT NULL,
    overhearer_id  INTEGER NOT NULL,
    place_id       TEXT NOT NULL,                 -- place where overhear occurred (= sender's place)
    PRIMARY KEY (message_id, overhearer_id),
    FOREIGN KEY (message_id) REFERENCES direct_message(message_id)
);

CREATE INDEX IF NOT EXISTS idx_overhear_overhearer
    ON overhear(overhearer_id);
