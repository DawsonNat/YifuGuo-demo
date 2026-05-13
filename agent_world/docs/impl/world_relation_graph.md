# RelationGraph 实现文档

> 路径: `agent_world/world/relation_graph.py`（+ `agent_world/world/relation_types/*.py`）
> 对应 LAYOUT §: §2.A RelationGraph / §3.2 relation 表 / §3.5 follow 双轨投影 / §9.5 C1 决议 / §10.1 + §10.7 RelationTypeRegistrar
> 上游依赖文档: `world_registrars.md`（RelationTypeRegistrar 的 Base 类与 Meta 用法）
> 下游依赖文档: `world_connectivity.md`（消费 `contacts_of(a)` 算 φ_RDC）、`world_capability_table.md`（部分 hook 可联动 capability）

## 1. 模块定位

RelationGraph 是 WorldState 七元组中 $R_t$ 段的内存容器与唯一写口。它把 `world.db.relation` 在启动期 bulk load 到内存，运行期对外暴露：(a) 多类型有向边的增删；(b) `contacts_of(agent) → Iterable[(other, type)]` 的高效查询；(c) `on_change(src, dst, type, op)` 钩子链——其中**最关键**的内置钩子是把 `mutual_follow / follower` 类边**同步**投影到 pool_*.db.follow 表（§3.5 双轨方案）。

relation_type 通过 conscribe 注册（C1 决议）：每种类型是一个继承 `RelationBase` 的类，类级元数据 + 可选 hooks 决定该类型边的语义（对称性、是否参与"contacts"集合、是否投影到 pool follow、互斥集合、自然语言模板、on_create / on_break 钩子）。

输入：启动 bulk load + ScriptEngine 的 `RelationChangeEffect` + agent 的 `RELATION_CHANGE` action（FOLLOW/UNFOLLOW 也走这条路）。
输出：内存图增量、写 `world.db.relation`、调用 hooks（其中默认 hook 写 pool_*.db.follow）。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 多边内存图模式 | OASIS | `oasis/social_agent/agent_graph.py:25-292` | PATTERN | 仅借鉴"启动 bulk load + 双向索引 + 同步落 DB"，OASIS 是单类型 follow，不够用 |
| follow 表写入语义 | OASIS（fork 后） | `oasis/social_platform/schema/follow.sql` + `platform.py` follow handler | EDIT（在 fork 内已修） | RelationGraph.on_change 的默认钩子直接写该 SQL；fork 不动 follow 表自身 |
| relation_type 注册 | conscribe 1.1.1 | LAYOUT §10.1 / §10.7 | NEW（Path A metaclass 继承） | `RelationBase`(metaclass=RelationTypeRegistrar.Meta)；strip_suffixes=["Relation"]；MVP 8 种初始类型 |
| 双轨同步策略 | LAYOUT §3.5 | A2 决议 | NEW | 同步写、放弃跨 DB 原子性；启动期全量重建 pool follow（drop & insert） |

## 3. 关键改动 (相对来源仓库)

全新写。OASIS `agent_graph` 只支持单类型 follow/mute，无法承载 LAYOUT 要求的 8+ 类多边语义、对称自动双写、互斥集合、跨 DB 投影。设计要点：

- **类型即类**：每种 relation_type 是一个 conscribe 注册的类，元数据通过类属性声明（不放配置文件），运行期由 `RelationTypeRegistrar` 解析。这样 8 种 MVP 类型 + 用户后续扩展走同一通路。
- **对称双写**：类元数据 `symmetric=True` 时，`add(a, b, T)` 在内存与 DB 同时写两条边 `(a, b, T)` 与 `(b, a, T)`；break 同理双删。symmetric=False 走单边。
- **互斥抛错**（C1 严格语义）：类元数据 `mutually_exclusive: tuple[str, ...]` 列出与本类不能共存的其他类型；写入前若发现 `(src, dst, mut)` 已存在则**抛 RelationConflict**（不 silent，让调用方决定回滚），由 ActionDispatcher 把异常转为该次 action 的 silent 失败（写 `direct_message(delivered=0)` 不适用，因为这是关系不是消息——但需走 LAYOUT §9.5 #4 的"其他 silent"分支）。
- **on_change 默认钩子**：内置 `_pool_follow_projection_hook(src, dst, type, op)`——若 `relation_type.project_to_pool=True` 且双方都持 `account_<feed>` capability，则对每个匹配的 pool 同步 `INSERT OR IGNORE / DELETE FROM follow`。

