# GroupMessageBus 实现文档

> 路径: `agent_world/buses/group_message.py`
> 对应 LAYOUT §: §2.C GroupMessageBus / §3.2 chat_group + group_member + group_message + group_event(第 12 张表) + direct_message / §6.1 步骤 5 sweep + 步骤 7 SEND_TO_GROUP / §B6 持久队列重投
> 上游依赖文档: `world_db.md`, `dispatcher.md`, `connectivity.md`, `place_store.md`, `clock.md`, `world_step.md`
> 下游依赖文档: `perception.md`, `segment.md`, `action_logger.md`

## 1. 模块定位

GroupMessageBus 是 Agent World 三大直接通信 Bus 中"多对多订阅式群聊"的实现, 也是 LAYOUT A1 决议——**OASIS 群聊代码已删, 群聊三表搬到 world.db**——的承接模块。它自写 SQL 直接操作 4 张 world.db 表(`chat_group / group_member / group_message / group_event`)与 `direct_message` 表; 5 个公开 method(`create_group / join_group / leave_group / kick_member / send_to_group`)分别对应群生命周期事件。LAYOUT B6 的"持久队列重投"由本模块的 `sweep_undelivered()` 实现, 由 WorldStep 在轮初步骤 5 调用; 退群 / 被踢同时清理该成员未读的群消息(防止"地下室出来收漏掉的消息"误读已退群的内容)。

