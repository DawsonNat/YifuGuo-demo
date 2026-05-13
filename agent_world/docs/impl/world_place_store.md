# PlaceStore 实现文档

> 路径: `agent_world/world/place_store.py`（+ `agent_world/world/place_types/*.py`）
> 对应 LAYOUT §: §2.A PlaceStore / §3.2 place + agent_location 表 / §3.5 follow 双轨触发源 / §10.7 PlaceTypeRegistrar
> 上游依赖文档: `world_registrars.md`（PlaceTypeRegistrar 的 Base 类来源）
> 下游依赖文档: `world_connectivity.md`（消费 `agents_at` 反向索引、coverage 矩阵）、`world_relation_graph.md`（共享 on_change 投影时机参考）、`world_capability_table.md`（联动 on_enter / on_leave 钩子）

## 1. 模块定位

PlaceStore 是 WorldState 七元组中 $P + L_t$ 段的内存容器与权威读写口。它把 `world.db.place / coverage / agent_location` 三张表在启动期一次性加载到内存，运行期对外暴露：(a) "agent → place" 与 "place → set(agent)" 的双向索引；(b) place 静态属性查询（含 `attrs` JSON）；(c) coverage 邻接矩阵；(d) `move(agent, new_place)` 这唯一的状态变更入口（同时写 DB 与触发钩子）。

输入：启动时全量加载的 SQLite 行 + 运行期由 ActionDispatcher 在 MOVE 结算阶段、ScriptEngine 通过 `MoveEffect` / `PlaceMutationEffect` 发起的写请求。
输出：`L_t(agent_id) → place_id`、`agents_at(place_id) → Set[agent]`、`attrs(place_id) → dict`、`coverage(src, dst) → CoverageEdge | None`。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 内存图容器骨架 | OASIS | `oasis/social_agent/agent_graph.py:25-292` | PATTERN | 仅参考 igraph 内存图模式（双向索引 + 启动期 bulk load + 运行期增量改），不引用代码 |
| `create_db()` schema 加载 | OASIS（fork） | `oasis/social_platform/database.py:84-201` | PATTERN | place / agent_location / coverage 三张 .sql 由 WorldDB.executescript 加载，不在本模块完成 |
| place_types 注册层 | conscribe 1.1.1 | LAYOUT §10.1 / §10.7 | NEW（依赖 `_registrars.py`） | metaclass Path A；`PlaceBase`(metaclass=PlaceTypeRegistrar.Meta)；strip_suffixes=["Place"]；MVP 类型可极简（如 `BarPlace / StreetPlace / ServerPlace`），主要承担"behavior_hint 默认值 + on_enter/on_leave 钩子"声明 |

## 3. 关键改动 (相对来源仓库)

- 全新写。设计灵感取自 OASIS `agent_graph` 的"启动 bulk load + 运行期纯内存读写 + 同步落 DB"三件套，但 OASIS 的图节点是 agent，本模块的图节点是 place，两者拓扑性质不同（place 数 ≤ 数百，coverage 是稠密的二维 dict 而非边表）。
- 不复用 OASIS `igraph`：MVP place 数量级很小（数十到数百），手写 `Dict[place_id, PlaceRecord]` + `Dict[agent_id, place_id]` + 反向 `Dict[place_id, Set[agent_id]]` 即可，避免 igraph 启动开销。
- 引入 conscribe `PlaceTypeRegistrar`：让 place 的语义不只是 attrs JSON，而是可挂"类型级钩子"（例如 `BarPlace.on_enter(agent)` 自动 grant `account_drinks` capability）；MVP 不强制每个 place 走类型，未声明 `place_type` 字段者按裸 attrs 处理。

## 4. 核心逻辑

### 4.1 数据结构