## 4. 核心逻辑

### 4.1 数据结构

```
RelationEdge:
    src_agent: int
    dst_agent: int
    relation_type: str           # 必须命中 RelationTypeRegistrar 的 key
    created_at: int              # world.t
    expires_at: int | None       # 可选时限关系（如 ex_lover 不过期，但保留字段）
    metadata: dict | None        # 自定义（剧本可塞 reason 等）

# 内存索引（双向 + 按类型）
RelationGraph:
    edges:            Dict[(src, dst, type), RelationEdge]
    out_by_agent:     Dict[agent, Dict[type, Set[other]]]   # contacts_of 主路径
    in_by_agent:      Dict[agent, Dict[type, Set[other]]]   # 反向（如查"谁 follow 我"）
    on_change_hooks:  list[Callable[(src,dst,type,op,world,t), Awaitable]]
    type_meta:        Dict[type, RelationTypeMeta]          # 由 Registrar 启动时填

RelationTypeMeta:                # 解析自 RelationBase 子类的类属性
    name: str                    # snake_case key（如 "mutual_follow"）
    symmetric: bool              # True 时自动双写
    is_contact: bool             # True 时进入 contacts_of(a) 输出（默认 True）
    project_to_pool: bool        # True 时触发默认 follow 投影 hook
    mutually_exclusive: tuple[str, ...]   # 禁止共存的类型 key 列表
    display_template: str        # 自然语言模板（"{src} is in love with {dst}"），供 PerceptionBuilder / Zep translator 用
    on_create: Callable | None
    on_break:  Callable | None
```

**MVP 8 种初始类型**（落在 `agent_world/world/relation_types/*.py`）：

| 类名 | key | symmetric | is_contact | project_to_pool | mutually_exclusive | 备注 |
|---|---|---|---|---|---|---|
| `MutualFollowRelation` | mutual_follow | True | True | True | () | 投影到双向 follow（A→B 与 B→A 都写 pool follow） |
| `FollowerRelation` | follower | False | True | True | () | 单向：src 关注 dst；只投 src→dst 一条 pool follow |
| `FriendRelation` | friend | True | True | False | () | 不投 pool；prompt 提示用 |
| `LoverRelation` | lover | True | True | False | ("ex_lover",) | 与 ex_lover 互斥 |
| `ExLoverRelation` | ex_lover | True | True | False | ("lover",) | 与 lover 互斥 |
| `FamilyRelation` | family | True | True | False | () | 血缘/姻亲，不细分；扩展点 |
| `ColleagueRelation` | colleague | True | True | False | () | 同组织 |
| `MuteRelation` | mute | False | False | True\* | () | is_contact=False（mute 不算"联系人"）；project_to_pool 投到 pool_*.db.mute（注：这是 mute 表不是 follow 表，由专用钩子处理） |

\* MuteRelation 实际投影目标是 `mute` 表而非 follow 表，钩子分发逻辑在默认投影 hook 内按 `relation_type` 分流。

不变量：
1. `edges[(s,d,T)]` 存在 ⇔ `d ∈ out_by_agent[s][T]` 且 `s ∈ in_by_agent[d][T]`。
2. 若 `type_meta[T].symmetric` 则 `(s,d,T)` 存在 ⇔ `(d,s,T)` 存在。
3. 互斥集合检查在写入前完成；命中即 `raise RelationConflict`（不静默降级）。
4. `relation_type` 必须在 `RelationTypeRegistrar` 中可解析；启动期未知类型 fail-fast。
5. `on_change_hooks` 调用顺序与注册顺序一致；任一 hook 抛错**仅 log warn**（不阻断主写——因为内存与 world.db 已落，跨 DB 投影失败不该回滚 world.db；启动期可全量重建兜底）。

### 4.2 关键流程 / 算法

**conscribe 注册模式（Path A metaclass 继承）**：

