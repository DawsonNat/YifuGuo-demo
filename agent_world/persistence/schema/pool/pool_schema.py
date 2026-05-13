"""Pool schema helper — exposes the path of the fork's OASIS schema dir.

The 13 pool DDL files live in the OASIS fork
(``vendor/oasis/oasis/social_platform/schema/``) and are loaded by
``oasis.social_platform.database.create_db()``. Agent World does not keep
its own copy; this module is a thin import-time helper used by
``persistence/pool_db.py`` (and tests) when they need the directory path
without going through OASIS internals.

See ``agent_world/docs/impl/persistence_schema_pool.md`` and LAYOUT §3.3.
"""
from __future__ import annotations

import os
import os.path as osp
from typing import List

# 13 tables that live in the pool DB after Agent World A1 (the three group
# chat tables — chat_group / group_member / group_message — were migrated
# to world.db).
POOL_TABLES: List[str] = [
    "user",
    "post",
    "follow",
    "mute",
    "like",
    "dislike",
    "report",
    "trace",
    "rec",
    "comment",
    "comment_like",
    "comment_dislike",
    "product",
]


def get_pool_schema_dir() -> str:
    """Return absolute path to the fork's OASIS schema directory.

    Resolution order:

    1. ``OASIS_POOL_SCHEMA_DIR`` env var (override, mainly for tests).
    2. ``oasis.social_platform.database.get_schema_dir_path()`` if the
       OASIS package is importable.
    3. Fallback path computed relative to the repo root
       (``vendor/oasis/oasis/social_platform/schema``).
    """
    override = os.environ.get("OASIS_POOL_SCHEMA_DIR")
    if override:
        return override

    try:
        from oasis.social_platform.database import (  # type: ignore
            get_schema_dir_path)
        return get_schema_dir_path()
    except Exception:
        pass

    # Fallback: compute relative to this file
    # (.../agent_world/persistence/schema/pool/pool_schema.py).
    here = osp.dirname(osp.abspath(__file__))
    repo_root = osp.abspath(osp.join(here, "..", "..", "..", ".."))
    return osp.join(repo_root, "vendor", "oasis", "oasis",
                    "social_platform", "schema")
