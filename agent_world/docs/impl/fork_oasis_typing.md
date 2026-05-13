# OASIS typing.ActionType fork 实现文档

> 路径: `vendor/oasis/oasis/social_platform/typing.py`
> 对应 LAYOUT §: §4 OASIS 总表（typing.py 行）/ §2.G 新 action / §6.1 ActionDispatcher 路由
> 上游依赖文档: 无（最底层枚举）
> 下游依赖文档: `fork_oasis_platform.md`, `fork_oasis_agent.md`, `fork_oasis_agent_action.md`, `fork_oasis_agents_generator.md`, `agents_dynamic_tools_and_profile.md`

## 1. 模块定位
`typing.ActionType` 是 OASIS 的全局动作枚举，被 `Channel` 消息、`Platform` dispatch、`SocialAgent.available_actions`、Camel tool schema 同时引用。Agent World 在 fork 内**裁剪 + 扩展**这个 enum：删 OASIS 群聊监听值；保留群聊三类操作枚举但路由改向 Bus；新增 6 项直接通信 / 世界态修改 action（B3 + v0.3 UPDATE_STATE）。

输入：无（静态枚举）。
输出：被 `ActionDispatcher.route_table`、`PerceptionBuilder` 失败原因日志、`agent_action.py` 6 个新 method、`dynamic_tools.compute_available_tools` 共同消费。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `ActionType` enum 12 个 FEED 项 | OASIS | `oasis/social_platform/typing.py`（全文 enum 定义） | KEEP | CREATE_POST / REPOST / QUOTE_POST / LIKE_POST / DISLIKE_POST / CREATE_COMMENT / LIKE_COMMENT / DISLIKE_COMMENT / FOLLOW / UNFOLLOW / MUTE / UNMUTE |
| `ActionType` 群聊 4 项 | OASIS | 同上 | EDIT | CREATE_GROUP / JOIN_GROUP / LEAVE_GROUP / SEND_TO_GROUP 枚举值保留，但 dispatch 改路由 GroupMessageBus |
| `LISTEN_FROM_GROUP` 枚举值 | OASIS | 同上 | DELETE | OASIS 主动 listen 模型被 PerceptionBuilder.incoming_messages 取代，不再需要 |
| 6 个新枚举值 | — | — | NEW | SPEAK_TO_LOCAL / SEND_MESSAGE / REQUEST_MOVE / RELATION_CHANGE / CAPABILITY_CHANGE / UPDATE_STATE |

## 3. 关键改动 (相对来源仓库)

- **改动 1（删枚举）**：删除 `LISTEN_FROM_GROUP`。原因：OASIS 群聊采用"主动 listen"模型——agent 显式 LISTEN 才把消息从队列拉出来；Agent World 改成"被动接收"——`PerceptionBuilder.incoming_messages` 直接读 `world.db.direct_message WHERE arrive_at<=t AND delivered=1`，agent 无需显式动作。
- **改动 2（保留 4 个群聊枚举但语义改写）**：`CREATE_GROUP / JOIN_GROUP / LEAVE_GROUP / SEND_TO_GROUP` 字符串值不变，确保旧测试 / log 不破；但 `ActionDispatcher.route_table` 把它们映射到 `GroupMessageBus.{create_group,join_group,leave_group,send_to_group}`，**不再**走 `Channel → Platform`（A1 配套）。
- **改动 3（新增 6 项）**：
  - `SPEAK_TO_LOCAL`：地点内 F2F 公开发言；走 FaceToFaceBus；同地点 agent 立即可见（micro-tick 内）。
  - `SEND_MESSAGE`：定向 RDC 消息；走 RemoteMessageBus；下轮可见（lockstep + arrive_at delay）。
  - `REQUEST_MOVE`：申请移动到目标地点；ActionDispatcher 不立即生效，挂入 `pending_moves`，轮末步骤 9 由 BehaviorCompressor + WorldState 串行结算（§6.1）。
  - `RELATION_CHANGE`：agent 主动建立 / 取消关系（受 conscribe relation_type 元数据约束：对称写、互斥抛错）。
  - `CAPABILITY_CHANGE`：agent 自主获取 / 失去某能力（受 PlaceType.on_enter/on_leave 钩子或剧本约束；agent 自调一般用于"放弃能力"）。
  - `UPDATE_STATE`：agent 自反式修改 `current_state` 文本（B5 / v0.3）；不走 Bus，由 ActionDispatcher 直接改 `world.agents[a].current_state`。
