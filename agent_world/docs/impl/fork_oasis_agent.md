# OASIS SocialAgent fork 实现文档

> 路径: `vendor/oasis/oasis/social_agent/agent.py`
> 对应 LAYOUT §: §4 OASIS 总表（agent.py 行 / A4）/ §2.G Agent 扩展 / §6.1 micro-tick decision / §6.3 PerceptionBuilder / B5 4 段 prompt
> 上游依赖文档: `fork_oasis_typing.md`, `fork_oasis_agent_action.md`, `agents_dynamic_tools_and_profile.md`, `fork_oasis_agents_generator.md`
> 下游依赖文档: 无（最上层 LLM agent）

## 1. 模块定位
OASIS `SocialAgent` 是 LLM agent 的载体：持有 camel `ChatAgent`、profile、Channel、`available_actions`，每轮 `perform_action_by_llm()` 拼 prompt → 调 LLM → 拿 tool_call → 通过 `agent_action.py` 的 method 写回 Channel。Agent World 在 fork 内**整片重写其与世界的接口层**：把 `Channel` 替换为 `PlatformManager`（A4，多池统一接口）；加 6 个实例字段（前 3 项是 WorldState 引用，后 3 项是 B5 人格分层）；`perform_action_by_llm` 改用 `PerceptionBuilder.build` 拿 `(system_prompt, observation)` 然后按 4 段拼 system prompt；`available_actions` 改 per-step 动态计算（与 `dynamic_tools.py` 配合）。

输入：每轮被 `WorldStep` 在 micro-tick 内调度（同地点串行）；通过 PerceptionBuilder 获得 `(system_prompt, observation)`；profile / 人格字段在 `agents_generator.py` 装配期注入。
输出：调用 `agent_action.py` 上的 method，最终通过 `PlatformManager.dispatch(action_type, **kwargs)` 路由到 Bus / Pool / WorldState。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `SocialAgent.__init__` | OASIS | `oasis/social_agent/agent.py:55-124` | EDIT | `channel: Channel` → `platform_manager: PlatformManager`；加 6 字段 |
| `perform_action_by_llm` | OASIS | `oasis/social_agent/agent.py:127` 起 | EDIT | 改为调 `PerceptionBuilder.build`；删 `to_text_prompt` 调用；4 段 system prompt 拼接 |
| `available_actions` 静态过滤模式 | OASIS | `oasis/social_agent/agent.py:87-104` | PATTERN | 仅作模式参考；改写为 per-step 动态调用 `dynamic_tools.compute_available_tools` |
| `update_memory` | OASIS | `oasis/social_agent/agent.py:286-291` | KEEP | DialogueInjection / compressor 写摘要进 ChatMemory 用 |
| `to_text_prompt` | OASIS | `oasis/social_agent/agent_environment.py:33-135` | DELETE | 整个文件删除（B2）；本类不再调用 |

## 3. 关键改动 (相对来源仓库)

- **改动 1（构造签名替换）**：`__init__` 把 `channel: Channel` 形参换成 `platform_manager: PlatformManager`（A4）。`platform_manager` 暴露 `dispatch(action_type, **kwargs) -> Awaitable[Any]`，由 ActionDispatcher 路由到 Bus / Pool / WorldState 六选一。原 `channel.write_to_send_queue / read_from_receive_queue` 调用全部消失（这些路径迁移到 `agent_action.py` 内部走 `platform_manager.dispatch`）。
- **改动 2（加 6 实例字段）**：
  - `location: str`（WorldState 引用，不 cache 副本；getter 读 `world.places.L_t[self.agent_id]`）
  - `relations: RelationView`（同上，引用 RelationGraph）
  - `capabilities: CapabilitySet`（同上，引用 CapabilityTable）
  - `soul: str`（B5 人格分层；profile 生成期写入；运行时不变）
  - `long_term_goal: str`（B5；中等稳定，剧本极少改）
  - `current_state: str`（B5；动态字段；StateChangeEffect / UPDATE_STATE 都写它）
  前 3 项不真持有数据，仅是访问 WorldState 的代理；后 3 项是 agent 私有 string。
