# ConnectivityResolver 实现文档

> 路径: `agent_world/world/connectivity.py`
> 对应 LAYOUT §: §2.A ConnectivityResolver / §6.2 一个动作样例 / §6.3 一个感知样例
> 上游依赖文档: `world_place_store.md`（消费 `agents_at` + coverage 矩阵）、`world_relation_graph.md`（消费 `contacts_of` + `is_contact` 元数据）、`world_capability_table.md`（消费 `agents_with` 反向索引）
> 下游依赖文档: 无（被 PerceptionBuilder / Bus / dynamic_tools 直接消费，不再调下层）

## 1. 模块定位

ConnectivityResolver 是"agent 之间能不能走某条通道"的唯一判定层。它不持有自己的状态，只组合 PlaceStore / RelationGraph / CapabilityTable / MultiPoolPlatformManager 四个状态源，把"谁能 F2F / RDC / GRP / FEED 到谁"的判定集中起来——这样 Bus 层、PerceptionBuilder、dynamic_tools 都共享同一份 φ 谓词，避免散落的 ad hoc 检查。

四个 φ 谓词：
- **φ_F2F(a, b)**：a 与 b 同地点（含同地点的 overhear 资格）。
- **φ_RDC(a, b)**：a 能远程私聊 b（联系人 + 双方 capability + coverage 矩阵开放 RDC channel）。
- **φ_GRP(a, group_id)**：a 是 group 成员且当前位置允许 GRP（coverage 在 sender → receiver 双向都开放 GRP）。
- **φ_FEED(a, feed)**：a 持 `account_<feed>` capability，且 a 当前 place 的 coverage 允许该 feed（场景化：火星无 Twitter 时配置 coverage 屏蔽）。

输入：四个状态模块的内存索引。
输出：布尔 + 批量集合查询。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 反向索引模式 | OASIS | `agent_graph.py:25-292` | PATTERN | 仅参考"用 dict[key, Set] 做反向索引"思路；本模块自身不存索引（索引归 PlaceStore / CapabilityTable） |
| 谓词组合 | — | LAYOUT §2.A / §6.2 / §6.3 | NEW | 全新写 |

## 3. 关键改动 (相对来源仓库)

无来源，全新写。设计灵感：把"判定逻辑"与"状态存储"解耦，让 PlaceStore/RelationGraph/CapabilityTable 各自专注 CRUD，让 ConnectivityResolver 专注"如何把三者拼成业务谓词"。这样：
- Bus 层调 φ_F2F / φ_RDC / φ_GRP 做 send 前的 coverage / capability 校验（失败 → silent + `delivered=0`）。
- PerceptionBuilder 调 `reachable_agents` 拼 `obs.contacts.can_reach_now`。
- dynamic_tools 调 φ_FEED 决定 FEED 类工具是否出现在 LLM tool list。

φ 谓词不持锁、不写状态、不发钩子；纯内存读，O(1) 或 O(|contacts|)。

## 4. 核心逻辑

### 4.1 数据结构

ConnectivityResolver 自身**无状态**。仅持四个引用：

```
ConnectivityResolver:
    places:        PlaceStore
    relations:     RelationGraph
    capabilities:  CapabilityTable
    pools:         MultiPoolPlatformManager
    # 可选缓存（MVP 不开）：
    # _cache_RDC: dict[(a,b), bool]   # 每 micro-tick 起手清空
```

复用的反向索引（来自下游模块）：
- `places.agents_at(p) -> frozenset[int]`：同地点反向索引（φ_F2F 主路径）。
- `places.coverage(src, dst) -> CoverageEdge`：含 `latency_ticks` + `channels: set[str]`。
- `relations.contacts_of(a) -> Iterable[(other, type)]`：仅返回 `is_contact=True` 类型的边。
- `capabilities.agents_with(cap) -> frozenset[int]`：例如 `agents_with("signal_uplink")`、`agents_with("account_twitter")`。

