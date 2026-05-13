# world/_registrars.py 实现文档

> 路径: `agent_world/world/_registrars.py`
> 对应 LAYOUT §: §10.1 注册模式 / §10.7 5 个 Registrar 一览（其中 3 个 world 域：relation_type / capability_type / place_type）/ §9.1 conscribe 作用域
> 上游依赖文档: 无（只依赖 conscribe 1.1.1 库本身）
> 下游依赖文档: `world_relation_graph.md`、`world_capability_table.md`、`world_place_store.md`（这三份分别消费各自的 Registrar 与 Base 类）

## 1. 模块定位

`agent_world/world/_registrars.py` 用 conscribe `create_registrar()` 集中创建 world 域三个 Registrar 与对应的 `Base` 类，作为 metaclass Path A（继承即注册）的入口：

- `RelationTypeRegistrar` + `RelationBase`
- `CapabilityTypeRegistrar` + `CapabilityBase`
- `PlaceTypeRegistrar` + `PlaceBase`

为什么必须存在：conscribe 5 个扩展层（C3 决议）中有 3 个属于 world 域，把它们的 Registrar 与 Base 类放同一文件可以(a) 避免循环 import；(b) 让"world 域共享一个发现入口"——启动期 runner 调三次 `conscribe.discover("agent_world.world.relation_types" / "capability_types" / "place_types")` 完成所有子类注册；(c) 让 RelationGraph / CapabilityTable / PlaceStore 三个状态模块**只 import Base 类**而非 Registrar 自身（Registrar 仅在启动初始化与 schema 生成时用），降低耦合。

输入：conscribe 库 + Protocol 声明 + 文件常量（discriminator / strip_suffixes）。
输出：3 个 Registrar 单例 + 3 个 Base 抽象类。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| Registrar / Meta 用法 | conscribe 1.1.1 | `/Users/qly/QLY/code/conscribe`（LAYOUT §10） | KEEP（API 用法） | `create_registrar(...)` 返回 Registrar 对象，`Registrar.Meta` 是 metaclass |
| metaclass 继承注册（Path A） | LAYOUT | §10.1 + script/_registrars.py 的对称结构 | NEW | script 域有 EffectRegistrar / TriggerRegistrar 同模式（见 `agent_world/script/_registrars.py`），world 域照抄 |
| Pydantic v2 兼容性 | conscribe 1.1.1 | LAYOUT §10.4 | KEEP | conscribe 自动处理 BaseModel 子类的 metaclass 合并；本项目 3 个 Base 类不继承 BaseModel，不会触发任何冲突 |

## 3. 关键改动 (相对来源仓库)

无来源，全新写。设计与 `agent_world/script/_registrars.py` 对称：

- 每个 Registrar 用各自的 `discriminator_field="type"`（YAML / JSON 配置时用 `type` 字段路由到子类）。
- `strip_suffixes` 各自不同：
  - relation_type: `["Relation"]` → `LoverRelation` 解析为 key `"lover"`
  - capability_type: `["Capability"]` → `AccountTwitterCapability` 解析为 `"account_twitter"`
  - place_type: `["Place"]` → `BarPlace` 解析为 `"bar"`
- 每个 Base 类标记 `__abstract__ = True`（conscribe 约定，标记基类自身不参与注册）。
- 每个 Base 类只声明**最小协议**（meta 字段 + 可选 hook 签名），不实现任何业务逻辑——业务逻辑在子类与状态模块（RelationGraph / CapabilityTable / PlaceStore）之间分摊。

## 4. 核心逻辑

### 4.1 数据结构

