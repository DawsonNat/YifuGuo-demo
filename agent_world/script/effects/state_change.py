"""``StateChangeEffect`` —— 写 ``world.agents[agent_id].current_state``。

LAYOUT B5 v0.3 新增；与 agent 自己发出的 ``UPDATE_STATE`` action 写
同一字段，不区分来源（schema 不分 origin）。
"""

from __future__ import annotations

import logging
from typing import Any

from agent_world.script._registrars import EffectBase

log = logging.getLogger(__name__)


class StateChangeEffect(EffectBase):
    """Set an agent's ``current_state`` (LAYOUT B5).

    Args:
        agent_id: Target agent ID.
        new_state: Replacement string for the system-prompt
            ``Current State`` segment. Same field UPDATE_STATE writes.
    """

    def __init__(self, *, agent_id: int, new_state: str) -> None:
        self.agent_id = int(agent_id)
        self.new_state = str(new_state)

    async def apply(self, world: Any) -> None:
        # Preferred path: WorldState.set_current_state — auto-creates an
        # AgentRuntime entry when the agent hasn't been spawned yet, matching
        # UPDATE_STATE action semantics (LAYOUT B5).
        setter = getattr(world, "set_current_state", None)
        if callable(setter):
            setter(self.agent_id, self.new_state)
            return

        # Fallback: direct write into world.agents (legacy path).
        agents = getattr(world, "agents", None)
        if agents is None:
            raise RuntimeError("StateChangeEffect requires world.agents")
        agent = agents.get(self.agent_id) if hasattr(agents, "get") else None
        if agent is None:
            log.warning("StateChangeEffect: agent %s not found", self.agent_id)
            return
        setattr(agent, "current_state", self.new_state)