- **改动 4（净增量计算）**：删 1 + 新增 6 = **净增 5**；总 enum 成员从 OASIS 原数（12 FEED + 4 GROUP + 1 LISTEN_FROM_GROUP = 17）变为 (12 + 4 + 6) = **22**。
- **改动 5（与 dispatch 路由表一致性）**：`ActionDispatcher.route_table: Dict[ActionType, RouteTarget]`，6 个新枚举的 RouteTarget 分别为 F2FBus / RDCBus / WorldState(MOVE 队列) / RelationGraph / CapabilityTable / WorldState(UPDATE_STATE 直写)。FEED 12 个走 Pool；4 个 GROUP 走 GRPBus。

## 4. 核心逻辑

### 4.1 数据结构

`ActionType(str, Enum)` 成员清单（fork 后最终态）：

```
# FEED 类（12，KEEP）
CREATE_POST, REPOST, QUOTE_POST,
LIKE_POST, DISLIKE_POST,
CREATE_COMMENT, LIKE_COMMENT, DISLIKE_COMMENT,
FOLLOW, UNFOLLOW, MUTE, UNMUTE

# 群聊类（4，KEEP 枚举值；路由改 Bus）
CREATE_GROUP, JOIN_GROUP, LEAVE_GROUP, SEND_TO_GROUP

# 直接通信 + 世界态（6，NEW）
SPEAK_TO_LOCAL, SEND_MESSAGE, REQUEST_MOVE,
RELATION_CHANGE, CAPABILITY_CHANGE, UPDATE_STATE
```

不变量：
- enum value（小写下划线 string）等于 `agent_action.py` 中对应 method 名，保证 OASIS 反射 dispatch（`getattr(self, action_type.value)`）能命中。
- 不再保留 `LISTEN_FROM_GROUP`；任何引用都需在 fork 内一并清掉（grep checklist：`agent_action.py`、`platform.py`、可能的测试文件）。

### 4.2 关键流程 / 算法

无运行时算法（纯 enum）；但需保证以下静态约束：
1. enum 成员名 ↔ method 名 1:1（反射调用前提）。
2. `ActionDispatcher.route_table` 必须覆盖所有枚举成员；缺失则 dispatch 时 KeyError。
3. `dynamic_tools.compute_available_tools` 输出的 ActionType 子集必须严格属于本 enum。

### 4.3 与其他模块的交互

- 上游调用方：所有 fork 内 `getattr(self, action_type.value)` 调用点；`ActionDispatcher.route_table`；camel tool schema 生成器（`agent.py` 间接通过 `self.tools` 暴露给 LLM）。
- 下游被调方：无（最底层）。
- 共享状态：无运行时状态；纯静态定义。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class ActionType(str, Enum):
    # FEED
    CREATE_POST = "create_post"
    REPOST = "repost"
    QUOTE_POST = "quote_post"
    LIKE_POST = "like_post"
    DISLIKE_POST = "dislike_post"
    CREATE_COMMENT = "create_comment"
    LIKE_COMMENT = "like_comment"
    DISLIKE_COMMENT = "dislike_comment"
    FOLLOW = "follow"
    UNFOLLOW = "unfollow"
    MUTE = "mute"
    UNMUTE = "unmute"
    # GROUP（路由到 Bus）
    CREATE_GROUP = "create_group"
    JOIN_GROUP = "join_group"
    LEAVE_GROUP = "leave_group"
    SEND_TO_GROUP = "send_to_group"
    # NEW（B3 + v0.3）
    SPEAK_TO_LOCAL = "speak_to_local"
    SEND_MESSAGE = "send_message"
    REQUEST_MOVE = "request_move"
    RELATION_CHANGE = "relation_change"
    CAPABILITY_CHANGE = "capability_change"
    UPDATE_STATE = "update_state"
```

### 5.2 IPC / Flask / SQL

- 无（纯枚举）。

## 6. 配置入口

无配置依赖。但下游配置：
- `dynamic_tools.compute_available_tools` 依据 capability / connectivity 过滤 ActionType 子集。
- `simulation_config_ext.world_config.events` 中 `OnAction` trigger 的 `action: <ActionType.value>` 字段必须取本 enum 中的值。

## 7. 待决策 / 风险

- N2：`UPDATE_STATE` 滥用治理。仅靠 prompt 引导；schema 不限制频次；如观测到滥用，剧本可加 effect 锁定 current_state。
- 与 dynamic_tools 的同步漂移风险：增删 enum 成员需同步更新 `compute_available_tools` 的 capability/connectivity 过滤表与 camel tool schema；建立 grep checklist：`ActionType.` 全文出现位置每次都核对。
- 隐含：camel ChatAgent 把 `self.tools` 转为 OpenAI tool schema 时依赖 method docstring + 类型注解；新增 6 个 method 的 docstring/注解必须严格写好（详见 `fork_oasis_agent_action.md`）。
