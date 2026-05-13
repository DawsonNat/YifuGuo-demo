# persistence/world_db.py 实现文档

> 路径: `agent_world/persistence/world_db.py`
> 对应 LAYOUT §: §2.H WorldDB / §3.1 双层 DB / §3.2 12 张表 / §6.1 micro-tick / §6.3 PerceptionBuilder
> 上游依赖文档: `persistence_schema_world.md`
> 下游依赖文档: 无 (其余模块通过本模块的 CRUD API 访问 world.db)

## 1. 模块定位

`world_db.py` 是 **`world.db` 这一单文件 SQLite 的访问层** —— 包含 12 张世界级表 (8 张全新 + 3 张从 OASIS 搬来的群聊表 + 1 张新 `group_event` 表)。它负责: (a) 启动期一次性把 `agent_world/persistence/schema/world/*.sql` 12 个 DDL 文件 `executescript` 进 DB; (b) 给上层 (Bus / WorldStep / PerceptionBuilder / ScriptEngine / RelationGraph 等) 提供细粒度 CRUD + 几条 hot-path 查询; (c) 作为**唯一写者**收口 —— 由一把 `asyncio.Lock` 串行化所有写操作, 不依赖 SQLite WAL (B8 决议)。

- **输入**: 上层模块发起的 INSERT / UPDATE / DELETE / SELECT 调用; 来自 PerceptionBuilder 的 micro-tick 高频读
- **输出**: 内存对象 (`Dict[str, Any]` 或 dataclass) / 写操作的影响行数 / 自增 message_id

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `create_db()` 初始化模板 | OASIS | `oasis/social_platform/database.py:84-201` | PATTERN | 同样的 `sqlite3.connect` + 逐文件 `executescript` 模式; 路径换成 `agent_world/persistence/schema/world/` |
| schema 文件路径常量列表 | OASIS | `database.py:21-40` | PATTERN | 用同样的"`*_SCHEMA_SQL` 文件名常量 + 12 个 `cursor.executescript`"风格, 不复用 OASIS 的常量 |
| 单写者并发模型 | — | — | NEW | OASIS 没有 asyncio.Lock; 这里因为 micro-tick 多 coroutine 并发写 (B8) 必须自己加 |
| `direct_message` / `overhear` / `script_event_log` / `group_event` CRUD | — | — | NEW | 全新表, 无前例 |

## 3. 关键改动 (相对 OASIS database.py)

- **改动 1**: 表数量 12 (vs OASIS 的 16)。删: OASIS 的 `user / post / follow / mute / like / dislike / report / trace / rec / comment / comment_like / comment_dislike / product` (这 13 张归 pool_*.db); 群聊三张 (`chat_group / group_member / group_message`) 反向**搬入** world.db。
- **改动 2**: 群聊三张表来到 world.db 后 FK 失效 —— world.db 没有 `user` 表。`group_member.agent_id`、`group_message.sender_id` 的 `FOREIGN KEY ... REFERENCES user(agent_id)` 全部去掉, 改成 `INTEGER NOT NULL` 裸列 (见 LAYOUT §3.2 末尾说明)。
- **改动 3**: `direct_message` 是全新表, 字段集**远超**任何 OASIS 群聊表 —— 有 `arrive_at` (B1.1, micro-tick 边界投递)、`attempted_at` (B9, 失败 obs 透传)、`delivered ∈ {-1, 0, 1}` (B6, 持久队列重投状态)、`channel_type ∈ {F2F, RDC, GRP}`、`place_id` (overhear 关联)。
- **改动 4**: 新增第 12 张表 `group_event` (B6 join/leave/kick 1 轮透传)。
- **改动 5**: 写并发 —— 所有 write API 走同一把 `asyncio.Lock`, 读 API 不上锁 (SQLite 同 connection 的并发读安全)。
- **改动 6**: 提供 5 条**复合查询 API** (PerceptionBuilder 与 DeliveryQueue 高频调用), OASIS 全无对应物: `fetch_incoming` / `fetch_failed` / `fetch_overhear` / `fetch_group_events` / `sweep_undelivered`。

