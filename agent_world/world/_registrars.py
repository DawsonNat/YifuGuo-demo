"""World 域 conscribe Registrar 中枢。

本模块用 ``conscribe.create_registrar`` 集中创建 world 域三个 Registrar 与对应
``Base`` 抽象类，构成 metaclass Path A（"继承即注册"）的入口：

- :class:`RelationTypeRegistrar` + :class:`RelationBase`
- :class:`CapabilityTypeRegistrar` + :class:`CapabilityBase`
- :class:`PlaceTypeRegistrar` + :class:`PlaceBase`

子类落到 ``agent_world/world/{relation_types,capability_types,place_types}/`` 三
个目录下；启动期 runner 调三次 ``conscribe.discover(...)`` 触发 import → 子类
通过 metaclass 钩子自动写入对应 Registrar。

参考: LAYOUT §10.1 (Path A) / §10.7 (Registrar 一览) / impl/world_registrars.md。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from conscribe import create_registrar


# ---------------------------------------------------------------------------
# Protocols (runtime_checkable) — 仅声明最小协议，业务逻辑分摊给状态模块。
# ---------------------------------------------------------------------------

@runtime_checkable
class RelationTypeProtocol(Protocol):
    """RelationType 元数据协议。

    Attributes:
        symmetric: True 表示 A→B 蕴含 B→A（如 friend）。
        is_contact: True 表示该关系参与 RDC（远端可联系）coverage 计算。
        project_to_pool: True 表示该关系会被投影到 pool 层（OASIS follow 等）。
        mutually_exclusive: 与该关系互斥的其他 relation key（启动期校验）。
        display_template: 用于 perception / log 的展示模板。
    """

    symmetric: bool
    is_contact: bool
    project_to_pool: bool
    mutually_exclusive: tuple[str, ...]
    display_template: str


@runtime_checkable
class CapabilityTypeProtocol(Protocol):
    """CapabilityType 元数据协议。

    Attributes:
        display_name: 给 UI / perception 的显示名。
        requires_place_type: 该 capability 是否要求 agent 处于某 place_type；
            None 表示与位置无关。
        auto_revoke_on_leave: 离开关联 place_type 时是否自动撤销。
    """

    display_name: str
    requires_place_type: str | None
    auto_revoke_on_leave: bool


@runtime_checkable
class PlaceTypeProtocol(Protocol):
    """PlaceType 钩子协议（MVP 仅约定 enter/leave 钩子）。"""

    async def on_enter(self, agent_id: int, place: Any, world: Any) -> None: ...
    async def on_leave(self, agent_id: int, place: Any, world: Any) -> None: ...


# ---------------------------------------------------------------------------
# Registrar 单例（模块级；不重复 create_registrar）。
# ---------------------------------------------------------------------------

RelationTypeRegistrar = create_registrar(
    "relation_type",
    RelationTypeProtocol,
    discriminator_field="type",
    strip_suffixes=["Relation"],
)

CapabilityTypeRegistrar = create_registrar(
    "capability_type",
    CapabilityTypeProtocol,
    discriminator_field="type",
    strip_suffixes=["Capability"],
)

PlaceTypeRegistrar = create_registrar(
    "place_type",
    PlaceTypeProtocol,
    discriminator_field="type",
    strip_suffixes=["Place"],
)


# ---------------------------------------------------------------------------
# Base 抽象类 —— 所有 world 域子类的入口。
#
# 任意继承自 Base 的非抽象类，在 import 期通过 metaclass 钩子自动注册。
# Base 自身用 ``__abstract__ = True`` 标记，跳过注册（conscribe 约定）。
# ---------------------------------------------------------------------------

class RelationBase(metaclass=RelationTypeRegistrar.Meta):
    """RelationType 抽象基类（不进入注册表）。"""

    __abstract__ = True

    # 元数据默认值；子类覆盖即可。
    symmetric: bool = False
    is_contact: bool = True
    project_to_pool: bool = False
    mutually_exclusive: tuple[str, ...] = ()
    display_template: str = "{src} -> {dst}"

    async def on_create(self, edge: Any, world: Any, t: int) -> None:
        """关系建立时调用。默认 no-op。"""
        return None

    async def on_break(self, edge: Any, world: Any, t: int) -> None:
        """关系解除时调用。默认 no-op。"""
        return None


class CapabilityBase(metaclass=CapabilityTypeRegistrar.Meta):
    """CapabilityType 抽象基类（不进入注册表）。"""

    __abstract__ = True

    display_name: str = ""
    requires_place_type: str | None = None
    auto_revoke_on_leave: bool = False

    async def on_grant(self, agent: int, world: Any, t: int) -> None:
        """capability 授予时调用。默认 no-op。"""
        return None

    async def on_revoke(self, agent: int, world: Any, t: int) -> None:
        """capability 撤销时调用。默认 no-op。"""
        return None


class PlaceBase(metaclass=PlaceTypeRegistrar.Meta):
    """PlaceType 抽象基类（不进入注册表）。"""

    __abstract__ = True

    # 子类可声明类型默认 attrs；place 实例 attrs 覆盖（参见 world_place_store.md §7）。
    default_attrs: dict[str, Any] = {}

    async def on_enter(self, agent_id: int, place: Any, world: Any) -> None:
        """agent 进入该 place 时调用。默认 no-op。"""
        return None

    async def on_leave(self, agent_id: int, place: Any, world: Any) -> None:
        """agent 离开该 place 时调用。默认 no-op。"""
        return None


__all__ = [
    "RelationTypeProtocol",
    "CapabilityTypeProtocol",
    "PlaceTypeProtocol",
    "RelationTypeRegistrar",
    "CapabilityTypeRegistrar",
    "PlaceTypeRegistrar",
    "RelationBase",
    "CapabilityBase",
    "PlaceBase",
]
