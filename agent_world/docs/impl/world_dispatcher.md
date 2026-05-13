# ActionDispatcher 实现文档

> 路径: `agent_world/world/dispatcher.py`
> 对应 LAYOUT §: §2.A ActionDispatcher / §6.1 步骤 7 路由表 / §6.2 一个动作 / §4 OASIS typing.py 改动 / §2.G agent_action.py 改动
> 上游依赖文档: `agents_fork_agent_action_py.md`(待写, 6 个新 action method 调本模块的 `dispatch`), `world_perception.md`(同轮 F2F 写入会被本轮后续 agent 通过 PerceptionBuilder 看到)
> 下游依赖文档: `bus_face_to_face.md`(待写), `bus_remote_message.md`(待写), `bus_group_message.md`(待写), `pools_manager.md`(待写), `script_engine.md`(待写), `memory_compressor.md`(待写)

## 1. 模块定位

ActionDispatcher 是 LLM tool_call -> 子系统的**单一路由层**. agent 的 6 个新 method(`speak_to_local / send_message / request_move / relation_change / capability_change / update_state`) + OASIS 原 12 个 FEED 类 + 4 个 GROUP 类 action 全部在此**反射 dispatch 到 6 类后端**:

1. **F2FBus** (SPEAK_TO_LOCAL, 同地点立即送达 + overhear)
2. **RDCBus** (SEND_MESSAGE, 跨地点延迟送达)
3. **GRPBus** (SEND_TO_GROUP / CREATE_GROUP / JOIN_GROUP / LEAVE_GROUP)
4. **Pool** (12 个 FEED 类 -> `MultiPoolPlatformManager.platform_for(p,f).Channel`)
5. **ScriptEngine** (REQUEST_MOVE 走审批 / RELATION_CHANGE / CAPABILITY_CHANGE 当成 effect 触发)
6. **WorldState 直写** (UPDATE_STATE 不走 Bus, 直接改 `world.agents[a].current_state`)

存在理由: agent_action 不能直连具体 Bus(否则 agent fork 与子系统耦合); 路由表+反射 dispatch 让 agent 只 emit `(action_type, kwargs)`, 由本模块决定落到哪个 Bus / DB / 内存.

- 输入: `agent: SocialAgent`, `action: ToolCall(action_type, kwargs)`, `world: WorldState`, `t: int`
- 输出: 写入 world.db / pool_*.db / world.agents 内存; 不返回值(异常进入 B4 retry / silent 路径)
- 调用频率: 每 agent 每 micro-tick 0~1 次(决策成功才 dispatch)

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| 反射 dispatch 模式 | OASIS | `vendor/oasis/oasis/social_platform/platform.py:148` 附近(`getattr(self, action_type.value)`) | PATTERN | 借模式不引用; OASIS 是 platform 内部反射, 我们扩到 6 类后端 |
| ActionType enum | OASIS(fork) | `vendor/oasis/oasis/social_platform/typing.py` | EDIT | 删 `LISTEN_FROM_GROUP`; 加 6 项(B3 + v0.3 UPDATE_STATE) |
| `_record_trace` | OASIS | `vendor/oasis/oasis/social_platform/platform_utils.py:188-217` | KEEP | 仅 FEED 类 action 内 Platform 自己调; 直接通信类**不写** pool trace(A5) |
| `ManualAction / LLMAction` | OASIS | `vendor/oasis/oasis/environment/env_action.py` | KEEP | dispatcher 不直接用, 但 ScriptEngine effect 内部用 ManualAction 注入 |
| MOVE 触发 BehaviorCompressor | LAYOUT v0.3 / Memory | §2.A 备注 + §6.1 步骤 9 | NEW | 路由前 hook `BehaviorCompressor.on_move(agent)`(异步, 不阻塞) |
| B4 非法 action retry / silent | LAYOUT v0.3 / B4 | §0 v0.3 改动表 | NEW | parse_error / arg_missing retry<=1; 其余 silent + `delivered=0` |
| §6.2 SEND_MESSAGE 全路径 | LAYOUT v0.3 / B1.1 + B9 | §6.2 | NEW | arrive_at 计算与失败/成功分支 |

## 3. 关键改动 (相对 OASIS `platform.py:148` 反射 dispatch)

