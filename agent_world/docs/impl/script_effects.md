# Script Effects 实现文档

> 路径: `agent_world/script/effects/*.py` (一文件一类: `move.py`, `relation_change.py`, `capability_change.py`, `broadcast_event.py`, `dialogue_injection.py`, `place_mutation.py`, `state_change.py`)
> 对应 LAYOUT §: §2.E effects/ / §10.1 (metaclass 继承自动注册) / §10.7 EffectRegistrar / §3.2 (写入表) / B5 (StateChange v0.3)
> 上游依赖文档: `script_registrars.md`
> 下游依赖文档: `script_engine.md` (engine 调 apply), `script_loader.md` (YAML→实例)

## 1. 模块定位

`effects/` 目录下的 7 个 effect 子类是剧本"做了什么" 的执行单元。每个子类:

- 继承 `EffectBase` (script_registrars.md), 通过 metaclass 自动注册到 `EffectRegistrar`。
- 实现 `async def apply(self, world) -> None`: 在 ScriptEngine 主循环步骤 2 被批量调用, 直接改 WorldState / world.db / ChatMemory / Zep。
- 提供 `summary() -> str` (默认实现继承自 EffectBase): 写进 `world.db.script_event_log.effect_summary` 供报告复盘。

7 种 effect (LAYOUT §2.E + B5 v0.3):

| 类 | 文件 | 影响 |
|---|---|---|
| `MoveEffect` | `move.py` | 改 agent 位置 (world.db.agent_location) |
| `RelationChangeEffect` | `relation_change.py` | 加/减一条多类型关系边 (world.db.relation) |
| `CapabilityChangeEffect` | `capability_change.py` | 授予/撤销 capability (world.db.capability) |
| `BroadcastEventEffect` | `broadcast_event.py` | 给一组 agent 推送 scripted_notification (engine.notify_agent) |
| `DialogueInjectionEffect` | `dialogue_injection.py` | 替指定 agent 注入 ChatMemory + Zep 一条记忆 |
| `PlaceMutationEffect` | `place_mutation.py` | 改地点元数据 / coverage 矩阵 (world.db.place + coverage) |
| `StateChangeEffect` | `state_change.py` | **v0.3 新**: 改 agent.current_state, 与 UPDATE_STATE action 同字段 |

输入: WorldState 引用 + 子类自身字段 (来自 YAML)。
输出: 副作用——写 world.db / Zep / 内存态。

