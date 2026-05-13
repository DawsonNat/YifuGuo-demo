# OASIS agent_action.py fork 实现文档

> 路径: `vendor/oasis/oasis/social_agent/agent_action.py`
> 对应 LAYOUT §: §4 OASIS 总表（agent_action.py 行 / B3 + UPDATE_STATE）/ §2.G action_tools 删除 / §6.1 ActionDispatcher 路由 / §6.2 SEND_MESSAGE 例
> 上游依赖文档: `fork_oasis_typing.md`, `fork_oasis_agent.md`
> 下游依赖文档: 无（被 LLM tool_call 直接调用，最末端）

## 1. 模块定位
OASIS `agent_action.py` 是把 ActionType → Channel 写入的胶水层：每个 method 名等于 ActionType.value，内部组 message 调 `Channel.write_to_send_queue`。Agent World 在 fork 内**新增 6 个 method**（B3 + v0.3 UPDATE_STATE），并把这 6 个 method 的写入路径从 `Channel.write_to_send_queue` 改为 `self.platform_manager.dispatch(action_type, **kwargs)`——dispatch 由上层 `ActionDispatcher` 实现路由到 FaceToFaceBus / RemoteMessageBus / GroupMessageBus / WorldState 等子系统。

输入：camel ChatAgent 的 tool_call（每个 method 暴露给 LLM 当工具）。
输出：副作用——写 `world.db.{direct_message, overhear, relation, capability, group_event, ...}` 或修改 `WorldState.agents[a].current_state` / `WorldState.agents[a].location` 等内存字段。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| FEED 类 12 个 method 模板 | OASIS | `oasis/social_agent/agent_action.py:22-759` | KEEP | create_post / repost / quote_post / like_post / dislike_post / create_comment / like_comment / dislike_comment / follow / unfollow / mute / unmute |
| group 4 个 method | OASIS | 同文件 | EDIT | create_group / join_group / leave_group / send_to_group：路由由 Channel→Platform 改为 platform_manager.dispatch → GroupMessageBus |
| `listen_from_group` method | OASIS | 同文件 | DELETE | 与 `LISTEN_FROM_GROUP` enum 一并删除 |
| 6 个新 method（speak_to_local 等） | — | — | NEW | 文件末尾追加；不调 Channel；调 platform_manager.dispatch |
| `Channel.write_to_receive_queue` | OASIS | `social_platform/channel.py` | SKIP | 6 个新 method 都不调 |

## 3. 关键改动 (相对来源仓库)

- **改动 1（删 listen_from_group）**：与 `fork_oasis_typing.md` 删 `LISTEN_FROM_GROUP` 配套，本 method 一并删除。任何引用一并清理（grep checklist：本文件 + agent.py + platform.py）。
- **改动 2（4 个 group method 改路由）**：`create_group / join_group / leave_group / send_to_group` 不再 `await self.channel.write_to_send_queue(...)`，改为 `await self.platform_manager.dispatch(ActionType.JOIN_GROUP, **kwargs)`。dispatch 内部由 ActionDispatcher 路由到 GroupMessageBus（A1）。
- **改动 3（新增 6 个 method）**，文件末尾追加：
  - `speak_to_local(content: str)`：F2F 公开发言；dispatch → FaceToFaceBus → world.db.{direct_message, overhear}。
  - `send_message(target_id: int, content: str)`：定向 RDC；dispatch → RemoteMessageBus → world.db.direct_message（成功/失败 delivered 标志，arrive_at 由 coverage.latency_ticks 计算）。
  - `request_move(place_id: str, reason: str = "")`：申请 MOVE；dispatch → WorldState.pending_moves（轮末步骤 9 串行结算；先到先得）。
  - `relation_change(target_id: int, relation_type: str, op: str)`：op ∈ {"create", "break"}；dispatch → RelationGraph.write（内部检查 conscribe relation_type 元数据：对称自动双写、互斥抛错；on_change 钩子投影到 pool follow）。
  - `capability_change(capability: str, op: str)`：op ∈ {"acquire", "release"}；dispatch → CapabilityTable.write（受 PlaceType.on_enter/on_leave 钩子或剧本约束；agent 自调一般用于"放弃")。
  - `update_state(new_state: str)`：dispatch → WorldState 直接改 `world.agents[self.agent_id].current_state = new_state`；不走 Bus；不写 DB；下轮 PerceptionBuilder 自动读到新值。
