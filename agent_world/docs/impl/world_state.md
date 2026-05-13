# WorldState 实现文档

> 路径: `agent_world/world/state.py`
> 对应 LAYOUT §: §2.A WorldState（表行 198）/ §3.1-3.2（双层 DB 物理布局）/ §6.3（Observation 派生）
> 上游依赖文档: `world_clock.md`（提供单全局 `t`）, `place_store.md`, `relation_graph.md`, `capability_table.md`, `connectivity.md`, `pools_manager.md`（`MultiPoolPlatformManager`）, `persistence_world_db.md`（world.db CRUD 与 12 张表）
> 下游依赖文档: `world_step.md`（micro-tick 主循环消费 WorldState）, `perception.md`（从 WorldState 派生 Observation）, `dispatcher.py`（写回 WorldState 的多个子段）, `script_engine.md`（effect 改 WorldState）, `memory_compressor.md`（读 segment / 写 ChatMemory hook 时需要 agent 引用）

## 1. 模块定位

`WorldState` 是仿真器的"世界总线柄"——它本身**不存数据**，只是把七元组 $\langle P, A, L_t, R_t, C_t, F_t, M_t\rangle$ 的各个子段聚合成单一引用对象，交给 `WorldStep` / `PerceptionBuilder` / `ActionDispatcher` / `ScriptEngine` 共同读写。每个子段（PlaceStore / RelationGraph / CapabilityTable / MultiPoolPlatformManager / 等）自己负责持久化到 `world.db` 或 `pool_*.db`；`WorldState` 只持引用 + 少数无主字段（如 `agents` 字典里的 `current_state` / `soul` / `long_term_goal`，由 `UPDATE_STATE` 与 `StateChangeEffect` 共享写入）。

- **输入**：启动期由 runner 用 `simulation_config.json + world.db + pool_*.db` 装配出 7 个子段 → 注入构造函数。
- **输出**：被 WorldStep 全程持有；所有读路径（PerceptionBuilder）和写路径（ActionDispatcher / ScriptEngine）都从这一对象进入。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 容器骨架（聚合 platform / agent_graph / clock 字段） | OASIS | `vendor/oasis/oasis/environment/env.py:50-116`（`OasisEnv.__init__`） | PATTERN | 把 OASIS 单 platform 字段替换为 `platform_manager`，新增 4 个子段引用 |
| 单 graph / igraph 容器风格 | OASIS | `vendor/oasis/oasis/social_agent/agent_graph.py:25-292` | PATTERN | 仅作内存图聚合方式参考；本项目子段是独立 store，不内联 igraph |
| Clock 注入风格 | OASIS | `vendor/oasis/oasis/clock/clock.py` | KEEP | 通过 `world_clock.md` 引入；`WorldState.clock` 直接持有 `Clock` 实例 |
| 七元组数学定义 | 需求文档 | `AGENT_WORLD_REQUIREMENTS.md` §3 | NEW | LAYOUT §2.A 表行 198 明确字段对应关系 |

## 3. 关键改动 (相对 OASIS `OasisEnv`)

- **改动 1**：把 `self.platform: Platform` 换成 `self.pools: MultiPoolPlatformManager`。OASIS 单平台假设废除，改为按 (place, feed) 寻址多池（LAYOUT §2.A 表行 198；§2.D）。
- **改动 2**：新增 4 个子段引用 `places / relations / capabilities / connectivity`，分别对应 $P+L_t / R_t / C_t / \phi$。OASIS 原 `OasisEnv` 没有这些概念。
- **改动 3**：新增 `world_db: WorldDB` 句柄，给 PerceptionBuilder / Bus 直接 SELECT `direct_message / overhear / group_event` 使用（LAYOUT §3.2 12 张表）。pool 数据走 `pools.platform_for(p, f)`。
- **改动 4**：新增 `agents: Dict[int, AgentRuntime]`，每个 `AgentRuntime` 持 `soul / long_term_goal / current_state` 三段（B5 4 段 system prompt 来源）+ `last_message_seen_at`（B1.1 incoming 去重水位）；这些字段非 store-owned，是 WorldState 自有的小内存表。`UPDATE_STATE` action 与 `StateChangeEffect` 都直写此字段。
- **改动 5**：`script: ScriptEngine` 与 `memory: MemoryHub`（含 segment / compressor / multi_graph_updater）作为聚合字段持有，方便 dispatcher 路由。
- **改动 6**：去掉 OASIS 的 `agent_environment.py` 句柄——`PerceptionBuilder` 不挂在 WorldState 上，由 WorldStep 直接调用并把 WorldState 作为参数传入；WorldState 保持"纯数据聚合"职责（LAYOUT §2.A 表行 204；§4 表行 422 "DELETE"）。

