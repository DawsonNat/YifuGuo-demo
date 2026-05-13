# agents/dynamic_tools.py + agents/profile.py 实现文档

> 路径:
> - `agent_world/agents/dynamic_tools.py`
> - `agent_world/agents/profile.py`
>
> 对应 LAYOUT §: §2.G Agent 扩展 / §6.1 micro-tick decision / §6.3 PerceptionBuilder / B5 4 段 prompt
> 上游依赖文档: `fork_oasis_typing.md`, `fork_oasis_agent.md`, `fork_oasis_agents_generator.md`, `fork_oasis_agent_action.md`
> 下游依赖文档: 无（叶子模块）

## 1. 模块定位

`dynamic_tools.py` 在每轮 micro-tick 内为单个 agent **per-step 计算可用工具集合**，按 capability / connectivity / 关系状态过滤 ActionType 子集 → 返回一组 `Callable`（绑定到 `agent_action.py` method），由 `agent.py:perform_action_by_llm` 写到 `self.tools` / `chat_agent.tools` 上交给 LLM。它取代 OASIS 原本在 `agent.py:87-104` 的"装配期一次性静态过滤"。

`profile.py` 扩展 `OasisAgentProfile` dataclass 加 6 个字段（B5 + 世界态投影）：`location / relations / capabilities / soul / long_term_goal / current_state`；同时给 MiroFish `oasis_profile_generator` 的 LLM prompt 加上 soul / long_term_goal / current_state 三段生成指令；CSV / JSON 导出列同步。

输入：
- dynamic_tools：`(world: WorldState, agent: SocialAgent, t: int) -> List[Callable]`
- profile：profile 数据源（CSV 行 / JSON dict / LLM 生成结果）

输出：
- dynamic_tools：一组 callable tool 函数（每轮可不同）
- profile：填充 6 字段后的 dataclass 实例 + 同步 prompt 生成 + 同步 CSV/JSON 导出列

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `available_actions` 静态过滤模式 | OASIS | `social_agent/agent.py:87-104` | PATTERN | 仅作模式参考；改写为 per-step 动态调用 |
| ActionType 全集 | OASIS（fork 后） | `social_platform/typing.py` | KEEP | 22 个枚举（详见 `fork_oasis_typing.md`） |
| `OasisAgentProfile` dataclass | MiroFish | `backend/app/services/oasis_profile_generator.py:30-140` | EDIT | 加 6 字段（v0.3） |
| profile_generator LLM prompt | MiroFish | `backend/app/services/oasis_profile_generator.py:286-412` 附近的 prompt 段 | EDIT | 加 3 段指令（soul / long_term_goal / current_state） |
| Twitter / Reddit profile 导出 | MiroFish | `oasis_profile_generator.py:1070-1119` (Twitter CSV) / `:1146-1193` (Reddit JSON) | EDIT | 加 6 列 |

## 3. 关键改动 (相对来源仓库)

### 3.1 dynamic_tools.py（NEW）

- **改动 1（接口签名）**：`async def compute_available_tools(world, agent, t) -> List[Callable]`，返回 callable list（每个 callable 是 `agent_action.py` 上的 method 已 bound to `agent.agent_action`）。
- **改动 2（过滤逻辑）**：按以下条件过滤 ActionType 全集：
  - **Capability**：FEED 类 12 个 method 需要 `account_<feed>` capability（如 `account_twitter`）；`speak_to_local` 不需要任何 capability；`send_message` 需要 `signal_emitter` 或类似可发信号的 capability（具体由 capability_type 元数据决定）。
  - **Connectivity**：`speak_to_local` 需要同地点至少有 1 个其他 agent；`send_message` 需要至少 1 个 contact 满足 φ_RDC（可达即可，目标具体留给 LLM）；FEED 类需要该 agent 在该 pool 有账号且 pool 在该地点可见（`feeds_at(L_t)`）。
  - **Relation**：`relation_change(target, type, op='create')` 仅当目标存在；`op='break'` 仅当对应边存在。Per-step 计算时不预生成具体 (target, type) 笛卡尔积——只决定"是否暴露 relation_change 工具"；具体参数由 LLM 在工具调用时填，由 `agent_action.relation_change` 内部校验。
  - **Always available**：`update_state` 始终可用；`request_move` 当 `len(coverage[L_t][*]) > 0` 时可用。
