# ScriptEngine 实现文档

> 路径: `agent_world/script/engine.py`
> 对应 LAYOUT §: §2.E ScriptEngine / §6.1 (步骤 1-2 + 步骤 7 SCRIPTED) / §7.2 (RELOAD_SCRIPTS) / §9.5 #7 (C2)
> 上游依赖文档: `script_loader.md`, `script_registrars.md`, `script_triggers.md`, `script_effects.md`
> 下游依赖文档: 无 (被 WorldStep / ActionDispatcher / IPC server 调用)

## 1. 模块定位

`ScriptEngine` 是 Agent World 的"剧本驱动器", 承担三件事:

1. **每轮触发**: WorldStep 主循环步骤 1-2 调用 `due_events(world, t)` + `apply(effects, world)`, 把"该 tick 应该触发的剧本事件"翻译成 Effect 对象并落到世界态(world.db.{place, relation, capability, agent_location, ...})与 ChatMemory/Zep。
2. **热加载去重**: 通过显式 IPC `RELOAD_SCRIPTS` (LAYOUT §9.5 #7 / C2) 增量追加新事件; 用 `loaded_event_ids` + `applied_events` 两个集合保证已加载/已触发的 event_id 永不重触发。
3. **被动通知**: 当 ActionDispatcher 收到 agent 的 action 时, 调 `inject_event(...)` / `pending_for(agent)` 让 OnAction 类 trigger 与 PerceptionBuilder 的 `scripted_notification` 字段联通。

输入: WorldState 引用 + 当前 `t` + (可选) IPC 重加载请求 / dispatcher 的 action hook。
输出: 一组待 apply 的 Effect 实例 + `world.db.script_event_log` 写入 + `Observation.scripted_notification` 数据源。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 强制注入动作模式 | OASIS | `oasis/environment/env_action.py` `ManualAction` 类 | PATTERN | 借其"系统侧绕过 LLM 直接产生 Action"的思路, 但 ScriptEngine 产物是 Effect 不是 Action |
| 主循环步骤位置 | OASIS | `oasis/environment/env.py:136-198` `step()` 骨架 | PATTERN | ScriptEngine 在 WorldStep 步骤 1-2 (LLM gather 之前) 跑完 |
| event_id 用户写明 | MiroFish | `run_parallel_simulation.py:1180-1207` `event_config.initial_posts` | PATTERN | 不让系统 hash 生成, 用户在 YAML 写明, 方便 reload 时比对 |
| `script_event_log` 表写入 | LAYOUT §3.2 | world.db.script_event_log DDL | NEW | apply 后追加一行供 report_agent 复盘 |

## 3. 关键改动 (相对来源仓库)

- 全新写, 不直接 import OASIS 任何代码。设计灵感来自 OASIS `ManualAction` 与 MiroFish `initial_posts`。
- 与 OASIS `ManualAction` 的区别: `ScriptEngine` 不构造 LLM Action (CREATE_POST/LIKE/...), 而是构造 Effect (Move/RelationChange/StateChange/...), Effect 直接改 WorldState/world.db, 不走 ActionDispatcher 路由。
- 热加载语义: IPC `RELOAD_SCRIPTS` 仅"读 YAML、对比、增量追加 trigger 实例"; 不 reload 已 applied 的事件, 也不 reload 已 loaded 但还没 fire 的事件 (它们的内存态以已加载为准, 二次 reload 内容不同则 warn 拒绝)。
- 过期事件 (`AtTime.t <= world.t`) 在 `reload_from_yaml` 阶段就被 **忽略 + warn**, 不入 `loaded_event_ids`。

## 4. 核心逻辑

### 4.1 数据结构

```
ScriptEngine:
  loaded_event_ids:  Set[str]              # 全部已加载入内存的 event_id (含尚未 fire 的)
  applied_events:    Set[str]              # 已 fire 并 apply 完成的 event_id (持久写到 world.db.script_event_log)
  events:            Dict[str, EventBundle]
                                           # event_id → (trigger, effect, raw_yaml_meta)
  pending_notifications: Dict[int, List[str]]
                                           # agent_id → 待透传给 PerceptionBuilder 的 scripted_notification 字符串
  on_action_index:   Dict[(actor_id, action_type), List[event_id]]
                                           # OnAction trigger 的反向索引
  world:             WorldState
```

不变量:
- `applied_events ⊆ loaded_event_ids`
- 一个 event_id 一旦进 `applied_events`, 永不二次触发, 即使 RELOAD_SCRIPTS 重读也跳过
- 启动时从 `world.db.script_event_log` 全量恢复 `applied_events` (crash recovery)
- `events[id]` 仅持有"未 fire" 事件; fire 后从 dict 移除, id 留在 `applied_events` 即可

### 4.2 关键流程 / 算法

**初始化 (启动期)**:
```
1. load applied_events from world.db.script_event_log (SELECT DISTINCT event_id)
2. reload_from_yaml(initial_scripts.yaml)          # 见下
3. ActionDispatcher.register_hook(self.on_action_dispatched)
```

**reload_from_yaml(path)** (启动期 + RELOAD_SCRIPTS IPC 共用):
```
parsed = ScriptLoader.load(path)                   # 见 script_loader.md
for ev in parsed.events:
    if ev.id in loaded_event_ids:
        continue                                   # 已加载, 跳过 (不动内存)
    if ev.id in applied_events:
        continue                                   # 已触发过, 跳过
    if isinstance(ev.trigger, AtTimeTrigger) and ev.trigger.t <= world.t:
        log.warning(f"event {ev.id} is expired (t={ev.trigger.t} <= now={world.t}), skip")
        continue                                   # 过期忽略 + warn
    events[ev.id]      = ev
    loaded_event_ids.add(ev.id)
    if isinstance(ev.trigger, OnActionTrigger):
        on_action_index[(ev.trigger.actor_id, ev.trigger.action_type)].append(ev.id)
```

**due_events(world, t) -> List[(event_id, effect)]** (WorldStep 步骤 1):
```
due = []
for id, bundle in events.items():
    if bundle.trigger.fires(world, t):             # 见 script_triggers.md
        due.append((id, bundle.effect))
return due
```

**apply(due, world)** (WorldStep 步骤 2):
```
for (id, effect) in due:
    try:
        await effect.apply(world)                  # 子类各自实现, 见 script_effects.md
        world.world_db.insert_script_event_log(id, world.t, effect.summary())
        applied_events.add(id)
        events.pop(id, None)
    except Exception as e:
        log.error(f"effect {id} failed: {e}"); continue   # 失败不写 log, id 仍在 events, 下轮可重试
```

**inject_event(event_dict)** (IPC INJECT_SCRIPT_EVENT):
```
single-event 版本的 reload_from_yaml: 走同一份 dedup 逻辑
```

**on_action_dispatched(actor_id, action_type, payload)** (ActionDispatcher hook):
```
for id in on_action_index.get((actor_id, action_type), []):
    bundle = events[id]
    bundle.trigger.notify(actor_id, action_type, payload)   # 让 OnAction 内部置 fired flag
```

**pending_for(agent) -> List[str]** (PerceptionBuilder 调):
```
return self.pending_notifications.pop(agent.id, [])     # 取出后清空, 仅透传 1 轮
```

某些 effect (如 BroadcastEvent) 在 apply 时会调 `engine.notify_agent(agent_id, text)`, 把字符串塞进 `pending_notifications`。

### 4.3 与其他模块的交互

- **上游调用方**:
  - `WorldStep.step()` 步骤 1: `due = engine.due_events(world, t)`
  - `WorldStep.step()` 步骤 2: `await engine.apply(due, world)`
  - `PerceptionBuilder.build()`: `obs.scripted_notification = engine.pending_for(agent)`
  - `ActionDispatcher.dispatch()`: `engine.on_action_dispatched(...)`
  - `IPCServer` (RELOAD_SCRIPTS / INJECT_SCRIPT_EVENT): `engine.reload_from_yaml(path)` / `engine.inject_event(d)`

- **下游被调方**:
  - `ScriptLoader.load(path)` (script_loader.md)
  - 各 Effect 子类的 `apply(world)` (script_effects.md)
  - 各 Trigger 子类的 `fires(world, t)` / `notify(...)` (script_triggers.md)
  - `world.world_db.insert_script_event_log(...)` (persistence/world_db.py)

- **共享状态**:
  - 写 `world.db.script_event_log` (event_id, triggered_at, effect_summary)
  - 通过 effect 间接写 `world.db.{place, relation, capability, agent_location, direct_message, group_*}`
  - 通过 DialogueInjection effect 间接写 ChatMemory + Zep `graph_{agent}`
  - 不直接读写 pool_*.db (pool 写入由 effect 内部完成)

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from typing import Any, Dict, List, Optional, Set, Tuple
from agent_world.script._registrars import EffectBase, TriggerBase

class EventBundle:
    id: str
    trigger: TriggerBase
    effect: EffectBase
    raw_yaml: Dict[str, Any]

class ScriptEngine:
    def __init__(self, world: "WorldState") -> None: ...

    # WorldStep 主循环钩子
    def due_events(self, world: "WorldState", t: int) -> List[Tuple[str, EffectBase]]: ...
    async def apply(
        self,
        due: List[Tuple[str, EffectBase]],
        world: "WorldState",
    ) -> None: ...

    # 热加载 (IPC RELOAD_SCRIPTS)
    def reload_from_yaml(self, path: str) -> Dict[str, int]:
        """returns {'loaded': N, 'skipped_already_loaded': M, 'skipped_applied': K, 'skipped_expired': J}"""
        ...

    # 单事件注入 (IPC INJECT_SCRIPT_EVENT)
    def inject_event(self, event_dict: Dict[str, Any]) -> str:
        """returns event_id; raises if id duplicates or fields invalid"""
        ...

    # PerceptionBuilder
    def pending_for(self, agent: "Agent") -> List[str]: ...

    # ActionDispatcher hook
    def on_action_dispatched(
        self,
        actor_id: int,
        action_type: str,
        payload: Dict[str, Any],
    ) -> None: ...

    # Effect 反向通知 agent (BroadcastEvent / DialogueInjection 内部用)
    def notify_agent(self, agent_id: int, text: str) -> None: ...

    # 状态查询
    @property
    def loaded_event_ids(self) -> Set[str]: ...
    @property
    def applied_events(self) -> Set[str]: ...
```

### 5.2 IPC / Flask / SQL

- **IPC**:
  - `RELOAD_SCRIPTS { path: str }` → 调 `reload_from_yaml(path)`, 返回 dedup 统计
  - `INJECT_SCRIPT_EVENT { event: dict }` → 调 `inject_event(event)`, 返回 event_id
- **Flask**:
  - `POST /simulations/<id>/reload-scripts` → IPC RELOAD_SCRIPTS
  - `POST /simulations/<id>/inject-event` → IPC INJECT_SCRIPT_EVENT
- **SQL 写**:
  - `INSERT INTO world.db.script_event_log(event_id, triggered_at, effect_summary)`
- **SQL 读** (启动恢复):
  - `SELECT DISTINCT event_id FROM world.db.script_event_log`

## 6. 配置入口

读 `simulation_config.json.world_config.events`:
```yaml
world_config:
  events:
    - id: act1__alice_moves__01           # 用户写明, 不允许系统生成
      trigger: { type: at_time, t: 10 }
      effect:  { type: move, agent_id: 5, place_id: "moon_base" }
```

启动时 `engine.reload_from_yaml(<simulation_dir>/scripts.yaml)` 读完整配置。

默认值: 无 (所有字段必填; conscribe Pydantic schema 拒空)。

验证规则:
- `id` 必填且全局唯一; 重复定义启动期抛错 (reload 期 warn 跳过)
- `trigger.type` 必须命中 TriggerRegistrar 已注册子类
- `effect.type` 必须命中 EffectRegistrar 已注册子类
- 过期 AtTime 事件 (启动期 t > trigger.t 不可能, 但 reload 期可能) → warn + skip

## 7. 待决策 / 风险

- **N1 (LAYOUT §9.5.1)**: MOVE 之外的"被动行为边界" (被踢出群/剧本强制传送) 是否触发 BehaviorCompressor。当前 `MoveEffect.apply` 已通过 ActionDispatcher hook 触发 compressor; 剧本强制 Move 走同一路径; 群聊踢人不触发, 由 N1 单独决议。
- **本模块自暴露的风险**:
  - **多事件同 tick 触发顺序**: `due_events` 返回顺序为 dict 插入顺序 (Python 3.7+ 保证); 同 tick 多 effect 之间无显式依赖图, 用户需自行保证幂等。
  - **OnAction trigger 的 notify 是同步的**: ActionDispatcher 调 hook 时同步置 fired flag; 下一轮 `due_events` 才 fire。如果剧本要求"action 即刻触发 effect", MVP 不支持 (一致性优先)。
  - **reload 期 events 内存增长**: 长仿真 + 高频 reload 可能让 `loaded_event_ids` / `applied_events` 集合膨胀。MVP 量级 (千级 event) 无虑; 100w 量级 (#8) 后再压测。
  - **跨 effect 失败原子性**: 同 tick 多 effect 中一个失败, 已 apply 的不回滚; 与 LAYOUT §3 跨 DB 不原子立场一致。
