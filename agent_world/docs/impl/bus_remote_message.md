# RemoteMessageBus 实现文档

> 路径: `agent_world/buses/remote_message.py`
> 对应 LAYOUT §: §2.C RemoteMessageBus / §3.2 direct_message(arrive_at + attempted_at + delivered) / §6.1 步骤 7 (SEND_MESSAGE 路径) / §6.2 一个动作示例 / §B1.1 arrive_at 计算
> 上游依赖文档: `world_db.md`, `dispatcher.md`, `connectivity.md`, `place_store.md`, `relation_graph.py.md`, `capability_table.md`, `clock.md`
> 下游依赖文档: `perception.md`, `segment.md`, `action_logger.md`

## 1. 模块定位

RemoteMessageBus 是 Agent World 三大直接通信 Bus 中的"远程定向通信器"。当 agent 调用 `SEND_MESSAGE(target, content)` 时, 它先用 ConnectivityResolver 校验 $\phi_{RDC}$(capability ∧ relation ∧ coverage), 通过则按 coverage matrix 计算 `arrive_at = world.t + latency_ticks` 写入 `direct_message`(channel_type='RDC', delivered=1); 校验失败则写一条 `delivered=0` 占位行, 由 PerceptionBuilder 在下一轮 `obs.recent_failed_attempts` 透传 1 轮(LAYOUT §B9)。这是**lockstep 通信**(本轮发, 至少下轮收), 与 F2F 的"同 micro-tick 立即可见"形成对比。

