"""In-memory RelationGraph (LAYOUT §2.A / §3.2 / impl/world_relation_graph.md).

Multi-typed directed-edge graph backing the ``R_t`` segment of WorldState.

Type metadata (``symmetric`` / ``is_contact`` / ``project_to_pool`` /
``mutually_exclusive`` / ``display_template``) is read directly off
:class:`agent_world.world._registrars.RelationBase` subclasses via
:class:`RelationTypeRegistrar`.

Per LAYOUT B8 the only persistent writer is :class:`WorldDB`; this module
forwards eagerly on every ``add`` / ``remove``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from agent_world.persistence.world_db import WorldDB
from agent_world.world._registrars import RelationTypeRegistrar


# ---------------------------------------------------------------------------
# Records & hook signatures
# ---------------------------------------------------------------------------

@dataclass
class RelationEdge:
    src_agent: int
    dst_agent: int
    relation_type: str
    created_at: int = 0
    expires_at: Optional[int] = None
    metadata: Optional[dict] = None


@dataclass
class RelationTypeMeta:
    name: str
    symmetric: bool = False
    is_contact: bool = True
    project_to_pool: bool = False
    mutually_exclusive: Tuple[str, ...] = ()
    display_template: str = "{src} -> {dst}"


# (src, dst, relation_type, op) -> None  (op in {"add", "remove"})
RelationHook = Callable[[int, int, str, str], None]


class RelationConflict(Exception):
    """Raised when ``add`` would violate a ``mutually_exclusive`` rule."""


# ---------------------------------------------------------------------------
# RelationGraph
# ---------------------------------------------------------------------------

class RelationGraph:
    """Authoritative store for typed agent-agent relations."""

    def __init__(self, world_db: WorldDB) -> None:
        self.world_db = world_db
        self.edges: Dict[Tuple[int, int, str], RelationEdge] = {}
        self.out_by_agent: Dict[int, Dict[str, Set[int]]] = {}
        self.in_by_agent: Dict[int, Dict[str, Set[int]]] = {}
        self.type_meta: Dict[str, RelationTypeMeta] = {}
        self._on_change: List[RelationHook] = []

    # -- boot ---------------------------------------------------------------

    def load_from_db(self, world_db: Optional[WorldDB] = None) -> None:
        db = world_db or self.world_db
        self.edges.clear()
        self.out_by_agent.clear()
        self.in_by_agent.clear()
        self.type_meta.clear()

        # Pull metadata from RelationBase subclasses (Path A registry).
        for key, type_cls in RelationTypeRegistrar.get_all().items():
            self.type_meta[key] = RelationTypeMeta(
                name=key,
                symmetric=bool(getattr(type_cls, "symmetric", False)),
                is_contact=bool(getattr(type_cls, "is_contact", True)),
                project_to_pool=bool(getattr(type_cls, "project_to_pool", False)),
                mutually_exclusive=tuple(getattr(type_cls, "mutually_exclusive", ()) or ()),
                display_template=getattr(type_cls, "display_template", "{src} -> {dst}"),
            )

        # Bulk read: WorldDB.relations_of() filters by agent_id, so for the
        # boot-time full sweep we reach the connection directly (read-only).
        try:
            cursor = db._conn.execute("SELECT * FROM relation")
            rows = [dict(r) for r in cursor.fetchall()]
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            edge = RelationEdge(
                src_agent=int(row["src_agent"]),
                dst_agent=int(row["dst_agent"]),
                relation_type=row["relation_type"],
                created_at=int(row.get("created_at", 0) or 0),
                expires_at=row.get("expires_at"),
                metadata=row.get("metadata"),
            )
            self._index(edge)

    # -- read path ----------------------------------------------------------

    def has(self, src: int, dst: int, relation_type: str) -> bool:
        return (src, dst, relation_type) in self.edges

    def contacts_of(self, agent: int) -> Iterable[Tuple[int, str]]:
        out_map = self.out_by_agent.get(agent, {})
        for rtype, others in out_map.items():
            meta = self.type_meta.get(rtype)
            if meta is not None and not meta.is_contact:
                continue
            for other in others:
                yield (other, rtype)

    def relations_between(self, a: int, b: int) -> List[str]:
        out: List[str] = []
        for rtype, others in self.out_by_agent.get(a, {}).items():
            if b in others:
                out.append(rtype)
        return out

    def edges_of(self, agent: int) -> Iterable[RelationEdge]:
        for rtype, others in self.out_by_agent.get(agent, {}).items():
            for other in others:
                edge = self.edges.get((agent, other, rtype))
                if edge is not None:
                    yield edge

    # -- write path ---------------------------------------------------------

    async def add(
        self,
        src: int,
        dst: int,
        relation_type: str,
        *,
        t: int = 0,
        metadata: Optional[dict] = None,
    ) -> None:
        meta = self._require_meta(relation_type)
        # mutual-exclusion check before any write
        for mut in meta.mutually_exclusive:
            if (src, dst, mut) in self.edges or (
                meta.symmetric and (dst, src, mut) in self.edges
            ):
                raise RelationConflict(
                    f"({src},{dst},{relation_type}) conflicts with existing {mut}"
                )

        if (src, dst, relation_type) in self.edges:
            return  # idempotent

        edge = RelationEdge(src, dst, relation_type, created_at=t, metadata=metadata)
        self._index(edge)
        await self.world_db.add_relation(
            src, dst, relation_type, created_at=t, metadata=_jsonify(metadata)
        )
        self._fire(src, dst, relation_type, "add")

        if meta.symmetric and src != dst:
            twin_key = (dst, src, relation_type)
            if twin_key not in self.edges:
                twin = RelationEdge(dst, src, relation_type, created_at=t, metadata=metadata)
                self._index(twin)
                await self.world_db.add_relation(
                    dst, src, relation_type, created_at=t, metadata=_jsonify(metadata)
                )
                self._fire(dst, src, relation_type, "add")

    async def remove(self, src: int, dst: int, relation_type: str, *, t: int = 0) -> None:
        meta = self._require_meta(relation_type)
        if (src, dst, relation_type) not in self.edges:
            return
        self._unindex(src, dst, relation_type)
        await self.world_db.remove_relation(src, dst, relation_type)
        self._fire(src, dst, relation_type, "remove")

        if meta.symmetric and src != dst and (dst, src, relation_type) in self.edges:
            self._unindex(dst, src, relation_type)
            await self.world_db.remove_relation(dst, src, relation_type)
            self._fire(dst, src, relation_type, "remove")

    # -- hook registration --------------------------------------------------

    def register_on_change(self, cb: RelationHook) -> None:
        self._on_change.append(cb)

    # -- internals ----------------------------------------------------------

    def _require_meta(self, relation_type: str) -> RelationTypeMeta:
        meta = self.type_meta.get(relation_type)
        if meta is None:
            # MVP: fall back to default meta so unregistered types still work
            meta = RelationTypeMeta(name=relation_type)
            self.type_meta[relation_type] = meta
        return meta

    def _index(self, edge: RelationEdge) -> None:
        key = (edge.src_agent, edge.dst_agent, edge.relation_type)
        self.edges[key] = edge
        self.out_by_agent.setdefault(edge.src_agent, {}).setdefault(
            edge.relation_type, set()
        ).add(edge.dst_agent)
        self.in_by_agent.setdefault(edge.dst_agent, {}).setdefault(
            edge.relation_type, set()
        ).add(edge.src_agent)

    def _unindex(self, src: int, dst: int, relation_type: str) -> None:
        self.edges.pop((src, dst, relation_type), None)
        out_types = self.out_by_agent.get(src, {})
        if relation_type in out_types:
            out_types[relation_type].discard(dst)
            if not out_types[relation_type]:
                out_types.pop(relation_type)
        in_types = self.in_by_agent.get(dst, {})
        if relation_type in in_types:
            in_types[relation_type].discard(src)
            if not in_types[relation_type]:
                in_types.pop(relation_type)

    def _fire(self, src: int, dst: int, relation_type: str, op: str) -> None:
        for hook in self._on_change:
            try:
                hook(src, dst, relation_type, op)
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning(
                    "RelationGraph on_change hook %r failed: %s", hook, exc
                )


def _jsonify(metadata: Optional[dict]) -> Optional[str]:
    if metadata is None:
        return None
    if isinstance(metadata, str):
        return metadata
    import json

    try:
        return json.dumps(metadata)
    except (TypeError, ValueError):
        return None


__all__ = ["RelationGraph", "RelationEdge", "RelationTypeMeta", "RelationConflict"]