- 输入: 5 类公开调用(create / join / leave / kick / send_to_group) + WorldStep 每轮调一次 `sweep_undelivered`。
- 输出: 4 张群聊表 + `direct_message` 表的若干行变更; 不返回业务实体, 仅返回 id 或 None。
- 不调 OASIS Channel / `Platform.send_to_group`(后者已在 fork 中删除)。
- 不写 pool_*.db; 不调 Zep。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径(含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| INSERT 群消息 SQL 模板 | OASIS(已 fork 后被删) | `vendor/oasis/oasis/social_platform/platform.py:1448-1495` 的 `send_to_group()` 删除前版本 | PATTERN | 仅作"成员循环 INSERT direct_message"的 SQL 形态参考, 不引用其代码 |
| chat_group / group_member / group_message DDL | OASIS schema | `vendor/oasis/oasis/social_platform/schema/{chat_group,group_member,group_message}.sql`(原版, 已删除并搬到 world.db) | EDIT | 搬到 `agent_world/persistence/schema/world/`; 去掉 `agent_id` FK→user 约束(world.db 无 user 表) |
| group_event 新表 | LAYOUT §3.2 | 第 12 张表 DDL | NEW | 全新写, MVP 唯一新增 world.db 表 |
| 持久队列重投(B6) | LAYOUT §B6 | §3.2 + §6.1 步骤 5 + §6.3 PerceptionBuilder | NEW | sweep 由 WorldStep 调 |
| ConnectivityResolver coverage 过滤 | 本项目 | `agent_world/world/connectivity.py::phi_GRP(member, sender_place)` | KEEP | send_to_group 的成员可达性检查 |
| 单写者 Lock | 本项目 | `WorldDB.write_lock: asyncio.Lock` (B8) | KEEP | 所有写操作进 lock |

## 3. 关键改动(相对来源仓库)

- 改动 1: 不调 OASIS `Platform.{send_to_group, create_group, join_group, leave_group, listen_from_group}`——这五个 method 已在 fork 中删除(LAYOUT §4 / A1)。本 Bus 自写 SQL 操作 world.db。
- 改动 2: 三张原 OASIS 群聊表(`chat_group / group_member / group_message`)从 `pool_*.db` 搬到 `world.db`; `group_member.agent_id` 与 `group_message.sender_id` 原 FK→user 去除(world.db 无 user 表), 改为 `INTEGER NOT NULL`。
- 改动 3: 新增第 12 张表 `group_event`(LAYOUT §3.2)记录 join/leave/kick——PerceptionBuilder 按 `occurred_at == t-1` 拼进 `obs.group_events` 仅 1 轮透传。
- 改动 4: send_to_group 加 coverage 过滤(LAYOUT §B6 + §2.C): 对每个群成员检查 `phi_GRP(member, sender_place)`, 失败成员**仍写一行 direct_message** 但 `delivered=0`(B6 持久队列), 由 sweep_undelivered 在后续轮重投。
- 改动 5: leave_group / kick_member 同时执行 (a) `INSERT INTO group_event(event_type='leave'|'kick')`、(b) `DELETE FROM group_member WHERE group_id=? AND agent_id=?`、(c) `DELETE FROM direct_message WHERE recipient_id=? AND group_id=? AND delivered=0`(LAYOUT §B6 清未读, 防止"地下室回来"读到已退群的旧消息)。
- 改动 6: create_group / join_group 也写 `group_event`(event_type='create' 用 'join' 替代——只有 join/leave/kick 三类; create 仅写 chat_group + 创建者的 join 事件)。
- 改动 7: 新增 `sweep_undelivered()`——由 WorldStep 步骤 5 调用; 扫 `delivered=0 AND group_id IS NOT NULL` 且 recipient 当前可达的行, 改 `delivered=1, arrive_at=world.t`(LAYOUT §6.1 步骤 5 + §3.2 注)。

## 4. 核心逻辑

### 4.1 数据结构

GroupMessageBus 自身**无内存状态**, 持有以下引用(不拥有):

- `self.world_db: WorldDB`
- `self.places: PlaceStore` —— 提供 `L_t[agent_id]`
- `self.connectivity: ConnectivityResolver` —— 提供 `phi_GRP(agent_id, sender_place) -> bool`(成员是否可在当前位置接收来自 sender_place 的群消息; 复用 coverage 矩阵)
- `self.clock: Clock`
- `self.config: ChannelConfig` —— 用于 GRP delay

不变量:
- `chat_group.group_id` 全局唯一(AUTOINCREMENT)。
- `group_member` 的 `(group_id, agent_id)` 唯一; 退群 / 被踢后 DELETE。
- `direct_message.group_id IS NOT NULL` 且 `channel_type='GRP'` 表示这是群消息的成员拷贝(每成员一行); `direct_message.delivered ∈ {-1, 0, 1}`:
  - `-1`: 已取消(退群清理路径之外目前不主动写 -1; LAYOUT §3.2 字段定义保留此值, 退群清理用 DELETE 而非 UPDATE delivered=-1; -1 留给未来"消息撤回"扩展, MVP 不写)。
  - `0`: 投递失败 / 等待重投。
  - `1`: 已投达。
- `group_event.event_type ∈ {'join', 'leave', 'kick'}`(CHECK 约束); `actor_id` 在 join/leave 等于 agent_id, kick 时是踢人者。

### 4.2 关键流程 / 算法

**create_group(creator_id, group_name) -> group_id**
```
t = clock.now()
async with world_db.write_lock:
  group_id = INSERT INTO chat_group(group_name, created_by=creator_id, created_at=t) RETURNING group_id
  INSERT INTO group_member(group_id, agent_id=creator_id, joined_at=t)
  INSERT INTO group_event(group_id, agent_id=creator_id,
                          event_type='join', occurred_at=t,
                          actor_id=creator_id)
return group_id
```

**join_group(agent_id, group_id) -> bool**
```
t = clock.now()
async with world_db.write_lock:
  if exists(SELECT 1 FROM group_member WHERE group_id=? AND agent_id=?):
      return False
  INSERT INTO group_member(group_id, agent_id, joined_at=t)
  INSERT INTO group_event(group_id, agent_id, event_type='join',
                          occurred_at=t, actor_id=agent_id)
return True
```

**leave_group(agent_id, group_id) -> bool**
```
t = clock.now()
async with world_db.write_lock:
  rows = DELETE FROM group_member WHERE group_id=? AND agent_id=?
  if rows == 0: return False
  INSERT INTO group_event(group_id, agent_id, event_type='leave',
                          occurred_at=t, actor_id=agent_id)
  DELETE FROM direct_message
    WHERE recipient_id=? AND group_id=? AND delivered=0   # B6 清未读
return True
```

**kick_member(actor_id, target_id, group_id) -> bool**
```
t = clock.now()
async with world_db.write_lock:
  # MVP 不强制鉴权 actor_id 是否管理员; 上层 dispatcher 决定权限策略
  rows = DELETE FROM group_member WHERE group_id=? AND agent_id=target_id
  if rows == 0: return False
  INSERT INTO group_event(group_id, agent_id=target_id,
                          event_type='kick', occurred_at=t,
                          actor_id=actor_id)
  DELETE FROM direct_message
    WHERE recipient_id=target_id AND group_id=? AND delivered=0
return True
```

**send_to_group(sender_id, group_id, content) -> message_id**
```
t        = clock.now()
sp       = places.L_t[sender_id]
delay    = self._resolve_delay()              # 见下
arrive   = t + delay
async with world_db.write_lock:
  # 1) 写一行 group_message 作为"群消息原本"
  msg_id = INSERT INTO group_message(group_id, sender_id, content,
                                     sent_at=t) RETURNING message_id

  # 2) 取所有成员(除 sender)
  members = SELECT agent_id FROM group_member
              WHERE group_id=? AND agent_id != sender_id

  # 3) 对每个成员: coverage 过滤后写一行 direct_message
  for m in members:
    if connectivity.phi_GRP(m, sp):
        INSERT INTO direct_message(
          sender_id, recipient_id=m, group_id=group_id,
          channel_type='GRP', content,
          place_id=sp, attempted_at=t, arrive_at=arrive, delivered=1)
    else:
        INSERT INTO direct_message(
          sender_id, recipient_id=m, group_id=group_id,
          channel_type='GRP', content,
          place_id=sp, attempted_at=t, arrive_at=t, delivered=0)
        # → 后续轮 sweep_undelivered 重投
return msg_id

_resolve_delay() -> int:
  return self.config.default_delays.get("GRP", 1)
  # MVP: 不区分群成员之间的 (src→dst); 全用 GRP 默认 delay; 进阶可扩展为
  # 按每个成员单独算 coverage[sp → L_t(m)].latency_ticks。
```

**sweep_undelivered() -> int**(由 WorldStep 步骤 5 调用)
```
t = clock.now()
# 找到所有 delivered=0 的群消息行, 检查其 recipient 是否当前可达
async with world_db.write_lock:
  candidates = SELECT message_id, recipient_id, place_id FROM direct_message
                 WHERE delivered=0 AND group_id IS NOT NULL
  swept = 0
  for row in candidates:
      if connectivity.phi_GRP(row.recipient_id, row.place_id):
          UPDATE direct_message
             SET delivered=1, arrive_at=?
           WHERE message_id=?
          # arrive_at = t (重投生效时刻; 不是 t+delay, 因为 delay 已算过)
          swept += 1
return swept
```

注:
- `place_id` 在 sweep 时是**原始发送时刻 sender 的地点**——这是 phi_GRP 的输入参数; 用以保持"消息从哪里发出"语义。
- sweep 不区分这条消息是 1 轮前还是 100 轮前发的——只看 recipient 当前是否可达。LAYOUT §B6 的"地下室回来收漏消息"语义即由此实现。
- 退群清理(DELETE delivered=0)保证 sweep 不会把"已退群的人"重新唤醒读消息。
- LAYOUT §6.1 步骤 5 顺序: sweep 先于 micro-tick——本轮 micro-tick 内, agent 已能在 PerceptionBuilder 读到刚刚 sweep 改为 delivered=1 的消息(arrive_at=t, t<=t 命中)。

### 4.3 与其他模块的交互

- 上游调用方:
  - `agent_world/world/dispatcher.py::ActionDispatcher.dispatch` 路由 `CREATE_GROUP / JOIN_GROUP / LEAVE_GROUP / SEND_TO_GROUP` 到本 Bus 对应 method。`KICK` 由 ScriptEngine 或专门 effect 触发, 不是 agent action(MVP)。
  - `agent_world/world/step.py::WorldStep` 在轮初步骤 5 调 `await group_message_bus.sweep_undelivered()`(LAYOUT §6.1)。
  - `agent_world/runner/action_logger.py` 写 actions.jsonl(channel_type='GRP', group_id, delivered)。
- 下游被调方:
  - `agent_world/persistence/world_db.py::WorldDB.{execute, executemany, fetch_all}` 全部经 `write_lock`(写) 或直接走(读, 但 sweep 内的读+写整体在同一 lock 内防 race)。
  - `agent_world/world/connectivity.py::ConnectivityResolver.phi_GRP(agent_id, sender_place)`。
  - `agent_world/world/place_store.py::PlaceStore.L_t`。
- 共享状态:
  - 写 `world.db.chat_group / group_member / group_message / group_event / direct_message`。
  - 读 `world.db.group_member`(send_to_group 取成员列表)。
  - 不读不写 `pool_*.db`; 不调 Zep。

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class GroupMessageBus:
    def __init__(
        self,
        world_db: "WorldDB",
        places: "PlaceStore",
        connectivity: "ConnectivityResolver",
        clock: "Clock",
        config: "ChannelConfig",
    ) -> None: ...

    async def create_group(
        self, creator_id: int, group_name: str
    ) -> int:
        """Create chat_group, add creator as first member, write 'join' event.
        Returns the new group_id."""
        ...

    async def join_group(
        self, agent_id: int, group_id: int
    ) -> bool:
        """Insert group_member row + 'join' group_event.
        Returns False if agent_id is already a member."""
        ...

    async def leave_group(
        self, agent_id: int, group_id: int
    ) -> bool:
        """Delete group_member row, write 'leave' group_event,
        and DELETE direct_message WHERE recipient=agent_id AND
        group_id=group_id AND delivered=0 (clear unread, B6).
        Returns False if agent_id is not a member."""
        ...

    async def kick_member(
        self, actor_id: int, target_id: int, group_id: int
    ) -> bool:
        """Same as leave_group but actor_id != target_id; event_type='kick';
        actor_id stored in group_event.actor_id."""
        ...

    async def send_to_group(
        self, sender_id: int, group_id: int, content: str
    ) -> int:
        """Write one group_message row + N direct_message rows (one per
        non-sender member). Members failing phi_GRP get delivered=0 (queued
        for sweep_undelivered). Returns group_message.message_id."""
        ...

    async def sweep_undelivered(self) -> int:
        """Called by WorldStep step 5 each tick. Scans direct_message
        WHERE delivered=0 AND group_id IS NOT NULL; for each row whose
        recipient is currently reachable via phi_GRP, UPDATE delivered=1,
        arrive_at=world.t. Returns number of rows swept."""
        ...
```

### 5.2 IPC / Flask / SQL

无 IPC 命令(group 操作通过 agent action 触发); 无 Flask 路由。

SQL 操作清单:

| 操作 | 表 | 字段 / 条件 | 触发 method |
|---|---|---|---|
| INSERT | `world.db.chat_group` | `group_name, created_by, created_at=t` | create_group |
| INSERT | `world.db.group_member` | `group_id, agent_id, joined_at=t` | create_group, join_group |
| DELETE | `world.db.group_member` | `WHERE group_id=? AND agent_id=?` | leave_group, kick_member |
| INSERT | `world.db.group_event` | `group_id, agent_id, event_type∈{join,leave,kick}, occurred_at=t, actor_id` | create_group(=join), join_group, leave_group, kick_member |
| INSERT | `world.db.group_message` | `group_id, sender_id, content, sent_at=t` | send_to_group |
| INSERT | `world.db.direct_message` | `sender_id, recipient_id, group_id, channel_type='GRP', content, place_id, attempted_at=t, arrive_at=t+delay 或 t, delivered=1 或 0` | send_to_group(每成员一行) |
| DELETE | `world.db.direct_message` | `WHERE recipient_id=? AND group_id=? AND delivered=0` | leave_group, kick_member(B6 清未读) |
| SELECT | `world.db.group_member` | `WHERE group_id=? AND agent_id != sender_id` | send_to_group(取成员列表) |
| SELECT | `world.db.group_member` | `EXISTS(... WHERE group_id=? AND agent_id=?)` | join_group(去重检查) |
| SELECT | `world.db.direct_message` | `WHERE delivered=0 AND group_id IS NOT NULL` | sweep_undelivered |
| UPDATE | `world.db.direct_message` | `SET delivered=1, arrive_at=t WHERE message_id=?` | sweep_undelivered |

## 6. 配置入口

从 `simulation_config.json` 读取(LAYOUT §7.1):

| 字段 | 默认 | 用途 | 验证规则 |
|---|---|---|---|
| `channel_config.default_delays.GRP` | `1` | send_to_group 写 direct_message 的 arrive_at = t + delay | 必须 `>= 1` |
| `channel_config.group_message.redeliver_undelivered` | `true` | sweep_undelivered 的总开关; false 时 sweep 直接 return 0 | bool |
| `channel_config.group_event_ttl_ticks` | `1` | PerceptionBuilder 透传 group_event 几轮 — 本 Bus 不读, 仅作约定 | MVP 固定 1 |
| `world_config.coverage[]` | (用户填) | phi_GRP 间接读取(通过 ConnectivityResolver) | latency_ticks >= 0 |

注: MVP 的 GRP delay 不按"每个成员单独算 coverage[sp→L_t(m)].latency_ticks"——一律用 `default_delays.GRP`。如需精细化, 可在 4.2 send_to_group 步骤 3 把 `arrive` 改成 per-member 计算; 不是 MVP 范围。

## 7. 待决策 / 风险

- 风险 1(LAYOUT §9.5.1 N3): `sweep_undelivered` 在 100w agent + 大量 delivered=0 行时性能压力——需 `idx_direct_message_undelivered_group(delivered, group_id)` 索引; D 类讨论(N3)再压测优化。
- 风险 2(LAYOUT §9.5.1 N4 关联): MVP 内 sweep 是 O(候选行数) 全表扫——若有 10w 条历史 delivered=0 行, 每轮都扫所有, 即使大部分 phi_GRP 失败仍走 if 分支。后期考虑 `delivered=0 AND attempted_at >= t-WINDOW` 时间窗口截断, MVP 不做。
- 风险 3: kick_member 的鉴权——MVP 不在本 Bus 强制 actor_id 是管理员; 上层 dispatcher 或 effect 决定权限。如果 LLM agent 滥用 kick, 需上层加规则。
- 风险 4: leave/kick 的 DELETE 不原子地与 sweep race——两者都在 `write_lock` 内, B8 单写者 Lock 保证串行, 不会 race。
- 风险 5: direct_message.delivered=-1(已取消)目前不写——LAYOUT §3.2 保留枚举值给"消息撤回"等未来场景, MVP 用 DELETE 而非 UPDATE delivered=-1 实现退群清理, 保持 sweep 扫描集合最小化。
- 风险 6: send_to_group 同时有 N 条 INSERT direct_message——大群(N=100+) 时单条 send 持锁时间长; LAYOUT §9.6 G 决议 MVP 接受, 后期可考虑 `executemany` 批量插入。本模块设计上预留 `executemany` 切换点, 不影响外部 API。
- 风险 7: PerceptionBuilder 读 group_event 的 SQL 由 perception 模块负责(LAYOUT §6.3 SQL 模板); 本 Bus 不查 group_event, 只写。仅 1 轮透传由 PerceptionBuilder 的 WHERE 条件保证, 本 Bus 不主动 GC group_event 旧行(MVP 接受表无限增长, 与 script_event_log 同策略)。
