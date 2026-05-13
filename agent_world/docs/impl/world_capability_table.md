# CapabilityTable 实现文档

> 路径: `agent_world/world/capability_table.py`（+ `agent_world/world/capability_types/*.py`）
> 对应 LAYOUT §: §2.A CapabilityTable / §3.2 capability 表 / §10.7 CapabilityTypeRegistrar
> 上游依赖文档: `world_registrars.md`（CapabilityTypeRegistrar 的 Base 类与 Meta 用法）
> 下游依赖文档: `world_connectivity.md`（消费 `agents_with(cap)` 反向索引，做 φ_RDC / φ_FEED 资格判定）

## 1. 模块定位

CapabilityTable 是 WorldState 七元组中 $C_t$ 段的内存容器与权威读写口。Capability 是"agent 能不能干某事"的离散能力位（如 `account_twitter` / `account_reddit` / `signal_uplink` / `wallet_eth` / `phys_drive`）。它把 `world.db.capability` 在启动期 bulk load 到内存，运行期对外暴露：(a) `has(agent, cap)`；(b) `agents_with(cap) → Set[agent]` 反向索引；(c) `grant / revoke`（同步落 DB，写 `granted_at` / `revoked_at` 不删历史行）；(d) `on_change` 钩子（capability 联动 dynamic_tools 与 PlaceStore.on_enter/on_leave）。

输入：启动 bulk load + ScriptEngine 的 `CapabilityChangeEffect` + agent action `CAPABILITY_CHANGE`（受限：通常只剧本能改）+ PlaceStore 的 `on_enter` 钩子（如 `BarPlace.on_enter` grant `account_drinks`）。
输出：`has` / `agents_with` 内存查询；写 `world.db.capability`；调 dynamic_tools 重算。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 反向索引内存图模式 | OASIS | `oasis/social_agent/agent_graph.py:25-292` | PATTERN | 仅模式参考 |
| (granted_at, revoked_at) 历史保留模式 | LAYOUT | §3.2 capability 行 | NEW | 不 DELETE，保留审计；当前态 = `granted_at IS NOT NULL AND revoked_at IS NULL` |
| capability_types 注册层 | conscribe 1.1.1 | LAYOUT §10.1 / §10.7 | NEW（依赖 `_registrars.py`） | metaclass Path A；`CapabilityBase`(metaclass=CapabilityTypeRegistrar.Meta)；strip_suffixes=["Capability"] |

## 3. 关键改动 (相对来源仓库)

全新写。设计要点：

- **当前态由"半开区间"决定**：每条 `(agent, capability)` 在 DB 中可有多行，每行 `granted_at INTEGER NOT NULL` + `revoked_at INTEGER NULL`。当前态 = 存在至少一行 `revoked_at IS NULL`。grant 重复时不开新行（幂等）；revoke 写 `UPDATE ... SET revoked_at=? WHERE granted_at=? AND revoked_at IS NULL`。
- **反向索引必备**：`agents_with(cap)` 是 ConnectivityResolver 算 φ_RDC / φ_FEED 的高频路径（百万 agent 不能扫表），MVP 直接维护 `Dict[capability, Set[agent]]`。
- **capability_types 注册层**（C3 列入 conscribe 5 项）：每种 capability 是一个继承 `CapabilityBase` 的类，类元数据声明该 capability 的 `display_name` / `requires_place_type`（可选：仅在某类 place 才能持有）/ `auto_revoke_on_leave`（离开特定 place 时自动 revoke，如 `account_drinks` 离开 BarPlace 时撤回）/ on_grant / on_revoke 钩子。MVP 主要类型走 Tier 1 元数据即可，钩子可后期补。

## 4. 核心逻辑

### 4.1 数据结构

```
CapabilityRecord:
    agent_id: int
    capability: str               # 命中 CapabilityTypeRegistrar 的 key（不强制，MVP 允许裸字符串）
    granted_at: int               # world.t
    revoked_at: int | None        # None 表示当前持有
    metadata: dict | None

CapabilityTable:
    # 当前态视图（当前持有者 only）
    by_agent:       Dict[agent, Set[capability]]
    by_capability:  Dict[capability, Set[agent]]      # 反向索引（agents_with 主路径）
    # 历史（含已 revoke）
    history:        list[CapabilityRecord]            # append-only；DB 是真相，内存仅缓存当前态需要的快照
    type_meta:      Dict[capability, CapabilityTypeMeta]
    on_change_hooks: list[Callable[(agent, cap, op, world, t), Awaitable]]

CapabilityTypeMeta:
    name: str
    display_name: str
    requires_place_type: str | None       # 只在某类 place 才能持有；离开自动 revoke
    auto_revoke_on_leave: bool            # 与 PlaceStore.on_leave 联动开关
    on_grant: Callable | None
    on_revoke: Callable | None
```

