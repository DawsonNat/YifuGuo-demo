# OASIS agents_generator.py fork 实现文档

> 路径: `vendor/oasis/oasis/social_agent/agents_generator.py`
> 对应 LAYOUT §: §4 OASIS 总表（agents_generator.py 行）/ §2.G profile 扩展 / §6.1 装配
> 上游依赖文档: `fork_oasis_agent.md`, `agents_dynamic_tools_and_profile.md`
> 下游依赖文档: 无

## 1. 模块定位
OASIS `agents_generator.py` 是 SocialAgent 批量装配工厂：5 个 entry point（按 profile 来源不同：CSV / dict / Pydantic / generator / pre-built）各负责把 profile 数据 → 构造 `SocialAgent` 实例 → 注册到 AgentGraph。Agent World 在 fork 内**保留 5 个 entry point 结构**，但每个 entry point 内：(1) profile 字段补 6 项（v0.3 含 B5 三人格段）；(2) 构造 `SocialAgent` 时注入 `platform_manager` 而非单 `channel`（A4 配套）；(3) `available_actions` 不再静态计算——改成只把 dynamic_tools 钩子留好，运行期由 `agent.py:perform_action_by_llm` 每轮重新计算（详见 `agents_dynamic_tools_and_profile.md`）。

输入：profile 数据源（CSV / JSON / Pydantic）+ `world: WorldState` + `platform_manager: PlatformManager` + `model_factory`。
输出：`AgentGraph` 实例（OASIS 沿用），内部含 N 个 `SocialAgent`。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 5 个 entry point | OASIS | `oasis/social_agent/agents_generator.py:34-649` | KEEP | 函数骨架不动；内部装配细节 EDIT |
| profile 解析（CSV / JSON / Pydantic） | OASIS | 同文件 | EDIT | 把 6 个新字段写入 profile dataclass + 装配期透传 |
| `available_actions` 静态过滤 | OASIS | `social_agent/agent.py:87-104`（被 agents_generator 间接驱动） | DELETE/PATTERN | 不再在装配期计算；交给 dynamic_tools per-step |
| Channel 注入 | OASIS | 各 entry point 装配 SocialAgent 时 | EDIT | 改 `channel=` 为 `platform_manager=` |

## 3. 关键改动 (相对来源仓库)

- **改动 1（profile 6 字段写入）**：5 个 entry point 在解析 profile 数据时（不论 CSV / JSON / dict / Pydantic）都需把 `location / relations / capabilities / soul / long_term_goal / current_state` 6 个字段读出来传给 `SocialAgent.__init__`。具体 dataclass 修改在 `agents_dynamic_tools_and_profile.md`；本文件只负责"装配期把字段传进去"。
- **改动 2（构造参数替换）**：`SocialAgent(channel=ch, ...)` → `SocialAgent(platform_manager=pm, world=world, ...)`。`platform_manager` 是 `MultiPoolPlatformManager` 实例；`world` 是 WorldState 实例。`channel` 不再传——FEED 类 method 通过 `platform_manager.channel_for(pool_id)` 间接拿到（详见 `fork_oasis_agent_action.md` 的 `channel` 字段保留约定）。
- **改动 3（available_actions 改 per-step）**：删除装配期对 `available_actions` 的静态计算（OASIS 原通过 capability / role 过滤 ActionType 子集）。装配后 `agent.tools` 字段为空 list；`perform_action_by_llm` 第一轮调用时由 `dynamic_tools.compute_available_tools` 填充。装配期可选打 log "deferred-tool-init for agent {id}"。
- **改动 4（5 个 entry point 行为）**：保持原有签名与外部使用方式不变（MiroFish / 测试代码无需改入口）；仅内部参数装配链路改写。
- **改动 5（profile_generator prompt 接入）**：`agents_generator` 不直接调 LLM 生 profile（那是 `oasis_profile_generator.py` 的事），但消费它产出的 `OasisAgentProfile` dataclass；本 fork 改动只确认 dataclass 含 6 字段后能正确传递。`oasis_profile_generator` 的 prompt 改动（增加 soul/long_term_goal/current_state 3 段生成指令）见 `agents_dynamic_tools_and_profile.md`。

## 4. 核心逻辑

### 4.1 数据结构

5 个 entry point（OASIS 原命名沿用，伪签名）：

```python
async def generate_agents_from_csv(csv_path, world, platform_manager, model_factory) -> AgentGraph: ...
async def generate_agents_from_json(json_path, world, platform_manager, model_factory) -> AgentGraph: ...
async def generate_agents_from_dict(dict_list, world, platform_manager, model_factory) -> AgentGraph: ...
async def generate_agents_from_pydantic(profile_list, world, platform_manager, model_factory) -> AgentGraph: ...
async def generate_agents_pre_built(...) -> AgentGraph: ...
```