```python
# agent_world/world/_registrars.py
RelationTypeRegistrar = create_registrar(
    "relation_type",
    RelationTypeProtocol,       # 仅约定 meta 字段
    discriminator_field="type",
    strip_suffixes=["Relation"],
)

class RelationBase(metaclass=RelationTypeRegistrar.Meta):
    __abstract__ = True
    # 子类通过类属性声明元数据：
    symmetric: bool = False
    is_contact: bool = True
    project_to_pool: bool = False
    mutually_exclusive: tuple[str, ...] = ()
    display_template: str = "{src} → {dst}"
    # 可选 hooks：
    async def on_create(self, edge, world, t): ...
    async def on_break(self,  edge, world, t): ...
```

每个 `agent_world/world/relation_types/lover.py` 子类继承 `RelationBase` 即自动注册；启动时 `conscribe.discover("agent_world.world.relation_types")` 触发 import。

**写入流程 `add(src, dst, type, *, world, t, metadata=None)`**：

```
1. meta = type_meta[type]   # 不存在 → KeyError fail
2. # 互斥检查
   for mut in meta.mutually_exclusive:
       if (src, dst, mut) in edges or (meta.symmetric and (dst, src, mut) in edges):
           raise RelationConflict(src, dst, type, mut)
3. # 已存在则 noop（幂等）
   if (src, dst, type) in edges: return
4. # 写主边
   edge = RelationEdge(src, dst, type, t, ...)
   edges[(src,dst,type)] = edge
   out_by_agent[src][type].add(dst)
   in_by_agent[dst][type].add(src)
   INSERT INTO world.db.relation(...)
5. # 对称双写
   if meta.symmetric and src != dst:
       同步生成 (dst, src, type) 的 edge + 索引 + INSERT
6. # 触发 hooks（含类型自身 on_create 与全局 on_change_hooks）
   if meta.on_create: await meta.on_create(edge, world, t)
   for h in on_change_hooks: await h(src, dst, type, "add", world, t)
       # 默认包含 _pool_follow_projection_hook
```

**删除流程 `remove(src, dst, type, *, world, t)`**：与 add 镜像；symmetric 时双删；触发 `on_break` + `on_change_hooks` 的 `op="remove"` 调用。

**`contacts_of(agent) -> Iterable[(other, type)]`**：
```
for type, others in out_by_agent[agent].items():
    if not type_meta[type].is_contact: continue   # mute 不算联系人
    for o in others: yield (o, type)
```

**默认 pool follow 投影 hook（启动 wiring）**：

```
async def _pool_follow_projection_hook(src, dst, type, op, world, t):
    meta = world.relations.type_meta[type]
    if not meta.project_to_pool: return
    target_table = "mute" if type == "mute" else "follow"
    for feed in world.pools.feeds_for_both(src, dst):
        # feeds_for_both: 双方都持 account_<feed> 才投影
        async with world.pools.pool_for(feed).db_lock:
            if op == "add":    INSERT OR IGNORE INTO {target_table}(...)
            else:              DELETE FROM {target_table} WHERE follower_id=? AND followee_id=?
        # symmetric 类型由 RelationGraph 自动两次调用本 hook（src↔dst 各一次），无需在此特判
```

**启动期全量重建 pool follow（A2 兜底）**：runner 启动顺序中（`run_agent_world_simulation.py` 早段）：
```
for pool in pools:
    await pool.db.execute("DELETE FROM follow")
for (s, d, T), _ in relation_graph.edges:
    if type_meta[T].project_to_pool:
        await _pool_follow_projection_hook(s, d, T, "add", world, t=0)
```

### 4.3 与其他模块的交互

- 上游调用方:
  - `WorldStep` 启动序列（bulk load + 全量重建 pool follow）
  - `ScriptEngine` 的 `RelationChangeEffect`
  - `ActionDispatcher`（agent action: RELATION_CHANGE / FOLLOW / UNFOLLOW，FOLLOW/UNFOLLOW 内部转译为 `add/remove(src, dst, "follower")` 或 `mutual_follow`）
  - `PerceptionBuilder.build`（读 `contacts_of(a)` + `display_template` 拼 `obs.contacts`）
  - `ConnectivityResolver.φ_RDC`（读 `contacts_of(a)` 判 RDC 可达性）
- 下游被调方:
  - `WorldDB.execute`（`relation` 表 INSERT/DELETE）
  - `MultiPoolPlatformManager.pool_for(feed).db`（默认 hook 同步写 pool_*.db.follow / mute）
  - `CapabilityTable.has(a, "account_<feed>")`（projection hook 内判定）