## 4. 核心逻辑

### 4.1 数据结构（七元组对应）

| 数学符号 | 物理字段 | 持有类型 | 持久化位置 |
|---|---|---|---|
| $P$（地点集合 + 层级） | `places: PlaceStore` | LAYOUT §2.A `place_store.py` | `world.db.place`（启动全量加载，PlaceMutation effect 增量改） |
| $A$（agent 集合） | `agents: Dict[int, AgentRuntime]`（含 soul / long_term_goal / current_state / last_message_seen_at） | dict + dataclass | profile 来自 config；运行时字段无独立 SQL 表，crash 后由 ChatMemory 摘要 + script_event_log 回放 |
| $L_t$（agent → place 映射） | `places.L_t: Dict[int, str]`（PlaceStore 内部维护反向索引 `Dict[place, Set[agent]]`） | LAYOUT §2.A `place_store.py` | `world.db.agent_location` |
| $R_t$（多类型关系图） | `relations: RelationGraph` | LAYOUT §2.A `relation_graph.py` | `world.db.relation`；on_change 钩子投影到 `pool_*.db.follow`（LAYOUT §3.5） |
| $C_t$（能力表 + 反向索引） | `capabilities: CapabilityTable` | LAYOUT §2.A `capability_table.py` | `world.db.capability` |
| $F_t$（feed / 池清单） | `pools: MultiPoolPlatformManager` | LAYOUT §2.D `pools/manager.py` | `pool_*.db`（每池一文件） |
| $M_t$（记忆三层） | `memory: MemoryHub`（含 `multi_graph_updater / multi_graph_manager / segment / compressor / retrieval`） | LAYOUT §2.F | Zep `agent_{id} / place_{id} / world` 三层 graph |

**辅助字段（不属于七元组但 WorldState 持有）**：
- `clock: Clock`（单全局，LAYOUT §2.A 表行 206；详见 `world_clock.md`）
- `connectivity: ConnectivityResolver`（4 个 $\phi$ 谓词；不是状态而是派生器，但聚合在此方便 dispatcher 调用）
- `script: ScriptEngine`
- `world_db: WorldDB`（12 张表 CRUD 句柄）
- `pending_moves: Dict[int, str]`（agent_id → 目标 place_id；lockstep 队列，由 REQUEST_MOVE 通过审批后压入，WorldStep 步骤 9 串行结算，详见 `world_step.md`）
- `delivery_lock: asyncio.Lock`（LAYOUT §9.6 决议 G / B8：world.db 单写者锁，所有 Bus 写 `direct_message / group_*` 都走它）

**不变量**：
- `places.L_t.keys() == agents.keys()`（每个 agent 必有当前 place；启动前已校验）
- `len({a.last_message_seen_at}) == |agents|`（初始为 0，单调递增）
- 七元组中**只有** `agents.{soul, long_term_goal, current_state, last_message_seen_at}` 由 WorldState 直接拥有；其他字段都是子段引用，禁止在 `state.py` 中绕过子段直接修改。

### 4.2 关键流程

WorldState 自身只有 3 类操作：

