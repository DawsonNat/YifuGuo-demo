"""``world_config`` 节 Pydantic v2 schema。

对应 LAYOUT §7.1 + docs/impl/config_layer.md §3.2 / §4.1。

设计要点：

- ``places`` / ``coverage`` 静态结构在这里定义（MVP 用宽松 ``attrs: dict``，
  ``place.type`` discriminator 暂不强制——等 ``PlaceTypeRegistrar`` 起内容后由
  ScriptEngine / runner 二次校验）。
- ``events`` 故意保持 ``list[dict]``：conscribe 在启动期 ``discover`` 后会自动
  组装 ``trigger`` / ``effect`` 的 Discriminated Union，由 ScriptEngine 在
  ``load_from_yaml`` 之外的路径上做严格校验。这避免本模块在 import 期就触发
  conscribe 注册表查询（顺序敏感，见 docs/impl/config_layer.md §7 待决策）。
- ``ChannelConfig`` / ``MemoryConfig`` 全字段带默认值，便于 simulation 配置
  仅写差异即可。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Places + coverage
# ---------------------------------------------------------------------------

class PlaceConfig(BaseModel):
    """单个 place 的配置项（LAYOUT §7.1 places 节）。

    ``attrs`` 中按约定可包含 ``timezone: str``（IANA 时区名，仅叙事）与
    ``behavior_hint: str | None``（B5 第 4 段 prompt）；其它字段不限。
    """

    model_config = ConfigDict(extra="forbid")

    place_id: str
    attrs: dict[str, Any] = Field(default_factory=dict)


class CoverageEntry(BaseModel):
    """coverage 邻接矩阵单条边（LAYOUT §3.2 + §7.1）。"""

    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    latency_ticks: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Channel config（B1.1 + B6 + B9）
# ---------------------------------------------------------------------------

class ChannelDelays(BaseModel):
    """三通道默认 delay（tick 数）。"""

    model_config = ConfigDict(extra="forbid")

    F2F: int = Field(default=0, ge=0)
    RDC: int = Field(default=1, ge=0)
    GRP: int = Field(default=1, ge=0)


class ChannelConfig(BaseModel):
    """``channel_config`` 节（LAYOUT §7.1）。"""

    model_config = ConfigDict(extra="forbid")

    default_delays: ChannelDelays = Field(default_factory=ChannelDelays)
    # group_message: { "redeliver_undelivered": True, ... } —— 字段较少且后续可能
    # 增删（B6 持久队列重投开关），MVP 保留为 dict。
    group_message: dict[str, Any] = Field(
        default_factory=lambda: {"redeliver_undelivered": True}
    )
    failed_attempt_ttl_ticks: int = Field(default=1, ge=0)
    group_event_ttl_ticks: int = Field(default=1, ge=0)


# ---------------------------------------------------------------------------
# Memory config（行为级压缩 + B4 retry policy）
# ---------------------------------------------------------------------------

class MemoryCompressorConfig(BaseModel):
    """``memory_config.compressor`` 子节。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    max_raw_actions: int = Field(default=30, ge=1)
    summary_sentences: str = "1-3"


class RetryPolicyConfig(BaseModel):
    """``memory_config.retry_policy`` 子节（B4）。"""

    model_config = ConfigDict(extra="forbid")

    parse_error_max_retry: int = Field(default=1, ge=0)
    arg_missing_max_retry: int = Field(default=1, ge=0)
    other_max_retry: int = Field(default=0, ge=0)


class MemoryConfig(BaseModel):
    """``memory_config`` 节（LAYOUT §7.1）。"""

    model_config = ConfigDict(extra="forbid")

    compressor: MemoryCompressorConfig = Field(default_factory=MemoryCompressorConfig)
    retry_policy: RetryPolicyConfig = Field(default_factory=RetryPolicyConfig)


# ---------------------------------------------------------------------------
# WorldConfig 顶层装配
# ---------------------------------------------------------------------------

class WorldConfig(BaseModel):
    """``world_config`` 节顶层（LAYOUT §7.1）。

    ``events`` 保持 ``list[dict]``——ScriptEngine 在 conscribe ``discover``
    完成后再用自动生成的 Pydantic Discriminated Union 校验，避免本模块对
    Registrar 启动顺序产生硬依赖。
    """

    model_config = ConfigDict(extra="forbid")

    places: list[PlaceConfig] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("places")
    @classmethod
    def _unique_place_ids(cls, places: list[PlaceConfig]) -> list[PlaceConfig]:
        seen: set[str] = set()
        for p in places:
            if p.place_id in seen:
                raise ValueError(f"duplicate place_id: {p.place_id!r}")
            seen.add(p.place_id)
        return places


__all__ = [
    "ChannelConfig",
    "ChannelDelays",
    "CoverageEntry",
    "MemoryCompressorConfig",
    "MemoryConfig",
    "PlaceConfig",
    "RetryPolicyConfig",
    "WorldConfig",
]