每个 entry point 的内部步骤：
1. 解析数据源 → 一组 `OasisAgentProfile`（含 6 字段）
2. 对每个 profile：
   - 构造 `SocialAgent(agent_id, profile, platform_manager, world, model_factory, soul, long_term_goal, current_state, ...)`
   - `agent.tools = []`（占位，per-step 填充）
   - 注册到 `AgentGraph`
3. 返回 `AgentGraph`

不变量：
- 装配完成后，每个 SocialAgent 的 6 字段都已填充（profile 校验在 dataclass 层做）。
- `agent.platform_manager is platform_manager`（共享同一实例）。

### 4.2 关键流程 / 算法

```
for entry in [csv, json, dict, pydantic, pre_built]:
    profiles = parse(source)             # OASIS 原解析逻辑 + 6 字段读取
    for p in profiles:
        agent = SocialAgent(
            agent_id=p.agent_id,
            profile=p,
            platform_manager=platform_manager,
            world=world,
            soul=p.soul,
            long_term_goal=p.long_term_goal,
            current_state=p.current_state,
            model_factory=model_factory,
        )
        # agent.location/relations/capabilities 通过 property 读 world，无需再写
        # agent.tools 留空，运行期填
        agent_graph.add(agent)
    return agent_graph
```

### 4.3 与其他模块的交互

- 上游调用方：`runner/run_agent_world_simulation.py` 在启动期调一次（5 选 1 entry point，按配置决定）。
- 下游被调方：
  - `OasisAgentProfile` dataclass（消费）
  - `SocialAgent.__init__`（构造）
  - `AgentGraph.add`（注册）
- 共享状态：
  - 不直接写 DB；构造期把 6 字段从 profile 透传到 SocialAgent 实例。
  - `world.places.L_t[agent_id] = profile.location`：装配期一次性把 location 写进 WorldState（这是装配的副作用，PlaceStore 提供 `bulk_load(agents=[...])`）。
  - `world.relations.bulk_load(profile.relations)` / `world.capabilities.bulk_load(profile.capabilities)` 同理。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
async def generate_agents_from_csv(
    csv_path: str,
    world: WorldState,
    platform_manager: PlatformManager,
    model_factory: ModelFactory,
    **kwargs,
) -> AgentGraph: ...

async def generate_agents_from_json(
    json_path: str,
    world: WorldState,
    platform_manager: PlatformManager,
    model_factory: ModelFactory,
    **kwargs,
) -> AgentGraph: ...

# 其余 3 个 entry point 同模式
```

签名相对 OASIS 原版主要变化：
- `channel: Channel` 形参 → `platform_manager: PlatformManager`
- 新增 `world: WorldState` 形参（用于注入 location/relations/capabilities 的初值并提供 property 引用）

### 5.2 IPC / Flask / SQL

- 无 IPC / Flask 直接暴露。
- SQL：装配期通过 `world.places.bulk_load(...)` 等 API 间接写 `world.db.{agent_location, relation, capability}`（启动期一次性写）；不写 pool_*.db。

## 6. 配置入口

`agent_configs[*]` 来自 `simulation_config.json`，每条对应一个 profile dict（CSV/JSON 等格式由 entry point 决定）：
- `agent_id` / `name` / `bio`（OASIS 原字段）
- `location: str`（必填）
- `relations: List[Tuple[int, str]]`（必填，可为空 list）
- `capabilities: List[str]`（必填，可为空 list）
- `soul: str`（必填，由 profile_generator 生成）
- `long_term_goal: str`（必填）
- `current_state: str`（必填）

校验：dataclass 层做（详见 `agents_dynamic_tools_and_profile.md`）；agents_generator 装配前若有缺字段直接抛错，不做 silent fallback。

## 7. 待决策 / 风险

- 隐含：装配期顺序——必须先把 PlaceStore / RelationGraph / CapabilityTable 的初始数据 bulk_load 完，再构造 SocialAgent，否则 agent property 读 world 会读到空。runner 入口需保证装配链路顺序（详见 `runner/run_agent_world_simulation.py`，本文件不展开）。
- 隐含：5 个 entry point 在 OASIS 原版可能仍引用 `channel` 字段做 attribute lookup；fork 期需 grep `agents_generator.py` 全文一次性替换。
- 与 `agents_dynamic_tools_and_profile.md` 同步漂移：profile dataclass 字段名一旦变化，本文件 5 个 entry point 都要跟。
