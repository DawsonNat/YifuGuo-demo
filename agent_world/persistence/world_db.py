"""world.db access layer (L1).

Single-file SQLite holding 12 world-level tables. Hosts every write through one
``asyncio.Lock`` (LAYOUT B8 single-writer decision); reads run unlocked on the
same ``sqlite3.Connection`` (SQLite is safe for concurrent reads on a single
connection in autocommit mode).

This module exposes thin CRUD helpers that buses, the dispatcher, the perception
builder, the script engine, and the world stores all consume. Method names align
with the column names declared in
``agent_world/persistence/schema/world/*.sql``.

Design notes
------------
* Sync ``sqlite3`` is used for the MVP. Write methods are ``async def`` so
  callers can await the lock; reads are plain ``def``.
* ``isolation_level=None`` puts the connection in autocommit mode; transactions
  are short (single-statement) so explicit BEGIN/COMMIT is unnecessary for the
  current call sites.
* DDL is driven by reading every ``*.sql`` file under
  ``persistence/schema/world/`` and running ``executescript``; idempotent thanks
  to ``CREATE TABLE IF NOT EXISTS`` in each DDL.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional


# --------------------------------------------------------------------------- #
# Row dataclasses (only for the hot perception-path queries)
# --------------------------------------------------------------------------- #


@dataclass
class DirectMessageRow:
    message_id: int
    sender_id: Optional[int]
    recipient_id: int
    group_id: Optional[int]
    channel_type: str
    content: str
    place_id: Optional[str]
    attempted_at: int
    arrive_at: int
    delivered: int


@dataclass
class OverhearRow:
    message_id: int
    overhearer_id: int
    place_id: str
    sender_id: Optional[int]
    content: str
    attempted_at: int


@dataclass
class GroupEventRow:
    event_id: int
    group_id: int
    agent_id: int
    event_type: str
    occurred_at: int
    actor_id: Optional[int]


# --------------------------------------------------------------------------- #
# WorldDB
# --------------------------------------------------------------------------- #


SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "schema", "world")


class WorldDB:
    """Access layer for ``world.db`` (12 tables)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._write_lock = asyncio.Lock()

    # ----- lifecycle -------------------------------------------------------

    def init_schema(self) -> None:
        """Run ``executescript`` over every DDL file in schema/world/."""
        files = sorted(f for f in os.listdir(SCHEMA_DIR) if f.endswith(".sql"))
        for name in files:
            with open(os.path.join(SCHEMA_DIR, name), "r", encoding="utf-8") as f:
                self._conn.executescript(f.read())

    # Backwards-compatible alias used in some specs.
    initialize = init_schema

    def close(self) -> None:
        self._conn.close()

    # ----- internal helpers -----------------------------------------------

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, tuple(params))

    # =====================================================================
    # place / coverage / agent_location
    # =====================================================================

    async def upsert_place(
        self,
        place_id: str,
        parent_id: Optional[str],
        place_type: str,
        attrs: str = "{}",
        capacity: Optional[int] = None,
        created_at: int = 0,
    ) -> None:
        async with self._write_lock:
            self._exec(
                """
                INSERT INTO place(place_id, parent_id, place_type, capacity, attrs, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(place_id) DO UPDATE SET
                    parent_id = excluded.parent_id,
                    place_type = excluded.place_type,
                    capacity = excluded.capacity,
                    attrs = excluded.attrs
                """,
                (place_id, parent_id, place_type, capacity, attrs, created_at),
            )

    def list_places(self) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT * FROM place").fetchall()]

    def get_place_attrs(self, place_id: str) -> Optional[str]:
        row = self._exec(
            "SELECT attrs FROM place WHERE place_id = ?", (place_id,)
        ).fetchone()
        return None if row is None else row["attrs"]

    async def upsert_coverage(
        self,
        src_place: str,
        dst_place: str,
        latency_ticks: int,
        can_reach: int = 1,
    ) -> None:
        async with self._write_lock:
            self._exec(
                """
                INSERT INTO coverage(src_place, dst_place, can_reach, latency_ticks)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(src_place, dst_place) DO UPDATE SET
                    can_reach = excluded.can_reach,
                    latency_ticks = excluded.latency_ticks
                """,
                (src_place, dst_place, can_reach, latency_ticks),
            )

    def list_coverage(self) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT * FROM coverage").fetchall()]

    def get_latency(self, src_place: str, dst_place: str) -> Optional[int]:
        row = self._exec(
            "SELECT latency_ticks, can_reach FROM coverage WHERE src_place=? AND dst_place=?",
            (src_place, dst_place),
        ).fetchone()
        if row is None or not row["can_reach"]:
            return None
        return int(row["latency_ticks"])

    async def set_location(self, agent_id: int, place_id: str, t: int) -> None:
        async with self._write_lock:
            self._exec(
                """
                INSERT INTO agent_location(agent_id, place_id, arrived_at)
                VALUES(?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    place_id = excluded.place_id,
                    arrived_at = excluded.arrived_at
                """,
                (agent_id, place_id, t),
            )

    def get_location(self, agent_id: int) -> Optional[dict]:
        row = self._exec(
            "SELECT * FROM agent_location WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def agents_at(self, place_id: str) -> list[int]:
        rows = self._exec(
            "SELECT agent_id FROM agent_location WHERE place_id=?", (place_id,)
        ).fetchall()
        return [int(r["agent_id"]) for r in rows]

    # =====================================================================
    # relation
    # =====================================================================

    async def add_relation(
        self,
        src_agent: int,
        dst_agent: int,
        relation_type: str,
        created_at: int,
        expires_at: Optional[int] = None,
        metadata: Optional[str] = None,
    ) -> None:
        async with self._write_lock:
            self._exec(
                """
                INSERT OR REPLACE INTO relation
                    (src_agent, dst_agent, relation_type, created_at, expires_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (src_agent, dst_agent, relation_type, created_at, expires_at, metadata),
            )

    async def remove_relation(
        self, src_agent: int, dst_agent: int, relation_type: str
    ) -> int:
        async with self._write_lock:
            cur = self._exec(
                "DELETE FROM relation WHERE src_agent=? AND dst_agent=? AND relation_type=?",
                (src_agent, dst_agent, relation_type),
            )
            return cur.rowcount

    def relations_of(self, agent_id: int) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM relation WHERE src_agent=? OR dst_agent=?",
            (agent_id, agent_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def fetch_active_relations(self, t: int) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM relation WHERE expires_at IS NULL OR expires_at > ?",
            (t,),
        ).fetchall()
        return [dict(r) for r in rows]

    # =====================================================================
    # capability
    # =====================================================================

    async def grant(
        self,
        agent_id: int,
        capability: str,
        granted_at: int,
        metadata: Optional[str] = None,
    ) -> None:
        async with self._write_lock:
            self._exec(
                """
                INSERT OR REPLACE INTO capability
                    (agent_id, capability, granted_at, revoked_at, metadata)
                VALUES (?, ?, ?, NULL, ?)
                """,
                (agent_id, capability, granted_at, metadata),
            )

    async def revoke(self, agent_id: int, capability: str, revoked_at: int) -> int:
        async with self._write_lock:
            cur = self._exec(
                """
                UPDATE capability SET revoked_at = ?
                WHERE agent_id = ? AND capability = ? AND revoked_at IS NULL
                """,
                (revoked_at, agent_id, capability),
            )
            return cur.rowcount

    def agents_with(self, capability: str) -> list[int]:
        rows = self._exec(
            "SELECT agent_id FROM capability WHERE capability=? AND revoked_at IS NULL",
            (capability,),
        ).fetchall()
        return [int(r["agent_id"]) for r in rows]

    def has(self, agent_id: int, capability: str) -> bool:
        row = self._exec(
            """
            SELECT 1 FROM capability
            WHERE agent_id=? AND capability=? AND revoked_at IS NULL
            LIMIT 1
            """,
            (agent_id, capability),
        ).fetchone()
        return row is not None

    def fetch_active_capabilities(self, t: int) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM capability WHERE revoked_at IS NULL OR revoked_at > ?",
            (t,),
        ).fetchall()
        return [dict(r) for r in rows]

    # =====================================================================
    # direct_message (B1.1 + B6 + B9)
    # =====================================================================

    async def insert_message(
        self,
        *,
        sender_id: Optional[int],
        recipient_id: int,
        group_id: Optional[int],
        channel_type: str,
        content: str,
        place_id: Optional[str],
        attempted_at: int,
        arrive_at: int,
        delivered: int,
    ) -> int:
        async with self._write_lock:
            cur = self._exec(
                """
                INSERT INTO direct_message
                    (sender_id, recipient_id, group_id, channel_type, content,
                     place_id, attempted_at, arrive_at, delivered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sender_id,
                    recipient_id,
                    group_id,
                    channel_type,
                    content,
                    place_id,
                    attempted_at,
                    arrive_at,
                    delivered,
                ),
            )
            return int(cur.lastrowid)

    async def mark_delivered(self, message_id: int, arrive_at: int) -> None:
        async with self._write_lock:
            self._exec(
                "UPDATE direct_message SET delivered=1, arrive_at=? WHERE message_id=?",
                (arrive_at, message_id),
            )

    async def cancel_undelivered(self, message_id: int) -> None:
        async with self._write_lock:
            self._exec(
                "UPDATE direct_message SET delivered=-1 WHERE message_id=?",
                (message_id,),
            )

    async def purge_undelivered_for_group(
        self, group_id: int, agent_id: int
    ) -> int:
        async with self._write_lock:
            cur = self._exec(
                """
                UPDATE direct_message SET delivered=-1
                WHERE recipient_id=? AND group_id=? AND delivered=0
                """,
                (agent_id, group_id),
            )
            return cur.rowcount

    # ---- PerceptionBuilder hot path ----

    def fetch_arrived_for(
        self, recipient_id: int, t: int, last_seen: int = -1
    ) -> list[DirectMessageRow]:
        rows = self._exec(
            """
            SELECT * FROM direct_message
            WHERE recipient_id=? AND delivered=1
              AND arrive_at <= ? AND arrive_at > ?
            ORDER BY arrive_at, message_id
            """,
            (recipient_id, t, last_seen),
        ).fetchall()
        return [_dm_row(r) for r in rows]

    # Spec alias.
    fetch_incoming = fetch_arrived_for

    def fetch_f2f_history_at(
        self,
        place_id: str,
        t_now: int,
        since_t: int,
        limit: int = 30,
    ) -> list[tuple[int, int, int, str]]:
        """Return deduped F2F utterances at ``place_id`` in ``[since_t, t_now]``.

        F2F broadcasts insert one ``direct_message`` row per recipient with
        identical ``(sender_id, attempted_at, content)``; we collapse those
        copies via GROUP BY so callers see one transcript line per utterance.
        Returned tuples are ``(attempted_at, sender_id, message_id, content)``,
        oldest first.
        """
        rows = self._exec(
            """
            SELECT MIN(message_id) AS message_id, sender_id,
                   attempted_at, content
            FROM direct_message
            WHERE channel_type='F2F' AND place_id=?
              AND attempted_at >= ? AND attempted_at <= ?
            GROUP BY sender_id, attempted_at, content
            ORDER BY attempted_at, message_id
            LIMIT ?
            """,
            (place_id, since_t, t_now, limit),
        ).fetchall()
        return [
            (int(r["attempted_at"]), int(r["sender_id"]),
             int(r["message_id"]), str(r["content"]))
            for r in rows
        ]

    def fetch_failed_attempts_for(
        self, sender_id: int, t_minus_1: int
    ) -> list[DirectMessageRow]:
        rows = self._exec(
            """
            SELECT * FROM direct_message
            WHERE sender_id=? AND delivered=0 AND attempted_at=?
            ORDER BY message_id
            """,
            (sender_id, t_minus_1),
        ).fetchall()
        return [_dm_row(r) for r in rows]

    fetch_failed = fetch_failed_attempts_for

    def fetch_undelivered_group_messages(self) -> list[tuple[int, int, int]]:
        """Return ``(message_id, recipient_id, group_id)`` for B6 sweep candidates."""
        rows = self._exec(
            """
            SELECT message_id, recipient_id, group_id
            FROM direct_message
            WHERE delivered=0 AND group_id IS NOT NULL
            """
        ).fetchall()
        return [
            (int(r["message_id"]), int(r["recipient_id"]), int(r["group_id"]))
            for r in rows
        ]

    list_undelivered_group = fetch_undelivered_group_messages

    async def sweep_undelivered(self, ready: list[tuple[int, int]]) -> int:
        """Bulk update ``(message_id, new_arrive_at)`` rows to ``delivered=1``."""
        if not ready:
            return 0
        async with self._write_lock:
            self._conn.executemany(
                "UPDATE direct_message SET delivered=1, arrive_at=? WHERE message_id=?",
                [(arrive_at, mid) for (mid, arrive_at) in ready],
            )
            return len(ready)

    # =====================================================================
    # overhear
    # =====================================================================

    async def insert_overhear(
        self, message_id: int, overhearer_id: int, place_id: str
    ) -> None:
        async with self._write_lock:
            self._exec(
                """
                INSERT OR IGNORE INTO overhear(message_id, overhearer_id, place_id)
                VALUES (?, ?, ?)
                """,
                (message_id, overhearer_id, place_id),
            )

    def fetch_overhear_for(
        self, agent_id: int, since: int
    ) -> list[OverhearRow]:
        rows = self._exec(
            """
            SELECT o.message_id, o.overhearer_id, o.place_id,
                   m.sender_id, m.content, m.attempted_at
            FROM overhear o JOIN direct_message m USING(message_id)
            WHERE o.overhearer_id=? AND m.attempted_at >= ?
            ORDER BY m.attempted_at, o.message_id
            """,
            (agent_id, since),
        ).fetchall()
        return [
            OverhearRow(
                message_id=int(r["message_id"]),
                overhearer_id=int(r["overhearer_id"]),
                place_id=str(r["place_id"]),
                sender_id=(None if r["sender_id"] is None else int(r["sender_id"])),
                content=str(r["content"]),
                attempted_at=int(r["attempted_at"]),
            )
            for r in rows
        ]

    fetch_overhear = fetch_overhear_for

    # =====================================================================
    # script_event_log
    # =====================================================================

    async def insert_event(
        self, event_id: str, triggered_at: int, payload: str
    ) -> None:
        async with self._write_lock:
            self._exec(
                """
                INSERT OR IGNORE INTO script_event_log(event_id, triggered_at, payload)
                VALUES (?, ?, ?)
                """,
                (event_id, triggered_at, payload),
            )

    # Spec alias.
    append_script_event = insert_event

    def list_events(
        self,
        *,
        since: Optional[int] = None,
        until: Optional[int] = None,
    ) -> list[dict]:
        if since is None and until is None:
            rows = self._exec(
                "SELECT * FROM script_event_log ORDER BY triggered_at"
            ).fetchall()
        else:
            lo = -(1 << 62) if since is None else since
            hi = (1 << 62) if until is None else until
            rows = self._exec(
                """
                SELECT * FROM script_event_log
                WHERE triggered_at >= ? AND triggered_at <= ?
                ORDER BY triggered_at
                """,
                (lo, hi),
            ).fetchall()
        return [dict(r) for r in rows]

    # =====================================================================
    # chat_group / group_member / group_message  (minimal CRUD for L3 GRP bus)
    # =====================================================================

    async def insert_group(self, name: str) -> int:
        async with self._write_lock:
            cur = self._exec("INSERT INTO chat_group(name) VALUES (?)", (name,))
            return int(cur.lastrowid)

    async def delete_group(self, group_id: int) -> None:
        async with self._write_lock:
            self._exec("DELETE FROM chat_group WHERE group_id=?", (group_id,))

    def get_group(self, group_id: int) -> Optional[dict]:
        row = self._exec(
            "SELECT * FROM chat_group WHERE group_id=?", (group_id,)
        ).fetchone()
        return None if row is None else dict(row)

    async def insert_group_member(self, group_id: int, agent_id: int) -> None:
        async with self._write_lock:
            self._exec(
                """
                INSERT OR IGNORE INTO group_member(group_id, agent_id)
                VALUES (?, ?)
                """,
                (group_id, agent_id),
            )

    async def delete_group_member(self, group_id: int, agent_id: int) -> int:
        async with self._write_lock:
            cur = self._exec(
                "DELETE FROM group_member WHERE group_id=? AND agent_id=?",
                (group_id, agent_id),
            )
            return cur.rowcount

    def list_group_members(self, group_id: int) -> list[int]:
        rows = self._exec(
            "SELECT agent_id FROM group_member WHERE group_id=?", (group_id,)
        ).fetchall()
        return [int(r["agent_id"]) for r in rows]

    def list_groups_of(self, agent_id: int) -> list[int]:
        rows = self._exec(
            "SELECT group_id FROM group_member WHERE agent_id=?", (agent_id,)
        ).fetchall()
        return [int(r["group_id"]) for r in rows]

    async def insert_group_message(
        self, group_id: int, sender_id: int, content: str
    ) -> int:
        async with self._write_lock:
            cur = self._exec(
                """
                INSERT INTO group_message(group_id, sender_id, content)
                VALUES (?, ?, ?)
                """,
                (group_id, sender_id, content),
            )
            return int(cur.lastrowid)

    # =====================================================================
    # group_event (B6)
    # =====================================================================

    async def insert_group_event(
        self,
        group_id: int,
        agent_id: int,
        event_type: str,
        occurred_at: int,
        actor_id: Optional[int] = None,
    ) -> int:
        async with self._write_lock:
            cur = self._exec(
                """
                INSERT INTO group_event
                    (group_id, agent_id, event_type, occurred_at, actor_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, agent_id, event_type, occurred_at, actor_id),
            )
            return int(cur.lastrowid)

    def fetch_for_agent(self, agent_id: int, t_minus_1: int) -> list[GroupEventRow]:
        rows = self._exec(
            """
            SELECT ge.* FROM group_event ge
            JOIN group_member gm ON gm.group_id = ge.group_id
            WHERE gm.agent_id = ? AND ge.occurred_at = ?
            ORDER BY ge.event_id
            """,
            (agent_id, t_minus_1),
        ).fetchall()
        return [
            GroupEventRow(
                event_id=int(r["event_id"]),
                group_id=int(r["group_id"]),
                agent_id=int(r["agent_id"]),
                event_type=str(r["event_type"]),
                occurred_at=int(r["occurred_at"]),
                actor_id=(None if r["actor_id"] is None else int(r["actor_id"])),
            )
            for r in rows
        ]

    fetch_group_events = fetch_for_agent


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _dm_row(r: sqlite3.Row) -> DirectMessageRow:
    return DirectMessageRow(
        message_id=int(r["message_id"]),
        sender_id=(None if r["sender_id"] is None else int(r["sender_id"])),
        recipient_id=int(r["recipient_id"]),
        group_id=(None if r["group_id"] is None else int(r["group_id"])),
        channel_type=str(r["channel_type"]),
        content=str(r["content"]),
        place_id=(None if r["place_id"] is None else str(r["place_id"])),
        attempted_at=int(r["attempted_at"]),
        arrive_at=int(r["arrive_at"]),
        delivered=int(r["delivered"]),
    )