- OASIS 原反射只在单 Platform 内: `action_type.value` -> `getattr(platform, name)`. Agent World 把 router 抽到外层, **跨 6 类后端**:
  - 直接通信(F2F/RDC/GRP) 完全离开 OASIS Platform, 进 `agent_world/buses/`
  - FEED 类仍下到 OASIS Platform, 但要先经 `MultiPoolPlatformManager.platform_for(place, feed)` 选实例
  - 世界级 effect(MOVE / RELATION / CAPABILITY) 走 ScriptEngine, 不直接写 world.db
  - UPDATE_STATE **绕过所有 Bus 与 ScriptEngine**, 直接修改 `world.agents[agent.id].current_state`(v0.3 B5)
- **MOVE 路由前异步 hook**: `asyncio.create_task(BehaviorCompressor.on_move(agent.id, old_place))` 在 ScriptEngine 收到 REQUEST_MOVE 之前 fire-and-forget, 不阻塞 dispatch. MOVE 真正生效在 §6.1 步骤 9.
- 非法 action 处理(B4): 把"retry / silent / 失败 obs 透传"三件事编码进 dispatcher, 不再让 agent 自己处理:
  - 解析错 / 字段缺失 -> 抛 retryable, agent 在 retry 的 prompt **临时**附错误信息(不持久化), retry<=1
  - 其余非法(coverage / capability / 目标不存在 / 已退群) -> silent, 写 `direct_message(delivered=0, attempted_at=t)` 入 world.db, 由下轮 PerceptionBuilder 拼到 `obs.recent_failed_attempts`(仅 1 轮)

## 4. 核心逻辑

### 4.1 数据结构

```python
@dataclass(slots=True)
class ToolCall:
    action_type: ActionType        # vendor/oasis/.../typing.py 的枚举
    kwargs: dict
    raw_text: str                  # 原始 LLM 输出, retry 时附 error 用

class DispatchOutcome(Enum):
    OK = "ok"
    RETRY = "retry"                # parse_error / arg_missing
    SILENT = "silent"              # 其他非法 -> 写 delivered=0
    NOOP = "noop"                  # ScriptEngine 审批未通过(REQUEST_MOVE 排队中)

@dataclass(slots=True)
class DispatchResult:
    outcome: DispatchOutcome
    error_msg: str | None          # retry 时回灌 prompt
    written_to: list[str]          # 调试用: ["world.db.direct_message", "pool_earth_twitter.db.post"]
```

**路由表(§6.1 步骤 7 完整表, hardcode 在 dispatcher.py)**:

| ActionType | 后端 | 主要写入 | 是否 lockstep |
|---|---|---|---|
| `SPEAK_TO_LOCAL` | F2FBus | `world.db.direct_message(channel='F2F', arrive_at=t, delivered=1)` + `world.db.overhear` | 否(本轮 micro-tick 内同地点立即可见) |
| `SEND_MESSAGE` | RDCBus | `world.db.direct_message(channel='RDC', arrive_at=t+delay, delivered=0/1)` | 是(下轮才被 obs 读到) |
| `SEND_TO_GROUP` | GRPBus | `world.db.{group_message, direct_message(channel='GRP')}` | 是 |
| `CREATE_GROUP` | GRPBus | `world.db.{chat_group, group_member, group_event(type='join')}` | 是(下轮 obs.group_events) |
| `JOIN_GROUP` | GRPBus | `world.db.{group_member, group_event(type='join')}` | 是 |
| `LEAVE_GROUP` | GRPBus | `world.db.{group_member DELETE, group_event(type='leave')}` + 清未读 | 是 |
| `CREATE_POST` | Pool | `pool_*.db.{post, trace}` | 是 |
| `REPOST` | Pool | `pool_*.db.{post, trace}` | 是 |
| `QUOTE_POST` | Pool | `pool_*.db.{post, trace}` | 是 |
| `LIKE_POST` | Pool | `pool_*.db.{like, trace}` | 是 |
| `DISLIKE_POST` | Pool | `pool_*.db.{dislike, trace}` | 是 |
| `CREATE_COMMENT` | Pool | `pool_*.db.{comment, trace}` | 是 |
| `LIKE_COMMENT` | Pool | `pool_*.db.{comment_like, trace}` | 是 |
| `DISLIKE_COMMENT` | Pool | `pool_*.db.{comment_dislike, trace}` | 是 |
| `FOLLOW` | Pool | `pool_*.db.{follow, trace}` (同时镜像到 world.db.relation 由 RelationGraph.on_change 反向, 见 §3.5) | 是 |
| `UNFOLLOW` | Pool | `pool_*.db.{follow DELETE, trace}` | 是 |
| `MUTE` | Pool | `pool_*.db.{mute, trace}` | 是 |
| `UNMUTE` | Pool | `pool_*.db.{mute DELETE, trace}` | 是 |
| `REQUEST_MOVE` | ScriptEngine | 审批; 通过则**异步** hook BehaviorCompressor + 推迟到步骤 9 真正写 `world.db.agent_location` | 是 |
| `RELATION_CHANGE` | ScriptEngine | 走 `RelationChangeEffect` -> `world.db.relation` + RelationGraph.on_change 投影 | 是 |
| `CAPABILITY_CHANGE` | ScriptEngine | 走 `CapabilityChangeEffect` -> `world.db.capability` | 是 |
| `UPDATE_STATE` | **WorldState 直写(无 Bus)** | `world.agents[agent.id].current_state` (内存, 同时落一条 trace 到 world.db.script_event_log 供审计) | 否(本轮系统 prompt 第 3 段下次 build 即生效, 但同 micro-tick 不重 build) |

