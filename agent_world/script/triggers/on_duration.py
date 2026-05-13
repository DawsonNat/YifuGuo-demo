"""``OnDurationTrigger`` —— 在 ``[start_t, start_t + duration)`` 区间内触发。

纯函数式判定，不持有状态；ScriptEngine 触发一次后即将事件 GC。
"""

from __future__ import annotations

from typing import Any

from agent_world.script._registrars import TriggerBase


class OnDurationTrigger(TriggerBase):
    """Fire while ``world.t`` is within ``[start_t, start_t + duration)``.

    Args:
        start_t: Window start tick (inclusive).
        duration: Window length in ticks (exclusive end).
    """

    def __init__(self, *, start_t: int, duration: int) -> None:
        self.start_t = int(start_t)
        self.duration = int(duration)

    def fires(self, world: Any, t: int) -> bool:
        return self.start_t <= t < self.start_t + self.duration
