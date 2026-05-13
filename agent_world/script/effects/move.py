"""``MoveEffect`` —— 把 agent 移动到目标 place。

按用户 MVP 规范：直接调用 ``world.places.move``；高阶集成
（BehaviorCompressor.on_move、agent_location 落库等）由 dispatcher
路径在后续 P 阶段串起来。
"""

from __future__ import annotations

from typing import Any

from agent_world.script._registrars import EffectBase


class MoveEffect(EffectBase):
    """Move an agent to a new place.

    Args:
        agent_id: Target agent ID.
        place_id: Destination place ID.
    """

    def __init__(self, *, agent_id: int, place_id: str) -> None:
        self.agent_id = int(agent_id)
        self.place_id = str(place_id)

    async def apply(self, world: Any) -> None:
        places = getattr(world, "places", None)
        move_fn = getattr(places, "move", None) if places is not None else None
        if move_fn is None:
            raise RuntimeError("MoveEffect requires world.places.move")
        result = move_fn(self.agent_id, self.place_id)
        # ``move`` 在不同实现中可能是 sync / async；统一适配。
        if hasattr(result, "__await__"):
            await result