```
PlaceRecord:
    place_id: str                       # 主键，与 world.db.place.place_id 一致
    parent_id: str | None                # 层级（attrs 之外的硬字段）
    place_type: str | None               # 命中 PlaceTypeRegistrar 的 key，如 "bar"；可空
    attrs: dict                          # JSON 反序列化结果，约定字段:
                                         #   timezone: str  (IANA 名，仅叙事，不影响 tick)
                                         #   behavior_hint: str | None  (B5 第 4 段 prompt)
                                         #   capacity: int | None       (可选，MOVE 结算用)
                                         #   ... 其余用户自定义
    capacity: int | None                 # 从 attrs 提取，方便 MOVE 校验

CoverageEdge:
    src: str
    dst: str
    latency_ticks: int                   # 默认 0（同地点 F2F）；跨星球可大
    channels: set[str]                   # {"F2F", "RDC", "GRP"} 子集；缺省全开

# 顶层容器
PlaceStore:
    places:    Dict[place_id, PlaceRecord]
    coverage:  Dict[(src, dst), CoverageEdge]
    L:         Dict[agent_id, place_id]            # 即 L_t（单值，agent 始终在唯一地点）
    agents_at: Dict[place_id, Set[agent_id]]       # 反向索引，O(1) 同地点查询
    on_enter_hooks: list[Callable[(agent, place, world), Awaitable[None]]]
    on_leave_hooks: list[Callable[(agent, place, world), Awaitable[None]]]
```

不变量：
1. `agents_at` 与 `L` 必须互为反向：`L[a] == p ⇔ a ∈ agents_at[p]`，每次 `move` 必须**原子**修改两边。
2. 每个 agent 在任一时刻**只**位于一个 place（`len({p | a in agents_at[p]}) <= 1`）。
3. `coverage[(p, p)]` 默认存在且 `latency_ticks=0, channels={F2F,RDC,GRP}`（同地点零延迟）。
4. `place_type` 若非空必须能在 `PlaceTypeRegistrar` 中解析；启动期校验失败即 fail-fast。
5. attrs 的 `timezone` 字段仅供叙事 prompt 使用，**不**进入 tick / channel_delay 计算。

### 4.2 关键流程 / 算法

**启动期 `load_all(world_db)`**：
```
1. SELECT * FROM place           → 构造 PlaceRecord（attrs JSON.loads）
2. SELECT * FROM coverage        → 构造 CoverageEdge；缺自环则补默认
3. SELECT * FROM agent_location  → 填 L 与 agents_at（反向索引同步建）
4. for p in places:
     if p.place_type: PlaceTypeRegistrar.resolve(p.place_type)  # fail-fast 校验
5. for type_cls in PlaceTypeRegistrar.iter_classes():
     append type_cls.on_enter / on_leave 到 hooks 列表
```

**运行期 `move(agent, new_place, world, t)`**（唯一状态变更入口）：
```
1. old_place = L[agent]
2. if old_place == new_place: return                # noop
3. if capacity 检查不通过: raise CapacityFull       # 由 dispatcher 转 silent
4. # 触发离开钩子（含类型钩子）
   for hook in on_leave_hooks: await hook(agent, old_place, world)
5. # 原子改两边索引
   agents_at[old_place].remove(agent)
   L[agent] = new_place
   agents_at[new_place].add(agent)
6. # 同步落 world.db.agent_location（UPDATE WHERE agent_id=?）
7. for hook in on_enter_hooks: await hook(agent, new_place, world)
```

**注**：BehaviorCompressor.on_move 由 ActionDispatcher 在调 `place_store.move` **之前** hook（LAYOUT §6.1 步骤 9 + dispatcher 行），不属于 PlaceStore 自身；本模块只触发 on_enter / on_leave（capability 联动等）。

**`PlaceMutation` effect 路径**：
```
ScriptEngine.apply(PlaceMutationEffect(place_id, attrs_patch))
  → place_store.mutate_attrs(place_id, patch)
    → 内存 PlaceRecord.attrs.update(patch)
    → UPDATE place SET attrs=? WHERE place_id=?
    → 不触发 on_enter/on_leave（属性变更不算位置变更）
```

### 4.3 与其他模块的交互

- 上游调用方:
  - `WorldStep`（每轮经 `agents_at` 分组活跃 agent 做 micro-tick）
  - `PerceptionBuilder.build`（读 `L[a]`、`attrs(p)`、`agents_at(p)` 拼 `obs.self_location / location_attrs / co_located_agents`）
  - `ActionDispatcher`（MOVE 结算阶段 `await place_store.move(...)`）
  - `ScriptEngine`（`MoveEffect` / `PlaceMutationEffect`）
  - `ConnectivityResolver`（读 `coverage` 矩阵 + `agents_at` 做 φ 谓词）
  - Flask `LIST_PLACES` / `MOVE_AGENT` IPC handler（runner 侧）
- 下游被调方:
  - `world.world_db`（持久化 UPDATE / SELECT，单写者由 WorldStep 持的 `asyncio.Lock` 保护）
  - `CapabilityTable.grant / revoke`（在 BarPlace.on_enter 这类 PlaceType 钩子内）
