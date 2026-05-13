"""In-memory PlaceStore (LAYOUT §2.A / §3.2 / impl/world_place_store.md).

MVP container for the ``P + L_t`` segment of the WorldState seven-tuple. Holds:

- ``places``:    place_id -> :class:`PlaceRecord`
- ``coverage``:  (src, dst) -> ``latency_ticks`` (with full :class:`CoverageEdge`)
- ``L``:         agent_id -> place_id (single-valued, agent is at exactly one place)
- ``agents_at``: place_id -> set[agent_id] (reverse index of ``L``)

Single-writer rule (LAYOUT B8): all DB mutation goes through :class:`WorldDB` which
owns the asyncio.Lock; this module forwards eagerly on every write — ``move`` is
``async`` so callers can await the WorldDB lock.

Hooks are plain sync callbacks (per task brief). PlaceTypeRegistrar subclasses
declared via :class:`agent_world.world._registrars.PlaceBase` plug their
``on_enter`` / ``on_leave`` (possibly-async) methods onto the hook lists at
``load_from_db`` time; user-supplied callbacks may also be registered via
``register_on_enter`` / ``register_on_leave``.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional, Set

from agent_world.persistence.world_db import WorldDB
from agent_world.world._registrars import PlaceTypeRegistrar


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class PlaceRecord:
    place_id: str
    parent_id: Optional[str] = None
    place_type: Optional[str] = None
    capacity: Optional[int] = None
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CoverageEdge:
    src: str
    dst: str
    latency_ticks: int = 0
    can_reach: bool = True


# Hook signatures: (agent_id, place_record, world) -> None  (sync)
PlaceHook = Callable[[int, PlaceRecord, Any], None]


# ---------------------------------------------------------------------------
# PlaceStore
# ---------------------------------------------------------------------------

class PlaceStore:
    """Authoritative in-memory store for places, coverage, and agent locations."""

    def __init__(self, world_db: WorldDB) -> None:
        self.world_db = world_db
        self.places: Dict[str, PlaceRecord] = {}
        self.coverage_map: Dict[tuple, CoverageEdge] = {}
        self.L: Dict[int, str] = {}
        self._agents_at: Dict[str, Set[int]] = {}
        self._on_enter: list[PlaceHook] = []
        self._on_leave: list[PlaceHook] = []

    # -- boot ---------------------------------------------------------------

    def load_from_db(self, world_db: Optional[WorldDB] = None) -> None:
        """Boot-time bulk load from world.db. Idempotent.

        Reads are sync (WorldDB read methods are sync per LAYOUT B8). The whole
        method is sync — call before the asyncio loop starts.
        """
        db = world_db or self.world_db
        self.places.clear()
        self.coverage_map.clear()
        self.L.clear()
        self._agents_at.clear()

        for row in db.list_places():
            rec = PlaceRecord(
                place_id=row["place_id"],
                parent_id=row.get("parent_id"),
                place_type=row.get("place_type"),
                capacity=row.get("capacity"),
                attrs=_parse_attrs(row.get("attrs")),
            )
            self.places[rec.place_id] = rec
            self._agents_at.setdefault(rec.place_id, set())

        for row in db.list_coverage():
            edge = CoverageEdge(
                src=row["src_place"],
                dst=row["dst_place"],
                latency_ticks=int(row.get("latency_ticks", 0) or 0),
                can_reach=bool(row.get("can_reach", 1)),
            )
            self.coverage_map[(edge.src, edge.dst)] = edge

        # self-loops default to zero-latency reachable
        for pid in self.places:
            self.coverage_map.setdefault(
                (pid, pid), CoverageEdge(pid, pid, latency_ticks=0, can_reach=True)
            )

        # WorldDB.get_location takes an agent_id; bulk load via raw SELECT.
        for agent_id, place_id in _scan_agent_locations(db):
            self.L[agent_id] = place_id
            self._agents_at.setdefault(place_id, set()).add(agent_id)

        # Wire PlaceType subclass hooks (sync wrappers around possibly-async methods).
        for _key, type_cls in PlaceTypeRegistrar.get_all().items():
            instance = type_cls()
            self._on_enter.append(_as_sync_hook(instance.on_enter))
            self._on_leave.append(_as_sync_hook(instance.on_leave))

    # -- read path ----------------------------------------------------------

    def agents_at(self, place_id: str) -> Set[int]:
        return set(self._agents_at.get(place_id, ()))

    def attrs(self, place_id: str) -> Dict[str, Any]:
        rec = self.places.get(place_id)
        return dict(rec.attrs) if rec else {}

    def record(self, place_id: str) -> Optional[PlaceRecord]:
        return self.places.get(place_id)

    def L_t(self, agent_id: int) -> Optional[str]:
        return self.L.get(agent_id)

    def coverage_latency(self, src: str, dst: str) -> Optional[int]:
        edge = self.coverage_map.get((src, dst))
        return edge.latency_ticks if edge and edge.can_reach else None

    def all_places(self) -> Iterable[PlaceRecord]:
        return self.places.values()

    # -- write path ---------------------------------------------------------

    async def move(self, agent_id: int, new_place: str, *, world: Any = None, t: int = 0) -> None:
        """Move ``agent_id`` to ``new_place``; eagerly persists to WorldDB.

        Atomically updates ``L`` and the reverse index, then fires on_leave /
        on_enter hooks. Hook exceptions are logged-and-swallowed (LAYOUT §2.A
        invariants: hook failure must not roll back world.db state).
        """
        if new_place not in self.places:
            raise KeyError(f"unknown place_id: {new_place}")

        old_place = self.L.get(agent_id)
        if old_place == new_place:
            return

        old_record = self.places.get(old_place) if old_place else None
        new_record = self.places[new_place]

        if old_record is not None:
            for hook in self._on_leave:
                _safe_call(hook, agent_id, old_record, world)

        if old_place is not None:
            self._agents_at.setdefault(old_place, set()).discard(agent_id)
        self.L[agent_id] = new_place
        self._agents_at.setdefault(new_place, set()).add(agent_id)

        # Persist via WorldDB (single-writer lock owned by WorldDB).
        await self.world_db.set_location(agent_id, new_place, t=t)

        for hook in self._on_enter:
            _safe_call(hook, agent_id, new_record, world)

    # -- hook registration --------------------------------------------------

    def register_on_enter(self, cb: PlaceHook) -> None:
        self._on_enter.append(cb)

    def register_on_leave(self, cb: PlaceHook) -> None:
        self._on_leave.append(cb)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _as_sync_hook(method: Callable[..., Any]) -> PlaceHook:
    """Wrap possibly-async PlaceBase.on_enter/on_leave into a sync callback.

    Async coroutines from un-overridden defaults return a no-op coroutine; we
    drop the awaitable. PlaceType authors who need real async work should
    register their own hook via :meth:`PlaceStore.register_on_enter`.
    """

    def _wrapped(agent_id: int, place: PlaceRecord, world: Any) -> None:
        result = method(agent_id, place, world)
        if inspect.iscoroutine(result):
            result.close()  # default no-op coroutine — discard

    return _wrapped


def _safe_call(hook: PlaceHook, agent_id: int, place: PlaceRecord, world: Any) -> None:
    try:
        hook(agent_id, place, world)
    except Exception as exc:  # noqa: BLE001 — hooks must never break the writer
        import logging

        logging.getLogger(__name__).warning(
            "PlaceStore hook %r failed for agent=%s place=%s: %s",
            hook, agent_id, place.place_id, exc,
        )


def _parse_attrs(raw: Any) -> Dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    return {}


def _scan_agent_locations(db: WorldDB) -> Iterable[tuple[int, str]]:
    """Bulk-read all (agent_id, place_id) rows from agent_location.

    WorldDB.get_location takes a specific agent_id, so for boot-time bulk load
    we reach into the connection directly. Read-only, no lock needed.
    """
    try:
        cursor = db._conn.execute("SELECT agent_id, place_id FROM agent_location")
    except Exception:  # noqa: BLE001
        return []
    return [(int(r["agent_id"]), str(r["place_id"])) for r in cursor.fetchall()]


__all__ = ["PlaceStore", "PlaceRecord", "CoverageEdge"]