为什么必须存在: LAYOUT §2.E 明确把 effect 定义为可扩展层 (conscribe 注册); 7 种类型覆盖 MVP 期所有"剧本能做的事"。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| Dialogue 注入 ChatMemory | OASIS | `social_agent/agent.py:286-291` `update_memory` | KEEP (调用) | DialogueInjection 内部调此 method |
| Move 写 agent_location | LAYOUT §3.2 | world.db.agent_location | NEW | 与 agent 自己 REQUEST_MOVE 走同一字段 |
| StateChange 与 UPDATE_STATE 共享字段 | LAYOUT §6.1 步骤 7 | `world.agents[a].current_state` | NEW | 剧本 + agent 双写, schema 不区分来源 |
| relation_type 元数据 | LAYOUT §2.A RelationGraph | world/relation_types/*.py | KEEP (调用) | RelationChange 调 RelationGraph API, 由 RelationGraph 处理 symmetric/mutually_exclusive |
| Coverage 写法 | LAYOUT §3.2 coverage | (src_place, dst_place, latency_ticks) | NEW | PlaceMutation 可改 coverage |

## 3. 关键改动 (相对来源仓库)

- 全新写。设计灵感来自 LAYOUT §2.E 的 7 类划分 (含 B5 新增 StateChange)。
- **StateChangeEffect 与 agent 端 UPDATE_STATE 共享 `world.agents[agent_id].current_state` 字段**, 不区分写入来源, 与 LAYOUT B5 决议一致。
- **DialogueInjectionEffect** 不直接构造 ChatMemory 数据; 复用 OASIS `agent.update_memory` (agent.py:286-291) 接口, 同时入队 Zep `graph_{agent_id}`。
- **MoveEffect** 不直接调 `world.places.move`, 而是经过 ActionDispatcher 路由 (与 agent REQUEST_MOVE 走同一路径), 让 `BehaviorCompressor.on_move` hook 一致触发 (LAYOUT §6.1 步骤 9)。
- **RelationChangeEffect** 不写 SQL, 调 `world.relations.add(src, dst, type)` / `.remove(...)`; symmetric 双写、mutually_exclusive 抛错由 RelationGraph 处理 (LAYOUT C1)。投影到 pool_*.db.follow 由 `on_change` 钩子自动完成。
- **PlaceMutationEffect** 写 `world.db.{place, coverage}` 后立刻刷 PlaceStore 内存态; 不重启 ConnectivityResolver 反向索引 (增量更新)。

## 4. 核心逻辑

### 4.1 数据结构

每个子类的 `__init__` 字段 (即 YAML schema):

| 子类 | 字段 |
|---|---|
| `MoveEffect` | `agent_id: int`, `place_id: str` |
| `RelationChangeEffect` | `src_agent: int`, `dst_agent: int`, `relation_type: str`, `op: Literal["add", "remove"]` (default `"add"`), `expires_at: Optional[int] = None` |
| `CapabilityChangeEffect` | `agent_id: int`, `capability: str`, `op: Literal["grant", "revoke"]` |
| `BroadcastEventEffect` | `targets: List[int] \| Literal["all", "place:<id>"]`, `text: str` |
| `DialogueInjectionEffect` | `agent_id: int`, `speaker: str` (NPC 名或叙述者), `content: str` |
| `PlaceMutationEffect` | `place_id: str`, `attrs_patch: Optional[Dict[str, Any]] = None`, `coverage_patch: Optional[List[CoverageEdge]] = None` |
| `StateChangeEffect` | `agent_id: int`, `new_state: str` |

`CoverageEdge = {src: str, dst: str, latency_ticks: int}`。

不变量:
- 所有 effect 的 `apply` 必须是 idempotent-safe 风格 (重复 apply 同一实例不应破坏世界态), 但 ScriptEngine 已通过 `applied_events` 集合防重, 因此子类内部不必再去重。
- effect 实例不持有 ScriptEngine 引用; 反向通知通过 `world.script.notify_agent(...)` 间接调 (BroadcastEvent 用)。

### 4.2 关键流程 / 算法

**MoveEffect.apply(world)**:
```
old_place = world.places.L_t.get(self.agent_id)
await world.dispatcher.handle_move(           # 共用 agent REQUEST_MOVE 路径
    agent_id=self.agent_id,
    new_place=self.place_id,
    source="script",                          # 区分来源 (供 trace 用)
)
# dispatcher 内部:
#   1. 调 BehaviorCompressor.on_move(agent_id, old_place)  # 异步, 不阻塞
#   2. 写 world.db.agent_location SET place_id=?
#   3. 更新 PlaceStore 反向索引
```

**RelationChangeEffect.apply(world)**:
```
if self.op == "add":
    await world.relations.add(self.src_agent, self.dst_agent, self.relation_type, expires_at=self.expires_at)
else:
    await world.relations.remove(self.src_agent, self.dst_agent, self.relation_type)
# RelationGraph 内部:
#   - symmetric=True 时自动双写
#   - mutually_exclusive 命中时抛错 (effect.apply 抛错被 ScriptEngine 捕获并 log)
#   - on_change 钩子同步投影到 pool_*.db.follow (mutual_follow / follower 类)
```

**CapabilityChangeEffect.apply(world)**:
```
if self.op == "grant":
    await world.capabilities.grant(self.agent_id, self.capability, granted_at=world.t)
else:
    await world.capabilities.revoke(self.agent_id, self.capability, revoked_at=world.t)
# CapabilityTable 内部用 (granted_at, revoked_at) 模式区分历史 (LAYOUT §3.2)
# 触发 ConnectivityResolver / dynamic_tools 缓存失效
```

**BroadcastEventEffect.apply(world)**:
```
target_ids = resolve_targets(self.targets, world)  # "all" / "place:<id>" / List[int]
for aid in target_ids:
    world.script.notify_agent(aid, self.text)      # 进 ScriptEngine.pending_notifications
# PerceptionBuilder 下轮把这些字符串拼进 obs.scripted_notification
```

**DialogueInjectionEffect.apply(world)**:
```
agent = world.agents[self.agent_id]
# 1. ChatMemory: 复用 OASIS 接口
agent.update_memory(role="user", content=f"[{self.speaker}]: {self.content}")
# 2. Zep: 入队 graph_{agent_id}
await world.memory.updater.enqueue(
    graph_id=f"agent_{self.agent_id}",
    kind="dialogue",
    text=f"{self.speaker} said to you: {self.content}",
)
```

**PlaceMutationEffect.apply(world)**:
```
if self.attrs_patch is not None:
    await world.world_db.update_place_attrs(self.place_id, self.attrs_patch)
    world.places.refresh_attrs(self.place_id)             # 内存态同步
if self.coverage_patch is not None:
    for edge in self.coverage_patch:
        await world.world_db.upsert_coverage(edge.src, edge.dst, edge.latency_ticks)
    world.connectivity.refresh_coverage()                 # 反向索引重建 (增量)
```

**StateChangeEffect.apply(world)** (v0.3 新):
```
world.agents[self.agent_id].current_state = self.new_state
# 不写 world.db; current_state 是 in-memory 字段, 由 WorldState.flush 周期落库 (如设计)
# 与 UPDATE_STATE action 同路径 (LAYOUT B5)
```

### 4.3 与其他模块的交互

- **上游调用方**:
  - `ScriptEngine.apply(due, world)`: 遍历调每个 `effect.apply(world)`
- **下游被调方** (按 effect 子类):
  - MoveEffect → `world.dispatcher.handle_move` → world.db.agent_location, BehaviorCompressor
  - RelationChangeEffect → `world.relations.add/remove` → world.db.relation, pool_*.db.follow (经 on_change)
  - CapabilityChangeEffect → `world.capabilities.grant/revoke` → world.db.capability
  - BroadcastEventEffect → `world.script.notify_agent` → ScriptEngine.pending_notifications
  - DialogueInjectionEffect → OASIS `agent.update_memory` (vendor/oasis/social_agent/agent.py:286-291) + `world.memory.updater.enqueue` → ChatMemory + Zep
  - PlaceMutationEffect → `world.world_db.update_place_attrs` / `upsert_coverage` → world.db.{place, coverage}
  - StateChangeEffect → `world.agents[id].current_state` (内存态)
- **共享状态** (写):
  - world.db.{place, coverage, agent_location, relation, capability}
  - pool_*.db.follow (经 RelationGraph.on_change)
  - ChatMemory (经 agent.update_memory)
  - Zep `graph_{agent_id}` (经 memory.updater)
  - ScriptEngine.pending_notifications (内存)
  - WorldState.agents[*].current_state (内存)

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from typing import Any, Dict, List, Literal, Optional, Union
from agent_world.script._registrars import EffectBase

class MoveEffect(EffectBase):
    """Move an agent to a new place.

    Args:
        agent_id: Target agent ID.
        place_id: Destination place.
    """
    def __init__(self, *, agent_id: int, place_id: str) -> None: ...
    async def apply(self, world: "WorldState") -> None: ...


class RelationChangeEffect(EffectBase):
    """Add or remove a typed relation edge.

    Args:
        src_agent: Source agent ID.
        dst_agent: Destination agent ID.
        relation_type: Registered relation_type key.
        op: 'add' (default) or 'remove'.
        expires_at: Optional tick at which relation auto-expires.
    """
    def __init__(
        self,
        *,
        src_agent: int,
        dst_agent: int,
        relation_type: str,
        op: Literal["add", "remove"] = "add",
        expires_at: Optional[int] = None,
    ) -> None: ...
    async def apply(self, world: "WorldState") -> None: ...


class CapabilityChangeEffect(EffectBase):
    """Grant or revoke a capability.

    Args:
        agent_id: Target agent ID.
        capability: Registered capability_type key.
        op: 'grant' or 'revoke'.
    """
    def __init__(
        self,
        *,
        agent_id: int,
        capability: str,
        op: Literal["grant", "revoke"],
    ) -> None: ...
    async def apply(self, world: "WorldState") -> None: ...


class BroadcastEventEffect(EffectBase):
    """Push a scripted_notification text to a set of agents.

    Args:
        targets: List of agent IDs, or 'all', or 'place:<place_id>'.
        text: Notification body.
    """
    def __init__(
        self,
        *,
        targets: Union[List[int], str],
        text: str,
    ) -> None: ...
    async def apply(self, world: "WorldState") -> None: ...


class DialogueInjectionEffect(EffectBase):
    """Inject one dialogue line into an agent's ChatMemory + Zep graph.

    Args:
        agent_id: Recipient agent.
        speaker: Source name (NPC or narrator).
        content: Dialogue body.
    """
    def __init__(
        self,
        *,
        agent_id: int,
        speaker: str,
        content: str,
    ) -> None: ...
    async def apply(self, world: "WorldState") -> None: ...


class PlaceMutationEffect(EffectBase):
    """Patch a place's attrs and/or coverage edges.

    Args:
        place_id: Target place.
        attrs_patch: Partial dict to merge into place.attrs.
        coverage_patch: List of {src, dst, latency_ticks} edges to upsert.
    """
    def __init__(
        self,
        *,
        place_id: str,
        attrs_patch: Optional[Dict[str, Any]] = None,
        coverage_patch: Optional[List[Dict[str, Any]]] = None,
    ) -> None: ...
    async def apply(self, world: "WorldState") -> None: ...


class StateChangeEffect(EffectBase):
    """Set an agent's current_state (B5; same field as UPDATE_STATE action).

    Args:
        agent_id: Target agent.
        new_state: Replacement string for system prompt 'Current State' segment.
    """
    def __init__(self, *, agent_id: int, new_state: str) -> None: ...
    async def apply(self, world: "WorldState") -> None: ...
```

### 5.2 IPC / Flask / SQL

- **SQL 写** (按 effect):
  - MoveEffect: `UPDATE world.db.agent_location SET place_id=? WHERE agent_id=?`
  - RelationChangeEffect: `INSERT/DELETE world.db.relation`; 经 on_change 投影到 `pool_*.db.follow`
  - CapabilityChangeEffect: `INSERT world.db.capability(...)` 或 `UPDATE ... SET revoked_at=?`
  - DialogueInjectionEffect: 不直接写 SQL (写 ChatMemory + Zep)
  - PlaceMutationEffect: `UPDATE world.db.place SET attrs=?` / `INSERT OR REPLACE INTO world.db.coverage`
  - StateChangeEffect: 仅写内存 (WorldState.agents[*].current_state)
  - BroadcastEventEffect: 仅写内存 (ScriptEngine.pending_notifications)
- **IPC / Flask**: 无独立路由; 全部由 ScriptEngine 调度。

## 6. 配置入口

每个 effect 子类的 `__init__` 签名 + Google docstring `Args:` 段即 conscribe Tier 1 schema (LAYOUT §10.3)。

YAML 形态示例:
```yaml
effect: { type: move, agent_id: 5, place_id: "moon_base" }
effect: { type: relation_change, src_agent: 1, dst_agent: 2, relation_type: "lover", op: "add" }
effect: { type: capability_change, agent_id: 3, capability: "account_twitter", op: "grant" }
effect: { type: broadcast_event, targets: "place:bar_anchor", text: "A loud crash echoes." }
effect: { type: dialogue_injection, agent_id: 7, speaker: "Narrator", content: "You feel a chill." }
effect: { type: place_mutation, place_id: "bar_anchor", attrs_patch: { behavior_hint: "现在气氛紧张" } }
effect: { type: state_change, agent_id: 5, new_state: "焦虑、想离开酒吧" }
```

## 7. 待决策 / 风险

- **N1 (LAYOUT §9.5.1)**: BehaviorCompressor 是否在 MoveEffect 触发的 Move 也跑。当前设计**复用 dispatcher 路径所以会跑**; 与 agent 自己 REQUEST_MOVE 一致。
- **N2 (LAYOUT §9.5.1)**: StateChangeEffect 滥用治理。当前 schema 不限制写频率; 极端情况下剧本作者每 tick 改 current_state 会导致 Soul/Long-term Goal 段以下 prompt 段持续翻动, 影响 prompt cache 命中。MVP 不限制, 后期可加 throttle 或 cooldown_ticks 字段。
- **跨 effect 失败原子性**: 同 tick 多 effect, 一个抛错 (如 RelationChange 命中 mutually_exclusive) 时, 已 apply 的不回滚; 与 LAYOUT §3 跨 DB 不原子立场一致。
- **BroadcastEvent 的 `targets="all"` 性能**: 100w agent scale (LAYOUT §9.5 #8) 下需注意 `pending_notifications` dict 大小; MVP 千级量级无虑。
- **DialogueInjection 的 speaker 不存在**: speaker 仅作为字符串进 ChatMemory, 不与 agent 表关联; 即使写了不存在的 NPC 名也不报错 (设计上允许"叙述者"/"环境提示"等非 agent 来源)。