- 共享状态: 读写 `world.db.{place, coverage, agent_location}` 三表；不读 pool_*.db；不直接写 Zep（`place_{id}` graph 由 retrieval 层在 on_enter 灌注，本模块只发出钩子）。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class PlaceStore:
    def __init__(self, world_db: WorldDB) -> None: ...
    async def load_all(self) -> None: ...

    # 读路径（纯内存，无锁）
    def L_t(self, agent_id: int) -> str | None: ...
    def attrs(self, place_id: str) -> dict: ...
    def record(self, place_id: str) -> PlaceRecord: ...
    def agents_at(self, place_id: str) -> frozenset[int]: ...
    def all_places(self) -> Iterable[PlaceRecord]: ...
    def coverage(self, src: str, dst: str) -> CoverageEdge | None: ...
    def latency(self, src: str, dst: str, channel: str) -> int: ...

    # 写路径（持锁，内存 + DB 同步）
    async def move(self, agent_id: int, new_place: str, *, world, t: int) -> None: ...
    async def mutate_attrs(self, place_id: str, patch: dict) -> None: ...
    async def add_place(self, record: PlaceRecord) -> None: ...        # PlaceMutation 用
    async def add_coverage(self, edge: CoverageEdge) -> None: ...

    # 钩子（PlaceTypeRegistrar 注册的类型钩子在 load_all 末尾自动 append）
    def register_on_enter(self, cb) -> None: ...
    def register_on_leave(self, cb) -> None: ...

class PlaceBase(metaclass=PlaceTypeRegistrar.Meta):
    """每个具体 place type 的基类；子类覆盖 on_enter / on_leave 即可。"""
    __abstract__ = True
    async def on_enter(self, agent_id: int, place: PlaceRecord, world) -> None: ...
    async def on_leave(self, agent_id: int, place: PlaceRecord, world) -> None: ...
```

### 5.2 IPC / Flask / SQL (如适用)

- IPC: `LIST_PLACES`（无参；返回所有 PlaceRecord 概要给 UI）；`MOVE_AGENT(agent_id, place_id)`（runner 侧 handler 直接调 `place_store.move`）。
- Flask: `GET /simulations/<id>/places`、`POST /simulations/<id>/move-agent`（透传到 IPC）。
- SQL:
  - 启动: `SELECT * FROM place / coverage / agent_location`。
  - 运行: `UPDATE agent_location SET place_id=?, updated_at=? WHERE agent_id=?`；`UPDATE place SET attrs=? WHERE place_id=?`；`INSERT OR IGNORE INTO place(...)` / `INSERT OR IGNORE INTO coverage(...)`。

## 6. 配置入口

从 `simulation_config.json.world_config.places` 读初始 place 列表（每条含 `place_id / parent_id / place_type / attrs`），从 `world_config.coverage` 读邻接矩阵。验证规则：
- `attrs.timezone` 必须是合法 IANA 名（解析失败仅 warn，不 fail-fast）。
- `attrs.behavior_hint` 可空；空时 PerceptionBuilder 输出 `(none)`。
- `place_type` 若给定必须在 `PlaceTypeRegistrar` 中存在（fail-fast）。
- `coverage[(p, p)]` 缺失时自动补 `latency_ticks=0, channels={F2F,RDC,GRP}`。
- 不存在 `agent_configs[i].location` 指向的 place_id → fail-fast。

## 7. 待决策 / 风险

- LAYOUT §9.5 #8（100w agent scale）：`agents_at` 反向索引在百万级 agent 下的内存与 mutation 成本未压测；MVP 不优化。
- N5（arrive_at 兼容）：本模块不直接持有 arrive_at，但 MOVE 在 §6.1 步骤 9 统一结算意味着 `agents_at` 在 micro-tick 中**保持不变**；这点须由 WorldStep 保证（PlaceStore 自身不阻止 micro-tick 内调 `move`，但调用方不应在 micro-tick 阶段调）。
- PlaceType 与 attrs 的交叠：MVP 允许两条路径同时存在（裸 attrs 配置 + PlaceType 类挂钩子），运行期没有冲突；但若两者都设置同字段（如 attrs.behavior_hint vs PlaceType 类默认），优先级未定义——MVP 约定**实例 attrs 覆盖类型默认**。
- 跨 DB 原子性已在 §9.6 C 接受：本模块 `move` 写完 world.db 即返回，不等 RelationGraph.on_change 引发的 pool_*.db.follow 投影完成。