不变量：
1. `cap ∈ by_agent[a]` ⇔ `a ∈ by_capability[cap]`；grant / revoke 必须**原子**改两边。
2. grant 幂等：`grant(a, cap)` 在 `cap ∈ by_agent[a]` 时不开新行、不触发钩子。
3. revoke 幂等：`revoke(a, cap)` 在 `cap ∉ by_agent[a]` 时 noop。
4. DB 与内存当前态须一致：`SELECT capability FROM capability WHERE agent_id=? AND revoked_at IS NULL` 必须等于 `by_agent[a]`。
5. 命中 `auto_revoke_on_leave` 的 capability 在 PlaceStore.on_leave 钩子中**先**被 revoke，**再**进 PlaceStore 的索引切换（顺序由 PlaceStore 的钩子链保证）。
6. 钩子异常仅 log warn，不阻断主写（与 RelationGraph 同策略）。

### 4.2 关键流程 / 算法

**启动 `load_all(world_db)`**：
```
SELECT * FROM capability WHERE revoked_at IS NULL
  → 填 by_agent / by_capability
SELECT * FROM capability                      # 含历史，仅供 report_agent
  → 填 history
for cap, meta in CapabilityTypeRegistrar.iter():
    type_meta[cap] = meta
# 安装 PlaceStore.on_leave 钩子（auto_revoke_on_leave 类型）
place_store.register_on_leave(_auto_revoke_on_leave_hook)
```

**`grant(agent, cap, *, world, t, metadata=None)`**：
```
1. if cap in by_agent[agent]: return   # 幂等
2. # 类型校验（若注册了）
   meta = type_meta.get(cap)
   if meta and meta.requires_place_type:
       p = world.places.record(world.places.L_t(agent))
       if p.place_type != meta.requires_place_type: raise CapabilityNotAllowed
3. INSERT INTO capability(agent_id, capability, granted_at) VALUES(?, ?, t)
4. by_agent[agent].add(cap); by_capability[cap].add(agent)
5. if meta and meta.on_grant: await meta.on_grant(agent, world, t)
6. for h in on_change_hooks: await h(agent, cap, "grant", world, t)
   # 默认包含 dynamic_tools.recompute(agent)
```

**`revoke(agent, cap, *, world, t)`**：
```
1. if cap not in by_agent[agent]: return
2. UPDATE capability SET revoked_at=t WHERE agent_id=? AND capability=? AND revoked_at IS NULL
3. by_agent[agent].discard(cap); by_capability[cap].discard(agent)
4. on_revoke + on_change_hooks 同上
```

**PlaceStore.on_leave 联动 `_auto_revoke_on_leave_hook(agent, place, world)`**：
```
for cap, meta in type_meta.items():
    if not meta.auto_revoke_on_leave: continue
    if meta.requires_place_type and place.place_type == meta.requires_place_type:
        if cap in by_agent[agent]:
            await revoke(agent, cap, world=world, t=world.t)
```

**dynamic_tools 联动**：默认 `on_change_hook` 调 `agent_world.agents.dynamic_tools.recompute(agent, world)`，让 camel.ChatAgent.tools 在下次 perform_action_by_llm 时反映最新 capability（FEED 类工具按 `account_<feed>` 出现/消失，SEND_MESSAGE 按 `signal_uplink` 出现/消失）。

### 4.3 与其他模块的交互

- 上游调用方:
  - `WorldStep` 启动序列（bulk load）
  - `ScriptEngine` 的 `CapabilityChangeEffect`
  - `ActionDispatcher`（agent action `CAPABILITY_CHANGE`，受限）
  - `PlaceStore.on_enter / on_leave` 钩子（PlaceType 类钩子内调 `grant / revoke`）
  - `PerceptionBuilder.build`（读 `by_agent[a]` 拼 obs；读 `agents_with` 不在感知期，而在 ConnectivityResolver 的批量调用里）
