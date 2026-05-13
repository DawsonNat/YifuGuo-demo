-- world.db: coverage
-- Directed reachability matrix between places; drives phi_RDC and channel delay.
CREATE TABLE IF NOT EXISTS coverage (
    src_place      TEXT NOT NULL,
    dst_place      TEXT NOT NULL,
    can_reach      INTEGER NOT NULL DEFAULT 1,    -- 0/1 boolean
    latency_ticks  INTEGER NOT NULL DEFAULT 0,    -- B1.1 channel delay (ticks)
    PRIMARY KEY (src_place, dst_place),
    FOREIGN KEY (src_place) REFERENCES place(place_id),
    FOREIGN KEY (dst_place) REFERENCES place(place_id)
);
