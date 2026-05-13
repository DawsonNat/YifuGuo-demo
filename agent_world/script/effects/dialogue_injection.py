"""``DialogueInjectionEffect`` —— 替指定 agent 注入一条对话记忆。

复用 OASIS ``social_agent/agent.py:286`` 的 ``update_memory`` 接口；
本 MVP 版按用户规范只接受 ``(agent_id, text)``，role 默认走 SYSTEM。
"""

from __future__ import annotations

import logging
from typing import Any

from agent_world.script._registrars import EffectBase

log = logging.getLogger(__name__)


class DialogueInjectionEffect(EffectBase):
    """Inject one dialogue line into an agent's chat memory.

    Args:
        agent_id: Recipient agent ID. Must be present in
            ``world.agents``.
        text: Dialogue body. Surfaces as a SYSTEM-role message in the
            agent's ChatMemory.
    """

    def __init__(self, *, agent_id: int, text: str) -> None:
        self.agent_id = int(agent_id)
        self.text = str(text)

    async def apply(self, world: Any) -> None:
        agents = getattr(world, "agents", None)
        if agents is None:
            raise RuntimeError("DialogueInjectionEffect requires world.agents")

        agent = agents.get(self.agent_id) if hasattr(agents, "get") else None
        if agent is None:
            log.warning("DialogueInjectionEffect: agent %s not found", self.agent_id)
            return

        update_memory = getattr(agent, "update_memory", None)
        if update_memory is None:
            raise RuntimeError(f"agent {self.agent_id} has no update_memory()")

        # OASIS 的 update_memory 接受 BaseMessage + role（见 agent.py:286）。
        # MVP 阶段尝试构造 BaseMessage；如 camel 不可用，再退回最朴素的
        # ``(content=..., role=...)`` 调用。
        message = self._make_message()
        try:
            if message is not None:
                result = update_memory(message=message, role=self._system_role())
            else:
                result = update_memory(content=self.text, role="system")
        except TypeError:
            # 兼容自定义 mock：直接传文本。
            result = update_memory(self.text)

        if hasattr(result, "__await__"):
            await result

    # -- helpers -----------------------------------------------------------

    def _make_message(self) -> Any:
        try:
            from camel.messages import BaseMessage  # type: ignore
            from camel.types import OpenAIBackendRole  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        try:
            return BaseMessage.make_user_message(
                role_name=OpenAIBackendRole.SYSTEM,
                content=self.text,
            )
        except Exception:  # noqa: BLE001
            return None

    def _system_role(self) -> Any:
        try:
            from camel.types import OpenAIBackendRole  # type: ignore

            return OpenAIBackendRole.SYSTEM
        except Exception:  # noqa: BLE001
            return "system"