- **改动 3（perform_action_by_llm 重写）**：
  - 原 OASIS 调 `agent_environment.to_text_prompt(...)` 拿一段大字符串；
  - 现改为：
    ```
    sys_prompt, obs = await PerceptionBuilder.build(self, world, t)   # 返回 (str, Observation)
    user_prompt = obs.to_user_prompt()                                # Observation 自带格式化
    self.tools = await dynamic_tools.compute_available_tools(world, self, t)  # per-step 动态
    response = await self.chat_agent.astep(user_prompt, system_prompt)  # camel astep（系统 prompt 由 4 段拼接得到）
    ```
  - 4 段 system prompt 由 PerceptionBuilder 已拼好（§6.3）：`[Soul, Long-term Goal, Current State, Place Behavior Rule]`，顺序固定（前两段 prompt cache 友好）。
- **改动 4（available_actions 改 per-step）**：原 OASIS `available_actions` 在 `__init__` 静态计算（L87-104）；fork 后改成不再持久化属性，每轮 `perform_action_by_llm` 内部赋值 `self.tools = compute_available_tools(...)`。已确认 `camel.ChatAgent.tools` 是可写属性。详见 `agents_dynamic_tools_and_profile.md`。
- **改动 5（update_memory 接口保留）**：`compressor.on_move` 把 Haiku 摘要 append 进 ChatMemory 时调本 method；DialogueInjection effect 也通过它把剧本注入对话上下文。
- **改动 6（`agent_environment.py` 整删）**：fork 内 `agent.py` 不再 `from .agent_environment import to_text_prompt`；该 import 行删除。

## 4. 核心逻辑

### 4.1 数据结构

`SocialAgent` 实例字段（fork 后最终态）：

```python
class SocialAgent:
    agent_id: int
    profile: OasisAgentProfile          # 含 6 字段（详见 agents_dynamic_tools_and_profile.md）
    chat_agent: camel.ChatAgent
    platform_manager: PlatformManager   # NEW（替代 channel）
    # 世界态引用（不 cache）
    location: WorldStateRef[str]        # 通过 property 读 world.places.L_t
    relations: WorldStateRef[Set[Tuple[int,str]]]
    capabilities: WorldStateRef[Set[str]]
    # B5 人格三段
    soul: str                           # 长不变（prompt cache prefix）
    long_term_goal: str                 # 中等稳定
    current_state: str                  # 动态（UPDATE_STATE / StateChangeEffect 写）
    # camel ChatAgent.tools 每轮重写
    tools: list[Callable]               # 由 dynamic_tools 计算
```

不变量：
- `soul / long_term_goal / current_state` 都是非空 string（profile 生成期保证）。
- `current_state` 的写者只有两个：UPDATE_STATE action（自反式）+ StateChangeEffect（剧本）；两者写入同一字段，无锁需求（地点内串行；剧本在 micro-tick 之外）。
- `location / relations / capabilities` 不在 agent 上 cache 副本——避免 agent 字段与 WorldState 不一致。

### 4.2 关键流程 / 算法

`perform_action_by_llm(world, t)` 主流程（fork 后）：

```
async def perform_action_by_llm(self, world, t):
    # Step 1: 感知
    sys_prompt, obs = await PerceptionBuilder.build(self, world, t)
    user_prompt = obs.to_user_prompt()

    # Step 2: 动态工具
    self.tools = await dynamic_tools.compute_available_tools(world, self, t)
    self.chat_agent.tools = self.tools          # camel.ChatAgent 可写

    # Step 3: LLM 调用
    response = await self.chat_agent.astep(user_prompt, system_prompt=sys_prompt)
    tool_calls = response.info.get("tool_calls", [])

    # Step 4: 落地（agent_action.py 内 method 调 platform_manager.dispatch）
    for call in tool_calls:
        method = getattr(self.agent_action, call.tool_name)   # agent_action.py
        await method(**call.args)
        # method 内部已 dispatch；下一步骤的副作用（micro-tick 内 F2F 同地点可见）由 PerceptionBuilder 下次 build 自动反映
```