## 4. 核心逻辑

### 4.1 数据结构

- **连接**: 单进程内单一 `sqlite3.Connection` (runner 子进程持有)。`isolation_level=None` (autocommit) + 显式 `BEGIN/COMMIT` 包裹批量写。
- **行表示**: 默认 `sqlite3.Row` (按列名取); 高频查询的几条返回 dataclass (`DirectMessageRow`, `OverhearRow`, `GroupEventRow`)。
- **写锁**: `self._write_lock: asyncio.Lock` —— 所有 INSERT/UPDATE/DELETE 走 `async with self._write_lock`。
- **不变量**:
  - `direct_message.arrive_at >= attempted_at` (失败时两者可相等)
  - `direct_message.delivered ∈ {-1, 0, 1}` (CHECK 约束 + 应用层断言)
  - `channel_type ∈ {'F2F', 'RDC', 'GRP'}` (CHECK 约束)
  - `(group_id IS NULL) ⇔ (channel_type ≠ 'GRP')`
  - `group_event.event_type ∈ {'join', 'leave', 'kick'}` (CHECK 约束)
  - `relation` 写入触发 `RelationGraph.on_change` 回调 (本模块**不主动触发**, 由 RelationGraph 包装层调用)

### 4.2 关键流程 / 算法

**(a) 启动初始化**
```
WorldDB(db_path).initialize()
  → conn = sqlite3.connect(db_path)
  → for sql_file in SCHEMA_FILES:           # 12 个文件按字典序无所谓
      conn.executescript(open(sql_file).read())
  → conn.commit()
```
注: 与 OASIS `create_db` 行为一致 —— 表已存在则 DDL 含 `CREATE TABLE IF NOT EXISTS` 是安全的 (现有 DDL 全部用 IF NOT EXISTS)。

**(b) 写流程 (Bus 调用样例)**
```
async with world_db._write_lock:
    cur = conn.execute("INSERT INTO direct_message (...) VALUES (?,?,?,?,?,?,?,?,?)",
                       (sender, recipient, group_id, channel_type, content, place_id,
                        attempted_at, arrive_at, delivered))
    msg_id = cur.lastrowid
    conn.commit()
return msg_id
```

**(c) PerceptionBuilder 读流程 (LAYOUT §6.3)**
```
fetch_incoming(recipient_id, t, last_seen):
    SELECT * FROM direct_message
     WHERE recipient_id = ?
       AND delivered = 1
       AND arrive_at <= ?
       AND arrive_at > ?
     ORDER BY arrive_at, message_id

fetch_failed(sender_id, t_minus_1):                 # B9 仅 1 轮透传
    SELECT * FROM direct_message
     WHERE sender_id = ?
       AND delivered = 0
       AND attempted_at = ?

fetch_overhear(agent_id, since_t):
    SELECT o.*, m.content, m.sender_id, m.place_id
      FROM overhear o JOIN direct_message m USING(message_id)
     WHERE o.overhearer_id = ? AND m.attempted_at >= ?

fetch_group_events(agent_id, t_minus_1):            # B6 仅 1 轮透传
    SELECT ge.* FROM group_event ge
      JOIN group_member gm ON gm.group_id = ge.group_id
     WHERE gm.agent_id = ? AND ge.occurred_at = ?
```

**(d) DeliveryQueue 重投 (B6, 每轮轮初执行一次)**
```
sweep_undelivered(t):
    # 选出 delivered=0 且接收者现已可达的群聊消息
    rows = SELECT message_id, recipient_id, group_id
             FROM direct_message
            WHERE delivered = 0 AND group_id IS NOT NULL
    for r in rows:
        if connectivity.phi_GRP(r.recipient_id, r.group_id, t):
            UPDATE direct_message
               SET delivered = 1, arrive_at = ?
             WHERE message_id = ?
```
注: 现可达性判断由调用方 (DeliveryQueue) 委托给 `ConnectivityResolver`, 本模块只接受最终 `(message_id, new_arrive_at)` 列表批量更新。