注意: 12 个 FEED 类里的 `LISTEN_FROM_GROUP` 已**从 typing.py 删除**(由 PerceptionBuilder 主动 pull 取代); 4 个 GROUP 类(CREATE/JOIN/LEAVE/SEND_TO_GROUP) 枚举值不变但**路由改向 GRPBus**, 不再调 OASIS Platform.

### 4.2 关键流程 / 算法

**主入口**:

```
dispatch(agent, tool_call, world, t) -> DispatchResult:
    # 1. 解析 + 字段校验
    try:
        action_type = ActionType(tool_call.action_type)
        validate_kwargs(action_type, tool_call.kwargs)   # Pydantic schema
    except ParseError as e:
        return DispatchResult(RETRY, error_msg=str(e))
    except MissingArgError as e:
        return DispatchResult(RETRY, error_msg=str(e))
    # retry 上限由调用方(agent.astep) 控制, retry_count <= 1

    # 2. MOVE pre-hook (异步, 不阻塞)
    if action_type == REQUEST_MOVE:
        old_place = world.places.L_t[agent.id]
        asyncio.create_task(
            BehaviorCompressor.on_move(agent.id, old_place)
        )   # 失败/超时由 compressor 内部处理, 不抛回

    # 3. 路由 (反射模式: 表驱动)
    handler = ROUTE_TABLE[action_type]      # str -> async fn
    try:
        await handler(agent, tool_call.kwargs, world, t)
    except SilentRejection as e:
        # B4: coverage / capability / 目标不存在 / 已退群
        await world.world_db.insert_direct_message(
            sender_id=agent.id,
            recipient_id=e.intended_recipient,
            channel_type=e.channel,           # F2F/RDC/GRP
            content=e.intended_content,
            attempted_at=t,
            arrive_at=t,                      # 失败 arrive 仅占位
            delivered=0,
            place_id=world.places.L_t[agent.id],
            group_id=e.group_id,              # GRP 时填
        )
        return DispatchResult(SILENT, error_msg=None)

    # 4. 成功记录 segment(供 compressor 拼 raw log)
    world.memory.segment.append(agent.id, (t, action_type, tool_call.kwargs))

    return DispatchResult(OK)
```

**B4 非法 action 处理细则**:
- `parse_error`(LLM 输出格式坏) -> RETRY, prompt 临时附 `# Last error: <msg>`, 不写 world.db, 不进 ChatMemory; retry<=1, 仍失败则 SILENT
- `arg_missing`(字段缺) -> 同上
- `coverage_fail` / `capability_fail` / `target_not_found` / `already_left_group` -> SILENT, 写 `direct_message(delivered=0, attempted_at=t)`, 由下轮 PerceptionBuilder 拼 `obs.recent_failed_attempts`(仅 1 轮)
- silent 路径**不**回灌 ChatMemory, **不**进 Zep——失败仅在下一轮 prompt 里短暂可见, 帮助 agent 自纠

**§6.2 SEND_MESSAGE 全路径(B1.1 + B9 综合)**:

```
agent.astep returns tool_call(SEND_MESSAGE, target=42, content="...")
-> ActionDispatcher.dispatch
-> ConnectivityResolver.phi_RDC(sender, 42)        # 读 capability + relation + coverage (全内存)
   delay = coverage[L_t(sender) -> L_t(42)].latency_ticks   # 默认 1; 跨星球场景可大
   - 失败:
       INSERT direct_message(channel='RDC', sender, recipient=42,
                             attempted_at=t, arrive_at=t,         # arrive=attempted 仅占位
                             delivered=0, place_id=L_t(sender))
       -> 下轮 PerceptionBuilder 把这条以 attempted_at==t 拼进 obs.recent_failed_attempts (仅 1 轮)
       -> 不回灌 ChatMemory, 不进 Zep
   - 成功:
       INSERT direct_message(channel='RDC', sender, recipient=42,
                             attempted_at=t, arrive_at=t+delay,
                             delivered=1, place_id=L_t(sender))
       -> world.t' >= t+delay 时, agent 42 的 obs.incoming_messages 才会读到
-> segment[sender].append(("send_message", attempted_at=t, target=42, content=...))   # 进 raw segment
-> 不再写 pool trace (A5)
```