### 4.2 关键流程 / 算法

**φ_F2F(a, b) → bool**：
```
return places.L_t(a) == places.L_t(b) and a != b
```
（同地点必有 `coverage[(p, p)].channels ⊇ {F2F}`，PlaceStore 启动期保证。）

**φ_F2F_set(a) → frozenset[int]**：
```
p = places.L_t(a)
return places.agents_at(p) - {a}
```
用于 SPEAK_TO_LOCAL 的"广播给同地点所有人"。

**φ_RDC(a, b) → bool**：
```
1. # 联系人检查（is_contact=True 类型才算）
   if b not in {o for (o,t) in relations.contacts_of(a)}: return False
2. # 双方都需 signal_uplink（或类似 RDC 资格 capability）
   if not (capabilities.has(a, "signal_uplink") and capabilities.has(b, "signal_uplink")):
       return False
3. # coverage 矩阵：从 a 所在 place 到 b 所在 place 的 RDC channel 是否开放
   pa, pb = places.L_t(a), places.L_t(b)
   edge = places.coverage(pa, pb)
   if edge is None or "RDC" not in edge.channels: return False
4. return True
```

**φ_GRP(a, group_id) → bool**：
```
1. # 成员资格（DB 查询，因为 group_member 在 world.db；MVP 不缓存）
   if not group_message_bus.is_member(a, group_id): return False
2. # 默认要求 signal_uplink（与 RDC 同语义；可由 channel_config 配置覆盖）
   if not capabilities.has(a, "signal_uplink"): return False
3. # 当前 place 的 coverage 允许 GRP outbound
   pa = places.L_t(a)
   # 群聊 coverage 语义：a → 任意 receiver 都需要 GRP channel
   # MVP 用 self-coverage 标志表示"a 当前 place 是否有 GRP 通道接入"
   edge = places.coverage(pa, pa)
   if "GRP" not in edge.channels: return False
4. return True
```

**φ_FEED(a, feed) → bool**：
```
1. if not capabilities.has(a, f"account_{feed}"): return False
2. pa = places.L_t(a)
   edge = places.coverage(pa, pa)            # FEED 用 self-coverage 表达"该地点能上 feed"
   if feed not in edge.channels and "FEED" not in edge.channels:
       # 约定：channels 可写 "FEED"（任意 feed）或 specifc feed name；
       # 严格场景下用 specific 名（如 "twitter"）允许细粒度控制
       return False
3. return True
```

**批量 API `reachable_agents(a, channel) -> frozenset[int]`**：
```
match channel:
    "F2F": return φ_F2F_set(a)
    "RDC":
        candidates = {o for (o,t) in relations.contacts_of(a)}
        return frozenset(b for b in candidates if φ_RDC(a, b))
    "GRP":
        # 返回 a 所在 group 列表的成员并集（不去重 a 自己）
        return ...
```

**Bus 层使用模式**：
```
# RemoteMessageBus.send(sender, recipient, content, t):
if not connectivity.φ_RDC(sender, recipient):
    INSERT direct_message(channel='RDC', sender, recipient, content,
                          attempted_at=t, arrive_at=t, delivered=0,
                          place_id=places.L_t(sender))
    return                                         # silent，B9 透传 1 轮
delay = places.latency(places.L_t(sender), places.L_t(recipient), "RDC")
INSERT direct_message(..., attempted_at=t, arrive_at=t+delay, delivered=1)
```

**dynamic_tools 使用模式**：
```
# agent_world/agents/dynamic_tools.py 每 micro-tick 起算前：
tools = base_tools.copy()
for feed in pools.all_feeds():
    if connectivity.φ_FEED(agent, feed):
        tools.extend(feed_tools_of(feed))
if connectivity.φ_GRP_any(agent):
    tools.extend(group_tools)
agent.tools = tools
```

### 4.3 与其他模块的交互

