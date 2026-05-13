# ScriptLoader 实现文档

> 路径: `agent_world/script/loader.py`
> 对应 LAYOUT §: §2.E loader.py / §10.2 (YAML→实例) / §10.3 (Tier 1 schema) / §6.1 (热加载入口)
> 上游依赖文档: `script_registrars.md`, `script_triggers.md`, `script_effects.md`
> 下游依赖文档: `script_engine.md` (engine 调 load 接口)

## 1. 模块定位

`ScriptLoader` 是剧本子系统的"YAML 入口", 干两件事:

1. **解析** `world_config.events` 子树 (来自 `simulation_config.json` 或独立 `scripts.yaml`) 为强类型 Pydantic 配置对象 + 实例化 Trigger / Effect 子类。
2. **校验**: 用 conscribe Tier 1 自动生成的 Pydantic schema (LAYOUT §10.3) 一次性检查 (a) `type` 字段是否命中已注册子类; (b) 各子类字段类型是否合法; (c) `event_id` 全局唯一; (d) trigger / effect 是否同步出现。

输入: YAML/JSON 文件路径 (或已解析 dict)。
输出: `LoadedScripts` 对象, 含一组 `EventBundle = (id, trigger, effect, raw_yaml)`; 供 ScriptEngine 在启动期与 RELOAD_SCRIPTS IPC 期消费。

为什么必须存在: ScriptEngine 需要一个"可重入" 的纯函数式加载器——每次 RELOAD_SCRIPTS IPC 都会调一次 `load(path)`, 解析结果再交给 ScriptEngine 做去重 / 增量追加 (script_engine.md §4.2)。把"解析 + 校验" 与"内存态去重 + 应用" 分开, 便于测试与热加载。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `event_config.initial_posts` 微缩剧本 | MiroFish | `run_parallel_simulation.py:1180-1207` | PATTERN | 借其"YAML 描述少量事件" 的 schema 风格, 扩展为完整 trigger/effect 树 |
| Pydantic config 生成 | conscribe 1.1.1 | LAYOUT §10.2 / §10.3 | KEEP | 让 conscribe 帮生成 Pydantic schema, 不手写 |
| simulation_config_generator 风格 | MiroFish | `simulation_config_generator.py:150-174` | PATTERN | dataclass 顶层结构作为 `world_config.events` schema 参考 |
| 用户写明 event_id | LAYOUT §2.E (C2 决议) | — | NEW | 不让系统 hash 生成, 用户 YAML 写明 |

## 3. 关键改动 (相对来源仓库)

- 全新写。设计灵感来自 MiroFish `event_config.initial_posts` 的 YAML 微缩形态, 但扩展为完整 trigger / effect 树。
- **event_id 强制由用户在 YAML 写明** (LAYOUT C2): 加载期对每条 event 校验 `id` 字段非空且全局唯一; 不允许 system 生成 hash。理由: RELOAD_SCRIPTS 时需用 id 比对去重, 系统生成的 hash 在 YAML 微调后会变, 导致"看似同一事件" 被当作新事件二次触发。
- **trigger / effect 字段并列**: 每个 event 必须同时有 trigger 和 effect; 缺一抛错。Pydantic schema 强制。
- **不引入"事件序列" / "依赖 DAG"**: MVP 期一个 event_id 对应一对 (trigger, effect); 复杂剧情拆成多个 event_id 表达。
- **加载期不连 world**: Loader 是纯解析层, 不读 WorldState; 过期事件 (如 AtTime.t <= world.t) 的过滤交给 ScriptEngine.reload_from_yaml 完成 (那里能拿到 `world.t`)。

## 4. 核心逻辑

### 4.1 数据结构

```
LoadedScripts:
  events: List[EventBundle]
  raw:    Dict[str, Any]              # 原始 YAML dict, 供 debug

EventBundle:
  id:       str
  trigger:  TriggerBase                # 已实例化
  effect:   EffectBase                 # 已实例化
  raw_yaml: Dict[str, Any]             # 原始 dict, 含 type 字段, 用于 reload 比对
```

YAML 顶层 schema (Pydantic 化的伪结构):
```python
class EventConfig(BaseModel):
    id: str
    trigger: TriggerUnion          # conscribe 自动生成的 discriminated union
    effect: EffectUnion            # 同上

class ScriptsConfig(BaseModel):
    events: List[EventConfig]
```