```
A. 装配（启动期）
   load_world(config, world_db, pool_factory, clock) -> WorldState
     places       <- PlaceStore.load(world_db)
     relations    <- RelationGraph.load(world_db)
     capabilities <- CapabilityTable.load(world_db)
     pools        <- MultiPoolPlatformManager.build(config, pool_factory)
     connectivity <- ConnectivityResolver(places, relations, capabilities, pools)
     agents       <- {a.id: AgentRuntime(soul, long_term_goal, current_state, last_message_seen_at=0) for a in config.agent_configs}
     return WorldState(...)

B. 状态查询（被 PerceptionBuilder / Dispatcher 使用，详见 world_step.md §4.2 步骤 7）
   - 全部委托给子段：world.places.agents_at(p) / world.relations.contacts_of(a) / world.capabilities.has(a, c) / world.connectivity.phi_RDC(a, b) / world.pools.feeds_at(p)
   - direct_message / overhear / group_event 走 world.world_db SELECT（PerceptionBuilder 内部，LAYOUT §6.3）

C. 状态修改（被 ActionDispatcher / ScriptEngine 使用）
   - SPEAK_TO_LOCAL / SEND_MESSAGE / SEND_TO_GROUP -> Bus.send(...) -> world.world_db.execute(INSERT direct_message ...)（持 delivery_lock）
   - UPDATE_STATE       -> world.agents[a].current_state = new_state（无锁，仅 owner agent 自己的 micro-step 内写）
   - StateChangeEffect  -> world.agents[a].current_state = new_state（同字段；剧本与 agent 共享）
   - RelationChange     -> world.relations.add/remove(...)（钩子触发 pool follow 投影）
   - CapabilityChange   -> world.capabilities.grant/revoke(...)
   - PlaceMutation      -> world.places.mutate(...)
   - REQUEST_MOVE 审批  -> world.pending_moves[a] = new_place_id（WorldStep 步骤 9 串行结算时再调 places.move + 触发 BehaviorCompressor.on_move）
```

### 4.3 与其他模块的交互

- **上游调用方**：
  - `WorldStep`（持有整个 WorldState，每个 micro-tick 步骤都读写）
  - `PerceptionBuilder.build(agent, world, t)`（只读，详见 `perception.md`）
  - `ActionDispatcher.dispatch(agent, action, world, t)`（读 + 写多个子段）
  - `ScriptEngine.apply(effect, world)`（写 places / relations / capabilities / agents.current_state）
  - `BehaviorCompressor.on_move(agent_id, old_place, new_place)`（读 `agents[a]` 引用与 `memory.segment`）
- **下游被调方**：本模块基本不主动调任何东西；它是被动数据容器。`load_world` 启动期会调 `WorldDB.fetch_*` 与 `MultiPoolPlatformManager.build`。
- **共享状态**：
  - 读：`world.db` 12 张表（通过 PlaceStore / RelationGraph / CapabilityTable 间接读，启动时全量加载到内存）；`pool_*.db` 通过 `pools.platform_for(p,f)` 间接读。
  - 写：`world.db.{relation, capability, agent_location}` 通过对应子段写；`world.db.{direct_message, overhear, group_*}` 由 Bus 持 `delivery_lock` 写；`pool_*.db.follow` 由 `RelationGraph.on_change` 投影写。
  - Zep：`agents[a]` 与 `memory.multi_graph_manager` 联动（一对一映射 graph_id = `agent_{a}`）。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from dataclasses import dataclass
from typing import Dict, Optional
import asyncio

@dataclass
class AgentRuntime:
    agent_id: int
    soul: str
    long_term_goal: str
    current_state: str
    last_message_seen_at: int = 0