**(e) 退群 / 被踢清未读 (B6)**
```
purge_undelivered_for_group(group_id, agent_id):
    UPDATE direct_message
       SET delivered = -1
     WHERE recipient_id = ? AND group_id = ? AND delivered = 0
```
LAYOUT §3.2 用 `DELETE FROM direct_message ...`; 这里改为软删 (`delivered=-1`) 以保留审计 trace。可由配置切换。

### 4.3 与其他模块的交互

- **上游调用方**:
  - `world/dispatcher.py` (ActionDispatcher) — 写 `direct_message` (UPDATE_STATE 不写 DB)
  - `buses/face_to_face.py` — 写 `direct_message(channel='F2F')` + `overhear`
  - `buses/remote_message.py` — 写 `direct_message(channel='RDC')`
  - `buses/group_message.py` — 写 `direct_message(channel='GRP')` + `group_message` + `group_member` + `group_event`; 调 `purge_undelivered_for_group`
  - `world/perception.py` — 调 `fetch_incoming / fetch_failed / fetch_overhear / fetch_group_events`
  - `world/relation_graph.py` — 写 `relation` (并触发自身 on_change 钩子, 本模块不感知)
  - `world/capability_table.py` — 写 `capability`
  - `world/place_store.py` — 写 `place / coverage / agent_location`
  - `script/engine.py` — 写 `script_event_log`
  - `runner/run_agent_world_simulation.py` — 启动期调 `initialize()`; 每轮调 `sweep_undelivered`
- **下游被调方**: 仅 `sqlite3` 标准库 (本项目不引入 aiosqlite 等异步驱动; `asyncio.Lock` 串行化已足以保护并发写)
- **共享状态**:
  - 写: `world.db` 12 张表
  - 不读不写 pool_*.db 与 Zep
  - RelationGraph.on_change 钩子由 RelationGraph 自己投影到 pool_*.db.follow, **不**由 WorldDB 直接触发

## 5. 暴露 API

### 5.1 公开 class / function 签名

```python
class WorldDB:
    def __init__(self, db_path: str) -> None: ...
    def initialize(self) -> None: ...                         # 启动期建表
    def close(self) -> None: ...

    # ---- place / coverage / agent_location ----
    async def upsert_place(self, place_id: str, parent_id: str | None,
                           place_type: str, attrs: dict) -> None: ...
    async def upsert_coverage(self, src_place: str, dst_place: str,
                              latency_ticks: int) -> None: ...
    async def set_agent_location(self, agent_id: int, place_id: str,
                                 t: int) -> None: ...
    def fetch_all_places(self) -> list[dict]: ...             # 启动期全量加载到 PlaceStore
    def fetch_all_coverage(self) -> list[dict]: ...

    # ---- relation / capability ----
    async def insert_relation(self, src: int, dst: int, rtype: str,
                              created_at: int,
                              expires_at: int | None) -> None: ...
    async def delete_relation(self, src: int, dst: int, rtype: str) -> int: ...
    async def grant_capability(self, agent_id: int, capability: str,
                               granted_at: int) -> None: ...
    async def revoke_capability(self, agent_id: int, capability: str,
                                revoked_at: int) -> None: ...
    def fetch_active_relations(self, t: int) -> list[dict]: ...
    def fetch_active_capabilities(self, t: int) -> list[dict]: ...

    # ---- direct_message (B1.1 + B6 + B9) ----
    async def insert_direct_message(
        self, *, sender_id: int | None, recipient_id: int,
        group_id: int | None, channel_type: str, content: str,
        place_id: str | None, attempted_at: int, arrive_at: int,
        delivered: int,
    ) -> int: ...                                             # 返回 message_id
    async def mark_delivered(self, message_id: int, arrive_at: int) -> None: ...
    async def cancel_undelivered(self, message_id: int) -> None: ...  # delivered=-1
    async def purge_undelivered_for_group(
        self, group_id: int, agent_id: int) -> int: ...

    # ---- PerceptionBuilder hot path ----
    def fetch_incoming(self, recipient_id: int, t: int,
                       last_seen: int) -> list[DirectMessageRow]: ...
    def fetch_failed(self, sender_id: int, t_minus_1: int) -> list[DirectMessageRow]: ...
    def fetch_overhear(self, agent_id: int, since: int) -> list[OverhearRow]: ...
    def fetch_group_events(self, agent_id: int,
                           t_minus_1: int) -> list[GroupEventRow]: ...

    # ---- DeliveryQueue ----
    def list_undelivered_group(self) -> list[tuple[int, int, int]]: ...
        # (message_id, recipient_id, group_id)
    async def sweep_undelivered(
        self, ready: list[tuple[int, int]],  # (message_id, new_arrive_at)
    ) -> int: ...

    # ---- overhear ----
    async def insert_overhear(self, message_id: int, overhearer_id: int,
                              attempted_at: int) -> None: ...

    # ---- group ----
    async def insert_group(self, name: str, created_at: int) -> int: ...
    async def insert_group_member(self, group_id: int, agent_id: int,
                                  joined_at: int) -> None: ...
    async def delete_group_member(self, group_id: int, agent_id: int) -> None: ...
    async def insert_group_message(self, group_id: int, sender_id: int,
                                   content: str, sent_at: int) -> int: ...
    async def insert_group_event(self, group_id: int, agent_id: int,
                                 event_type: str, occurred_at: int,
                                 actor_id: int | None) -> int: ...

    # ---- script ----
    async def append_script_event(self, event_id: str, triggered_at: int,
                                  payload_json: str) -> None: ...
```

