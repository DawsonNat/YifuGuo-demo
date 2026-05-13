# Script Registrars 实现文档

> 路径: `agent_world/script/_registrars.py`
> 对应 LAYOUT §: §10.1 注册模式 (Path A) / §10.7 Registrar 一览 / §2.E 剧本目录
> 上游依赖文档: 无 (最底层)
> 下游依赖文档: `script_triggers.md`, `script_effects.md`, `script_loader.md`, `script_engine.md`

## 1. 模块定位

`_registrars.py` 是剧本子系统的"注册中枢", 干两件事:

1. 用 `conscribe.create_registrar()` 各创建一个 **EffectRegistrar** 与 **TriggerRegistrar**, 充当 effect / trigger 子类的元数据容器与 YAML→实例 路由。
2. 同步定义两个抽象基类 **EffectBase** 与 **TriggerBase**, 通过 `metaclass=Registrar.Meta` 让"继承即注册"(LAYOUT §10.1 Path A); 子类落到 `script/effects/*.py` 与 `script/triggers/*.py` 后, 调 `conscribe.discover()` 即触发 import 完成注册。

输入: 各 effect / trigger 子类的 import (副作用驱动)。
输出: 两个 Registrar 实例 + 两个 Base 类; 生成 Pydantic config schema (Tier 1, LAYOUT §10.3); 启动期 IDE 可选 stub `.pyi`。

为什么必须存在: ScriptEngine 需要"YAML 中的 `type: move` 字段路由到 `MoveEffect` 类" 的能力, 但又不能在 engine 里硬编码所有 effect 类型 (违反 OCP)。conscribe 提供"用户配置型枚举"的统一注册机制, MVP 阶段仅有这两层 (script 子系统), 另外 3 个 (relation_type / capability_type / place_type) 在 `world/_registrars.py` 同套模式但不属于本文档。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `create_registrar` API | conscribe 1.1.1 | `/Users/qly/QLY/code/conscribe` | KEEP | 项目作者维护; 直接 import 用 |
| Path A metaclass 继承模式 | conscribe 1.1.1 docs | LAYOUT §10.1 示例 | KEEP | 显式选 Path A, 不用装饰器路径 |
| Pydantic v2 兼容性 | conscribe 1.1.1 内置 | — | KEEP | `model_fields` 快速路径, LAYOUT §10.4 |
| 5 注册层架构对齐 | LAYOUT §10.7 | — | NEW | script 模块只用其中 2 个 (effect/trigger) |

## 3. 关键改动 (相对来源仓库)

- 全新写。`_registrars.py` 仅做"create + 定义 Base", 不写 effect/trigger 子类。
- 显式选 **metaclass 继承 (Path A)** 而非装饰器路径 (LAYOUT v0.2 C 类决议)。理由: 子类天生就是基类, `class XxxEffect(EffectBase)` 比 `@register class Xxx` 语义更直观, 也与 §10.7 五张表统一。
- `discriminator_field="type"` 与 `strip_suffixes=["Effect"]` / `["Trigger"]` 由 §10.7 锁死。
- 不在本文件写 `discover()` 调用; 由 `script/__init__.py` 或 ScriptEngine 启动期统一调。

## 4. 核心逻辑

### 4.1 数据结构

```
EffectRegistrar  : conscribe.Registrar[EffectProtocol]
TriggerRegistrar : conscribe.Registrar[TriggerProtocol]

EffectBase       : abstract class, metaclass=EffectRegistrar.Meta, __abstract__=True
TriggerBase      : abstract class, metaclass=TriggerRegistrar.Meta, __abstract__=True

EffectProtocol   : Protocol with `async def apply(self, world) -> None`
TriggerProtocol  : Protocol with `def fires(self, world, t) -> bool`
```

不变量:
- 任何继承 `EffectBase` 的非抽象类, 在 import 时自动注册到 EffectRegistrar; 类名经 `strip_suffixes=["Effect"]` + CamelCase→snake_case 转 key (`MoveEffect` → `"move"`, `StateChangeEffect` → `"state_change"`)。
- 同 key 重复注册抛错 (conscribe 内置)。
- 抽象类 (`EffectBase` 自身) 通过 `__abstract__ = True` 标记, 不参与注册。

### 4.2 关键流程 / 算法

