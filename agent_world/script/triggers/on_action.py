"""``OnActionTrigger`` —— 当 dispatcher 派发匹配 action 时由 ScriptEngine
推送 ``notify``，下一轮 ``fires`` 命中。

不轮询；纯 push 模式（参考 OASIS ``env_action.py`` 的 ManualAction 注入
思路）。
"""

from __future__ import annotations

from typing import Any, Dict

from agent_world.script._registrars import TriggerBase


class OnActionTrigger(TriggerBase):
    """Fire after a matching action is observed by the dispatcher.

    Args:
        action_type: ``ActionType`` enum name, e.g. ``"SPEAK_TO_LOCAL"``.
        **filters: Optional payload field filters. All listed
            key/value pairs must match the dispatched action's payload.
            Special key ``actor_id`` filters by acting agent id.
    """

    def __init__(self, *, action_type: str, **filters: Any) -> None:
        self.action_type = str(action_type)
        # actor_id 是常用过滤字段；单独抽出方便 dispatcher 快速比对，
        # 同时保留在 filters dict 里以维持统一语义。
        self.actor_id = filters.get("actor_id")
        self.filters: Dict[str, Any] = dict(filters)
        self._pending_fire: bool = False

    def notify(
        self,
        actor_id: Any = None,
        action_type: str | None = None,
        payload: Dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        """Called by ``ScriptEngine.on_action_dispatched``."""

        if action_type is not None and action_type != self.action_type:
            return
        if self.actor_id is not None and actor_id != self.actor_id:
            return

        payload = payload or {}
        for key, expected in self.filters.items():
            if key == "actor_id":
                continue
            if payload.get(key) != expected:
                return

        self._pending_fire = True

    def fires(self, world: Any, t: int) -> bool:
        # ScriptEngine 在 effect.apply 完成后会清掉本实例（事件 GC），
        # 因此读完 flag 后保留在 True 也不会重复触发；仍主动重置以便
        # 单元测试中复用同一实例。
        if self._pending_fire:
            self._pending_fire = False
            return True
        return False
