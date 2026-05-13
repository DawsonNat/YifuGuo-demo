"""CapabilityType 子类目录。

继承 ``agent_world.world._registrars.CapabilityBase`` 即自动注册。启动期
runner 调 ``conscribe.discover("agent_world.world.capability_types")``
触发 import。

MVP 内置子类将在 L1 ``world_capability_table`` 阶段补齐；本目录目前为
空占位。
"""