**delay 解析优先级(§7.1)**:
1. `coverage[src_place -> dst_place].latency_ticks` (具体)
2. `channel_config.default_delays[channel_type]` (默认)
3. fallback 0(F2F 默认; RDC/GRP 不应该 fallback 到 0, 配置缺失时报 warn)

**MOVE 与 BehaviorCompressor 协作时序**:
1. dispatcher 收到 `REQUEST_MOVE` 立刻 `asyncio.create_task(on_move(agent.id, old_place))`(不 await)
2. dispatcher 把 REQUEST_MOVE 转给 ScriptEngine 审批(同步)
3. 审批通过 -> 进 `pending_moves`(WorldStep 状态), dispatcher 返回
4. 审批失败 -> 走 SILENT 路径
5. WorldStep 步骤 9 真正写 `world.db.agent_location` 时, on_move task 可能已完成 / 仍在跑——不阻塞
6. on_move 失败/超时则保留 raw segment, 下次 MOVE 再试(N4)

### 4.3 与其他模块的交互

- **上游调用方**: `vendor/oasis/oasis/social_agent/agent_action.py` 的 6 个新 method + OASIS 原 12 个 FEED method + 4 个 GROUP method. 所有 method 内部从 `self.channel.write_to_receive_queue(...)` 改为 `await self.platform_manager.dispatch(...)` -> 最终调本模块的 `dispatch()`.
- **下游被调方**:
  - `agent_world/buses/face_to_face.py:FaceToFaceBus.send`
  - `agent_world/buses/remote_message.py:RemoteMessageBus.send`
  - `agent_world/buses/group_message.py:GroupMessageBus.{create,join,leave,kick,send}`
  - `agent_world/pools/manager.py:MultiPoolPlatformManager.platform_for(place, feed).<feed_method>` -> OASIS Platform 12 个 FEED method
  - `agent_world/script/engine.py:ScriptEngine.{request_move, apply_relation_change, apply_capability_change}`
  - `agent_world/world/state.py:WorldState.agents[a].current_state = new_state`(UPDATE_STATE)
  - `agent_world/memory/compressor.py:BehaviorCompressor.on_move`(异步 fire-and-forget)
  - `agent_world/memory/segment.py:Segment.append`(每条 OK action)
  - `agent_world/world/connectivity.py:ConnectivityResolver.{phi_F2F, phi_RDC, phi_GRP, phi_FEED}`
  - `agent_world/persistence/world_db.py:WorldDB.insert_direct_message`(silent 路径)
- **共享状态(读)**: world.db.{coverage, agent_location, capability, relation, group_member}(经 ConnectivityResolver 内存索引), `channel_config.default_delays`
- **共享状态(写)**:
  - `world.db.direct_message`(F2F/RDC/GRP 成功+失败, silent 路径)
  - `world.db.{group_message, group_member, chat_group, group_event}`(GRP 类间接经 GRPBus)
  - `world.db.script_event_log`(UPDATE_STATE 审计落档, REQUEST_MOVE/RELATION/CAPABILITY 间接经 ScriptEngine)
  - `pool_*.db.{post, like, follow, comment, ...}`(FEED 类间接经 OASIS Platform)
  - `world.agents[a].current_state`(UPDATE_STATE 直写内存)
  - `agent_world.memory.segment`(成功 action 都 append)

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class ActionDispatcher:
    def __init__(
        self,
        platform_manager: "MultiPoolPlatformManager",
        f2f_bus: "FaceToFaceBus",
        rdc_bus: "RemoteMessageBus",
        grp_bus: "GroupMessageBus",
        script: "ScriptEngine",
        connectivity: "ConnectivityResolver",
        world_db: "WorldDB",
        compressor: "BehaviorCompressor",
        segment: "Segment",
    ) -> None: ...

    async def dispatch(
        self,
        agent: "SocialAgent",
        tool_call: ToolCall,
        world: "WorldState",
        t: int,
    ) -> DispatchResult: ...

    # 内部 router: action_type -> async handler; 表驱动注册
    ROUTE_TABLE: ClassVar[dict[ActionType, "Callable[..., Awaitable[None]]"]]
