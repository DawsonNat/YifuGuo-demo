"""``CapabilityChangeEffect`` —— 授予 / 撤销 capability。"""

from __future__ import annotations

from typing import Any, Literal

from agent_world.script._registrars import EffectBase


class CapabilityChangeEffect(EffectBase):
    """Grant or revoke a capability for an agent.

    Args:
        agent_id: Target agent ID.
        capability: Registered capability_type key (e.g. ``"account_twitter"``).
        op: ``"grant"`` or ``"revoke"``.
    """

    def __init__(
        self,
        *,
        agent_id: int,
        capability: str,
        op: Literal["grant", "revoke"],
    ) -> None:
        if op not in ("grant", "revoke"):
            raise ValueError(f"CapabilityChangeEffect.op must be 'grant' or 'revoke', got {op!r}")
        self.agent_id = int(agent_id)
        self.capability = str(capability)
        self.op = op

    async def apply(self, world: Any) -> None:
        caps = getattr(world, "capabilities", None)
        if caps is None:
            raise RuntimeError("CapabilityChangeEffect requires world.capabilities")
        fn = getattr(caps, self.op, None)
        if fn is None:
            raise RuntimeError(f"world.capabilities missing '{self.op}' method")
        result = fn(self.agent_id, self.capability)
        if hasattr(result, "__await__"):
            await result
