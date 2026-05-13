# PerceptionBuilder 实现文档

> 路径: `agent_world/world/perception.py`
> 对应 LAYOUT §: §2.A PerceptionBuilder / §6.3 一个感知 / §3.2 world.db / §7.1 place.attrs
> 上游依赖文档: `world_dispatcher.md`(同轮内 F2F / UPDATE_STATE 写入会被本轮后续 agent 的 obs 读到), `world_state.md`(待写), `world_db.md`(待写)
> 下游依赖文档: `agents_fork_agent_py.md`(待写, OASIS `social_agent/agent.py:127` `perform_action_by_llm` 直接调本模块)

## 1. 模块定位

PerceptionBuilder 是仿真核心的"读侧网关": 在每轮 micro-tick 内, 一个 agent 即将决策前, 把 WorldState + world.db + pool_*.db + Zep 多源数据**收拢成单一 `Observation`**, 同时输出 4 段拼接好的 system prompt. 它**直接取代** OASIS `vendor/oasis/social_agent/agent_environment.py`(L33-135 的 `to_text_prompt()`), 该 OASIS 文件在 fork 后被删除.

- 输入: `agent: SocialAgent`, `world: WorldState`, `t: int`(当前 world.t)
- 输出: `(system_prompt: str, observation: Observation)`
- 调用频率: 每 agent 每 micro-tick 调一次, 严格在该 agent `astep()` 之前

存在理由: OASIS 原 `to_text_prompt()` 只看 pool 内 feed; Agent World 七元组 $\langle P,A,L_t,R_t,C_t,F_t,M_t\rangle$ 的"位置 / 关系 / 能力 / 跨池 / 直接通信 / 群事件 / 失败反馈 / 跨 graph 记忆"全要进 prompt, 必须重写一层.

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `to_text_prompt()` 模板拼接结构 | OASIS | `vendor/oasis/oasis/social_agent/agent_environment.py:33-135` | PATTERN | 仅作为"section by section 拼 prompt"的模式参考; 该文件 fork 后**删除** |
| pool feed refresh 接口 | OASIS | `vendor/oasis/oasis/social_platform/platform.py` `refresh()` | KEEP | 通过 `MultiPoolPlatformManager.feeds_at(place)` 间接调用 |
| Zep `quick_search` 并行检索 | MiroFish | `backend/app/services/oasis_profile_generator.py:286-412` `_search_zep_for_entity` | PATTERN | 参数化 graph_id, 并行 edges/nodes |
| Zep retrieval 包装 | MiroFish | `backend/app/services/zep_tools.py:1237-1270` `quick_search` + L45-54 `SearchResult.to_text` | KEEP | 经 `agent_world/memory/retrieval.py` 二次包装 |
| 4 段 prompt 拼接顺序 | LAYOUT v0.3 / B5 | §6.3 + §7.1 | NEW | Soul / Long-term Goal / Current State / Place Behavior Rule |
| Observation 字段表 | LAYOUT v0.3 | §6.3 11 字段表 | NEW | 包含 v0.3 新增 `recent_failed_attempts` / `group_events` |

## 3. 关键改动 (相对 OASIS `agent_environment.py`)

- **整片删除原文件**, 重写为 `agent_world/world/perception.py`. 不保留任何"environment / channel / receive_queue"概念——这些已被 PerceptionBuilder 主动 pull 模式取代.
- prompt 模板从单一 Jinja(OASIS 原) 拆为 **system / user 两份**:
  - **system_prompt** = 4 段固定顺序拼接(B5): `# Soul` → `# Long-term Goal` → `# Current State` → `# Place Behavior Rule`. 长稳前缀利于 prompt cache.
  - **user_prompt** = `Observation` 序列化(`# LOCATION / # CO-LOCATED / # CONTACTS / # FEEDS / # OVERHEARD / # INCOMING_MESSAGES / # SCRIPTED / # MEMORIES / # RECENT_FAILED_ATTEMPTS / # GROUP_EVENTS`).
- 数据来源跨三层(WorldState 内存 / world.db / pool_*.db / Zep), 不再单纯 channel pull.
- **incoming_messages** 按 `arrive_at <= world.t AND delivered=1` 过滤(B1.1); **不**按 `attempted_at` 过滤——RDC 延迟到达由 `arrive_at` 严格控制.
- **recent_failed_attempts** (B9): 仅当 `delivered=0 AND attempted_at == t-1` 时透传**仅 1 轮**, 不进 ChatMemory, 不进 Zep.
- **group_events** (B6): 仅当 `occurred_at == t-1` 时透传**仅 1 轮**.
- micro-tick 配套: 同一 `world.t` 内, 同地点的 F2F 消息 `arrive_at = t, delivered=1` 立刻被同地点下一个决策 agent 读到; RDC 因 `delay >= 1` 不会被本轮中插.