- **改动 4（统一通过 platform_manager.dispatch）**：6 个新 method 都通过同一接口落地，便于 ActionDispatcher 集中维护路由表（§6.1 路由清单 + §4 NEW action 行）。
- **改动 5（method 命名 ↔ ActionType.value 严格对齐）**：method 名必须等于对应 ActionType.value，否则 OASIS 反射 dispatch（`getattr(self, action_type.value)`）失败。已与 `fork_oasis_typing.md` 同步——共 22 个枚举对应 22 个 method（删 1 加 6）。
- **改动 6（method docstring + 类型注解严格）**：camel ChatAgent 把 method 转 OpenAI tool schema 依赖签名 + Google docstring `Args:` 段；6 个新 method 的 docstring 必须严格写好（参数名、含义、类型）以便 LLM 正确生成 tool_call。

## 4. 核心逻辑

### 4.1 数据结构

`SocialAgentAction` 实例字段（fork 后）：
- `agent_id: int`
- `platform_manager: PlatformManager`（NEW；替代原 `channel: Channel`）
- 原 `channel` 字段保留**仅供** FEED 类 12 个 method 使用——FEED 类仍走 Channel → Platform → pool_*.db 的路径（A5 决议：直接通信不写 pool trace；FEED 类继续写 pool trace）

不变量：
- 6 个新 method 都不读 / 写 `self.channel`；只调 `self.platform_manager.dispatch`。
- 4 个 group method 也不再调 `self.channel`（改 dispatch）。

### 4.2 关键流程 / 算法

新 6 个 method 的统一模板：

```python
async def <new_action>(self, **kwargs) -> Any:
    return await self.platform_manager.dispatch(
        ActionType.<NEW_ACTION>,
        agent_id=self.agent_id,
        **kwargs,
    )
```

详细路由（在 `world/dispatcher.py` 的 ActionDispatcher 实现，本文件不展开）：

| Method | dispatch 路由目标 | 副作用表 / 内存 |
|---|---|---|
| `speak_to_local` | FaceToFaceBus.send_local | world.db.{direct_message(channel='F2F', arrive_at=t, delivered=1), overhear} |
| `send_message` | RemoteMessageBus.send | world.db.direct_message(channel='RDC', arrive_at=t+delay, delivered=0/1) |
| `request_move` | WorldState.queue_pending_move | in-memory pending_moves 列表（轮末结算） |
| `relation_change` | RelationGraph.write | world.db.relation；钩子投影 pool_*.db.follow |
| `capability_change` | CapabilityTable.write | world.db.capability |
| `update_state` | WorldState.update_state | in-memory `world.agents[a].current_state`（不写 DB） |

`update_state` 是最简实现——dispatch 内部一行：

```python
world.agents[agent_id].current_state = new_state
```

不走 Bus；不写 DB；不持久化（重启后丢失，可接受——current_state 是动态人格层）。

### 4.3 与其他模块的交互

- 上游调用方：`SocialAgent.perform_action_by_llm` 内部 LLM tool_call 后通过 `getattr(self.agent_action, call.tool_name)` 调用。
- 下游被调方：
  - FEED 12 个 method：`Channel.write_to_send_queue`（KEEP）
  - GROUP 4 个 + NEW 6 个：`platform_manager.dispatch`
