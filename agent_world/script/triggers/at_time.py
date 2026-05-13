"""``AtTimeTrigger`` —— 在指定 tick 触发一次。

继承 :class:`agent_world.script._registrars.TriggerBase`，由 metaclass
自动注册到 ``TriggerRegistrar``（discriminator ``type=at_time``）。
"""

from __future__ import annotations

from typing import Any

from agent_world.script._registrars import TriggerBase


class AtTimeTrigger(TriggerBase):
    """Fire when ``world.t`` reaches the configured tick.

    Args:
        t: Target tick value (inclusive). Returns ``True`` once
            ``world.t == t``; ScriptEngine drops the event after
            firing so subsequent ticks no longer match.
    """

    def __init__(self, *, t: int) -> None:
        self.t = int(t)

    def fires(self, world: Any, t: int) -> bool:
        # Strict equality — ScriptEngine GC'd already-applied events, but a
        # past-due re-load during reload() should NOT re-fire on every
        # subsequent tick (LAYOUT C2 past-due ignored+warn).
        return int(t) == self.t
