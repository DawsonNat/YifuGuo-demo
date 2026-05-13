"""Agent World ``config/`` 子系统：仿真启动期的 Pydantic v2 schema 层。

模块组成（L2）：

- :mod:`agent_world.config.world_config` — ``world_config`` 节的 schema
  （``places`` / ``coverage`` / ``ChannelConfig`` / ``MemoryConfig``）。``events``
  保持 ``list[dict]``，由 ScriptEngine 通过 conscribe 自动生成的 Discriminated
  Union 在加载期再做二次校验（LAYOUT §10.2 + §10.3）。
- :mod:`agent_world.config.simulation_config_ext` — 顶层
  :class:`SimulationConfig`，在 MiroFish 原 dataclass 形状上加 ``world_config /
  channel_config / memory_config`` 三个新 key；其它 MiroFish 字段（``simulation_id
  / agent_configs / event_config / ...``）以 ``Optional[Any]`` 形式占位，留待
  app_services 真正接入 MiroFish 时再细化。

参考: LAYOUT §7.1 / §10；docs/impl/config_layer.md。
"""

from agent_world.config.simulation_config_ext import (
    SimulationConfig,
    load_from_yaml,
)
from agent_world.config.world_config import (
    ChannelConfig,
    ChannelDelays,
    CoverageEntry,
    MemoryCompressorConfig,
    MemoryConfig,
    PlaceConfig,
    RetryPolicyConfig,
    WorldConfig,
)

__all__ = [
    "ChannelConfig",
    "ChannelDelays",
    "CoverageEntry",
    "MemoryCompressorConfig",
    "MemoryConfig",
    "PlaceConfig",
    "RetryPolicyConfig",
    "SimulationConfig",
    "WorldConfig",
    "load_from_yaml",
]