- 输入: `SEND_MESSAGE(sender_id, target_id, content)` action(由 ActionDispatcher 路由进来)、当前 `world.t`、coverage matrix、capability 表、relation 图。
- 输出: `world.db.direct_message` 一行(成功 `delivered=1, arrive_at=t+delay`; 失败 `delivered=0, arrive_at=t`)。
- 不写 `overhear`(RDC 是远程, 不存在同地点旁观); 不调 OASIS Channel; 不写 `pool_*.db`。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径(含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| INSERT direct_message SQL 模板 | OASIS(已 fork 后被删) | `vendor/oasis/oasis/social_platform/platform.py:1448-1495` 的 `send_to_group()` 删除前版本 | PATTERN | 仅作 SQL 形态参考 |
| $\phi_{RDC}$ 三元校验 | 本项目 | `agent_world/world/connectivity.py::ConnectivityResolver.phi_RDC(src, dst) -> bool` | KEEP | 直接调; 校验 capability + relation + coverage |
| coverage latency 查询 | 本项目 | `agent_world/world/place_store.py::PlaceStore.coverage[(src_place, dst_place)].latency_ticks` | KEEP | 内存反向索引 |
| 默认 delay fallback | LAYOUT §7.1 | `simulation_config.json::channel_config.default_delays.RDC`(默认 1) | NEW | 三级覆盖优先级见 6 |
| 单写者 Lock | 本项目 | `WorldDB.write_lock: asyncio.Lock` (B8) | KEEP | 所有 INSERT 在 lock 内 |
| 失败透传机制 | LAYOUT §B9 | `direct_message.delivered=0 AND attempted_at == t-1` 由 PerceptionBuilder 1 轮透传 | NEW | 本 Bus 仅负责写 delivered=0 行, 不负责读 |

## 3. 关键改动(相对来源仓库)

- 改动 1: 全新写, 不沿用 OASIS Channel 异步队列——直接自写 SQL 写 `world.db`。
- 改动 2: 引入 `arrive_at` 字段(B1.1): 成功投递时 `arrive_at = world.t + delay`(其中 `delay >= 1`); PerceptionBuilder 在下一轮按 `arrive_at <= world.t` 过滤拉取, 实现 lockstep。
- 改动 3: 失败路径**不抛异常**——按 LAYOUT §B9 silent 策略, 写一条 `delivered=0, arrive_at=attempted_at(占位)` 行, 让 PerceptionBuilder 下一轮透传给 sender 的 `obs.recent_failed_attempts`(仅 1 轮)。
- 改动 4: delay 计算**三级覆盖优先级**(LAYOUT §7.1):
  1. `coverage[L_t(sender) → L_t(target)].latency_ticks` 最优先;
  2. 否则 `channel_config.default_delays.RDC`(默认 1);
  3. 否则 fallback `0`(理论上不应发生; 防御性保底)。
- 改动 5: 不写 pool trace(LAYOUT §3.4 / A5 决议)——RDC 是 world 级事件, 不进任何 pool db。

## 4. 核心逻辑

### 4.1 数据结构

RemoteMessageBus 自身**无内存状态**, 所有数据落 `world.db`。构造时持有以下引用(不拥有):

- `self.world_db: WorldDB`
- `self.places: PlaceStore` —— 提供 `L_t[agent_id]` 与 `coverage` 矩阵(供 latency 查询)
- `self.connectivity: ConnectivityResolver` —— 提供 `phi_RDC(src, dst) -> bool`
- `self.clock: Clock`
- `self.config: ChannelConfig` —— `default_delays.RDC` 等

不变量:
- 每行 direct_message 的 `attempted_at <= arrive_at`; 失败时取等号(`arrive_at = attempted_at`); 成功时 `arrive_at >= attempted_at + 1`(RDC delay 至少 1, F2F 才用 0)。
- `delivered ∈ {0, 1}`(RDC 不会写 -1; -1 是 GroupBus 退群清理用)。
- `place_id` 写发送瞬间 sender 所在地点(同 F2FBus, 用于审计与跨星球场景的延迟核对)。
- `group_id` 永远 `NULL`(RDC 不属于任何 group)。

### 4.2 关键流程 / 算法

```
send(sender_id, target_id, content) -> message_id:
  1. t            = self.clock.now()
  2. src_place    = self.places.L_t[sender_id]
  3. dst_place    = self.places.L_t[target_id]
  4. ok           = self.connectivity.phi_RDC(sender_id, target_id)
                    # 内部 = capability(sender, "comm_rdc")
                    #       ∧ has_relation_path(sender, target)   # 见 connectivity.py
                    #       ∧ coverage[src_place, dst_place].reachable

  5. if not ok:
       async with self.world_db.write_lock:
         id = INSERT INTO direct_message(
                  sender_id, recipient_id=target_id, group_id=NULL,
                  channel_type='RDC', content,
                  place_id=src_place, attempted_at=t, arrive_at=t,
                  delivered=0)
       return id
       # → 下轮 PerceptionBuilder 把这条以 attempted_at==t 的条件
       #   拼进 sender.obs.recent_failed_attempts(LAYOUT §B9, 仅 1 轮)

  6. # 成功路径: 计算 delay
     delay = self._resolve_delay(src_place, dst_place)
              # 三级优先级:
              # (a) coverage[(src,dst)].latency_ticks if exists else
              # (b) self.config.default_delays.RDC if exists else
              # (c) 0
     arrive = t + delay

  7. async with self.world_db.write_lock:
       id = INSERT INTO direct_message(
                sender_id, recipient_id=target_id, group_id=NULL,
                channel_type='RDC', content,
                place_id=src_place, attempted_at=t, arrive_at=arrive,
                delivered=1)
     return id
     # → 当 world.t' >= arrive 时, target.obs.incoming_messages 才会读到

_resolve_delay(src_place, dst_place) -> int:
  if (src_place, dst_place) in self.places.coverage:
      return self.places.coverage[(src_place, dst_place)].latency_ticks
  if "RDC" in self.config.default_delays:
      return self.config.default_delays["RDC"]
  return 0
```

注:
- 失败路径**仍然 INSERT**, 不是不写——原因: B9 失败透传需要这一行作为依据。`delivered=0` 行会保留, 不会被后续 sweep 改写为 1(因为 GroupBus 的 sweep 仅扫 `group_id IS NOT NULL`, 而 RDC 行 group_id 为 NULL)。
- 不做 retry——retry 是 LLM 解析层的事(LAYOUT §B4); 一旦到达 dispatcher 已是合法 action, 失败就是 silent + 1 轮透传。
- 不写 segment 与 overhear。segment append 由 dispatcher 在调用本 Bus 后单独完成; overhear 不适用。

### 4.3 与其他模块的交互

- 上游调用方:
  - `agent_world/world/dispatcher.py::ActionDispatcher.dispatch` 路由 `SEND_MESSAGE` 时调 `await remote_message_bus.send(...)`。
  - `agent_world/runner/action_logger.py` 在 dispatcher 之后读 message_id + delivered 写 actions.jsonl。
- 下游被调方:
  - `agent_world/persistence/world_db.py::WorldDB.execute`(走 `write_lock`)。
  - `agent_world/world/connectivity.py::ConnectivityResolver.phi_RDC(src, dst)`(纯读, 无锁)。
  - `agent_world/world/place_store.py::PlaceStore.{L_t, coverage}`(内存读)。
- 共享状态:
  - 写 `world.db.direct_message`(channel_type='RDC', delivered ∈ {0,1}, arrive_at = t 或 t+delay)。
  - 不写 `world.db.overhear`、`world.db.group_*`。
  - 读 `place_store` 内存; 不读不写 `pool_*.db`; 不调 Zep。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class RemoteMessageBus:
    def __init__(
        self,
        world_db: "WorldDB",
        places: "PlaceStore",
        connectivity: "ConnectivityResolver",
        clock: "Clock",
        config: "ChannelConfig",
    ) -> None: ...

    async def send(
        self,
        sender_id: int,
        target_id: int,
        content: str,
    ) -> int:
        """Validate phi_RDC and insert one direct_message row.

        On phi_RDC failure: delivered=0, arrive_at=attempted_at (silent
        per LAYOUT §B9; PerceptionBuilder transmits the failure into
        sender.obs.recent_failed_attempts for exactly one tick).
        On success: delivered=1, arrive_at=world.t + delay where delay is
        resolved by the three-tier priority (coverage > default_delays.RDC > 0).

        Returns the inserted direct_message.message_id.
        """
        ...

    def _resolve_delay(self, src_place: str, dst_place: str) -> int: ...
        # internal; documented for testability
```

### 5.2 IPC / Flask / SQL

无 IPC 命令; 无 Flask 路由。

SQL 操作清单:

| 操作 | 表 | 写入字段 | 触发条件 |
|---|---|---|---|
| INSERT | `world.db.direct_message` | `sender_id, recipient_id, group_id=NULL, channel_type='RDC', content, place_id, attempted_at=t, arrive_at=t, delivered=0` | $\phi_{RDC}$ 校验失败 |
| INSERT | `world.db.direct_message` | `sender_id, recipient_id, group_id=NULL, channel_type='RDC', content, place_id, attempted_at=t, arrive_at=t+delay, delivered=1` | $\phi_{RDC}$ 校验通过 |

不执行 UPDATE / DELETE; SELECT 仅在 `_resolve_delay` 通过 PlaceStore 内存读, 不直接打 SQLite。

## 6. 配置入口

从 `simulation_config.json` 读取(LAYOUT §7.1):

| 字段 | 默认 | 用途 | 验证规则 |
|---|---|---|---|
| `channel_config.default_delays.RDC` | `1` | 当 coverage 矩阵未给具体 (src→dst) latency 时的 fallback delay | 必须 `>= 1`(否则与 F2F 语义混淆); 启动期 Pydantic 校验 |
| `world_config.coverage[]` | (用户填) | (src_place, dst_place, latency_ticks); latency_ticks 优先级最高 | `latency_ticks >= 0`; (src, dst) 必须为已注册 place_id |
| `channel_config.failed_attempt_ttl_ticks` | `1` | 与本 Bus 间接相关——失败行被 PerceptionBuilder 透传几轮 | MVP 固定 1; 本 Bus 不读此字段 |

delay 三级覆盖优先级硬编码(LAYOUT §7.1):
1. `coverage[(src,dst)].latency_ticks`
2. `channel_config.default_delays.RDC`
3. `0`(防御性 fallback)

## 7. 待决策 / 风险

- 风险 1(LAYOUT §9.5.1 N5): `arrive_at` 字段仅 world.db.direct_message 有, pool_*.db 不受影响; 但若未来有"pool 层 RDC 镜像"需求(P7 跨池镜像), 需重新审视——目前不在 MVP 范围。
- 风险 2: $\phi_{RDC}$ 校验包含 relation 检查——LAYOUT §3 定义 RDC 需要 sender 与 target 之间存在 `is_contact=True` 的关系类型; relation 类型由 conscribe 注册(C1), 8 种初始类型中哪些 `is_contact=True` 由各 relation_type 类的元数据声明, 非本 Bus 决定。本 Bus 仅消费 `phi_RDC` 的布尔结果。
- 风险 3: 跨星球 30 tick 延迟(LAYOUT §7.1 示例)长时间占用 `delivered=1, arrive_at` 远期的行——这些行不会被 sweep 改写, PerceptionBuilder 用 `arrive_at <= t` 自然过滤, 没有性能问题; 但若总量极大, 索引 `idx_direct_message_recipient_arrive(recipient_id, arrive_at, delivered)` 必须建好。
- 风险 4: B9 透传"仅 1 轮"——若 sender 当时正在压缩(MOVE compressor 异步触发, LAYOUT §6.1 步骤 9) 错过这 1 轮, 失败信息会丢; LAYOUT 接受此损失, 不进入 ChatMemory / Zep。本 Bus 不解决该问题。
- 风险 5: `delivered=0` 失败行长期累积——MVP 不清理, 配合 `attempted_at == t-1` 的窗口过滤, 老的失败行只占空间不被读。后期可加定期 vacuum, 不在本 Bus 职责。
