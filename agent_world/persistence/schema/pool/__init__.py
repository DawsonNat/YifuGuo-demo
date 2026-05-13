"""Pool DB schema (Agent World).

Per ``agent_world/docs/impl/persistence_schema_pool.md`` and LAYOUT §3.3, the
pool DB (one ``pool_*.db`` per recommendation pool) reuses the OASIS fork
schema directly: 13 ``CREATE TABLE`` files living at::

    vendor/oasis/oasis/social_platform/schema/

We deliberately do **not** copy or symlink those ``.sql`` files into
``agent_world/``. Instead, the pool DB layer (``persistence/pool_db.py``)
calls ``oasis.social_platform.database.create_db(pool_path)`` which already
resolves the schema dir relative to the OASIS package and runs each DDL via
``executescript``.

The 13 tables (after Agent World A1 — group chat tables migrated to
``world.db``) are:

    user, post, follow, mute, like, dislike, report, trace, rec,
    comment, comment_like, comment_dislike, product

This package exposes one helper, :func:`get_pool_schema_dir`, returning the
absolute path to the fork's schema directory so callers (tests / tools)
can introspect it without importing OASIS internals.
"""
from __future__ import annotations

from .pool_schema import POOL_TABLES, get_pool_schema_dir

__all__ = ["POOL_TABLES", "get_pool_schema_dir"]
