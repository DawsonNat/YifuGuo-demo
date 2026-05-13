"""Script 子系统 conscribe Registrar 中枢。

本模块用 ``conscribe.create_registrar`` 集中创建 script 域两个 Registrar 与
对应 ``Base`` 抽象类，构成 metaclass Path A（"继承即注册"）的入口：

- :class:`EffectRegistrar`  + :class:`EffectBase`
- :class:`TriggerRegistrar` + :class:`TriggerBase`

子类落到 ``agent_world/script/{effects,triggers}/`` 两个目录下；启动期
ScriptEngine（或 runner）调 ``conscribe.discover(...)`` 触发 import → 子类
通过 metaclass 钩子自动写入对应 Registrar。

参考: LAYOUT §10.1 (Path A) / §10.7 (Registrar 一览) / impl/script_registrars.md。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from conscribe import create_registrar


# ---------------------------------------------------------------------------
# Protocols (runtime_checkable)
# ---------------------------------------------------------------------------

@runtime_checkable
class EffectProtocol(Protocol):
    """Effect 协议：异步对 world 施加变更。"""

    async def apply(self, world: Any) -> None: ...


@runtime_checkable
class TriggerProtocol(Protocol):
    """Trigger 协议：判断当前 tick 是否应该触发对应 effect。"""

    def fires(self, world: Any, t: int) -> bool: ...


# ---------------------------------------------------------------------------
# Registrar 单例（模块级；不重复 create_registrar）。
# ---------------------------------------------------------------------------

EffectRegistrar = create_registrar(
    "effect",
    EffectProtocol,
    discriminator_field="type",
    strip_suffixes=["Effect"],
)

TriggerRegistrar = create_registrar(
    "trigger",
    TriggerProtocol,
    discriminator_field="type",
    strip_suffixes=["Trigger"],
)


# ---------------------------------------------------------------------------
# Base 抽象类 —— 所有 script 域子类的入口。
# Base 自身用 ``__abstract__ = True`` 标记，跳过注册（conscribe 约定）。
# ---------------------------------------------------------------------------

class EffectBase(metaclass=EffectRegistrar.Meta):
    """Effect 抽象基类（不进入注册表）。"""

    __abstract__ = True

    async def apply(self, world: Any) -> None:
        """应用 effect。子类必须实现实际副作用。"""
        raise NotImplementedError

    def summary(self) -> str:
        """供 ``script_event_log`` 写入的简短描述；默认列出类型与字段。"""
        cls_name = type(self).__name__
        # 仅取 instance 字段的浅表示，避免泄漏大对象。
        try:
            fields = ",".join(f"{k}={v!r}" for k, v in vars(self).items())
        except TypeError:
            fields = ""
        return f"type={cls_name},fields={fields}"


class TriggerBase(metaclass=TriggerRegistrar.Meta):
    """Trigger 抽象基类（不进入注册表）。"""

    __abstract__ = True

    def fires(self, world: Any, t: int) -> bool:
        """判断当前 tick 是否触发。子类必须实现。"""
        raise NotImplementedError

    def notify(self, *args: Any, **kwargs: Any) -> None:
        """OnAction 类 trigger 由 dispatcher 推送事件时调用；默认 no-op。"""
        return None


__all__ = [
    "EffectProtocol",
    "TriggerProtocol",
    "EffectRegistrar",
    "TriggerRegistrar",
    "EffectBase",
    "TriggerBase",
]