- 共享状态：
  - 写 world.db 所有 7 张直接通信 / 关系 / 能力 / 群事件相关表（间接，通过 Bus）
  - 写 pool_*.db.{post, like, follow, comment, trace, …}（FEED 类间接通过 Channel → Platform）
  - 改 in-memory `WorldState.agents[*].current_state` / pending_moves

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class SocialAgentAction:
    def __init__(
        self,
        agent_id: int,
        platform_manager: PlatformManager,    # NEW
        channel: Channel,                     # KEEP（FEED 类用）
    ) -> None: ...

    # FEED 类 12 个 method KEEP（签名不变）
    async def create_post(self, content: str) -> dict: ...
    async def repost(self, post_id: int) -> dict: ...
    # ... 其余 10 个
    
    # GROUP 4 个 EDIT（路由改 dispatch；签名不变）
    async def create_group(self, name: str, members: list[int]) -> dict: ...
    async def join_group(self, group_id: int) -> dict: ...
    async def leave_group(self, group_id: int) -> dict: ...
    async def send_to_group(self, group_id: int, content: str) -> dict: ...
    
    # NEW 6 个
    async def speak_to_local(self, content: str) -> dict:
        """Speak openly to all co-located agents.

        Args:
            content: The utterance text.
        """
        ...

    async def send_message(self, target_id: int, content: str) -> dict:
        """Send a private remote message to a single contact.

        Args:
            target_id: Recipient agent_id.
            content: Message body.
        """
        ...

    async def request_move(self, place_id: str, reason: str = "") -> dict:
        """Request to move to a target place. Resolved at end of round (FCFS).

        Args:
            place_id: Destination place_id (must exist in world.places).
            reason: Optional natural-language reason for traceability.
        """
        ...

    async def relation_change(self, target_id: int, relation_type: str, op: str) -> dict:
        """Create or break a relation edge.

        Args:
            target_id: The other agent.
            relation_type: One of conscribe-registered relation_type names.
            op: 'create' or 'break'.
        """
        ...

    async def capability_change(self, capability: str, op: str) -> dict:
        """Acquire or release a capability.

        Args:
            capability: Capability name.
            op: 'acquire' or 'release'.
        """
        ...

    async def update_state(self, new_state: str) -> dict:
        """Update current_state (B5 dynamic persona segment). Use only for meaningful inner shifts.

        Args:
            new_state: New state description (1-3 sentences).
        """
        ...

    # DELETE: listen_from_group
```

### 5.2 IPC / Flask / SQL

- 无直接 IPC / Flask 暴露。
- SQL：通过 dispatch → Bus / WorldState 间接写 world.db；通过 Channel → Platform 间接写 pool_*.db。

## 6. 配置入口

无直接配置依赖。但下游：
- `channel_config.default_delays.RDC` 影响 `send_message` 的 `arrive_at` 计算（B1.1）。
- `channel_config.failed_attempt_ttl_ticks` 影响失败 send 的 obs.recent_failed_attempts 透传（B9）。
- `memory_config.compressor.max_raw_actions` 影响 raw segment 何时强制压缩（每个 method 调用都 append 一条 RawEntry）。

## 7. 待决策 / 风险

- N2：`update_state` 滥用；本文件不做频次限制（仅 prompt 引导）；如需要剧本可用 effect 锁定字段。
- B4 retry 范围：`parse_error / arg_missing` retry≤1，仅在该次 retry 的 prompt 临时附加错误信息，不持久化。具体 retry 逻辑在 `perform_action_by_llm` 内（详见 `fork_oasis_agent.md`）；本文件每个 method 不做错误处理 wrapping。
- 隐含：6 个新 method 必须能被 camel.ChatAgent.tools 识别为工具——OASIS 使用 docstring + 类型注解自动转 OpenAI tool schema；签名中 `Any` 返回类型在 OpenAI tool schema 不能体现，需保证返回结构是 `dict`（OASIS 12 个 FEED method 都是 `dict`），统一返回 `{"status": "ok"|"error", "detail": ...}`。