class WorldState:
    # 七元组 + 辅助字段
    places: "PlaceStore"
    relations: "RelationGraph"
    capabilities: "CapabilityTable"
    pools: "MultiPoolPlatformManager"
    connectivity: "ConnectivityResolver"
    agents: Dict[int, AgentRuntime]
    memory: "MemoryHub"
    script: "ScriptEngine"
    clock: "Clock"
    world_db: "WorldDB"
    pending_moves: Dict[int, str]
    delivery_lock: asyncio.Lock

    def __init__(
        self,
        *,
        places: "PlaceStore",
        relations: "RelationGraph",
        capabilities: "CapabilityTable",
        pools: "MultiPoolPlatformManager",
        connectivity: "ConnectivityResolver",
        agents: Dict[int, AgentRuntime],
        memory: "MemoryHub",
        script: "ScriptEngine",
        clock: "Clock",
        world_db: "WorldDB",
    ) -> None: ...

    @property
    def t(self) -> int: ...                                  # 委托给 self.clock.t

    # 便捷只读访问（dispatcher / perception 用）
    def agent(self, agent_id: int) -> AgentRuntime: ...
    def location_of(self, agent_id: int) -> str: ...         # 委托 places.L_t

    # 便捷写（lockstep 队列）
    def queue_move(self, agent_id: int, new_place_id: str) -> None: ...

    # 启动装配
    @classmethod
    async def load(
        cls,
        *,
        config: "SimulationConfig",
        world_db: "WorldDB",
        pool_factory: "PlatformFactory",
        clock: "Clock",
    ) -> "WorldState": ...
```

### 5.2 IPC / Flask / SQL

- 不直接暴露 IPC / Flask 路由（这些路由的 handler 持 WorldState 引用，但路由声明在 `app/api/`）。
- SQL：本模块不写裸 SQL；所有读写走子段或 `world.world_db.execute()`。

## 6. 配置入口

从 `simulation_config.json` 读取（LAYOUT §7.1）：
- `agent_configs[*].soul / long_term_goal / current_state` → 注入 `AgentRuntime`（profile_generator 负责生成 3 段）
- `agent_configs[*].location` → 启动写入 `world.db.agent_location` → PlaceStore.load 时回读
- `agent_configs[*].relations / capabilities` → 启动写入 `world.db.{relation, capability}` → 子段 load 时回读
- `world_config.places / coverage` → PlaceStore / ConnectivityResolver.load
- `world_graphs.{world, per_agent_template, per_place_template}` → MemoryHub.multi_graph_manager 注册

**默认值**：
- `AgentRuntime.last_message_seen_at = 0`
- `pending_moves = {}`
- 若 profile 缺 `current_state`，profile_generator 会写 `"(initial)"`，不抛错。

**验证规则**：
- 所有 `agent_configs[*].location` 必须在 `world_config.places.place_id` 集合中；否则启动 fail-fast。
- `agent_configs[*].relations[*].dst` 必须存在于 `agent_configs` 中。
- `capabilities` 中的能力名必须在 `CapabilityTypeRegistrar` 注册过（LAYOUT §10.7）。

## 7. 待决策 / 风险

- **N2**（LAYOUT §9.5.1）：`UPDATE_STATE` 滥用治理。`agents[a].current_state` 是无锁单 owner 写入，但若 Script 与 Agent 同 micro-tick 内争写需要语义裁定——MVP 假设两者不会同 t 写同一 agent；若出现，Script 走 effect 步骤（步骤 2，轮初）后于 agent 写（步骤 7 micro-tick），即剧本被 agent 自身 UPDATE_STATE 覆盖。后续需要决议是否允许 Script 后置覆盖。
- **N5**（LAYOUT §9.5.1）：`AgentRuntime.last_message_seen_at` 没有持久化字段，crash 后重启会重读所有 `delivered=1` 消息。MVP 接受（仅影响 obs 多塞历史消息一次）；正式版可在 world.db 加一张 `agent_runtime_state` 表。
- **风险**：WorldState 是巨型聚合根，单元测试时需要 mock 所有 7 个子段。建议测试用 `WorldState.load(...)` 接受可选 stub 参数。
- **风险**：`agents` 字段由 WorldState 拥有但又被 `BehaviorCompressor` / `MemoryHub` 间接引用，循环引用需要 `weakref` 或显式 `__del__` 清理（MVP 不强求，进程退出即释放）。