```
# 每个 Registrar 的输入 Protocol（runtime_checkable）
@runtime_checkable
class RelationTypeProtocol(Protocol):
    symmetric: bool
    is_contact: bool
    project_to_pool: bool
    mutually_exclusive: tuple[str, ...]
    display_template: str
    # on_create / on_break 可选

@runtime_checkable
class CapabilityTypeProtocol(Protocol):
    display_name: str
    requires_place_type: str | None
    auto_revoke_on_leave: bool
    # on_grant / on_revoke 可选

@runtime_checkable
class PlaceTypeProtocol(Protocol):
    # 当前 MVP 仅约定钩子；属性默认值不通过 Protocol 强约束
    async def on_enter(self, agent_id: int, place, world) -> None: ...
    async def on_leave(self, agent_id: int, place, world) -> None: ...

# 模块顶层常量（只在 _registrars.py 出现一次）
RelationTypeRegistrar   = create_registrar("relation_type",   RelationTypeProtocol,   discriminator_field="type", strip_suffixes=["Relation"])
CapabilityTypeRegistrar = create_registrar("capability_type", CapabilityTypeProtocol, discriminator_field="type", strip_suffixes=["Capability"])
PlaceTypeRegistrar      = create_registrar("place_type",      PlaceTypeProtocol,      discriminator_field="type", strip_suffixes=["Place"])

class RelationBase(metaclass=RelationTypeRegistrar.Meta):
    __abstract__ = True
    symmetric: bool = False
    is_contact: bool = True
    project_to_pool: bool = False
    mutually_exclusive: tuple[str, ...] = ()
    display_template: str = "{src} → {dst}"
    async def on_create(self, edge, world, t: int) -> None: ...
    async def on_break (self, edge, world, t: int) -> None: ...

class CapabilityBase(metaclass=CapabilityTypeRegistrar.Meta):
    __abstract__ = True
    display_name: str = ""
    requires_place_type: str | None = None
    auto_revoke_on_leave: bool = False
    async def on_grant (self, agent: int, world, t: int) -> None: ...
    async def on_revoke(self, agent: int, world, t: int) -> None: ...

class PlaceBase(metaclass=PlaceTypeRegistrar.Meta):
    __abstract__ = True
    default_attrs: dict = {}                  # 子类可声明类型默认 attrs；实例 attrs 覆盖（参见 world_place_store.md §7）
    async def on_enter(self, agent_id: int, place, world) -> None: ...
    async def on_leave(self, agent_id: int, place, world) -> None: ...
```

不变量：
1. 每个 Registrar 单例化（模块级变量）；不重复 `create_registrar(...)`。
2. `__abstract__ = True` 标记的 Base 类不进入注册表（conscribe 约定）。
3. 子类 `class XxxRelation(RelationBase): ...` 必须提供合法 CamelCase 名称（含 strip_suffixes 后非空）；否则 conscribe 启动期 fail-fast。
4. 同一 strip 后 key 重复（如两个类都解析为 `"lover"`）→ conscribe 抛错。
5. 子类**不**继承 camel.ChatAgent / Pydantic BaseModel（LAYOUT §10.4 双保险），避免 metaclass 合并潜在风险。

### 4.2 关键流程 / 算法

**模块初始化（import 期）**：
```
import conscribe
RelationTypeRegistrar   = create_registrar(...)    # 建 Registrar 实例 + 生成 Meta metaclass
CapabilityTypeRegistrar = create_registrar(...)
PlaceTypeRegistrar      = create_registrar(...)
class RelationBase(metaclass=...):  __abstract__ = True   # Meta 看到 __abstract__ 跳过注册
class CapabilityBase(metaclass=...): __abstract__ = True
class PlaceBase(metaclass=...):     __abstract__ = True
```

**子类定义触发自动注册（用户代码 import 期）**：
```
# agent_world/world/relation_types/lover.py
from agent_world.world._registrars import RelationBase

class LoverRelation(RelationBase):
    symmetric = True
    is_contact = True
    project_to_pool = False
    mutually_exclusive = ("ex_lover",)
    display_template = "{src} is in love with {dst}"

# class 体执行完后，metaclass __init__ 自动：
#   1. CamelCase → snake_case via strip_suffixes(["Relation"])  → "lover"
#   2. 校验 Protocol 字段
#   3. RelationTypeRegistrar.register("lover", LoverRelation)
```

**runner 启动序列调用**：
```
# agent_world/runner/run_agent_world_simulation.py 早段
import conscribe
conscribe.discover("agent_world.world.relation_types")
conscribe.discover("agent_world.world.capability_types")
conscribe.discover("agent_world.world.place_types")
# 用户扩展包追加（来自 simulation_config.json.world_config.*_packages）
for pkg in cfg.world_config.relation_type_packages:    conscribe.discover(pkg)
for pkg in cfg.world_config.capability_type_packages:  conscribe.discover(pkg)
for pkg in cfg.world_config.place_type_packages:       conscribe.discover(pkg)
# 此时 3 个 Registrar 已填满；RelationGraph / CapabilityTable / PlaceStore 在 load_all 时即可解析
```

**YAML schema 生成（MVP 本地手跑，不接 CI；§10.5）**：
```
$ conscribe generate-stubs --layer relation_type --output-dir agent_world/config/stubs/
$ conscribe generate-stubs --layer capability_type --output-dir agent_world/config/stubs/
$ conscribe generate-stubs --layer place_type --output-dir agent_world/config/stubs/
```

### 4.3 与其他模块的交互

- 上游调用方:
  - 启动期: `runner/run_agent_world_simulation.py` 调 `conscribe.discover(...)` 触发子类注册。
  - 配置加载: conscribe 自动生成的 Pydantic config 类（loader 用，路由 type 字段到子类）。
