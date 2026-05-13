"""Effect 子类目录。

继承 ``agent_world.script._registrars.EffectBase`` 即自动注册。启动期
ScriptEngine 调 ``conscribe.discover("agent_world.script.effects")`` 触发
import；为方便手动 import 与单元测试，本 ``__init__`` 也直接 re-export
七个具体子类。
"""

from agent_world.script.effects.move import MoveEffect
from agent_world.script.effects.relation_change import RelationChangeEffect
from agent_world.script.effects.capability_change import CapabilityChangeEffect
from agent_world.script.effects.broadcast_event import BroadcastEventEffect
from agent_world.script.effects.dialogue_injection import DialogueInjectionEffect
from agent_world.script.effects.place_mutation import PlaceMutationEffect
from agent_world.script.effects.state_change import StateChangeEffect

__all__ = [
    "MoveEffect",
    "RelationChangeEffect",
    "CapabilityChangeEffect",
    "BroadcastEventEffect",
    "DialogueInjectionEffect",
    "PlaceMutationEffect",
    "StateChangeEffect",
]