## 4. 核心逻辑

### 4.1 数据结构

```python
@dataclass(slots=True)
class ContactBrief:
    agent_id: int
    relation_types: list[str]
    can_reach_now: bool          # φ_RDC(self, contact)
    reason: str | None           # 不可达原因摘要 (e.g. "no coverage", "missing capability")

@dataclass(slots=True)
class IncomingMessage:
    message_id: int
    sender_id: int | None        # NULL = 系统消息
    channel_type: str            # 'F2F' | 'RDC' | 'GRP'
    group_id: int | None
    content: str
    place_id: str | None         # 发送时刻 sender 所在地
    attempted_at: int
    arrive_at: int

@dataclass(slots=True)
class FailedAttempt:
    message_id: int
    channel_type: str
    recipient_id: int
    group_id: int | None
    content: str
    attempted_at: int            # 严格 == t-1

@dataclass(slots=True)
class GroupEvent:
    event_id: int
    group_id: int
    agent_id: int                # 谁加入/离开/被踢
    event_type: str              # 'join' | 'leave' | 'kick'
    actor_id: int | None
    occurred_at: int             # 严格 == t-1

@dataclass(slots=True)
class Observation:
    # 11 个字段, 顺序与 LAYOUT §6.3 表一致
    self_location: str
    location_attrs: dict          # {timezone, behavior_hint, ...}
    co_located_agents: list[int]
    contacts: list[ContactBrief]
    feeds: list[FeedBrief]        # FeedBrief = (pool_id, feed_name, top_k_posts)
    incoming_messages: list[IncomingMessage]
    overheard: list[OverheardLine]
    recent_failed_attempts: list[FailedAttempt]   # 仅 1 轮
    group_events: list[GroupEvent]                # 仅 1 轮
    relevant_memories: list[MemoryHit]            # Zep quick_search 结果
    scripted_notification: str | None             # ScriptEngine.pending_for(agent)
```

**不变量**:
- `recent_failed_attempts` 内每条 `attempted_at == world.t - 1`(SQL 级保证, 不依赖应用层过滤)
- `group_events` 内每条 `occurred_at == world.t - 1`
- `incoming_messages` 内每条 `arrive_at <= world.t AND delivered=1`
- `co_located_agents` 不含 `agent.id` 自己
- `contacts` 与 `co_located_agents` 可重叠(同地点的 contact 既出现在 co_located 又出现在 contacts)

### 4.2 关键流程 / 算法

完整伪代码(直接对应 LAYOUT §6.3, **不是真实实现**):

```
build(agent, world, t) -> (system_prompt, user_observation):
    obs = Observation()

    # --- B1: 位置 / 同地点 / contact / feed (内存反向索引) ---
    obs.self_location  = world.places.L_t[agent.id]
    obs.location_attrs = world.places.attrs(obs.self_location)
        # 约定字段:
        #   timezone: str           (叙事用, 不影响 tick)
        #   behavior_hint: str|None (B5 第 4 段 prompt 来源)
        #   ... 其余自定义
    obs.co_located_agents = world.places.agents_at(obs.self_location)
    obs.contacts = [
        ContactBrief(b, can_reach_now=phi_RDC(agent, b), reason=...)
        for b in world.relations.contacts_of(agent)
    ]
    obs.feeds = [pool.brief for pool in world.pools.feeds_at(obs.self_location)
                 if world.capabilities.has(agent, f"account_{pool.feed}")]

    # --- B1.1: 已送达消息 (arrive_at 过滤) ---
    obs.incoming_messages = world.world_db.fetch(
        "SELECT * FROM direct_message "
        "WHERE recipient_id=? AND delivered=1 AND arrive_at<=? AND arrive_at>?_last_seen",
        agent.id, t, agent.last_message_seen_at)
    obs.overheard = world.world_db.fetch_overhear(agent, since=t-1)

    # --- B9: 失败短时透传 (仅 1 轮, 下轮自动消失) ---
    obs.recent_failed_attempts = world.world_db.fetch(
        "SELECT * FROM direct_message "
        "WHERE sender_id=? AND delivered=0 AND attempted_at=?",
        agent.id, t-1)

    # --- B6: 群事件短时透传 (仅 1 轮) ---
    obs.group_events = world.world_db.fetch(
        "SELECT ge.* FROM group_event ge "
        "JOIN group_member gm ON gm.group_id=ge.group_id "
        "WHERE gm.agent_id=? AND ge.occurred_at=?",
        agent.id, t-1)

    # --- 跨 graph 检索 ---
    obs.relevant_memories = MultiGraphRetriever.search(
        graph_ids=[f"agent_{agent.id}", f"place_{obs.self_location}"],
        query=agent.recent_intent,
    )
    obs.scripted_notification = world.script.pending_for(agent)

    # --- B5: 4 段 system prompt 拼接 (顺序固定, 不可调整) ---
    system_prompt = "\n\n".join([
        f"# Soul\n{agent.soul}",                                  # 长不变 -> prompt cache prefix
        f"# Long-term Goal\n{agent.long_term_goal}",              # 中等稳定
        f"# Current State\n{agent.current_state}",                # 动态 (StateChangeEffect / UPDATE_STATE 可改)
        f"# Place Behavior Rule\n{obs.location_attrs.behavior_hint or '(none)'}"
    ])
    return system_prompt, obs
```

