"""Trigger 子类目录。

继承 ``agent_world.script._registrars.TriggerBase`` 即自动注册。启动期
ScriptEngine 调 ``conscribe.discover("agent_world.script.triggers")`` 触发
import；为方便手动 import 与单元测试，本 ``__init__`` 也直接 re-export
四个具体子类。
"""

from agent_world.script.triggers.at_time import AtTimeTrigger
from agent_world.script.triggers.at_condition import AtConditionTrigger
from agent_world.script.triggers.on_action import OnActionTrigger
from agent_world.script.triggers.on_duration import OnDurationTrigger

__all__ = [
    "AtTimeTrigger",
    "AtConditionTrigger",
    "OnActionTrigger",
    "OnDurationTrigger",
]