- 上游调用方:
  - `FaceToFaceBus.send`（φ_F2F；overhear 列表 = φ_F2F_set 减去 sender / recipient）
  - `RemoteMessageBus.send`（φ_RDC）
  - `GroupMessageBus.send_to_group`（φ_GRP，每个 recipient 校验，失败者 `delivered=0`）
  - `PerceptionBuilder.build`（拼 `obs.contacts.can_reach_now`、`obs.feeds`）
  - `agent_world.agents.dynamic_tools.recompute`（φ_FEED + φ_GRP 决定工具集）
- 下游被调方:
  - `PlaceStore.{L_t, agents_at, coverage, latency}`（纯读）
  - `RelationGraph.contacts_of`（纯读）
  - `CapabilityTable.{has, agents_with}`（纯读）
  - `GroupMessageBus.is_member`（φ_GRP 唯一一处 DB 查询）
- 共享状态: 仅读，**不写**任何 DB / Zep / 内存索引。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class ConnectivityResolver:
    def __init__(
        self,
        places:       "PlaceStore",
        relations:    "RelationGraph",
        capabilities: "CapabilityTable",
        pools:        "MultiPoolPlatformManager",
        group_bus:    "GroupMessageBus",
    ) -> None: ...

    # 谓词
    def phi_F2F(self, a: int, b: int) -> bool: ...
    def phi_RDC(self, a: int, b: int) -> bool: ...
    def phi_GRP(self, a: int, group_id: int) -> bool: ...
    def phi_FEED(self, a: int, feed: str) -> bool: ...

    # 批量
    def co_located(self, a: int) -> frozenset[int]: ...                 # = φ_F2F_set
    def reachable_agents(self, a: int, channel: str) -> frozenset[int]: ...
    def reachable_feeds(self, a: int) -> frozenset[str]: ...
    def latency(self, a: int, b: int, channel: str) -> int: ...         # 转发 PlaceStore.latency
```

注：API 名 `phi_F2F` 等用 ASCII 别名；docstring 与日志中可写 φ 符号。

### 5.2 IPC / Flask / SQL (如适用)

无专属 IPC / Flask 路由。φ_GRP 路径会触发 `group_bus.is_member` 一次 DB 查询（`SELECT 1 FROM world.db.group_member WHERE group_id=? AND agent_id=?`）；其余谓词纯内存。

## 6. 配置入口

间接消费配置（自身不读 simulation_config）：
- coverage 矩阵字段语义在此约定：`channels` 集合元素允许 `"F2F" / "RDC" / "GRP"` 加任意 feed 名（如 `"twitter"`）；`"FEED"` 作为通配符表示"任意 feed 都可达"。
- `signal_uplink` 是 RDC / GRP 的默认资格 capability 名；`channel_config.rdc_capability` 可覆盖（MVP 不实现，约定不变量）。

## 7. 待决策 / 风险

- LAYOUT §9.5 #8 / N3：`reachable_agents("RDC", a)` 在百万 agent + 大 contacts_of(a) 时为 O(|contacts|) × O(coverage 字典查 + cap has)；都是 dict 查询，单次很快但叠加可观；MVP 不缓存。后期可在 micro-tick 起手按 (a, channel) 缓存一轮。
- φ_GRP 唯一 DB 查询是性能小风险——百万级群聊场景需建索引 `(group_id, agent_id)`；DDL 已在 LAYOUT §3.2 group_member 主键即满足。
- φ_FEED 的 coverage 表达力：当前用 self-coverage 的 `channels` 集合表达"地点是否有 feed 接入"，对"A 地点能发推但 B 地点能看推"的非对称场景表达不足；MVP 不展开，标记为后期 D 类讨论。
- 谓词与 micro-tick 一致性：micro-tick 内 PlaceStore / RelationGraph / CapabilityTable **可能被同地点的前一个 agent 改变**（例如 RELATION_CHANGE）——这是 micro-tick 的本意（顺序可见）。φ 谓词不缓存即可保持一致；若引入缓存须按 (write_op_count, t) 失效。