- 下游被调方:
  - `WorldDB.execute`（capability 表 INSERT / UPDATE）
  - `agent_world.agents.dynamic_tools.recompute`（默认 hook）
  - `MultiPoolPlatformManager`（grant `account_<feed>` 时触发 sign_up 到对应 pool；这条 wiring 由 hook 注册）
- 共享状态: 写 `world.db.capability`；通过 hook 间接触发 pool_*.db.user 的 sign_up（grant `account_<feed>` 时——LAYOUT §9.4 懒注册策略）；不直接写 Zep。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class CapabilityTable:
    def __init__(self, world_db: WorldDB) -> None: ...
    async def load_all(self) -> None: ...

    # 读（纯内存）
    def has(self, agent: int, cap: str) -> bool: ...
    def of(self, agent: int) -> frozenset[str]: ...
    def agents_with(self, cap: str) -> frozenset[int]: ...     # 反向索引
    def history_of(self, agent: int) -> list[CapabilityRecord]: ...
    def type_meta(self, cap: str) -> CapabilityTypeMeta | None: ...

    # 写（唯一变更入口）
    async def grant (self, agent: int, cap: str, *, world, t: int, metadata: dict | None = None) -> None: ...
    async def revoke(self, agent: int, cap: str, *, world, t: int) -> None: ...

    # 钩子
    def register_on_change(self, cb) -> None: ...

class CapabilityNotAllowed(Exception):
    """requires_place_type 不满足；ActionDispatcher 捕获走 silent。"""

class CapabilityBase(metaclass=CapabilityTypeRegistrar.Meta):
    __abstract__ = True
    display_name: str
    requires_place_type: str | None = None
    auto_revoke_on_leave: bool = False
    async def on_grant (self, agent: int, world, t: int) -> None: ...
    async def on_revoke(self, agent: int, world, t: int) -> None: ...
```

### 5.2 IPC / Flask / SQL (如适用)

- IPC: 无专属命令（通过 INJECT_SCRIPT_EVENT 注入 CapabilityChangeEffect）。
- Flask: `GET /simulations/<id>/world-state` 包含 capability 当前态。
- SQL:
  - 启动: `SELECT * FROM capability WHERE revoked_at IS NULL`；`SELECT * FROM capability` 全量供 report_agent。
  - 运行 grant: `INSERT INTO capability(agent_id, capability, granted_at, metadata) VALUES (?, ?, ?, ?)`。
  - 运行 revoke: `UPDATE capability SET revoked_at=? WHERE agent_id=? AND capability=? AND revoked_at IS NULL`。
  - capability 表主键设计为 `(agent_id, capability, granted_at)`（LAYOUT §3.2），允许同 agent 同 cap 多次 grant/revoke 历史并存。

## 6. 配置入口

从 `simulation_config.json.agent_configs[i].capabilities: list[str]` 读初始 capability 列表（启动期对每个 agent 调 `grant`，`t=0`）。`world_config.capability_type_packages: list[str]` 可追加 discover 包让用户扩展。验证规则：
- 启动初始 capability 若类型已注册且 `requires_place_type` 不满足（如初始位置不是 BarPlace 却 grant `account_drinks`）→ fail-fast。
- 类型未注册的 capability 字符串允许通过（MVP 兼容裸字符串），仅类型钩子相关功能不可用。

## 7. 待决策 / 风险

- LAYOUT §9.5 #8 / N3：`agents_with` 在百万 agent + 数十种 capability 下的 Set 内存可观；MVP 接受。
- 跨 DB：grant `account_<feed>` 触发 pool sign_up 是非原子（与 §9.6 C 一致策略）；启动期可由 runner 兜底校对（"world.db 有 account_<feed> 但 pool 无 user 行" → 补 sign_up）。
- on_grant / on_revoke 钩子链与 PlaceStore.on_enter 钩子链顺序：当 `BarPlace.on_enter` 调 `cap.grant(account_drinks)` 后，`account_drinks.on_grant` 又触发其它逻辑——MVP 限制钩子深度 1（钩子内不再触发新的 grant/revoke 引发的钩子链），靠约定不靠运行期检测。
- "agent 自调 CAPABILITY_CHANGE" 的合法性：MVP 倾向于**禁止 agent 通过 LLM tool 自 grant 重要 capability**（如 `account_<feed>`），dispatcher 对 agent 发起的 CAPABILITY_CHANGE 仅允许在白名单内（如自我 revoke）；白名单未在 LAYOUT 明确，本 doc 建议在 `world_config.capability_self_modify_whitelist` 列出。
