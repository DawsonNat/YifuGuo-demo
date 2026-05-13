"""MultiGraphManager (L2, ADAPTed from MiroFish ZepGraphMemoryManager).

Source: ``ref/MiroFish/backend/app/services/zep_graph_memory_updater.py``
        L479-554 (single-graph, sim_id-keyed registry).

Adaptations for Agent World v0.3 (LAYOUT §2.F):
    1. Registry key changed from ``sim_id`` (str) to ``(sim_id, graph_id)``
       (tuple). One simulation now owns N graphs (``agent_{id}`` /
       ``place_{id}`` / ``world``); all share a single Zep client but each
       is a separate ``MultiGraphUpdater`` instance with its own buffers.
    2. Lazy init via ``get_or_create(sim_id, graph_id, kind)``: first call
       for a given key creates and starts the updater; subsequent calls
       return the same instance.
    3. ``flush_all(sim_id=None)`` enumerates and flushes every updater
       belonging to ``sim_id`` (or all if None) -- used by world_step at
       round end (LAYOUT §6.1 step 10) and on shutdown.
    4. Convenience helpers (``for_agent`` / ``for_place`` / ``for_world``)
       expand ``world_graphs`` config templates so callers don't sprinkle
       ``f"agent_{id}"`` literals.
    5. Zep client is duck-typed; if absent, updaters run in dry-run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from agent_world.memory.updater import MultiGraphUpdater

logger = logging.getLogger("agent_world.memory.manager")


# Default templates (override via config); LAYOUT §2.F naming.
DEFAULT_PER_AGENT_TEMPLATE = "agent_{id}"
DEFAULT_PER_PLACE_TEMPLATE = "place_{id}"
DEFAULT_WORLD_GRAPH_ID = "world"


class MultiGraphManager:
    """``(sim_id, graph_id) -> MultiGraphUpdater`` registry.

    A single instance is process-wide (lives in app.services); multiple
    simulations may share it. Each ``(sim_id, graph_id)`` resolves to
    exactly one ``MultiGraphUpdater``.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        per_agent_template: str = DEFAULT_PER_AGENT_TEMPLATE,
        per_place_template: str = DEFAULT_PER_PLACE_TEMPLATE,
        world_graph_id: str = DEFAULT_WORLD_GRAPH_ID,
        updater_factory: Optional[Callable[..., MultiGraphUpdater]] = None,
    ) -> None:
        """Args:
            client: duck-typed Zep client (may be None for dry-run).
            per_agent_template / per_place_template: f-string templates
                taking ``{id}``; expanded by ``for_agent`` / ``for_place``.
            world_graph_id: literal graph_id for the world layer.
            updater_factory: optional override (testing / DI). Default
                constructs a real ``MultiGraphUpdater``.
        """
        self._client = client
        self._per_agent_template = per_agent_template
        self._per_place_template = per_place_template
        self._world_graph_id = world_graph_id

        self._factory: Callable[..., MultiGraphUpdater] = (
            updater_factory or (lambda c, sid, **kw: MultiGraphUpdater(c, sid, **kw))
        )

        self._updaters: Dict[Tuple[str, str], MultiGraphUpdater] = {}
        # one create-lock per sim_id avoids racing two get_or_create calls
        self._sim_locks: Dict[str, asyncio.Lock] = {}

        if client is None:
            logger.warning(
                "MultiGraphManager initialised without Zep client -- updaters "
                "will run in dry-run mode."
            )

    # ------------------------------------------------------------------ #
    # primary API                                                        #
    # ------------------------------------------------------------------ #

    async def get_or_create(
        self,
        sim_id: str,
        graph_id: str,
        kind: str = "behavior_summary",
    ) -> MultiGraphUpdater:
        """Return (or lazy-create + start) the updater for (sim_id, graph_id).

        ``kind`` is currently advisory: a single ``MultiGraphUpdater``
        instance handles all kinds for a given graph (kinds become buffer
        sub-keys inside the updater). The parameter is kept on the public
        signature so callers can pass it through and so future versions
        may shard further without breaking the call site.
        """
        key = (str(sim_id), str(graph_id))
        existing = self._updaters.get(key)
        if existing is not None:
            return existing

        lock = self._sim_locks.setdefault(str(sim_id), asyncio.Lock())
        async with lock:
            existing = self._updaters.get(key)
            if existing is not None:
                return existing
            updater = self._factory(self._client, str(sim_id))
            await updater.start()
            self._updaters[key] = updater
            logger.info(
                "Created updater: sim_id=%s graph_id=%s kind=%s",
                sim_id, graph_id, kind,
            )
            return updater

    # ------------------------------------------------------------------ #
    # convenience: template expansion + lookup                           #
    # ------------------------------------------------------------------ #

    def for_agent_graph_id(self, agent_id: int) -> str:
        return self._per_agent_template.format(id=agent_id)

    def for_place_graph_id(self, place_id: Any) -> str:
        return self._per_place_template.format(id=place_id)

    def for_world_graph_id(self) -> str:
        return self._world_graph_id

    async def for_agent(
        self, sim_id: str, agent_id: int, kind: str = "behavior_summary",
    ) -> MultiGraphUpdater:
        return await self.get_or_create(
            sim_id, self.for_agent_graph_id(agent_id), kind
        )

    async def for_place(
        self, sim_id: str, place_id: Any, kind: str = "behavior_summary",
    ) -> MultiGraphUpdater:
        return await self.get_or_create(
            sim_id, self.for_place_graph_id(place_id), kind
        )

    async def for_world(
        self, sim_id: str, kind: str = "system_event",
    ) -> MultiGraphUpdater:
        return await self.get_or_create(
            sim_id, self.for_world_graph_id(), kind
        )

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def flush_all(self, sim_id: Optional[str] = None) -> None:
        """Flush every updater (optionally restricted to one sim_id).

        Called by world_step at round end (LAYOUT §6.1 step 10) and on
        shutdown.
        """
        targets = [
            u for (s, _g), u in self._updaters.items()
            if sim_id is None or s == str(sim_id)
        ]
        if not targets:
            return
        await asyncio.gather(
            *[u.flush() for u in targets], return_exceptions=True
        )

    async def shutdown(self, sim_id: Optional[str] = None) -> None:
        """Stop and remove updaters for one sim_id (or all if None)."""
        keys = [
            k for k in list(self._updaters.keys())
            if sim_id is None or k[0] == str(sim_id)
        ]
        for k in keys:
            u = self._updaters.pop(k, None)
            if u is None:
                continue
            try:
                await u.stop()
            except Exception as e:  # noqa: BLE001
                logger.error("stop() failed for %s: %s", k, e)
        logger.info(
            "MultiGraphManager.shutdown done: sim_id=%s removed=%d remaining=%d",
            sim_id, len(keys), len(self._updaters),
        )

    # MiroFish-compatible alias
    shutdown_all = shutdown

    # ------------------------------------------------------------------ #
    # diagnostics                                                        #
    # ------------------------------------------------------------------ #

    def list_updaters(self, sim_id: Optional[str] = None) -> list:
        return [
            (s, g) for (s, g) in self._updaters.keys()
            if sim_id is None or s == str(sim_id)
        ]

    def get_all_stats(self) -> Dict[str, Any]:
        return {
            f"{s}|{g}": u.get_stats()
            for (s, g), u in self._updaters.items()
        }


__all__ = ["MultiGraphManager"]
