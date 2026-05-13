"""``WorldState`` — the seven-tuple aggregator (LAYOUT §2.A).

``WorldState`` itself stores **no data of its own** beyond the per-agent runtime
fields (``soul / long_term_goal / current_state / last_message_seen_at``); it
holds **references** to the L1 stores that own each tuple member:

    ⟨P, A, L_t, R_t, C_t, F_t, M_t⟩
       │  │   │     │    │    │    │
       │  │   │     │    │    │    └── memory (Zep, optional in MVP)
       │  │   │     │    │    └────── pools (MultiPoolPlatformManager)
       │  │   │     │    └─────────── capabilities (CapabilityTable)
       │  │   │     └──────────────── relations (RelationGraph)
       │  │   └────────────────────── places.L (agent → place)
       │  └────────────────────────── agents (Dict[int, Agent])
       └───────────────────────────── places (PlaceStore)

PerceptionBuilder, ActionDispatcher, ScriptEngine and BehaviorCompressor all
receive a ``WorldState`` reference and read/write the relevant sub-store.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

if TYPE_CHECKING:  # pragma: no cover - type hints only
    from agent_world.persistence.world_db import WorldDB
    from agent_world.pools.manager import MultiPoolPlatformManager
    from agent_world.world.capability_table import CapabilityTable
    from agent_world.world.clock import Clock
    from agent_world.world.place_store import PlaceStore
    from agent_world.world.relation_graph import RelationGraph

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AgentRuntime — per-agent world-owned fields (B5 4-segment system prompt).
# ---------------------------------------------------------------------------


@dataclass
class AgentRuntime:
    """Lightweight per-agent state owned by :class:`WorldState`.

    Distinct from the SocialAgent CAMEL ChatAgent: the SocialAgent (forked
    OASIS ``social_agent.agent.SocialAgent``) is registered in
    :attr:`WorldState.agents` once L4 fork wiring runs; until then this
    runtime stub captures the four B5 prompt fields.
    """

    agent_id: int
    soul: str = ""
    long_term_goal: str = ""
    current_state: str = "(initial)"
    # Tick at which ``current_state`` was last written. -1 = never; 0 = the
    # initial value seeded from scenario. PerceptionBuilder uses this to
    # surface a staleness warning ("your state is from t=0, you've moved
    # 5 ticks since — update_state to reflect reality").
    current_state_set_at: int = -1
    short_term_goal: str = ""
    last_message_seen_at: int = 0


# ---------------------------------------------------------------------------
# WorldState
# ---------------------------------------------------------------------------


class WorldState:
    """Aggregator over the seven-tuple sub-stores.

    The constructor follows the L4 task brief contract:

        ``WorldState(world_db, places, relations, caps, clock, pool_manager,
                     agents=None)``

    ``agents`` defaults to an empty dict and is populated when SocialAgent
    instances are created (L4 fork wiring).
    """

    def __init__(
        self,
        world_db: "WorldDB",
        places: "PlaceStore",
        relations: "RelationGraph",
        caps: "CapabilityTable",
        clock: "Clock",
        pool_manager: "MultiPoolPlatformManager",
        agents: Optional[Dict[int, Any]] = None,
    ) -> None:
        # Seven-tuple references (most are L1 stores).
        self.world_db = world_db
        self.places = places                # P + L_t (PlaceStore.L holds map)
        self.relations = relations          # R_t
        self.capabilities = caps            # C_t
        self.caps = caps                    # alias used by some L4 callers
        self.pools = pool_manager           # F_t (MultiPoolPlatformManager)
        self.clock = clock

        # A: agent registry. Populated by L4 SocialAgent fork; until then
        # empty. Values are typically SocialAgent instances; we duck-type:
        # callers reading current_state via :meth:`current_state` work for
        # both AgentRuntime and SocialAgent (both expose ``.current_state``).
        self.agents: Dict[int, Any] = agents if agents is not None else {}

        # Lockstep queues. ``delivery_lock`` is created lazily on first
        # access (so it binds to the running event loop, not the import-time
        # loop, which avoids cross-loop bugs on Python <3.10).
        self.pending_moves: Dict[int, str] = {}
        self._delivery_lock: Optional[asyncio.Lock] = None

    @property
    def delivery_lock(self) -> asyncio.Lock:
        if self._delivery_lock is None:
            self._delivery_lock = asyncio.Lock()
        return self._delivery_lock

    # ---------------------------------------------------------------- props

    @property
    def t(self) -> int:
        """Current global tick (proxy to :attr:`clock.t`)."""
        return int(self.clock.t)

    # ----------------------------------------------------------- registration

    def register_agent(self, agent_id: int, agent: Any) -> None:
        """Register a SocialAgent / AgentRuntime in :attr:`agents`."""
        self.agents[int(agent_id)] = agent

    def agent(self, agent_id: int) -> Any:
        """Return the agent object for ``agent_id`` (raises KeyError if missing)."""
        return self.agents[int(agent_id)]

    # ------------------------------------------------------------- accessors

    def agents_at(self, place_id: str) -> "Set[int]":
        """Agents currently located at ``place_id`` (Set, not List).

        Matches :meth:`PlaceStore.agents_at` so callers can use set operators
        (e.g. ``world.agents_at(p) - {self_id}``).
        """
        if hasattr(self.places, "agents_at"):
            try:
                return set(self.places.agents_at(place_id))
            except Exception:  # noqa: BLE001
                pass
        L = getattr(self.places, "L", {}) or {}
        return {a for a, p in L.items() if p == place_id}

    def location_of(self, agent_id: int) -> Optional[str]:
        """Return the place_id for ``agent_id`` (delegates to PlaceStore.L_t)."""
        L_t = getattr(self.places, "L_t", None)
        if callable(L_t):
            return L_t(agent_id)
        return getattr(self.places, "L", {}).get(int(agent_id))

    def relations_of(self, agent: Any) -> List[Tuple[int, str]]:
        """Return ``[(other_agent_id, relation_type), ...]`` for ``agent``.

        Accepts either an agent_id or any object exposing ``.agent_id`` /
        ``.id``.
        """
        agent_id = _coerce_agent_id(agent)
        if agent_id is None:
            return []
        try:
            return list(self.relations.contacts_of(agent_id))
        except Exception:  # noqa: BLE001
            return []

    def has_capability(self, agent: Any, cap: str) -> bool:
        """True if ``agent`` currently holds capability ``cap``."""
        agent_id = _coerce_agent_id(agent)
        if agent_id is None:
            return False
        return bool(self.capabilities.has(agent_id, cap))

    # ----------------------------------------------------- UPDATE_STATE (B5)

    def current_state(self, agent_id: int) -> Optional[str]:
        """Return ``agents[agent_id].current_state`` if present."""
        a = self.agents.get(int(agent_id))
        if a is None:
            return None
        return getattr(a, "current_state", None)

    def set_current_state(
        self, agent_id: int, new_state: str, t: Optional[int] = None,
    ) -> None:
        """UPDATE_STATE action target (LAYOUT B5).

        Both the agent's own ``UPDATE_STATE`` action and Script's
        ``StateChangeEffect`` write this same field — single owner per
        micro-tick (LAYOUT §9.5.1 N2). When ``t`` is supplied we also stamp
        ``current_state_set_at`` so PerceptionBuilder can detect staleness.
        """
        a = self.agents.get(int(agent_id))
        if a is None:
            # Fall back to creating a stub AgentRuntime so script effects
            # firing pre-fork still take effect.
            a = AgentRuntime(
                agent_id=int(agent_id),
                current_state=str(new_state),
                current_state_set_at=int(t) if t is not None else -1,
            )
            self.agents[int(agent_id)] = a
            return
        try:
            setattr(a, "current_state", str(new_state))
            if t is not None:
                try:
                    setattr(a, "current_state_set_at", int(t))
                except AttributeError:
                    pass
        except AttributeError:
            log.warning(
                "set_current_state(%s): agent has no settable current_state",
                agent_id,
            )

    def short_term_goal(self, agent_id: int) -> Optional[str]:
        """Return ``agents[agent_id].short_term_goal`` if present."""
        a = self.agents.get(int(agent_id))
        if a is None:
            return None
        return getattr(a, "short_term_goal", None)

    def set_short_term_goal(self, agent_id: int, new_goal: str) -> None:
        """UPDATE_SHORT_TERM_GOAL action target.

        Mirrors :meth:`set_current_state` semantics: writes to the agent's
        ``short_term_goal`` field so the next perception build surfaces it
        in the 5-segment system prompt.
        """
        a = self.agents.get(int(agent_id))
        if a is None:
            a = AgentRuntime(
                agent_id=int(agent_id), short_term_goal=str(new_goal),
            )
            self.agents[int(agent_id)] = a
            return
        try:
            setattr(a, "short_term_goal", str(new_goal))
        except AttributeError:
            log.warning(
                "set_short_term_goal(%s): agent has no settable short_term_goal",
                agent_id,
            )

    # -------------------------------------------------------- bookkeeping

    def queue_move(self, agent_id: int, new_place_id: str) -> None:
        """Queue a REQUEST_MOVE for resolution at WorldStep step 9."""
        self.pending_moves[int(agent_id)] = str(new_place_id)

    # -------------------------------------------------------- persistence

    async def flush(self) -> None:
        """Placeholder — most writes are eager (LAYOUT B8 single-writer).

        Sub-stores (PlaceStore / RelationGraph / CapabilityTable) write
        through to ``world.db`` on every mutation, so there is nothing to
        flush in the MVP. The hook is kept so WorldStep step 10 has a stable
        seam for later batched writers (Zep updater / agent_runtime_state
        snapshots, see LAYOUT §9.5.1 N5).
        """
        # Future: flush AgentRuntime.last_message_seen_at into a
        # ``agent_runtime_state`` table once that schema lands.
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _coerce_agent_id(agent: Any) -> Optional[int]:
    """Best-effort coercion of agent / agent_id / SocialAgent to ``int``."""
    if agent is None:
        return None
    if isinstance(agent, int):
        return agent
    for attr in ("agent_id", "id", "social_agent_id"):
        v = getattr(agent, attr, None)
        if isinstance(v, int):
            return v
    try:
        return int(agent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = ["WorldState", "AgentRuntime"]