- 下游被调方:
  - `agent_world/world/relation_graph.py` import `RelationBase` + `RelationTypeRegistrar`（前者供子类继承，后者供 RelationGraph.load_all 拉 type_meta）。
  - `agent_world/world/capability_table.py` 同理。
  - `agent_world/world/place_store.py` 同理。
  - `agent_world/world/relation_types/*.py`、`capability_types/*.py`、`place_types/*.py` 三个子目录下所有模块仅 import 各自 Base 类。
- 共享状态: 无 DB / Zep；纯进程内 Registrar 单例。注：每个 runner 子进程独立；多子进程不共享 Registrar 内存（与 LAYOUT §9.6 G 单写者 Lock 同思路：每子进程一份）。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
# agent_world/world/_registrars.py 顶层导出

RelationTypeRegistrar:   "Registrar"      # conscribe 1.1.1 Registrar 实例
CapabilityTypeRegistrar: "Registrar"
PlaceTypeRegistrar:      "Registrar"

class RelationBase(metaclass=RelationTypeRegistrar.Meta):
    __abstract__: bool = True
    symmetric: bool = False
    is_contact: bool = True
    project_to_pool: bool = False
    mutually_exclusive: tuple[str, ...] = ()
    display_template: str = "{src} → {dst}"
    async def on_create(self, edge, world, t: int) -> None: ...
    async def on_break (self, edge, world, t: int) -> None: ...

class CapabilityBase(metaclass=CapabilityTypeRegistrar.Meta):
    __abstract__: bool = True
    display_name: str = ""
    requires_place_type: str | None = None
    auto_revoke_on_leave: bool = False
    async def on_grant (self, agent: int, world, t: int) -> None: ...
    async def on_revoke(self, agent: int, world, t: int) -> None: ...

class PlaceBase(metaclass=PlaceTypeRegistrar.Meta):
    __abstract__: bool = True
    default_attrs: dict = {}
    async def on_enter(self, agent_id: int, place, world) -> None: ...
    async def on_leave(self, agent_id: int, place, world) -> None: ...
```

Registrar 对外常用方法（由 conscribe 提供，本模块只 re-export）：
```python
RelationTypeRegistrar.resolve(key: str) -> type           # CamelCase 子类
RelationTypeRegistrar.iter_classes() -> Iterable[type]
RelationTypeRegistrar.has(key: str) -> bool
RelationTypeRegistrar.config_model() -> type[BaseModel]   # Pydantic 配置类（loader 用）
```

### 5.2 IPC / Flask / SQL (如适用)

无。本模块不直接产生 IPC / Flask / SQL；对 SQL schema 的间接影响：
- `relation` 表 `relation_type` 字段值约束 = `RelationTypeRegistrar` 注册的 key 集合（启动期校验，DB 端不加 CHECK）。
- `capability` 表 `capability` 字段值约束类似（MVP 兼容裸字符串，不严格）。
- `place` 表新增隐式字段约束 `place_type IN PlaceTypeRegistrar` （启动期校验）。

## 6. 配置入口

从 `simulation_config.json.world_config` 读：
- `relation_type_packages: list[str] = []` 追加 discover 包，扩展用户自定义关系类型。
- `capability_type_packages: list[str] = []`
- `place_type_packages: list[str] = []`

默认值都为空——MVP 内置 8 种 relation + 数种 capability + 几种 place 已够用。验证规则：
- 包路径必须 import 成功（fail-fast）。
- discover 后的 key 与已注册重复 → conscribe 抛错（fail-fast）。

## 7. 待决策 / 风险

- LAYOUT §9.6 D 已决（C3）：conscribe 1.1.1 原生处理 Pydantic v2 + ChatAgent metaclass 兼容；本模块 3 个 Base 类不继承 BaseModel / ChatAgent，安全。
- LAYOUT §9.1 + §10.5 stub CI：MVP 不接 CI；本地手跑 `conscribe generate-stubs` 维护 `.pyi`；后期 D 类讨论是否上 pre-commit。
- 子类反向依赖：理论上子类的 hook（如 `LoverRelation.on_create`）可能想调 `world.places / world.capabilities`——但子类被实例化时 world 已就绪（启动期 discover 在 WorldState 构造前完成，但 hook 调用发生在运行期），不存在循环。
- 类型扩展 hot reload：当前 discover 只在 runner 启动调一次；运行期新增 relation_type 子类**不**支持（与剧本 RELOAD_SCRIPTS C2 不同——C2 仅追加 event 实例不追加新 class）。MVP 接受；后期需求驱动再补。
- 命名空间冲突：用户扩展包内的子类若与 MVP 内置类同名（如自己也写 `LoverRelation`）→ conscribe 抛错；建议用户用前缀（如 `MarsLoverRelation`）规避。
