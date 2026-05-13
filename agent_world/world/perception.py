"""PerceptionBuilder (L4 / LAYOUT §2.A / §6.3 / impl/world_perception.md).

Pure-read gateway that gathers everything an agent needs to decide on the
current micro-tick into a single :class:`Observation`, plus the 4-segment
``system_prompt`` (B5: ``# Soul`` / ``# Long-term Goal`` / ``# Current State``
/ ``# Place Behavior Rule``).

Sources, in build order (LAYOUT §6.3 pseudocode):

* ``self_location`` / ``location_attrs`` / ``co_located_agents``  — PlaceStore
* ``contacts``                                                     — RelationGraph + ConnectivityResolver.phi_rdc
* ``feeds``                                                        — MultiPoolPlatformManager.feeds_at + CapabilityTable
* ``incoming_messages``                                            — WorldDB.fetch_arrived_for (delivered=1, arrive_at<=t)
* ``overheard``                                                    — WorldDB.fetch_overhear_for (since=t-1)
* ``recent_failed_attempts`` (B9, 1 tick TTL)                      — WorldDB.fetch_failed_attempts_for(t-1)
* ``group_events``           (B6, 1 tick TTL)                      — WorldDB.fetch_for_agent(t-1)
* ``relevant_memories``                                            — MultiGraphRetriever.search (best-effort)
* ``scripted_notification``                                        — ScriptEngine.pending_for (best-effort)

Optional dependencies (``retriever``, ``script_engine``, ``pool_manager``)
fail soft: any exception logs and yields an empty / ``None`` field, so the
P0 demo doesn't crash when memory/script/pools haven't been wired yet.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agent_world.persistence.world_db import (
    DirectMessageRow,
    GroupEventRow,
    OverhearRow,
    WorldDB,
)
from agent_world.world.capability_table import CapabilityTable
from agent_world.world.connectivity import ConnectivityResolver
from agent_world.world.place_store import PlaceStore
from agent_world.world.relation_graph import RelationGraph

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Observation field types                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class ContactBrief:
    """One contact row in :attr:`Observation.contacts`."""

    agent_id: int
    can_reach_now: bool
    reason: Optional[str] = None
    relation_types: List[str] = field(default_factory=list)


@dataclass
class FeedBrief:
    """Tiny DTO around :class:`pools.PoolHandle.brief` for the LLM prompt."""

    pool_id: str
    feed_type: str
    display_name: str = ""


@dataclass
class PlaceBrief:
    """One destination row for :attr:`Observation.available_places_brief`.

    ``occupants`` is ``None`` when the place's ``attrs.roster_visible`` is
    falsy (closed-door scenes like a dungeon — the agent can know the place
    exists but not who is inside). Empty list ``[]`` means "visible AND empty".

    ``occupant_relations`` maps each visible occupant_id to that occupant's
    relation types **from the perceiving agent's POV** (e.g. ``{1: ['spouse']}``
    when self is married to agent 1). Missing key / empty list = no known
    relation (i.e. stranger). Lets the LLM treat "spouse's home with no
    spouse around" very differently from "lover's apartment".
    """

    place_id: str
    summary: str = ""
    occupants: Optional[List[int]] = None
    occupant_relations: Dict[int, List[str]] = field(default_factory=dict)


@dataclass
class Observation:
    """11-field observation per LAYOUT §6.3, in declaration order.

    Plus two extension fields for the demo prompt-builder:
    ``available_places_brief`` (per-destination summary + visible roster) and
    ``f2f_local_history`` (rolling F2F transcript at ``self_location``).
    """

    self_location: Optional[str]
    location_attrs: Dict[str, Any]
    co_located_agents: List[int]
    contacts: List[ContactBrief]
    feeds: List[Any]
    incoming_messages: List[DirectMessageRow]
    overheard: List[OverhearRow]
    recent_failed_attempts: List[DirectMessageRow]
    group_events: List[GroupEventRow]
    relevant_memories: List[Dict[str, Any]]
    scripted_notification: Optional[Any]
    available_places_brief: List[PlaceBrief] = field(default_factory=list)
    f2f_local_history: List[Tuple[int, int, int, str]] = field(default_factory=list)
    # Agents who just arrived at / departed from ``self_location`` at the end
    # of the previous tick. Empty at t=0 (no prior snapshot to diff against).
    # Lets the LLM update its "who is in the room with me right now" model
    # instead of carrying the previous tick's roster forward.
    recent_arrivals: List[int] = field(default_factory=list)
    recent_departures: List[int] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# PerceptionBuilder                                                           #
# --------------------------------------------------------------------------- #


class PerceptionBuilder:
    """Build (system_prompt, Observation) for one agent on one micro-tick."""

    # B5 5-segment system prompt headers, in order:
    #   (Soul, Long-term Goal, Current State, Short-term Goal, Place Behavior Rule)
    # Short-term Goal is the agent-mutable anchor against F2F drift; updated
    # via the UPDATE_SHORT_TERM_GOAL action. Override on the instance to
    # localise (e.g. Chinese demo runner).
    SEGMENT_HEADERS = (
        "# Soul",
        "# Long-term Goal",
        "# Current State",
        "# Short-term Goal",
        "# Place Behavior Rule",
    )

    def __init__(
        self,
        world_db: WorldDB,
        places: PlaceStore,
        relations: RelationGraph,
        caps: CapabilityTable,
        connectivity: ConnectivityResolver,
        retriever: Any = None,
        script_engine: Any = None,
        pool_manager: Any = None,
        segment_headers: Optional[tuple] = None,
    ) -> None:
        self.world_db = world_db
        self.places = places
        self.relations = relations
        self.caps = caps
        self.connectivity = connectivity
        self.retriever = retriever
        self.script_engine = script_engine
        self.pool_manager = pool_manager
        if segment_headers is not None:
            self.SEGMENT_HEADERS = tuple(segment_headers)
        # Movement diff cache: snapshot of ``places.L`` taken at the start of
        # the most recent tick, so we can derive per-place arrival/departure
        # lists as a perception event.
        self._loc_snapshot: Dict[int, Optional[str]] = {}
        self._loc_snapshot_t: int = -1
        self._cached_arrivals: Dict[str, List[int]] = {}
        self._cached_departures: Dict[str, List[int]] = {}

    # ------------------------------------------------------------------ #
    # main entry                                                         #
    # ------------------------------------------------------------------ #

    async def build(
        self,
        agent: Any,
        world: Any,
        t: int,
    ) -> Tuple[str, Observation]:
        agent_id = self._agent_id(agent)

        # --- B1: location / co-located / contacts / feeds ----------------
        self_location = self.places.L_t(agent_id)
        location_attrs: Dict[str, Any] = (
            self.places.attrs(self_location) if self_location else {}
        )
        co_located: List[int] = (
            sorted(self.places.agents_at(self_location) - {agent_id})
            if self_location
            else []
        )
        contacts = self._build_contacts(agent_id)
        feeds = self._build_feeds(agent_id, self_location)

        # --- B1.1 + B9 + B6: world.db reads (hot path) -------------------
        # B1.1 dedupe key: avoid the `or -1` trap — when the agent has read
        # up to t=0, `0 or -1 == -1` would re-emit message 0 forever.
        _last = getattr(agent, "last_message_seen_at", None)
        last_seen = -1 if _last is None else int(_last)
        incoming = self._safe_fetch(
            self.world_db.fetch_arrived_for, agent_id, t, last_seen
        )
        overheard = self._safe_fetch(
            self.world_db.fetch_overhear_for, agent_id, max(t - 1, 0)
        )
        recent_failed = self._safe_fetch(
            self.world_db.fetch_failed_attempts_for, agent_id, t - 1
        )
        group_events = self._safe_fetch(
            self.world_db.fetch_for_agent, agent_id, t - 1
        )

        # --- cross-graph memory + scripted notification (best effort) ----
        relevant_memories = await self._search_memories(agent, self_location)
        scripted = self._pending_script(agent_id)

        # --- destination briefs (per-place summary + visible roster) -----
        # Source list lives on the agent (DemoAgent.available_places); if
        # absent, the brief list is empty and the renderer falls back.
        # Build relation map (other_id -> [types]) from the contacts we
        # already computed, so brief occupants can be annotated.
        relation_map: Dict[int, List[str]] = {
            int(c.agent_id): list(c.relation_types) for c in contacts
        }
        avail = list(getattr(agent, "available_places", []) or [])
        place_briefs = self._build_place_briefs(
            agent_id, self_location, avail, relation_map,
        )

        # --- F2F transcript at current place (anti-loop context) ----------
        f2f_history: List[Tuple[int, int, int, str]] = []
        if self_location is not None:
            f2f_history = self._safe_fetch(
                self.world_db.fetch_f2f_history_at,
                self_location, t, max(t - 6, 0), 30,
            )

        # --- arrivals / departures at this place since last tick ----------
        self._refresh_movement_cache(t)
        if self_location is not None:
            arrivals = sorted(
                aid for aid in self._cached_arrivals.get(self_location, [])
                if int(aid) != int(agent_id)
            )
            departures = sorted(
                aid for aid in self._cached_departures.get(self_location, [])
                if int(aid) != int(agent_id)
            )
        else:
            arrivals, departures = [], []

        obs = Observation(
            self_location=self_location,
            location_attrs=location_attrs,
            co_located_agents=co_located,
            contacts=contacts,
            feeds=feeds,
            incoming_messages=list(incoming),
            overheard=list(overheard),
            recent_failed_attempts=list(recent_failed),
            group_events=list(group_events),
            relevant_memories=relevant_memories,
            scripted_notification=scripted,
            available_places_brief=place_briefs,
            f2f_local_history=list(f2f_history),
            recent_arrivals=arrivals,
            recent_departures=departures,
        )

        # --- B5: 5-segment system prompt (header order is hard-coded) ----
        soul = getattr(agent, "soul", "") or ""
        long_term_goal = getattr(agent, "long_term_goal", "") or ""
        current_state = getattr(agent, "current_state", "") or ""
        short_term_goal = (
            getattr(agent, "short_term_goal", "") or ""
        ).strip() or "(尚未设定——本拍可调用 update_short_term_goal 主动设定)"
        behavior_hint = location_attrs.get("behavior_hint") or "(none)"
        h_soul, h_goal, h_state, h_short, h_rule = self.SEGMENT_HEADERS
        system_prompt = "\n\n".join(
            [
                f"{h_soul}\n{soul}",
                f"{h_goal}\n{long_term_goal}",
                f"{h_state}\n{current_state}",
                f"{h_short}\n{short_term_goal}",
                f"{h_rule}\n{behavior_hint}",
            ]
        )
        return system_prompt, obs

    # ------------------------------------------------------------------ #
    # destination briefs                                                 #
    # ------------------------------------------------------------------ #

    def _refresh_movement_cache(self, t: int) -> None:
        """Diff this tick's start-of-tick L map against the last snapshot.

        Idempotent within a tick (only the first call at a new ``t`` does
        the diff). Builds per-place arrival / departure lists; first call
        ever yields empty diffs because there is no prior snapshot.
        """
        if int(t) == self._loc_snapshot_t:
            return
        current: Dict[int, Optional[str]] = dict(getattr(self.places, "L", {}) or {})
        if self._loc_snapshot_t < 0:
            self._cached_arrivals = {}
            self._cached_departures = {}
            self._loc_snapshot = current
            self._loc_snapshot_t = int(t)
            return
        arrivals: Dict[str, List[int]] = {}
        departures: Dict[str, List[int]] = {}
        all_aids = set(current) | set(self._loc_snapshot)
        for aid in all_aids:
            prev = self._loc_snapshot.get(aid)
            cur = current.get(aid)
            if prev == cur:
                continue
            if cur is not None:
                arrivals.setdefault(cur, []).append(int(aid))
            if prev is not None:
                departures.setdefault(prev, []).append(int(aid))
        self._cached_arrivals = arrivals
        self._cached_departures = departures
        self._loc_snapshot = current
        self._loc_snapshot_t = int(t)

    def _build_place_briefs(
        self,
        agent_id: int,
        self_location: Optional[str],
        candidate_pids: List[str],
        relation_map: Optional[Dict[int, List[str]]] = None,
    ) -> List[PlaceBrief]:
        """Build :class:`PlaceBrief` rows for every reachable destination.

        Per-place ``attrs.roster_visible`` controls whether occupants are
        exposed (cafe / public space = True; closed-door = False). The
        ``summary`` field falls back to the first line of ``behavior_hint``
        so authors can opt into a separate short tag.

        ``relation_map`` (other_id → [types]) annotates each visible occupant
        with the perceiving agent's social link to them, so the LLM can
        avoid e.g. wandering into a spouse's apartment when they are not the
        spouse.
        """
        rmap = relation_map or {}
        out: List[PlaceBrief] = []
        for pid in candidate_pids:
            if pid == self_location:
                continue
            if pid not in self.places.places:
                continue
            attrs = self.places.attrs(pid) or {}
            summary = (attrs.get("summary") or "").strip()
            if not summary:
                hint = (attrs.get("behavior_hint") or "").strip()
                summary = hint.splitlines()[0] if hint else ""
            occupants: Optional[List[int]] = None
            occ_rels: Dict[int, List[str]] = {}
            if bool(attrs.get("roster_visible", False)):
                # Exclude self even if somehow listed; defensive.
                occupants = sorted(
                    self.places.agents_at(pid) - {int(agent_id)}
                )
                for oid in occupants:
                    occ_rels[oid] = list(rmap.get(int(oid), []))
            out.append(
                PlaceBrief(
                    place_id=pid,
                    summary=summary,
                    occupants=occupants,
                    occupant_relations=occ_rels,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _agent_id(agent: Any) -> int:
        for attr in ("agent_id", "id", "user_id"):
            v = getattr(agent, attr, None)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        raise AttributeError("agent has no agent_id/id field")

    def _build_contacts(self, agent_id: int) -> List[ContactBrief]:
        """Group :meth:`RelationGraph.contacts_of` by peer + φ_RDC verdict."""
        peers: Dict[int, List[str]] = {}
        try:
            for other, rtype in self.relations.contacts_of(agent_id):
                peers.setdefault(int(other), []).append(str(rtype))
        except Exception as exc:  # noqa: BLE001
            log.warning("contacts_of(%s) failed: %s", agent_id, exc)
            return []

        out: List[ContactBrief] = []
        for other, rtypes in peers.items():
            try:
                reachable = self.connectivity.phi_rdc(agent_id, other)
            except Exception:  # noqa: BLE001
                reachable = False
            reason: Optional[str] = None
            if not reachable:
                reason = self._unreachable_reason(agent_id, other)
            out.append(
                ContactBrief(
                    agent_id=other,
                    can_reach_now=reachable,
                    reason=reason,
                    relation_types=sorted(set(rtypes)),
                )
            )
        out.sort(key=lambda c: c.agent_id)
        return out

    def _unreachable_reason(self, a: int, b: int) -> str:
        """Best-effort short string for B9-style debug prompts."""
        try:
            from agent_world.world.connectivity import RDC_CAPABILITY
        except Exception:  # noqa: BLE001
            RDC_CAPABILITY = "signal_uplink"  # type: ignore[assignment]
        if not self.caps.has(a, RDC_CAPABILITY):
            return "self missing signal_uplink"
        if not self.caps.has(b, RDC_CAPABILITY):
            return "peer missing signal_uplink"
        pa, pb = self.places.L_t(a), self.places.L_t(b)
        if pa is None or pb is None:
            return "location unknown"
        if (pa, pb) not in self.places.coverage_map:
            return "no coverage"
        edge = self.places.coverage_map.get((pa, pb))
        if edge is not None and not edge.can_reach:
            return "coverage closed"
        return "out of reach"

    def _build_feeds(self, agent_id: int, place_id: Optional[str]) -> List[Any]:
        if place_id is None or self.pool_manager is None:
            return []
        try:
            briefs = list(self.pool_manager.feeds_at(place_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("pool_manager.feeds_at(%s) failed: %s", place_id, exc)
            return []
        # Filter by ``account_<feed>`` capability.
        filtered: List[Any] = []
        for brief in briefs:
            feed = (
                getattr(brief, "feed_type", None)
                or getattr(brief, "feed", None)
                or getattr(brief, "feed_name", None)
            )
            if feed and self.caps.has(agent_id, f"account_{feed}"):
                filtered.append(brief)
        return filtered

    @staticmethod
    def _safe_fetch(fn: Any, *args: Any) -> List[Any]:
        try:
            return list(fn(*args) or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("WorldDB fetch %r failed: %s", getattr(fn, "__name__", fn), exc)
            return []

    async def _search_memories(
        self, agent: Any, place_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        if self.retriever is None:
            return []
        graph_ids: List[str] = []
        try:
            agent_id = self._agent_id(agent)
            graph_ids.append(f"agent_{agent_id}")
        except Exception:  # noqa: BLE001
            pass
        if place_id:
            graph_ids.append(f"place_{place_id}")
        query = str(getattr(agent, "recent_intent", "") or "")
        if not graph_ids or not query:
            return []
        try:
            res = self.retriever.search(graph_ids=graph_ids, query=query)
            if inspect.isawaitable(res):
                res = await res
            return list(res or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("retriever.search failed: %s", exc)
            return []

    def _pending_script(self, agent_id: int) -> Optional[Any]:
        if self.script_engine is None:
            return None
        try:
            pending = self.script_engine.pending_for(agent_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("script_engine.pending_for failed: %s", exc)
            return None
        if not pending:
            return None
        # ``pending_for`` returns a list of payloads; collapse to a single
        # newline-joined string for the user_prompt rendering.
        try:
            return "\n".join(str(p) for p in pending)
        except Exception:  # noqa: BLE001
            return pending


__all__ = [
    "ContactBrief",
    "FeedBrief",
    "PlaceBrief",
    "Observation",
    "PerceptionBuilder",
]