**4 段拼接顺序的设计意图(B5)**:
1. `Soul` 放最前——基本不变, 让 LLM provider 端的 prompt prefix cache 能命中
2. `Long-term Goal` 次之——episode 级稳定
3. `Current State` 第三——可变, 但通常一段 micro-tick 序列内不抖动; 由 `StateChangeEffect`(剧本) 或 `UPDATE_STATE`(agent 自反) 修改, 二者写同一字段 `world.agents[a].current_state`
4. `Place Behavior Rule` 最后——随 MOVE 切换, 动态最大. `behavior_hint` 为 None 时占位 `(none)`, 保证段数恒为 4(避免 prompt 结构漂移导致 LLM 输出不稳)

### 4.3 与其他模块的交互

- **上游调用方**: 只有一个——`vendor/oasis/oasis/social_agent/agent.py:perform_action_by_llm` (L127 附近). fork 后该 method 改为:
  ```
  sys_prompt, obs = await PerceptionBuilder.build(self, world, world.t)
  user_prompt = obs.render_to_text()
  resp = await self.llm.astep(sys_prompt, user_prompt)
  ```
- **下游被调方**:
  - `world.places.{L_t, attrs, agents_at}`(`PlaceStore`)
  - `world.relations.contacts_of`(`RelationGraph`)
  - `world.capabilities.has`(`CapabilityTable`)
  - `ConnectivityResolver.phi_RDC`
  - `world.pools.feeds_at(place)` + `MultiPoolPlatformManager.platform_for(p,f).refresh()`(读 pool_*.db.{post, rec})
  - `world.world_db.fetch{_overhear}`(`WorldDB`, 见下表)
  - `MultiGraphRetriever.search`(`agent_world/memory/retrieval.py` -> `zep_tools.quick_search`)
  - `world.script.pending_for(agent)`(`ScriptEngine`)
- **共享状态(读)**:
  - `world.db`: `agent_location` / `relation` / `capability` / `direct_message` / `overhear` / `group_event` / `group_member` (JOIN)
  - `pool_*.db`: `post` / `rec`(经 OASIS Platform.refresh)
  - Zep: `agent_{id}` / `place_{id}` graph (quick_search)
- **共享状态(写)**: 无. PerceptionBuilder 是**纯读**模块. `agent.last_message_seen_at` 的更新由 `agent.astep` 完成, 不是本模块职责.

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class PerceptionBuilder:
    def __init__(
        self,
        world_db: WorldDB,
        retriever: MultiGraphRetriever,
        connectivity: ConnectivityResolver,
    ) -> None: ...

    async def build(
        self,
        agent: "SocialAgent",
        world: "WorldState",
        t: int,
    ) -> tuple[str, Observation]: ...


@dataclass(slots=True)
class Observation:
    self_location: str
    location_attrs: dict
    co_located_agents: list[int]
    contacts: list[ContactBrief]
    feeds: list[FeedBrief]
    incoming_messages: list[IncomingMessage]
    overheard: list[OverheardLine]
    recent_failed_attempts: list[FailedAttempt]
    group_events: list[GroupEvent]
    relevant_memories: list[MemoryHit]
    scripted_notification: str | None

    def render_to_text(self) -> str: ...   # 把 11 字段拼成 user_prompt
