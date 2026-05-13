"""``ScriptLoader`` —— YAML → Pydantic 校验 → Trigger/Effect 实例。

本模块是 ``script/`` 子系统的 L2 入口；它的工作严格限定在三件事:

1. **discover**: 触发 ``agent_world.script.triggers`` / ``agent_world.script.effects``
   两个目录下所有子类的 import, 让 metaclass Path A 注册到对应 Registrar。
2. **parse + validate**: 用 conscribe 自动生成的 Pydantic discriminated union
   (``EffectRegistrar.config_union_type()`` / ``TriggerRegistrar.config_union_type()``)
   一次性校验 YAML 的 trigger / effect 字段 schema; 同步检查 ``id`` 非空、不重复。
3. **instantiate**: 调用 ``Registrar.get(type)(**kwargs)`` 把 Pydantic config 物化
   成具体 ``TriggerBase`` / ``EffectBase`` 子类实例。

Loader **不连 world**, **不跑 trigger/effect**——这些是 ``ScriptEngine`` (L3) 的活。
LAYOUT C2: event_id 由用户在 YAML 写明; 重复 ID 在单文件内立刻抛错, 跨 reload
比对则交给 :meth:`ScriptLoader.diff_against` 处理。

参考: LAYOUT §2.E / §10.2 / §10.3 / impl/script_loader.md。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

import yaml
from pydantic import BaseModel, ValidationError

from agent_world.script._registrars import (
    EffectBase,
    EffectRegistrar,
    TriggerBase,
    TriggerRegistrar,
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LoadedEvent:
    """单个剧本事件 (用户 YAML 写明的 id + 实例化的 trigger/effect)."""

    id: str
    trigger: TriggerBase
    effect: EffectBase
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadResult:
    """``ScriptLoader.load`` 的返回值; 也是 reload diff 的输入."""

    events: List[LoadedEvent]
    ignored_ids: List[str] = field(default_factory=list)

    def __iter__(self) -> Iterable[Any]:
        # 允许 ``new, ignored = loader.load(path, existing_ids=...)`` 写法.
        yield self.events
        yield self.ignored_ids


# ---------------------------------------------------------------------------
# Module discovery
# ---------------------------------------------------------------------------

_TRIGGER_PKG = "agent_world.script.triggers"
_EFFECT_PKG = "agent_world.script.effects"
_discovered = False


def discover() -> None:
    """Import trigger/effect 子模块, 触发 metaclass 自动注册.

    幂等; 多次调用只 import 一次. 优先用 ``conscribe.discover`` (若可用),
    回退到直接 import 包 ``__init__`` (子类在那里 re-export).
    """

    global _discovered
    if _discovered:
        return

    try:
        from conscribe import discover as _conscribe_discover  # type: ignore
    except ImportError:
        _conscribe_discover = None  # type: ignore

    for pkg in (_TRIGGER_PKG, _EFFECT_PKG):
        if _conscribe_discover is not None:
            try:
                _conscribe_discover(pkg)
                continue
            except Exception:
                # conscribe.discover 在某些版本期望 package 对象而非字符串;
                # 失败时回退手动 import.
                pass
        importlib.import_module(pkg)

    _discovered = True


# ---------------------------------------------------------------------------
# Pydantic schema (lazy-built; depends on registrar contents at call time)
# ---------------------------------------------------------------------------

_scripts_config_model: type[BaseModel] | None = None


def _build_scripts_config_model() -> type[BaseModel]:
    """构造顶层 ``ScriptsConfig`` Pydantic 模型 (lazy + cached).

    必须在 ``discover()`` 之后调用, 否则 union 不含子类.
    """

    global _scripts_config_model
    if _scripts_config_model is not None:
        return _scripts_config_model

    trigger_union = TriggerRegistrar.config_union_type()
    effect_union = EffectRegistrar.config_union_type()

    class EventConfig(BaseModel):
        id: str
        trigger: trigger_union  # type: ignore[valid-type]
        effect: effect_union  # type: ignore[valid-type]

    class ScriptsConfig(BaseModel):
        events: List[EventConfig]

    _scripts_config_model = ScriptsConfig
    return ScriptsConfig


# ---------------------------------------------------------------------------
# ScriptLoader
# ---------------------------------------------------------------------------


class ScriptLoader:
    """Stateless YAML → ``LoadedEvent`` 解析器.

    仅暴露 ``load`` / ``load_dict`` / ``diff_against`` 三个静态方法; 自身不持有
    状态——ScriptEngine 维护 ``loaded_event_ids`` 集合并在 reload 时调
    :meth:`load` 传入该集合做去重.
    """

    @staticmethod
    def load(
        path: str | Path,
        *,
        existing_ids: Sequence[str] | Set[str] | None = None,
    ) -> LoadResult:
        """Parse a YAML file, validate, instantiate, and (optionally) diff vs reload."""

        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return ScriptLoader.load_dict(raw, existing_ids=existing_ids)

    @staticmethod
    def load_dict(
        raw: Dict[str, Any],
        *,
        existing_ids: Sequence[str] | Set[str] | None = None,
    ) -> LoadResult:
        """Same as :meth:`load` but accepts a pre-parsed dict."""

        if not isinstance(raw, dict) or "events" not in raw:
            raise ValueError("script YAML root must be a dict containing 'events'")

        discover()
        ScriptsConfig = _build_scripts_config_model()

        try:
            cfg = ScriptsConfig.model_validate(raw)
        except ValidationError:
            raise  # 直接透传 Pydantic 错误 (含字段路径).

        existing: Set[str] = set(existing_ids or ())
        seen: Set[str] = set()
        new_events: List[LoadedEvent] = []
        ignored: List[str] = []

        for ev in cfg.events:  # type: ignore[attr-defined]
            ev_id = ev.id
            if not ev_id:
                raise ValueError("event id must be a non-empty string")
            if ev_id in seen:
                raise ValueError(f"duplicate event_id within file: {ev_id!r}")
            seen.add(ev_id)
            if ev_id in existing:
                ignored.append(ev_id)
                continue

            trigger_cls = TriggerRegistrar.get(ev.trigger.type)
            effect_cls = EffectRegistrar.get(ev.effect.type)
            trigger_kwargs = ev.trigger.model_dump(exclude={"type"})
            effect_kwargs = ev.effect.model_dump(exclude={"type"})

            new_events.append(
                LoadedEvent(
                    id=ev_id,
                    trigger=trigger_cls(**trigger_kwargs),
                    effect=effect_cls(**effect_kwargs),
                    raw=ev.model_dump(),
                )
            )

        return LoadResult(events=new_events, ignored_ids=ignored)

    @staticmethod
    def diff_against(
        events: Sequence[LoadedEvent],
        existing_ids: Sequence[str] | Set[str],
    ) -> Tuple[List[LoadedEvent], List[str]]:
        """Split ``events`` into (truly new, already-loaded ids).

        Mirrors the ``(new_events, ignored_existing_ids)`` shape requested by
        ScriptEngine reload semantics (LAYOUT C2). Useful when the caller
        already has a parsed list and just wants to dedupe against what's
        currently loaded in memory.
        """

        existing: Set[str] = set(existing_ids)
        new: List[LoadedEvent] = []
        ignored: List[str] = []
        for ev in events:
            if ev.id in existing:
                ignored.append(ev.id)
            else:
                new.append(ev)
        return new, ignored


__all__ = [
    "LoadedEvent",
    "LoadResult",
    "ScriptLoader",
    "discover",
]
