# FaceToFaceBus 实现文档

> 路径: `agent_world/buses/face_to_face.py`
> 对应 LAYOUT §: §2.C FaceToFaceBus / §3.2 direct_message + overhear / §6.1 步骤 7 (SPEAK_TO_LOCAL 路径) / §6.2 同步立即送达
> 上游依赖文档: `world_db.md`, `dispatcher.md`, `connectivity.md`, `place_store.md`, `clock.md`, `world_state.md`
> 下游依赖文档: `perception.md`, `segment.md`, `action_logger.md`

## 1. 模块定位

FaceToFaceBus 是 Agent World 三大直接通信 Bus 中的"零延迟近场广播器"。当 agent 调用 `SPEAK_TO_LOCAL(content)` 时, 它把这条话语**立即**写到 `world.db.direct_message`(channel_type='F2F', delivered=1, arrive_at=world.t), 并把同地点其他在场 agent 写到 `world.db.overhear` 表里, 用以支持**地点内 micro-tick 串行可见性**——同地点的下一个决策者在同一 `world.t` 内重读 `direct_message` 时, 会立刻看到刚说出的话(LAYOUT §6.1 micro-tick 核心收益)。

- 输入: `SPEAK_TO_LOCAL(sender_id, content[, target_ids?])` action(由 ActionDispatcher 路由进来)、当前 `world.t`、`world.places.L_t`(地点反向索引)。
- 输出: `world.db.direct_message` 的若干行(每个收件人一行) + `world.db.overhear` 的若干行(每个旁观者一行); 不返回值。
- 不调 OASIS Channel / Platform.send_to_group; 不写 pool_*.db; 不直接 enqueue Zep——后者由 ActionDispatcher 在路由后调 `segment.append`, 由 BehaviorCompressor 在 MOVE 时统一压缩。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径(含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| INSERT direct_message 多行模板 | OASIS(已 fork 后被删) | `vendor/oasis/oasis/social_platform/platform.py:1448-1495` 的 `send_to_group()` 删除前版本 | PATTERN | 仅作为"循环 INSERT 多收件人"的 SQL 骨架抄写参考, 不引用其代码 |
| 同地点反向索引读取 | 本项目 | `agent_world/world/place_store.py` 的 `agents_at(place_id) -> Set[int]` | KEEP | 直接调 |
| 发送时刻 sender 地点 | 本项目 | `agent_world/world/place_store.py` 的 `L_t[agent_id] -> place_id` | KEEP | 写入 `direct_message.place_id` |
| world.db 单写者 Lock | 本项目 | `agent_world/persistence/world_db.py` 的 `WorldDB.write_lock: asyncio.Lock` (B8) | KEEP | 所有 INSERT 在 `async with self.world_db.write_lock` 内 |
| Bus 与 Channel 分离 | LAYOUT 决议 | §2.C 表 + §6.1 步骤 7 | NEW | 全新写, F2F 不走 OASIS Channel |

## 3. 关键改动(相对来源仓库)

- 改动 1: 不调 OASIS `Platform.send_to_group` / `Channel.write_to_receive_queue`——直接自写 `INSERT INTO direct_message` 与 `INSERT INTO overhear`, 跳过 OASIS 异步消息队列。
- 改动 2: 引入 `arrive_at` 字段(B1.1, LAYOUT §3.2): F2F 固定 `arrive_at = world.t`, `delivered = 1`, 无 latency 查询; 但写法与 RDC/GRP 在 schema 上完全对齐, 方便 PerceptionBuilder 统一过滤。
- 改动 3: 新增 `overhear` 表写入逻辑——sender 同地点的所有非收件人 agent 都被写入 overhear 一行, 关联 `message_id`; 用以支撑"旁观者效应"。
- 改动 4: `delivered` 字段 F2F 永远 `1`(同地点立即送达, 不存在失败投递路径; 失败仅可能在 connectivity 校验阶段——但 SPEAK_TO_LOCAL 的连通性等价于"同地点", 已在 dispatcher 调本 Bus 之前由 dispatcher 过滤)。
- 改动 5: 全新写, 不沿用 OASIS Channel 异步队列模式; 设计灵感参考 OASIS `platform.py` 的 SQL 模板分块, 但只取 SQL 形态, 不取异步信道。

## 4. 核心逻辑

### 4.1 数据结构

FaceToFaceBus 自身**无内存状态**——它是无状态的 SQL 写入器, 所有数据落在 `world.db`。构造时只持有以下引用(不拥有):

- `self.world_db: WorldDB` —— 提供 `execute / executemany / write_lock`
- `self.places: PlaceStore` —— 提供 `L_t[agent_id]` 与 `agents_at(place_id)`
- `self.clock: Clock` —— 读 `world.t`(也可由调用方传入 `t`, 见 5.1)

不变量:
- 每次 `send` 调用, 一组 `direct_message` 行的 `attempted_at` 与 `arrive_at` 严格相等(都等于 `world.t`)。
- `direct_message.place_id` 等于 sender 在调用瞬间的 `L_t[sender_id]`; 若 sender 在调用前一刻 MOVE, 该字段记录的是 MOVE 之后的新地点(MOVE 在轮末步骤 9 结算, micro-tick 内 sender 地点不变, 所以这点天然成立)。
- `overhear` 行只为"同地点 ∧ 非收件人"的 agent 创建; 收件人仅在 `direct_message` 出现, 不重复出现在 `overhear`。
- 当 `target_ids` 省略时, "所有同地点 agent(除 sender)"都视作收件人, 此时 `overhear` 不写任何行。

### 4.2 关键流程 / 算法

```
send(sender_id, content, target_ids=None) -> List[message_id]:
  1. t           = self.clock.now()                                  # = world.t
  2. place_id    = self.places.L_t[sender_id]                        # 当前地点
  3. co_located  = self.places.agents_at(place_id) - {sender_id}     # 同地点其他人
  4. if target_ids is None:
         recipients = co_located
         overhearers = set()
     else:
         recipients = set(target_ids) & co_located                   # 只能对同地点对话
         overhearers = co_located - recipients                       # 旁观

  5. async with self.world_db.write_lock:                            # B8 单写者
       inserted_ids = []
       for r in recipients:
           id = INSERT INTO direct_message(
                    sender_id, recipient_id, group_id=NULL,
                    channel_type='F2F', content,
                    place_id, attempted_at=t, arrive_at=t,
                    delivered=1)
           inserted_ids.append(id)

       for msg_id in inserted_ids:
           for o in overhearers:
               INSERT INTO overhear(message_id, overhearer_id, place_id, attempted_at=t)
       return inserted_ids
```

注:
- `INSERT INTO overhear` 写在最后, 关联已生成的 `message_id`; 若用 `executemany` 优化则需先全量收集 message_id 再批量插。
- 失败路径: 如果 sender 调用本 Bus 时 `place_id` 为 None(罕见, agent 处于无地点状态), 上层 dispatcher 应已拦截; 本 Bus 防御性 raise `ValueError`(由 dispatcher 转 silent + `delivered=0` 一行特殊记录, 但不在本模块内决定)。
- 不抛 retry; F2F 没有连通性失败概念(同地点等价于可达)。

### 4.3 与其他模块的交互

- 上游调用方:
  - `agent_world/world/dispatcher.py::ActionDispatcher.dispatch` 在路由 `SPEAK_TO_LOCAL` 时调 `await face_to_face_bus.send(...)`。
  - `agent_world/runner/action_logger.py` 在 dispatcher 之后读 inserted_ids 写 actions.jsonl(含 channel_type='F2F' / arrive_at / delivered)。
- 下游被调方:
  - `agent_world/persistence/world_db.py::WorldDB.execute / executemany`(走 `write_lock`)。
  - `agent_world/world/place_store.py::PlaceStore.{L_t, agents_at}`(纯读, 无锁)。
- 共享状态:
  - 写 `world.db.direct_message`(channel_type='F2F', delivered=1, arrive_at=t)。
  - 写 `world.db.overhear`(每条 message 0..N 行, N=同地点非收件人数)。
  - 读 `world.places.L_t` 与 `world.places.agents_at`(内存)。
  - 不读不写 `pool_*.db`; 不调 Zep。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
from typing import Iterable, List, Optional

class FaceToFaceBus:
    def __init__(
        self,
        world_db: "WorldDB",
        places: "PlaceStore",
        clock: "Clock",
    ) -> None: ...

    async def send(
        self,
        sender_id: int,
        content: str,
        target_ids: Optional[Iterable[int]] = None,
    ) -> List[int]:
        """Insert F2F direct_message rows and overhear rows.

        Returns the list of newly inserted direct_message.message_id values,
        in iteration order of recipients. Caller (action_logger) uses these
        ids to write actions.jsonl entries.
        """
        ...
```

无类方法; 无静态方法; 无生成 group / connectivity 校验入口——这些都在 dispatcher 与 connectivity 模块。

### 5.2 IPC / Flask / SQL

无 IPC 命令; 无 Flask 路由。

SQL 操作清单:

| 操作 | 表 | 字段(写入) | 备注 |
|---|---|---|---|
| INSERT | `world.db.direct_message` | `sender_id, recipient_id, group_id=NULL, channel_type='F2F', content, place_id, attempted_at=t, arrive_at=t, delivered=1` | 一个收件人一行 |
| INSERT | `world.db.overhear` | `message_id(FK→direct_message), overhearer_id, place_id, attempted_at=t` | 每条消息 × 每个旁观者一行 |

不执行 UPDATE / DELETE / SELECT(SELECT 由 PerceptionBuilder 完成)。

## 6. 配置入口

从 `simulation_config.json::channel_config` 读取:

| 字段 | 默认 | 用途 | 验证规则 |
|---|---|---|---|
| `channel_config.default_delays.F2F` | `0` | 仅供"通用 delay 计算函数"参考; F2F 实际硬编码 0 | 必须为 0(否则违反"同地点立即送达"语义, 启动期校验) |

注: F2F 不读取 `coverage[src→dst].latency_ticks`——同地点不存在跨地点延迟。

`memory_config.compressor` 与 F2FBus 无直接交互; segment append 由 dispatcher 在路由后单独调。

## 7. 待决策 / 风险

- 风险 1(LAYOUT §9.5.1 N3 关联): `direct_message` 表无 `(channel_type, place_id)` 复合索引时, PerceptionBuilder 大量 agent 同地点扫描可能慢; 由 schema 层在 `direct_message.sql` 定义索引 `idx_direct_message_recipient_arrive(recipient_id, arrive_at, delivered)` 解决, 本模块不负责。
- 风险 2: 旁观者写 overhear 的"同地点非收件人"集合可能很大(地点容量 100+ 时, 每条 SPEAK 可能写 99+ 行 overhear); MVP 不限制, 上限取决于地点容量配置; D 类讨论(100w agent scale)再评估。
- 风险 3: `target_ids` 包含跨地点 agent 时, 4.2 步骤 4 用集合交把它们悄悄过滤掉; 不向调用方上抛错误。是否需要在 `recent_failed_attempts` 透传"目标不在同地点"的失败?——LAYOUT §6.2 的失败透传只针对 SEND_MESSAGE(RDC), F2F 的"目标不在同地点"等价于 "用错了 channel", 暂归类 silent。如后续观察到 LLM 频繁滥用, 再加 `delivered=0` 行回写。
- 风险 4: 单写者 Lock 在高并发地点(同地点 100 agent 串行 micro-tick 时)成为瓶颈——按 LAYOUT §9.6 G 项决议: MVP 安全第一; LLM 推理才是真正瓶颈, DB 不是。