```

### 5.2 IPC / Flask / SQL

**SQL 输入(读 world.db)**:

| 表 | 过滤条件 | 用途 |
|---|---|---|
| `agent_location` | `agent_id=?` | self_location |
| `relation` | `src_agent=? OR (dst_agent=? AND symmetric)` | contacts |
| `capability` | `agent_id=? AND revoked_at IS NULL` | feeds 过滤 + RDC 校验 |
| `direct_message` | `recipient_id=? AND delivered=1 AND arrive_at<=t AND arrive_at>last_seen` | incoming_messages |
| `direct_message` | `sender_id=? AND delivered=0 AND attempted_at=t-1` | recent_failed_attempts (B9) |
| `overhear` | `overhearer_id=? AND attempted_at>=t-1` | overheard |
| `group_event JOIN group_member` | `gm.agent_id=? AND ge.occurred_at=t-1` | group_events (B6) |

**SQL 输入(读 pool_*.db, 通过 OASIS Platform.refresh)**: `post` / `rec`. 不在本模块直接执行 SQL, 经 `MultiPoolPlatformManager.platform_for(p,f).refresh()` 间接调用.

**Zep 输入**: `quick_search(graph_id=agent_{id})`, `quick_search(graph_id=place_{loc})`.

**输出**: 无 SQL 写入.

无 IPC, 无 Flask 路由. 本模块为纯函数式读侧, 仅由 agent.py 调.

## 6. 配置入口

从 `simulation_config.json` 读:

| 配置 key | 默认 | 说明 |
|---|---|---|
| `world_config.places[*].attrs.timezone` | 无 | 字符串, IANA(如 `"America/New_York"`); 仅传给 user_prompt 的 `# LOCATION` 段, 不影响 tick |
| `world_config.places[*].attrs.behavior_hint` | None | 注入 system_prompt 第 4 段; None 时该段写 `(none)` |
| `channel_config.failed_attempt_ttl_ticks` | 1 | B9 透传轮数; 当前实现 hardcode 1, 变更需同时改 SQL `attempted_at=?` 的常量(预留 config 占位) |
| `channel_config.group_event_ttl_ticks` | 1 | 同上, B6 透传轮数 |
| `world_graphs.per_agent_template` | `agent_{id}` | Zep retrieve 时拼 graph_id |
| `world_graphs.per_place_template` | `place_{id}` | 同上 |
| `agent_configs[*].soul` | 必填 | system_prompt 第 1 段 |
| `agent_configs[*].long_term_goal` | 必填 | system_prompt 第 2 段 |
| `agent_configs[*].current_state` | 必填(初始) | system_prompt 第 3 段, 运行时可被 UPDATE_STATE / StateChangeEffect 改写 |

**验证规则**:
- `behavior_hint` 是字符串或 null; 长度建议 <= 200 字符(不强制)
- `timezone` 不验证 IANA 合法性(MVP)
- 4 段 system prompt 顺序在代码中 hardcode, 不允许配置覆盖(B5 决议)

## 7. 待决策 / 风险

- **N5(LAYOUT §9.5.1)**: `arrive_at` 字段仅 world.db.direct_message 有, pool_*.db.trace 不受影响. 本模块只读 world.db, 风险已隔离.
- **N3(LAYOUT §9.5.1) DeliveryQueue.sweep_undelivered 性能**: 100w agent + 群聊场景, `direct_message` 表的 `(delivered, recipient_id)` 索引压测. PerceptionBuilder 每 agent 每轮 5 次 SELECT(incoming + overhear + failed + group_event + capability), 索引未建好时是 N(agent) * N(message) 量级——P0/P1 阶段必须先建索引再压.
- **#8(LAYOUT §9.5)**: 100w agent scale 下, `co_located_agents` 反向索引 + `contacts_of` 邻接表的内存占用未评估; 与 ConnectivityResolver 共担, 暂列 D 类.
- **last_message_seen_at 一致性**: 本模块查询里写了 `arrive_at > agent.last_message_seen_at` 作为去重; 该字段的更新时机(agent.astep 内? PerceptionBuilder 末尾?) 需在 `agents_fork_agent_py.md` 落实, 否则可能漏读或重读. 当前默认: agent.astep 在每次 build 后立刻更新到 max(arrive_at).
- **Zep 检索失败的降级**: `MultiGraphRetriever.search` 超时 / 失败时是否阻塞决策? MVP 选择"Zep 失败则 obs.relevant_memories=[], log warn, 不抛", 但需在 retrieval.py 落实, 本模块仅消费.
- **`recent_failed_attempts` / `group_events` 透传 ttl 是否可调**: 当前 LAYOUT 锁 1 轮; 若未来需要 2-3 轮, SQL 里 `attempted_at = t-1` 要改成 `attempted_at >= t - ttl AND attempted_at < t`, 配置已预留 key.