- **改动 3（与 OASIS 静态过滤的区别）**：原 OASIS 把过滤结果写到 `agent.available_actions: list` 装配期生效；fork 后 `agent.tools` 在 `perform_action_by_llm` 每轮第一行被重赋值（`agent.tools = compute_available_tools(...)`），LLM 看到的工具集每轮可变（agent 离开酒吧后 `speak_to_local` 立即从工具集消失）。
- **改动 4（camel.ChatAgent.tools 可写性）**：已确认 `camel.ChatAgent.tools` 是可写属性（v0.x 实现里直接是 `self.tools = tools`，无 setter property 拦截）；运行期重赋值即下次 `astep` 生效。如未来 camel 升级把它做成 immutable，需改用"每轮 reconstruct ChatAgent" fallback。
- **改动 5（性能）**：`compute_available_tools` 不在每个 agent 上重新构建 callable——`agent.agent_action` 实例每个 method 都已 bound；本函数仅决定"返回哪些 method 引用"（List[Callable]）。无 LLM 调用 / 无 I/O，纯内存查询。

### 3.2 profile.py（EDIT MiroFish dataclass）

- **改动 6（dataclass 加 6 字段）**：
  - `location: str`（必填）
  - `relations: List[Tuple[int, str]]`（默认 `field(default_factory=list)`）
  - `capabilities: List[str]`（默认 list）
  - `soul: str`（必填，1-3 段长 paragraph）
  - `long_term_goal: str`（必填，1-2 句）
  - `current_state: str`（必填，1-3 句；可初始化为"刚开始的一天"等中性描述）
- **改动 7（profile_generator prompt 加 3 段）**：在生成 agent profile 的 LLM prompt 末尾追加：
  ```
  - soul: 这个 agent 的核心人格 / 价值观 / 长期不变的特质（1-3 段，长描述，prompt cache 友好）
  - long_term_goal: 半年到几年尺度的愿望或目标（1-2 句，中等稳定）
  - current_state: 当前情绪 / 内心状态 / 此时此刻的关注（1-3 句，动态，运行期会被 UPDATE_STATE 改写）
  ```
  生成结果直接进 `OasisAgentProfile.{soul, long_term_goal, current_state}`。
- **改动 8（CSV/JSON 导出列同步）**：
  - Twitter CSV（`oasis_profile_generator.py:1070-1119`）：加 6 列（location / relations_json / capabilities_json / soul / long_term_goal / current_state）；relations / capabilities 因为是 list 用 JSON 编码到单列。
  - Reddit JSON（`:1146-1193`）：加 6 个 key 到导出 dict。
- **改动 9（profile.py 的位置）**：dataclass 定义放 `agent_world/agents/profile.py`；MiroFish 的 `oasis_profile_generator.py` 在 ADAPT 后 import 这里的 dataclass（避免 dataclass 重复定义）。
- **改动 10（与 SocialAgent 的对接）**：`agents_generator` 把 profile.location / relations / capabilities 在装配期一次性写到 `world.places.bulk_load(...)` 等；profile 实例本身的这 3 字段保留作为"启动配置快照"，运行时 SocialAgent 不再读它们（读 world property）；profile.{soul, long_term_goal, current_state} 则直接传给 SocialAgent 实例字段。

## 4. 核心逻辑

### 4.1 数据结构

`OasisAgentProfile`（fork 后）：

```python
@dataclass
class OasisAgentProfile:
    # OASIS / MiroFish 原字段
    agent_id: int
    name: str
    bio: str
    age: int | None
    # ... 其余原字段（KEEP）

    # v0.3 新增 6 字段
    location: str
    relations: List[Tuple[int, str]] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    soul: str = ""
    long_term_goal: str = ""
    current_state: str = ""
```

不变量：
- `soul / long_term_goal / current_state` 三者非空（profile_generator 保证）。
- `location` 必须是合法 place_id（agents_generator 装配期校验）。
- `relations` 中每条 relation_type 必须在 conscribe 注册过（C1）。

### 4.2 关键流程 / 算法

`compute_available_tools` 伪流程：

