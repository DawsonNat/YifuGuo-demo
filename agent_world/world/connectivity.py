"""ConnectivityResolver (LAYOUT §2.A / §6.2 / impl/world_connectivity.md).

Read-only resolver that fuses the three L1 stores — :class:`PlaceStore`,
:class:`RelationGraph`, :class:`CapabilityTable` — into the four φ predicates
the rest of the system uses for "can A talk to B over channel X?":

- ``phi_f2f(a, b)``         same place
- ``phi_rdc(a, b)``         contact + signal_uplink + coverage open
- ``phi_grp(a, group_id)``  group member + signal_uplink + coverage open
- ``phi_feed(a, p, feed)``  place hosts feed + ``account_<feed>`` capability

Plus convenience batch APIs (``reachable_agents``) and a thin coverage-latency
forwarder. The resolver itself holds no state and never mutates upstream
indexes; reverse lookups go through ``PlaceStore._agents_at`` and
``CapabilityTable.by_capability`` directly.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Set

from agent_world.world.capability_table import CapabilityTable
from agent_world.world.place_store import PlaceStore
from agent_world.world.relation_graph import RelationGraph

# Default capability gating RDC + GRP. Per impl doc (§4.2) configurable via
# channel_config in later phases; MVP keeps it as a module-level constant.
RDC_CAPABILITY = "signal_uplink"
GRP_CAPABILITY = "signal_uplink"
FEED_CAPABILITY_PREFIX = "account_"


class ConnectivityResolver:
    """Stateless φ-predicate resolver over the three L1 stores."""

    def __init__(
        self,
        places: PlaceStore,
        relations: RelationGraph,
        caps: CapabilityTable,
        *,
        group_members: Optional[Mapping[int, Iterable[int]]] = None,
        place_feeds: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> None:
        self.places = places
        self.relations = relations
        self.caps = caps
        # MVP shims for L3 (GroupMessageBus / pool feed registry not yet wired):
        # group_id -> {agent_id, ...} ; place_id -> {feed_name, ...}
        self._group_members: Dict[int, Set[int]] = {
            int(g): set(int(a) for a in members)
            for g, members in (group_members or {}).items()
        }
        self._place_feeds: Dict[str, Set[str]] = {
            str(p): set(str(f) for f in feeds)
            for p, feeds in (place_feeds or {}).items()
        }

    # -- φ predicates -------------------------------------------------------

    def phi_f2f(self, a: int, b: int) -> bool:
        if a == b:
            return False
        pa = self.places.L_t(a)
        pb = self.places.L_t(b)
        return pa is not None and pa == pb

    def phi_rdc(self, a: int, b: int) -> bool:
        if a == b:
            return False
        if not any(other == b for (other, _t) in self.relations.contacts_of(a)):
            return False
        if not (self.caps.has(a, RDC_CAPABILITY) and self.caps.has(b, RDC_CAPABILITY)):
            return False
        pa, pb = self.places.L_t(a), self.places.L_t(b)
        if pa is None or pb is None:
            return False
        edge = self.places.coverage_map.get((pa, pb))
        return edge is not None and edge.can_reach

    def phi_grp(self, a: int, group_id: int) -> bool:
        members = self._group_members.get(int(group_id))
        if not members or a not in members:
            return False
        if not self.caps.has(a, GRP_CAPABILITY):
            return False
        pa = self.places.L_t(a)
        if pa is None:
            return False
        edge = self.places.coverage_map.get((pa, pa))
        return edge is not None and edge.can_reach

    def phi_feed(self, a: int, place: str, feed: str) -> bool:
        if not self.caps.has(a, f"{FEED_CAPABILITY_PREFIX}{feed}"):
            return False
        if self.places.L_t(a) != place:
            return False
        feeds = self._place_feeds.get(place, set())
        return feed in feeds

    # -- batch / convenience -----------------------------------------------

    def co_located(self, a: int) -> FrozenSet[int]:
        pa = self.places.L_t(a)
        if pa is None:
            return frozenset()
        return frozenset(self.places.agents_at(pa) - {a})

    def reachable_agents(self, a: int, channel: str) -> FrozenSet[int]:
        ch = channel.upper()
        if ch == "F2F":
            return self.co_located(a)
        if ch == "RDC":
            candidates = {other for (other, _t) in self.relations.contacts_of(a)}
            return frozenset(b for b in candidates if self.phi_rdc(a, b))
        if ch == "GRP":
            out: Set[int] = set()
            for gid, members in self._group_members.items():
                if a in members and self.phi_grp(a, gid):
                    out.update(m for m in members if m != a)
            return frozenset(out)
        return frozenset()

    def reachable_feeds(self, a: int) -> FrozenSet[str]:
        pa = self.places.L_t(a)
        if pa is None:
            return frozenset()
        return frozenset(
            feed for feed in self._place_feeds.get(pa, set())
            if self.phi_feed(a, pa, feed)
        )

    # -- coverage latency forwarder ----------------------------------------

    def latency(self, src_place: str, dst_place: str) -> int:
        """Return coverage latency in ticks; ``-1`` when unreachable.

        Thin pass-through to :meth:`PlaceStore.coverage_latency`. Bus layers use
        this to compute ``arrive_at = t + latency`` for RDC / GRP messages.
        """
        lat = self.places.coverage_latency(src_place, dst_place)
        return -1 if lat is None else int(lat)

    # -- MVP wiring helpers (L3 will replace these with real registries) ---

    def set_group_members(self, group_id: int, members: Iterable[int]) -> None:
        self._group_members[int(group_id)] = set(int(a) for a in members)

    def set_place_feeds(self, place_id: str, feeds: Iterable[str]) -> None:
        self._place_feeds[str(place_id)] = set(str(f) for f in feeds)


__all__ = ["ConnectivityResolver", "RDC_CAPABILITY", "GRP_CAPABILITY", "FEED_CAPABILITY_PREFIX"]