`TriggerUnion` / `EffectUnion` 由 conscribe 在 startup 期通过 `EffectRegistrar.build_pydantic_union()` (或等价 API) 生成, 含全部已注册子类的 discriminated union (按 `type` 字段)。

不变量:
- 同一 `events` 列表内 `id` 全局唯一; 重复抛 ValidationError。
- `trigger.type` / `effect.type` 必须命中 Registrar 已注册子类。
- 字段类型由 conscribe Tier 1 (`__init__` 签名) 严格校验。

### 4.2 关键流程 / 算法

**load(path) -> LoadedScripts**:
```
1. raw = yaml.safe_load(open(path))
   # 兼容两种来源:
   #   a) scripts.yaml 顶层就是 {events: [...]}
   #   b) simulation_config.json.world_config.events  (调用方拆好后再传 dict)
   if "events" not in raw:
       raise ValueError("missing 'events' key")

2. cfg = ScriptsConfig.model_validate(raw)
   # conscribe 已注册 EffectRegistrar / TriggerRegistrar
   # 此处 Pydantic v2 按 discriminator 字段自动路由到正确子类
   # 失败抛 ValidationError (含字段路径)

3. seen_ids = set()
   bundles = []
   for ev in cfg.events:
       if ev.id in seen_ids:
           raise ValueError(f"duplicate event_id: {ev.id}")
       seen_ids.add(ev.id)
       bundles.append(EventBundle(
           id=ev.id,
           trigger=ev.trigger,        # 已是 TriggerBase 子类实例
           effect=ev.effect,          # 已是 EffectBase 子类实例
           raw_yaml=ev.model_dump(),
       ))

4. return LoadedScripts(events=bundles, raw=raw)
```

**load_dict(d: dict) -> LoadedScripts**: 同上, 跳过 step 1 的 yaml.safe_load。供 IPC INJECT_SCRIPT_EVENT 单事件注入复用 (虽然 INJECT_SCRIPT_EVENT 也走 ScriptEngine.inject_event, 但底层校验复用同一份 Pydantic schema)。

**diff(old: LoadedScripts, new: LoadedScripts) -> ReloadDiff** (供 ScriptEngine 调试用, 可选):
```
ReloadDiff:
  added:    List[EventBundle]        # 在 new 但不在 old
  removed:  List[str]                # 在 old 但不在 new (event_id)
  changed:  List[(str, dict, dict)]  # 同 id 但 raw_yaml 不同 (id, old_raw, new_raw)
```
ScriptEngine 实际只用 `added`; `removed` / `changed` 仅 warn 提示。

### 4.3 与其他模块的交互

- **上游调用方**:
  - `ScriptEngine.reload_from_yaml(path)`: 启动期 + RELOAD_SCRIPTS IPC 期调 `ScriptLoader.load(path)`
  - `ScriptEngine.inject_event(d)`: 内部调 `ScriptLoader.load_dict({"events": [d]})` 复用校验
  - 测试代码可直接调 load / load_dict
- **下游被调方**:
  - `pyyaml.safe_load`
  - `EffectRegistrar.build_pydantic_union()` / `TriggerRegistrar.build_pydantic_union()` (conscribe 1.1.1)
  - `pydantic.BaseModel.model_validate`
  - 各 Trigger / Effect 子类的 `__init__` (Pydantic 内部调)
- **共享状态**: 无 DB / Zep / 内存态写入; 纯函数式解析。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from typing import Any, Dict, List
from pydantic import BaseModel
from agent_world.script._registrars import EffectBase, TriggerBase

class EventBundle:
    id: str
    trigger: TriggerBase
    effect: EffectBase
    raw_yaml: Dict[str, Any]

class LoadedScripts(BaseModel):
    events: List[EventBundle]
    raw: Dict[str, Any]

class ReloadDiff(BaseModel):
    added: List[EventBundle]
    removed: List[str]
    changed: List[Any]                # (id, old_raw, new_raw) tuples

