"""In-memory CapabilityTable (LAYOUT §2.A / §3.2 / impl/world_capability_table.md).

Per-agent capability set with reverse index for fast ``agents_with(cap)`` queries.

Persistence model (LAYOUT §3.2 ``capability`` table): rows are append-only;
``revoked_at`` is set on revoke (no DELETE) for auditability. The current state
view ``has(agent, cap)`` corresponds to "exists row with revoked_at IS NULL".

Hooks fire on ``grant`` / ``revoke`` (op = "grant" | "revoke") synchronously.
PlaceStore.on_leave can be wired separately to call ``revoke`` for capabilities
that declare ``auto_revoke_on_leave`` on their CapabilityBase subclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from agent_world.persistence.world_db import WorldDB
from agent_world.world._registrars import CapabilityTypeRegistrar


# ---------------------------------------------------------------------------
# Records & hooks
# ---------------------------------------------------------------------------

@dataclass
class CapabilityTypeMeta:
    name: str
    display_name: str = ""
    requires_place_type: Optional[str] = None
    auto_revoke_on_leave: bool = False


# (agent_id, capability, op) -> None  (op in {"grant", "revoke"})
CapabilityHook = Callable[[int, str, str], None]


class CapabilityNotAllowed(Exception):
    """Raised when ``requires_place_type`` is not satisfied at grant time."""


# ---------------------------------------------------------------------------
# CapabilityTable
# ---------------------------------------------------------------------------

class CapabilityTable:
    """Authoritative in-memory store for current-state capabilities."""

    def __init__(self, world_db: WorldDB) -> None:
        self.world_db = world_db
        self.by_agent: Dict[int, Set[str]] = {}
        self.by_capability: Dict[str, Set[int]] = {}
        self.type_meta: Dict[str, CapabilityTypeMeta] = {}
        self._on_change: List[CapabilityHook] = []

    # -- boot ---------------------------------------------------------------

    def load_from_db(self, world_db: Optional[WorldDB] = None) -> None:
        db = world_db or self.world_db
        self.by_agent.clear()
        self.by_capability.clear()
        self.type_meta.clear()

        for key, type_cls in CapabilityTypeRegistrar.get_all().items():
            self.type_meta[key] = CapabilityTypeMeta(
                name=key,
                display_name=getattr(type_cls, "display_name", "") or "",
                requires_place_type=getattr(type_cls, "requires_place_type", None),
                auto_revoke_on_leave=bool(getattr(type_cls, "auto_revoke_on_leave", False)),
            )

        # Pull current-state grants from WorldDB. The table stores both granted
        # and revoked rows; current state == ``revoked_at IS NULL``. Read-only
        # bulk scan is safe without the WorldDB lock.
        try:
            cursor = db._conn.execute(
                "SELECT agent_id, capability FROM capability WHERE revoked_at IS NULL"
            )
            rows = cursor.fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            agent = int(row["agent_id"])
            cap = str(row["capability"])
            self.by_agent.setdefault(agent, set()).add(cap)
            self.by_capability.setdefault(cap, set()).add(agent)

    # -- read path ----------------------------------------------------------

    def has(self, agent: int, cap: str) -> bool:
        return cap in self.by_agent.get(agent, set())

    def of(self, agent: int) -> Set[str]:
        return set(self.by_agent.get(agent, set()))

    def agents_with(self, cap: str) -> Set[int]:
        return set(self.by_capability.get(cap, set()))

    def all_capabilities(self) -> Iterable[str]:
        return self.by_capability.keys()

    # -- write path ---------------------------------------------------------

    async def grant(
        self,
        agent: int,
        cap: str,
        *,
        world: Any = None,
        t: int = 0,
        metadata: Optional[dict] = None,
    ) -> None:
        if cap in self.by_agent.get(agent, set()):
            return  # idempotent

        meta = self.type_meta.get(cap)
        if meta and meta.requires_place_type and world is not None:
            place_id = world.places.L_t(agent) if hasattr(world, "places") else None
            place = world.places.record(place_id) if place_id else None
            if place is None or place.place_type != meta.requires_place_type:
                raise CapabilityNotAllowed(
                    f"capability {cap!r} requires place_type "
                    f"{meta.requires_place_type!r}; agent {agent} is not there"
                )

        self.by_agent.setdefault(agent, set()).add(cap)
        self.by_capability.setdefault(cap, set()).add(agent)
        await self.world_db.grant(agent, cap, granted_at=t, metadata=_jsonify(metadata))
        self._fire(agent, cap, "grant")

    async def revoke(self, agent: int, cap: str, *, world: Any = None, t: int = 0) -> None:
        if cap not in self.by_agent.get(agent, set()):
            return  # idempotent
        self.by_agent[agent].discard(cap)
        bucket = self.by_capability.get(cap)
        if bucket is not None:
            bucket.discard(agent)
            if not bucket:
                self.by_capability.pop(cap, None)
        await self.world_db.revoke(agent, cap, revoked_at=t)
        self._fire(agent, cap, "revoke")

    # -- hook registration --------------------------------------------------

    def register_on_change(self, cb: CapabilityHook) -> None:
        self._on_change.append(cb)

    async def auto_revoke_on_leave(self, agent: int, place: Any, world: Any) -> None:
        """Revoke any auto-revoke caps whose ``requires_place_type`` matches.

        Called by WorldStep (or whoever wires PlaceStore.on_leave) inside the
        async ``move`` flow so the writes can be awaited. Hook-list registration
        keeps the sync callback contract; for the auto-revoke path we expose
        this async helper instead of forcing the place hook to be async.
        """
        place_type = getattr(place, "place_type", None)
        t = getattr(world, "t", 0) if world is not None else 0
        for cap, meta in list(self.type_meta.items()):
            if not meta.auto_revoke_on_leave:
                continue
            if meta.requires_place_type and place_type != meta.requires_place_type:
                continue
            if cap in self.by_agent.get(agent, set()):
                await self.revoke(agent, cap, world=world, t=t)

    # -- internals ----------------------------------------------------------

    def _fire(self, agent: int, cap: str, op: str) -> None:
        for hook in self._on_change:
            try:
                hook(agent, cap, op)
            except Exception as exc:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).warning(
                    "CapabilityTable on_change hook %r failed: %s", hook, exc
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


__all__ = ["CapabilityTable", "CapabilityTypeMeta", "CapabilityNotAllowed"]