```

### 5.2 IPC / Flask / SQL

- 无 IPC, 无 Flask 路由
- **SQL 写入(world.db)**:
  - `INSERT INTO direct_message(...)` (F2F 成功 / RDC 成功+失败 / GRP 经 GRPBus / silent fallback)
  - `INSERT INTO overhear(...)` (F2F 经 F2FBus, 同地点旁观者拷贝)
  - `INSERT INTO script_event_log(...)` (UPDATE_STATE 审计 + ScriptEngine 路径)
  - `INSERT INTO group_event(...)` (CREATE/JOIN/LEAVE/KICK 经 GRPBus)
- **SQL 写入(pool_*.db)**: 12 个 FEED 类对应 OASIS Platform 内部表, 不在本模块直接执行
- **SQL 读取**: 完全经 ConnectivityResolver 的内存反向索引, dispatcher 本身不直接 SELECT

## 6. 配置入口

从 `simulation_config.json` 读:

| 配置 key | 默认 | 说明 |
|---|---|---|
| `channel_config.default_delays.F2F` | 0 | F2F 永远 0(写死也可) |
| `channel_config.default_delays.RDC` | 1 | RDC 默认延迟; 具体 src->dst 优先 `coverage.latency_ticks` |
| `channel_config.default_delays.GRP` | 1 | 群聊默认延迟 |
| `world_config.coverage[*].latency_ticks` | (覆盖项) | 单条 src->dst 边的延迟; 跨星球可设 30 |
| `channel_config.retry_policy.parse_error_max_retry` | 1 | B4 retry 上限 |
| `channel_config.retry_policy.arg_missing_max_retry` | 1 | B4 retry 上限 |
| `channel_config.retry_policy.other_max_retry` | 0 | 其他错误直接 silent |
| `channel_config.failed_attempt_ttl_ticks` | 1 | 与 PerceptionBuilder 共享(B9 透传轮数) |
| `memory_config.compressor.enabled` | true | 是否在 MOVE 时 hook compressor |

**验证规则**:
- ROUTE_TABLE 中 22 个 ActionType 必须每个都有 handler, 启动期 assert
- `default_delays.F2F` 强制 0, 配置非 0 时报错
- `default_delays.RDC / GRP` 必须 >= 1, 否则破坏 lockstep 语义
- `coverage.latency_ticks` 必须 >= 0

## 7. 待决策 / 风险

- **N1(LAYOUT §9.5.1) Memory 压缩边界**: 当前仅 MOVE 触发 BehaviorCompressor. 被踢出群 / 剧本强制传送 是否也要 hook? 当前默认不 hook(仍走 max_raw_actions=30 兜底). 如有需求加显式 `END_BEHAVIOR` 内部 action.
- **N2 UPDATE_STATE 滥用**: dispatcher 不限制 update_state 频率; 若出现频繁滥用, 由 prompt 引导("only when meaningful inner shift") + 剧本 effect 锁定. dispatcher 端 schema 不限制.
- **N4 Haiku 摘要失败回退**: `asyncio.create_task(on_move(...))` fire-and-forget, 异常被 task 内部吞. dispatcher 不感知失败; compressor 自己保留 raw + 下次 MOVE 重试. MVP 不做指数退避.
- **REQUEST_MOVE 排队冲突(§6.1 注)**: 两人同时 MOVE 到 capacity=1 地点, 由 WorldStep 步骤 9 按 pending_moves 顺序串行处理, 后到者走 SILENT. dispatcher 端不处理 capacity 校验, 推到 ScriptEngine 审批.
- **A5 直接通信不写 pool trace**: 已锁. 若未来 report_agent 需要 SPEAK/SEND/SEND_TO_GROUP 复盘, 跨表 JOIN world.db.direct_message + pool_*.db.trace, 不在 dispatcher 重新双写.
- **G(LAYOUT §9.6) world.db 单写者 Lock**: 全部 INSERT 经 `WorldDB` 内部 `asyncio.Lock` 串行化(B8). dispatcher 高频调 insert_direct_message, 是首批锁竞争候选. MVP 接受, 性能瓶颈实际在 LLM 推理, 不在 DB.
- **反射 dispatch 的可观测性**: 表驱动后 22 个 handler, 出错栈定位需要每个 handler 内 log `action_type` + `agent.id`. dispatcher 主入口加 `logger.bind(action=..., agent=...)` 上下文, 由实现期落实.
- **OASIS typing.py 改动同步**: ROUTE_TABLE 与 fork 后 typing.py 的 ActionType 必须**完全对齐**. 启动期 assert `set(ROUTE_TABLE.keys()) == set(ActionType)`; 漏一个直接拒启动. 该 invariant 进 P0 卡口.