class ScriptLoader:
    @staticmethod
    def load(path: str) -> LoadedScripts:
        """Parse a YAML file into validated EventBundles. Raises ValidationError / ValueError."""
        ...

    @staticmethod
    def load_dict(d: Dict[str, Any]) -> LoadedScripts:
        """Same as load() but takes a pre-parsed dict (for IPC INJECT_SCRIPT_EVENT)."""
        ...

    @staticmethod
    def diff(old: LoadedScripts, new: LoadedScripts) -> ReloadDiff:
        """Compute added / removed / changed events between two loaded sets."""
        ...
```

### 5.2 IPC / Flask / SQL

无直接 IPC / Flask / SQL。本模块仅被 `ScriptEngine` 调用; IPC 路由 (RELOAD_SCRIPTS / INJECT_SCRIPT_EVENT) 在 `script_engine.md` §5.2 描述。

## 6. 配置入口

读取来源:
- 启动期: `simulation_config.json.world_config.events` (由 simulation_config_generator 生成或 UI 编辑) — 调用方拆出该子树后用 `load_dict` 加载, 或单独写到 `<simulation_dir>/scripts.yaml` 用 `load` 加载。
- 热加载: `<simulation_dir>/scripts.yaml` (RELOAD_SCRIPTS IPC 指定路径); 推荐与 simulation_config 分文件维护, 便于 git diff。

YAML 完整形态:
```yaml
events:
  - id: act1__alice_moves__01
    trigger: { type: at_time, t: 10 }
    effect:  { type: move, agent_id: 5, place_id: "moon_base" }

  - id: act1__bar_crash__broadcast
    trigger: { type: at_condition, expr: "len(world.places.agents_at('bar_anchor')) >= 3" }
    effect:  { type: broadcast_event, targets: "place:bar_anchor", text: "A loud crash echoes." }

  - id: act2__alice_anxious_after_alice_speaks
    trigger: { type: on_action, actor_id: 5, action_type: "SPEAK_TO_LOCAL" }
    effect:  { type: state_change, agent_id: 5, new_state: "心跳加速、想找借口离开" }
```

校验规则 (Pydantic + 自校验):
- `events` 必须存在; 空列表合法 (load 返回空 LoadedScripts)。
- 每条 event 必须含 `id` (非空字符串)、`trigger` (含 `type`)、`effect` (含 `type`)。
- `id` 在单次加载内全局唯一。
- `trigger.type` / `effect.type` 必须命中 Registrar 已注册子类; 否则 Pydantic ValidationError。
- 子类字段类型按 conscribe Tier 1 schema 校验 (e.g. `MoveEffect.agent_id: int` 写成 `"5"` 字符串会被 coerce 或拒绝, 视 Pydantic v2 strict 配置)。

## 7. 待决策 / 风险

- **YAML schema 演进**: MVP 不做版本字段。后期如 trigger / effect 子类大量增加导致 schema 不向后兼容, 考虑加顶层 `version: "v0.3"` 字段 + 迁移脚本。
- **stub 与 schema 生成 CI** (LAYOUT §10.5 / §9.6.B): MVP 不接 pre-commit; 用户在 YAML 写错字段名只在加载期发现, 不在写 YAML 时 IDE 提示。后期 D 类讨论。
- **`load_dict` 与 `load` 的重复校验**: ScriptEngine.inject_event 调 load_dict 后又自己做"id 是否已加载/已 applied" 去重; load_dict 内部已做"单次加载内 id 唯一" 校验。两层职责清晰: Loader 管"YAML 自身合法", Engine 管"与世界态/历史一致"。
- **本模块自暴露的风险**:
  - **conscribe build_pydantic_union 的 cache**: 如 startup 后动态加载新 effect 子类 (例如热加载新 .py 文件), Pydantic union 不会自动刷新; MVP 不支持运行期新增 effect 类型, 仅支持 YAML 内增加新 event_id (用已注册的子类)。
  - **YAML 文件不存在 / 格式错误**: load 抛 IOError / yaml.YAMLError; ScriptEngine 应捕获后向 IPC 调用方返回结构化错误 (而非 crash 子进程)。
  - **大文件**: 单个 scripts.yaml 千级 event 范围内 yaml.safe_load 性能无虑; 万级以上需评估 streaming 解析, 但 MVP 不预期此规模。