`update_memory` 流程（KEEP）：

```
def update_memory(self, role, content):
    self.chat_agent.memory.write_record(MessageRecord(role, content))
```

### 4.3 与其他模块的交互

- 上游调用方：`WorldStep.run_place(p, agents)` 串行循环里 `await a.perform_action_by_llm(world, t)`。
- 下游被调方：
  - `PerceptionBuilder.build` （读 WorldState + pool DB + Zep）
  - `dynamic_tools.compute_available_tools`
  - `camel.ChatAgent.astep`
  - 6 个新 method（在 `agent_action.py`，最终调 `platform_manager.dispatch`）
  - `compressor.on_move` 通过 `update_memory` 写 ChatMemory（间接）
- 共享状态：
  - 读 `world.db.{agent_location, relation, capability, direct_message, overhear, group_event, script_event_log}`、`pool_*.db.{post, rec, ...}`、Zep `agent_{id}` / `place_{p}` / `world` 三层（PerceptionBuilder 内部）
  - 写 `current_state`（in-memory；不直接写 DB；UPDATE_STATE 由 ActionDispatcher 直写）

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class SocialAgent:
    def __init__(
        self,
        agent_id: int,
        profile: OasisAgentProfile,
        platform_manager: PlatformManager,    # NEW（替 channel）
        model_factory: ModelFactory,
        world: WorldState,                    # 用于注入 location/relations/capabilities 引用
        soul: str,
        long_term_goal: str,
        current_state: str,
        ...                                   # OASIS 原其他参数
    ) -> None: ...

    @property
    def location(self) -> str: ...            # 代理 world.places.L_t

    @property
    def relations(self) -> "RelationView": ...

    @property
    def capabilities(self) -> "CapabilitySet": ...

    async def perform_action_by_llm(self, world: WorldState, t: int) -> None: ...

    def update_memory(self, role: str, content: str) -> None: ...
```

### 5.2 IPC / Flask / SQL

- 不直接暴露 IPC / Flask；通过 `WorldStep` + `MultiPoolPlatformManager` 间接驱动。
- 不直接执行 SQL；所有 DB 访问通过 PerceptionBuilder（读）与 agent_action.py → dispatch → Bus / Platform（写）。

## 6. 配置入口

`agent_configs[*]`（来自 `simulation_config.json`）字段（v0.3 已加 6 项）：
- `location: str`（启动初值；运行期由 MOVE 改）
- `relations: List[Tuple[int, str]]`（启动初值；运行期由 RELATION_CHANGE / RelationChangeEffect 改）
- `capabilities: List[str]`（启动初值；运行期由 CAPABILITY_CHANGE / CapabilityChangeEffect 改）
- `soul: str`（必填；profile_generator LLM 生成）
- `long_term_goal: str`（必填）
- `current_state: str`（必填初值）

profile_generator prompt 加 soul / long_term_goal / current_state 三段生成指令——详见 `agents_dynamic_tools_and_profile.md`。

## 7. 待决策 / 风险

- N1：Memory 压缩——MOVE 之外的"被动行为边界"（被踢出群、剧本传送）目前不触发 compressor；若需要，加 `END_BEHAVIOR` action（v0.3 不含）。
- N4：Haiku 摘要失败回退——compressor 异步失败保留 raw 不清 ChatMemory；本 agent 类无需感知。
- 隐含：camel ChatAgent.tools 在 v0.x 是可写属性；如未来 camel 版本升级把它做成 immutable，per-step 动态工具方案需要改用"每轮 reconstruct ChatAgent" fallback——本文件 grep checklist 警示。
- 隐含：B5 4 段 prompt 中 `Soul` 段最长且不变，理论上可借助 Anthropic prompt cache；但当前 camel.ChatAgent 不暴露 cache_control breakpoint API，需后续 D 类讨论是否补 patch。
