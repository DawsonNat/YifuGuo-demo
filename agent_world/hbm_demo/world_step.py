"""HBM demo WorldStep — parallel LLM decisions within each place."""

from __future__ import annotations

import asyncio
import logging
from typing import List

from agent_world.world.step import WorldStep, _extract_actions

log = logging.getLogger("agent_world.hbm_demo.world_step")


class HbmWorldStep(WorldStep):
    """Run co-located agents' ``perform_action_by_llm`` calls in parallel.

    Place-level groups still run concurrently via the base ``run_one_tick``
    ``asyncio.gather``; this subclass parallelizes the inner agent loop that
    the engine otherwise executes serially in ``_run_place``.
    """

    async def _run_single_agent(self, agent_id: int, t: int) -> None:
        agent = self._resolve_agent(agent_id)
        if agent is None:
            return

        decision = await self._decide(agent, agent_id, t)

        try:
            if hasattr(agent, "last_message_seen_at"):
                agent.last_message_seen_at = int(t)
        except Exception:  # noqa: BLE001
            pass

        if decision is None or isinstance(decision, Exception):
            if isinstance(decision, Exception):
                log.warning("agent %s decide failed: %s", agent_id, decision)
            return

        for atype, akwargs in _extract_actions(decision):
            try:
                await self.dispatcher.dispatch(
                    agent_id, atype, t, **(akwargs or {})
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "dispatch(agent=%s, action=%s) failed: %s",
                    agent_id,
                    atype,
                    exc,
                )

    async def _run_place(
        self, place_id: str, agent_ids: List[int], t: int
    ) -> None:
        if not agent_ids:
            return
        if len(agent_ids) == 1:
            await self._run_single_agent(agent_ids[0], t)
            return

        results = await asyncio.gather(
            *[self._run_single_agent(aid, t) for aid in agent_ids],
            return_exceptions=True,
        )
        for aid, result in zip(agent_ids, results):
            if isinstance(result, Exception):
                log.error(
                    "parallel _run_place(%s) agent=%s raised: %s",
                    place_id,
                    aid,
                    result,
                )