```
async def compute_available_tools(world, agent, t):
    place = world.places.L_t[agent.agent_id]
    co_loc = world.places.agents_at(place) - {agent.agent_id}
    caps = world.capabilities.of(agent.agent_id)
    can_rdc = any(world.connectivity.phi_rdc(agent.agent_id, b) for b in world.relations.contacts_of(agent.agent_id))
    feeds_here = world.pools.feeds_at(place)

    tools = []

    # always
    tools.append(agent.agent_action.update_state)

    # F2F
    if len(co_loc) > 0:
        tools.append(agent.agent_action.speak_to_local)

    # RDC
    if can_rdc and "signal_emitter" in caps:    # 具体 capability 名按 capability_type 元数据决定
        tools.append(agent.agent_action.send_message)

    # MOVE
    if world.places.has_outbound(place):
        tools.append(agent.agent_action.request_move)

    # RELATION / CAPABILITY 自反式总是允许暴露（具体校验在 method 内）
    tools.append(agent.agent_action.relation_change)
    tools.append(agent.agent_action.capability_change)

    # FEED 类（按 capability 决定）
    for feed in feeds_here:
        if f"account_{feed.feed}" in caps:
            tools.extend(_feed_tools_for(agent.agent_action, feed))    # 12 个 FEED method 中适用的子集

    # GROUP 类（按是否在该地点 + 是否在群里）
    if "group_chat" in caps:
        tools.extend([agent.agent_action.create_group, agent.agent_action.join_group,
                      agent.agent_action.leave_group, agent.agent_action.send_to_group])

    return tools
```

注意事项：
- 函数体内全部内存查询（PlaceStore / RelationGraph / CapabilityTable / ConnectivityResolver 都有反向索引）；不调 LLM、不 I/O。
- 同一 agent 同一 t 多次调用结果应一致（对 micro-tick 内 retry≤1 友好）。

### 4.3 与其他模块的交互

dynamic_tools：
- 上游调用方：`SocialAgent.perform_action_by_llm`（每轮第一步）。
- 下游被调方：`world.places.{L_t, agents_at, has_outbound}` / `world.relations.contacts_of` / `world.capabilities.of` / `world.connectivity.phi_rdc` / `world.pools.feeds_at`。
- 共享状态：只读 WorldState（不写）。

profile：
- 上游调用方：`oasis_profile_generator`（生成）/ `agents_generator`（消费 dataclass）/ Twitter CSV / Reddit JSON 导出器。
- 下游被调方：`SocialAgent.__init__`（接收 6 字段）/ `world.places.bulk_load` 等装配 API。
- 共享状态：dataclass 是值对象；不持有运行时状态。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
# agent_world/agents/dynamic_tools.py
async def compute_available_tools(
    world: WorldState,
    agent: SocialAgent,
    t: int,
) -> list[Callable]: ...

# agent_world/agents/profile.py
@dataclass
class OasisAgentProfile:
    agent_id: int
    name: str
    bio: str
    age: int | None
    # ... OASIS / MiroFish 原字段
    location: str
    relations: list[tuple[int, str]]
    capabilities: list[str]
    soul: str
    long_term_goal: str
    current_state: str
```

### 5.2 IPC / Flask / SQL

- 无直接 IPC / Flask / SQL 暴露。
- 通过 `agents_generator` 装配期间接驱动 `world.{places, relations, capabilities}.bulk_load`，最终写 `world.db.{agent_location, relation, capability}`（启动期一次性写）。

## 6. 配置入口

profile 字段从 `simulation_config.json:agent_configs[*]` 读取（详见 `fork_oasis_agents_generator.md` §6 字段列表）。默认值：
- `relations`: `[]`
- `capabilities`: `[]`
- `current_state`: 无默认（必须 profile_generator 生成；为空抛错）

`channel_config / memory_config` 不直接影响本模块，但通过 ActionDispatcher 间接影响 `compute_available_tools` 的过滤决定（如 `request_move` 是否暴露依赖 coverage 配置）。

## 7. 待决策 / 风险

- 9.5 #8 100w agent scale：`compute_available_tools` 每轮每 agent 调用一次；100w agent × 22 ActionType 过滤逻辑可能成为 micro-tick 内瓶颈；MVP 不优化，D 类讨论是否做"过滤结果 cache（按 (place, capabilities, relations)-hash 索引）"。
- N2：`UPDATE_STATE` 始终在 tools 中，依赖 LLM 自律；如出现频繁滥用，本函数可加"上 N 轮已调用过则本轮过滤掉"启发式（未实现）。
- camel 升级风险（详见 `fork_oasis_agent.md`）：`ChatAgent.tools` 一旦变 immutable，per-step 动态工具方案降级。
- profile 6 字段与 SocialAgent 实例字段间的"启动同步"：profile.{location, relations, capabilities} 在装配期写入 WorldState 后，profile 副本字段不再读；`{soul, long_term_goal, current_state}` 则在 SocialAgent 实例上长存。current_state 与 profile.current_state 在 UPDATE_STATE 之后会发散——profile 是启动快照，agent 实例是运行时真值；后期 dump 配置时取实例真值即可。