### 5.2 IPC / Flask / SQL

- **IPC**: 无 (本模块不直接对外开 IPC)
- **Flask**: 无直接路由; `app/services/report_agent.py` 通过本模块读 `direct_message / script_event_log` 拼复盘
- **SQL 输入**: 12 个 .sql DDL 文件 (`agent_world/persistence/schema/world/*.sql`)
- **SQL 输出**: 12 张表 —— 见 LAYOUT §3.2 表清单

## 6. 配置入口

`simulation_config.json` 中无专用 section; 间接读取以下字段:

- `simulation_dir`: 决定 `world.db` 物理路径 (`{simulation_dir}/world.db`)
- `channel_config.group_message.redeliver_undelivered: bool` (默认 true) — 控制 `sweep_undelivered` 是否启用
- `channel_config.failed_attempt_ttl_ticks: int` (默认 1) — 不直接由本模块用; PerceptionBuilder 用此值传 `t_minus_1` 参数
- `channel_config.group_event_ttl_ticks: int` (默认 1) — 同上

默认: 不开 WAL (B8 单写者 Lock 已够); `synchronous=NORMAL`; `journal_mode=DELETE`。

## 7. 待决策 / 风险

- **N3 (LAYOUT §9.5.1)**: 100w agent + 大群聊场景, `sweep_undelivered` 每轮全表扫描 `delivered=0 AND group_id IS NOT NULL` 是热点。MVP 先建索引 `idx_dm_undelivered ON direct_message(delivered, group_id, recipient_id) WHERE delivered=0` (部分索引), 后续 D 类讨论再做读写分离。
- **G (LAYOUT §9.6)**: 单 `asyncio.Lock` 是否成为瓶颈 —— v0.3 已决: MVP 接受; 性能瓶颈在 LLM 推理而非 DB。
- **跨 DB 原子性**: `relation` 写入 + pool_*.db.follow 投影非原子 (LAYOUT §3.5 / §9.6.C 已接受); 启动期重建 follow 表兜底。
- **purge_undelivered_for_group 软删 vs 硬删**: LAYOUT §3.2 说 `DELETE`, 本模块改 `delivered=-1` 软删。差异: 软删保留审计; 硬删省空间。是否暴露配置开关待 P6 阶段决定。
- **arrive_at 写错的兼容性 (N5)**: pool_*.db 的 trace 表无 arrive_at 字段, FEED 类 action 不受影响; 本模块负责的 world.db 是 arrive_at 唯一持有方。
- **群聊三表的字段命名**: OASIS 原表名是 `group_members` / `group_messages` (复数), 搬入 world.db 时统一改成单数 `group_member` / `group_message` (与 LAYOUT §3.2 一致); schema 文件名也跟着改。