**模块加载顺序**:
```
1. _registrars.py 被 import
   → create_registrar("effect", EffectProtocol, ...)  # EffectRegistrar 实例化
   → create_registrar("trigger", TriggerProtocol, ...)# TriggerRegistrar 实例化
   → class EffectBase(metaclass=EffectRegistrar.Meta)  # Base 定义
   → class TriggerBase(metaclass=TriggerRegistrar.Meta)
2. 启动期某处 (ScriptEngine.__init__ 或 script/__init__.py) 调:
   conscribe.discover("agent_world.script.effects")
   conscribe.discover("agent_world.script.triggers")
   → 触发 effects/*.py 与 triggers/*.py 全部 import
   → 每个子类创建瞬间 metaclass 钩子触发, 加进 Registrar.entries
3. ScriptLoader.load(yaml_path):
   → 读 YAML
   → conscribe 生成的 Pydantic config 类按 type discriminator 自动路由到正确子类
   → 实例化 → 返回 (trigger_instance, effect_instance) 对
```

**YAML → 实例 路由 (示意, 真实由 conscribe 完成)**:
```
yaml: { type: "move", agent_id: 5, place_id: "moon_base" }
→ EffectRegistrar.lookup("move") = MoveEffect
→ MoveEffect(agent_id=5, place_id="moon_base")
```

### 4.3 与其他模块的交互

- **上游调用方** (依赖本文件):
  - `agent_world.script.effects.*` 各 .py 文件 import `EffectBase`
  - `agent_world.script.triggers.*` 各 .py 文件 import `TriggerBase`
  - `script/loader.py` import 两个 Registrar 用于生成 Pydantic config schema
  - `script/engine.py` 启动期调 `conscribe.discover()` 触发注册
- **下游被调方**:
  - `conscribe.create_registrar` (conscribe 1.1.1)
  - `typing.Protocol` / `typing.runtime_checkable`
- **共享状态**: 无 DB / Zep 写入; 仅在进程内存维护 Registrar.entries 字典。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from typing import Any, Protocol, runtime_checkable
from conscribe import create_registrar

@runtime_checkable
class EffectProtocol(Protocol):
    async def apply(self, world: "WorldState") -> None: ...

@runtime_checkable
class TriggerProtocol(Protocol):
    def fires(self, world: "WorldState", t: int) -> bool: ...

EffectRegistrar = create_registrar(
    name="effect",
    protocol=EffectProtocol,
    discriminator_field="type",
    strip_suffixes=["Effect"],
)

TriggerRegistrar = create_registrar(
    name="trigger",
    protocol=TriggerProtocol,
    discriminator_field="type",
    strip_suffixes=["Trigger"],
)

class EffectBase(metaclass=EffectRegistrar.Meta):
    __abstract__ = True
    async def apply(self, world: "WorldState") -> None: ...
    def summary(self) -> str:
        """short text for script_event_log; default: type=...,fields=..."""
        ...

class TriggerBase(metaclass=TriggerRegistrar.Meta):
    __abstract__ = True
    def fires(self, world: "WorldState", t: int) -> bool: ...
    def notify(self, *args: Any, **kwargs: Any) -> None:
        """OnAction-class triggers override; default: no-op"""
        ...
```

### 5.2 IPC / Flask / SQL

无。本模块只是注册中枢, 不直接产出 IPC/Flask/SQL。

## 6. 配置入口

无独立配置。两个 Registrar 的元配置 (`discriminator_field` / `strip_suffixes`) 在源码中硬编码, 与 LAYOUT §10.7 表对齐。

`simulation_config.json` 中的 `world_config.events.*` 子树 schema 由 conscribe Tier 1 (LAYOUT §10.3: `__init__` 签名 + Google docstring `Args:` 段) 自动生成。MVP 不接 stub CI (LAYOUT §10.5)。

## 7. 待决策 / 风险

- **conscribe stub CI 时机** (LAYOUT §9.5 #B / §9.6.B): MVP 不接 pre-commit / CI 校验漂移。如出现"YAML 字段与子类签名不一致" 误用, 后期 D 类讨论。
- **跨注册表 wiring 暂未用** (LAYOUT §10.6): script 子系统的 effect / trigger 不依赖其他注册层 (relation_type/capability_type/place_type), 当前不设 `__wiring__`。如 RelationChangeEffect 想强约束 `type` 字段必须命中 RelationTypeRegistrar, 后期再加。
- **本模块自暴露的风险**:
  - **import 顺序**: 子类必须在 `discover()` 之前未被其他模块 import (否则双重注册抛错)。约定: 仅 `conscribe.discover()` 路径 import effects/triggers, 其他代码统一通过 Registrar.lookup 拿子类。
  - **抽象类标记**: 任何"中间抽象层" (如未来加 `RelationEffectBase(EffectBase)` 作为 RelationChange/RelationBreak 共同父类) 必须显式 `__abstract__ = True`, 否则会被 metaclass 当作具体子类注册。
