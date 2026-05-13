"""pool_*.db access wrapper (L2).

A thin wrapper around the fork's OASIS ``database.create_db()`` that adds the
Agent-World-specific helpers needed by the runtime — namely the **double-track
follow projection** (LAYOUT §3.5): the source-of-truth lives in
``world.db.relation``; ``pool_*.db.follow`` is a per-pool projection that the
RecSys algorithms read.

Scope (MVP):

* :meth:`PoolDB.init` opens / creates the per-pool SQLite file by delegating to
  ``oasis.social_platform.database.create_db``. After Agent World A1 the OASIS
  schema list contains 13 tables (group chat tables migrated to ``world.db``).
* :meth:`PoolDB.rebuild_follow_from_world` drops every row in ``follow`` and
  re-inserts the projection from ``world.db.relation`` for the given
  ``agent_ids`` (LAYOUT §3.5 startup recovery — A2).
* :meth:`PoolDB.add_follow` / :meth:`PoolDB.remove_follow` are the synchronous
  hooks called by ``RelationGraph.on_change`` (LAYOUT §3.5 sync write).
* :meth:`PoolDB.get_connection` returns the underlying ``sqlite3.Connection``
  for callers that need raw access (e.g., :class:`Platform` injection).

This wrapper deliberately stays thin: FEED-class writes (post / like / comment
/ trace / …) happen inside the OASIS ``Platform`` instance using the same
``Connection`` and are not mediated by this module.

See ``agent_world/docs/impl/persistence_pool_db.md`` and LAYOUT §2.H / §3.3 /
§3.5.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Iterable, List, Optional

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from agent_world.persistence.world_db import WorldDB

log = logging.getLogger(__name__)

# Relation types that project to the OASIS follow table (LAYOUT §3.5).
# - 'mutual_follow' is symmetric → write both (src,dst) and (dst,src).
# - 'follower'      is directed   → write only (src,dst).
_FOLLOW_PROJECTING_TYPES = frozenset({"mutual_follow", "follower"})
_SYMMETRIC_FOLLOW_TYPES = frozenset({"mutual_follow"})


class PoolDB:
    """Per-pool SQLite wrapper.

    Parameters
    ----------
    pool_path:
        Absolute path to the ``pool__<place>__<feed>.db`` file.
    feed:
        The feed identifier (e.g., ``'twitter'``, ``'reddit'``); kept on the
        instance for downstream factories that need it.
    """

    def __init__(self, pool_path: str, feed: str) -> None:
        self.pool_path: str = pool_path
        self.feed: str = feed
        self._conn: Optional[sqlite3.Connection] = None
        self._cursor: Optional[sqlite3.Cursor] = None

    # ----- lifecycle -------------------------------------------------------

    def init(self) -> None:
        """Create / open the pool DB by delegating to OASIS ``create_db``.

        After Agent World A1 the OASIS fork's ``create_db`` provisions 13
        tables (group chat tables removed). Idempotent if every DDL uses
        ``CREATE TABLE IF NOT EXISTS`` (the OASIS schemas do not, but the
        underlying ``CREATE TABLE`` will simply error and be swallowed by the
        OASIS try/except — re-opening a populated file is therefore safe).
        """
        # Imported lazily so this module doesn't pull OASIS at import time.
        from oasis.social_platform.database import create_db

        os.makedirs(os.path.dirname(self.pool_path) or ".", exist_ok=True)
        conn, cursor = create_db(self.pool_path)
        self._conn = conn
        self._cursor = cursor

    def close(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except sqlite3.Error:
                pass
            self._cursor = None
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    # ----- raw access ------------------------------------------------------

    def get_connection(self) -> sqlite3.Connection:
        """Return the underlying ``sqlite3.Connection``.

        Other modules (notably :class:`oasis.social_platform.Platform` via
        ``PlatformFactory``) need raw access to share the same connection for
        FEED-class writes. Raises :class:`RuntimeError` if :meth:`init` has not
        been called yet.
        """
        if self._conn is None:
            raise RuntimeError(
                "PoolDB.get_connection() called before init(); "
                "call PoolDB.init() first.")
        return self._conn

    # ----- follow projection (LAYOUT §3.5) ---------------------------------

    def add_follow(self, src: int, dst: int) -> None:
        """Insert a follow row (no-op on duplicate).

        Called by ``RelationGraph.on_change`` when a ``mutual_follow`` /
        ``follower`` edge is created. For symmetric ``mutual_follow`` the
        caller is responsible for invoking this once per direction.
        """
        conn = self.get_connection()
        try:
            conn.execute(
                "INSERT INTO follow (follower_id, followee_id, created_at) "
                "VALUES (?, ?, ?) ",
                (src, dst, None),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Row already present (uniqueness not enforced by schema, but
            # callers may double-write under retry); ignore silently.
            pass
        except sqlite3.Error as e:
            log.warning(
                "PoolDB(%s).add_follow(%s,%s) failed: %s",
                self.feed, src, dst, e,
            )

    def remove_follow(self, src: int, dst: int) -> None:
        """Delete the follow row matching ``(src, dst)``."""
        conn = self.get_connection()
        try:
            conn.execute(
                "DELETE FROM follow WHERE follower_id=? AND followee_id=?",
                (src, dst),
            )
            conn.commit()
        except sqlite3.Error as e:
            log.warning(
                "PoolDB(%s).remove_follow(%s,%s) failed: %s",
                self.feed, src, dst, e,
            )

    def rebuild_follow_from_world(
        self,
        world_db: "WorldDB",
        agent_ids: Iterable[int],
        t: int = 0,
    ) -> int:
        """Drop & re-insert ``follow`` from ``world.db.relation`` (A2).

        Runs at runner startup for crash recovery (LAYOUT §3.5):
        ``DROP & INSERT`` mode — increment hooks are not trusted across DB
        crashes, so the projection is rebuilt from the world-level truth.

        Parameters
        ----------
        world_db:
            The world-level DB providing :meth:`fetch_active_relations`.
        agent_ids:
            Iterable of agent ids that have an account in *this* pool. Edges
            whose endpoints are outside this set are skipped (an agent can be
            absent from a given pool — A3 lazy registration).
        t:
            World tick used to filter expired relations. Defaults to ``0``
            (boot time).

        Returns the number of rows inserted.
        """
        conn = self.get_connection()
        pool_users = set(int(a) for a in agent_ids)

        conn.execute("DELETE FROM follow")

        if not pool_users:
            conn.commit()
            return 0

        rows = world_db.fetch_active_relations(t)
        inserted = 0
        for r in rows:
            rtype = r.get("relation_type")
            if rtype not in _FOLLOW_PROJECTING_TYPES:
                continue
            src = r.get("src_agent")
            dst = r.get("dst_agent")
            if src is None or dst is None:
                continue
            if src not in pool_users or dst not in pool_users:
                continue
            try:
                conn.execute(
                    "INSERT INTO follow (follower_id, followee_id, created_at)"
                    " VALUES (?, ?, ?)",
                    (src, dst, None),
                )
                inserted += 1
                if rtype in _SYMMETRIC_FOLLOW_TYPES:
                    conn.execute(
                        "INSERT INTO follow (follower_id, followee_id, "
                        "created_at) VALUES (?, ?, ?)",
                        (dst, src, None),
                    )
                    inserted += 1
            except sqlite3.Error as e:  # pragma: no cover - defensive
                log.warning(
                    "PoolDB(%s).rebuild_follow_from_world insert "
                    "(%s→%s, %s) failed: %s",
                    self.feed, src, dst, rtype, e,
                )

        conn.commit()
        return inserted

    # ----- helpers ---------------------------------------------------------

    def list_user_ids(self) -> List[int]:
        """Return all ``user_id`` values currently registered in this pool."""
        conn = self.get_connection()
        rows = conn.execute("SELECT user_id FROM user").fetchall()
        return [int(r[0]) for r in rows]


__all__ = ["PoolDB"]