- 共享状态: 写 `world.db.relation`；通过默认 hook 写 `pool_*.db.follow / mute`（跨 DB 不原子，已接受）；不直接写 Zep（如需关系变化进 graph，由 translator 在 action 翻译阶段处理）。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class RelationGraph:
    def __init__(self, world_db: WorldDB) -> None: ...
    async def load_all(self) -> None: ...

    # 读
    def has(self, src: int, dst: int, type: str) -> bool: ...
    def contacts_of(self, agent: int) -> Iterable[tuple[int, str]]: ...
    def edges_of(self, agent: int, type: str | None = None) -> Iterable[RelationEdge]: ...
    def followers_of(self, agent: int) -> Iterable[int]: ...   # in_by_agent + project_to_pool 类型
    def type_meta(self, type: str) -> RelationTypeMeta: ...

    # 写（唯一变更入口）
    async def add(self, src: int, dst: int, type: str, *, world, t: int, metadata: dict | None = None) -> None: ...
    async def remove(self, src: int, dst: int, type: str, *, world, t: int) -> None: ...

    # 钩子注册
    def register_on_change(self, cb) -> None: ...

class RelationConflict(Exception):
    """互斥关系冲突；ActionDispatcher 捕获后走 silent 失败路径。"""

class RelationBase(metaclass=RelationTypeRegistrar.Meta):
    __abstract__ = True
    symmetric: bool
    is_contact: bool
    project_to_pool: bool
    mutually_exclusive: tuple[str, ...]
    display_template: str
    async def on_create(self, edge: RelationEdge, world, t: int) -> None: ...
    async def on_break (self, edge: RelationEdge, world, t: int) -> None: ...
```

### 5.2 IPC / Flask / SQL (如适用)

- IPC: 无专属命令；剧本注入走 `INJECT_SCRIPT_EVENT`（其 effect 可为 RelationChange）。
- Flask: 无专属路由；UI 查询通过 `GET /simulations/<id>/world-state` 包含 relation 段。
- SQL:
  - 启动: `SELECT * FROM relation`。
  - 运行: `INSERT INTO relation(src_agent, dst_agent, relation_type, created_at, expires_at, metadata) VALUES (...)`；`DELETE FROM relation WHERE src_agent=? AND dst_agent=? AND relation_type=?`。
  - Pool 投影: `INSERT OR IGNORE INTO follow(...)` / `DELETE FROM follow WHERE follower_id=? AND followee_id=?`（同样语义用于 `mute` 表）。

## 6. 配置入口

从 `simulation_config.json` 不直接读 relation 列表（关系初始态由 `agent_configs[i].relations` 字段或 `world_config.events` 中的初始 RelationChange effect 提供）。验证规则：
- `relation_type` 必须在 RelationTypeRegistrar 注册表中（fail-fast）。
- 互斥配置在初始态批量加载阶段同样校验；冲突 fail-fast（启动期不允许 silent 降级）。
- `RelationTypeRegistrar` 自身的发现路径在启动器固定为 `agent_world.world.relation_types`；用户扩展可在剧本配置 `world_config.relation_type_packages: list[str]` 追加 discover 包。

## 7. 待决策 / 风险

- 跨 DB 不原子（§9.6 C 已接受）：world.db.relation 写成功但 pool follow 投影 hook 抛错时，启动期全量重建兜底；MVP 不做补偿队列。
- N5 + §9.5 #8（百万 agent scale）：`out_by_agent` 与 `in_by_agent` 内存占用未压测；MVP 不优化。
- C1 互斥语义"抛错"的调用方处理：ActionDispatcher 必须捕 `RelationConflict` 并走 silent 路径（写 `direct_message(delivered=0)` 不适用——本身不是消息），因此 `Observation.recent_failed_attempts` 当前 schema 不能完整承载 RelationConflict 的反馈；MVP 暂用 trace 写一条系统级失败行 + log warn，UI 后期再补。
- on_change_hooks 顺序与异常隔离：默认 hook 抛错只 log，不回滚已落的 world.db.relation——这是有意设计（一致性兜底交给启动重建），但可能让 pool follow 短暂脱漏；§3.5 已声明可接受。
- relation 表 `metadata` JSON 字段尚未在 LAYOUT §3.2 显式列出；本 doc 建议加入（用于存放 reason 等剧本注入参数），需在 `schema/world/relation.sql` 补字段。
