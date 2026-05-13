"""顶层 :class:`SimulationConfig` —— MiroFish dataclass 形状的 Pydantic 扩展。

ADAPT 自 ``ref/MiroFish/backend/app/services/simulation_config_generator.py``
的 ``SimulationParameters``（L150-174），在原顶层基础上注入三个新 key：

- ``world_config`` — places / coverage / events（LAYOUT §7.1）
- ``channel_config`` — F2F / RDC / GRP 通道 delay + B6 / B9 TTL
- ``memory_config`` — 行为级压缩 + retry_policy

MVP 不需要把 MiroFish 全部字段精确复刻：``simulation_id`` 等以
``Optional[Any]`` 占位，等 ``app_services`` 真正接入 MiroFish 配置生成器时再
逐个补类型。

参考: docs/impl/config_layer.md §3.1 / §4.1。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from agent_world.config.world_config import (
    ChannelConfig,
    MemoryConfig,
    WorldConfig,
)


class SimulationConfig(BaseModel):
    """完整仿真配置根模型。

    新增的三个 key（``world_config / channel_config / memory_config``）走
    严格 Pydantic 模型；MiroFish 兼容字段先用 ``Optional[Any]`` 占位。
    """

    # extra="allow" —— MiroFish 字段还在持续演化，避免 MVP 阶段反复改 schema。
    model_config = ConfigDict(extra="allow")

    # --- MiroFish 兼容字段（占位）-------------------------------------------------
    simulation_id: Optional[Any] = None
    project_id: Optional[Any] = None
    graph_id: Optional[Any] = None
    world_graphs: Optional[Any] = None
    time_config: Optional[Any] = None
    agent_configs: Optional[Any] = None
    event_config: Optional[Any] = None
    twitter_config: Optional[Any] = None
    reddit_config: Optional[Any] = None

    # --- Agent World 新增三 key ------------------------------------------------
    world_config: WorldConfig = Field(default_factory=WorldConfig)
    channel_config: ChannelConfig = Field(default_factory=ChannelConfig)
    memory_config: MemoryConfig = Field(default_factory=MemoryConfig)


def load_from_yaml(path: str | Path) -> SimulationConfig:
    """从 YAML 文件加载并 Pydantic 校验。

    Args:
        path: ``simulation_config.yaml`` / ``.json`` 文件路径（``yaml.safe_load``
            兼容 JSON 子集）。

    Returns:
        校验后的 :class:`SimulationConfig` 实例。

    Raises:
        FileNotFoundError: 路径不存在。
        pydantic.ValidationError: 顶层字段缺失 / 类型错。
    """

    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return SimulationConfig.model_validate(raw)


__all__ = ["SimulationConfig", "load_from_yaml"]
