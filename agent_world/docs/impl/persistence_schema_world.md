# persistence/schema/world/*.sql 实现文档

> 路径: `agent_world/persistence/schema/world/*.sql` (12 个 DDL 文件)
> 对应 LAYOUT §: §3.2 world.db 12 张表清单 / §3.4 trace 归属 / §6.3 PerceptionBuilder 字段 / §7.4 (需求文档 DDL 来源)
> 上游依赖文档: 无
> 下游依赖文档: `persistence_world_db.md`

## 1. 模块定位

本目录是 **`world.db` 12 张表的 DDL 真相源**, 每表一个 `.sql` 文件。`agent_world/persistence/world_db.py` 在启动期对每个文件按顺序 `cursor.executescript`。表分三类:

- **8 张全新表** (Agent World 原创概念): place / coverage / agent_location / relation / capability / direct_message / overhear / script_event_log
- **3 张从 OASIS 搬来的群聊表**: chat_group / group_member / group_message —— 来自 `vendor/oasis/oasis/social_platform/schema/{chat_group,group_member,group_message}.sql`, 搬入后**去掉指向 user 表的 FK** (world.db 没有 user 表)
- **1 张全新群聊辅助表**: group_event (B6, join/leave/kick 1 轮透传)

输入: 无 (DDL 是声明)。输出: 12 个 `CREATE TABLE` + 必要的 `CREATE INDEX` 语句。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| chat_group.sql | OASIS | `oasis/social_platform/schema/chat_group.sql` | EDIT (搬迁) | 搬到本目录; DDL 原样 (无 FK 可改) |
| group_member.sql | OASIS | `oasis/social_platform/schema/group_member.sql:1-7` | EDIT (搬迁) | 搬来后**删 FK** `agent_id REFERENCES user(agent_id)`; 表名 `group_members` → `group_member` (单数化) |
| group_message.sql | OASIS | `oasis/social_platform/schema/group_message.sql:1-9` | EDIT (搬迁) | 搬来后**删 FK** `sender_id REFERENCES user(agent_id)`; 表名 `group_messages` → `group_message` (单数化) |
| 8 张新表 DDL 字段集 | 需求文档 §7.4 + LAYOUT §3.2 | — | NEW | 全新设计; 字段见各节 |
| group_event DDL | LAYOUT | LAYOUT §3.2 末尾完整 SQL | NEW | 直接使用 LAYOUT 给出的 DDL |

## 3. 关键改动 (相对来源)

- **搬迁 + 去 FK**: OASIS 群聊三表的 FK 都指向 `user` 或 `chat_group`。搬入 world.db 后 `user` 表不存在, 全部 FK 删掉, 列改为 `INTEGER NOT NULL`; `chat_group` 仍在本 DB, FK 可保留也可删, MVP 删 (避免 cascade 行为不一致)。
- **表名单数化**: OASIS 用 `group_members / group_messages` (复数), 本目录改 `group_member / group_message` (与 LAYOUT §3.2 表名一致, 也与项目习惯对齐)。
- **direct_message 字段集 (B1.1 + B6 + B9)**: 综合三个决议合并字段, 见下文 §4.1 完整 DDL。
- **relation 加 expires_at**: 比纯 OASIS follow 多 `expires_at INTEGER`, 支持有限期关系。
- **capability 的 (granted_at, revoked_at) 模式**: 不删行, 用时间戳标记起止 —— 方便审计。
- **新增 group_event 表**: B6 决议产物, OASIS 没有对应物; 1 轮 obs 透传后自然失效 (但保留持久行用于 report)。

## 4. 12 个 DDL 完整定义

### 4.1 `place.sql` (新)

```sql
CREATE TABLE IF NOT EXISTS place (
    place_id     TEXT PRIMARY KEY,
    parent_id    TEXT,                     -- 层级父地点; NULL = top
    place_type   TEXT NOT NULL,            -- conscribe place_type 注册名
    capacity     INTEGER,                  -- 容量上限; NULL = 无限
    attrs        TEXT NOT NULL DEFAULT '{}', -- JSON: timezone / behavior_hint / 自定义
    created_at   INTEGER NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES place(place_id)
);
CREATE INDEX IF NOT EXISTS idx_place_parent ON place(parent_id);
```
约定字段 (LAYOUT §6.3 + §7.1): `attrs.timezone: str` (IANA 名, 仅叙事), `attrs.behavior_hint: str | None` (B5 第 4 段 prompt), 其他自定义不限。**启动时全量加载**到 `PlaceStore` 内存。

### 4.2 `coverage.sql` (新)

```sql
CREATE TABLE IF NOT EXISTS coverage (
    src_place      TEXT NOT NULL,
    dst_place      TEXT NOT NULL,
    can_reach      INTEGER NOT NULL DEFAULT 1,    -- 0/1; 决定 φ_RDC 通过
    latency_ticks  INTEGER NOT NULL DEFAULT 0,    -- B1.1 channel delay 来源
    PRIMARY KEY (src_place, dst_place),
    FOREIGN KEY (src_place) REFERENCES place(place_id),
    FOREIGN KEY (dst_place) REFERENCES place(place_id)
);
```
启动时全量加载到 `ConnectivityResolver`; `latency_ticks` 是 B1.1 的关键字段 —— 跨星球场景可大 (LAYOUT §7.1 示例: earth_us → mars_base = 30)。`PlaceMutation` effect 也写本表。

### 4.3 `agent_location.sql` (新)

```sql
CREATE TABLE IF NOT EXISTS agent_location (
    agent_id     INTEGER PRIMARY KEY,             -- 每 agent 当前唯一位置
    place_id     TEXT NOT NULL,
    arrived_at   INTEGER NOT NULL,                -- 进入该地点的 world.t
    FOREIGN KEY (place_id) REFERENCES place(place_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_location_place ON agent_location(place_id);
```
反向索引 (place → agents) 由 `PlaceStore` 内存维护; 本表只是持久化快照。MOVE effect 真正写入**之前** `BehaviorCompressor.on_move` 已 hook (LAYOUT §3.2 注)。

### 4.4 `relation.sql` (新)

```sql
CREATE TABLE IF NOT EXISTS relation (
    src_agent       INTEGER NOT NULL,
    dst_agent       INTEGER NOT NULL,
    relation_type   TEXT NOT NULL,                -- conscribe relation_type 注册名
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER,                      -- NULL = 永久
    metadata        TEXT,                         -- JSON, 可选
    PRIMARY KEY (src_agent, dst_agent, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_relation_src ON relation(src_agent, relation_type);
CREATE INDEX IF NOT EXISTS idx_relation_dst ON relation(dst_agent, relation_type);
```
`relation_type` 通过 conscribe 注册 (C1, MVP 8 种: mutual_follow / follower / friend / lover / ex_lover / family / colleague / mute)。symmetric 类自动双写, mutually_exclusive 写入抛错 (由 `RelationGraph` 强制, 不在 schema 层)。

### 4.5 `capability.sql` (新)

```sql
CREATE TABLE IF NOT EXISTS capability (
    agent_id     INTEGER NOT NULL,
    capability   TEXT NOT NULL,
    granted_at   INTEGER NOT NULL,
    revoked_at   INTEGER,                         -- NULL = 当前生效
    metadata     TEXT,                            -- JSON, 可选
    PRIMARY KEY (agent_id, capability, granted_at)
);
CREATE INDEX IF NOT EXISTS idx_capability_active
    ON capability(agent_id, capability) WHERE revoked_at IS NULL;
```
"granted_at / revoked_at"模式 (LAYOUT §3.2): 不删行, 区分历史; `revoked_at IS NULL` 即生效中。`account_<feed>` capability 是新池注册触发器 (LAYOUT §3.5 / §9.4)。

### 4.6 `direct_message.sql` (新, B1.1 + B6 + B9 综合)

```sql
CREATE TABLE IF NOT EXISTS direct_message (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id     INTEGER,                        -- NULL 表示系统消息
    recipient_id  INTEGER NOT NULL,
    group_id      INTEGER,                        -- NULL = F2F/RDC; 非 NULL = GRP 拷贝
    channel_type  TEXT NOT NULL CHECK (channel_type IN ('F2F','RDC','GRP')),
    content       TEXT NOT NULL,
    place_id      TEXT,                           -- 发送时刻 sender 所在地点
    attempted_at  INTEGER NOT NULL,               -- 调 send 的 world.t (成败都填)
    arrive_at     INTEGER NOT NULL,               -- attempted_at + channel_delay (失败=attempted_at)
    delivered     INTEGER NOT NULL CHECK (delivered IN (-1, 0, 1))
                  -- -1 已取消 (退群清理) / 0 失败或等待重投 / 1 成功
);
CREATE INDEX IF NOT EXISTS idx_dm_recipient_arrive
    ON direct_message(recipient_id, arrive_at) WHERE delivered = 1;
CREATE INDEX IF NOT EXISTS idx_dm_failed_attempt
    ON direct_message(sender_id, attempted_at) WHERE delivered = 0;
CREATE INDEX IF NOT EXISTS idx_dm_undelivered_group
    ON direct_message(group_id, recipient_id) WHERE delivered = 0 AND group_id IS NOT NULL;
```
- **B1.1**: `arrive_at` 决定下一轮 PerceptionBuilder 是否能读到 (`arrive_at <= world.t AND delivered=1`)。F2F: `arrive_at = t`; RDC: `arrive_at = t + delay`。
- **B9**: `attempted_at == t-1 AND delivered=0` 拼进 `obs.recent_failed_attempts`, 仅 1 轮透传。
- **B6**: `delivered=0 AND group_id IS NOT NULL` 是持久重投队列; 退群时 `UPDATE ... SET delivered=-1` 清未读。
- 三个 partial index 覆盖三条 hot-path 查询 (incoming / failed / sweep)。

### 4.7 `overhear.sql` (新)

```sql
CREATE TABLE IF NOT EXISTS overhear (
    message_id     INTEGER NOT NULL,
    overhearer_id  INTEGER NOT NULL,
    place_id       TEXT NOT NULL,                 -- 发生 overhear 的地点 (= 发送者地点)
    PRIMARY KEY (message_id, overhearer_id),
    FOREIGN KEY (message_id) REFERENCES direct_message(message_id)
);
CREATE INDEX IF NOT EXISTS idx_overhear_overhearer
    ON overhear(overhearer_id);
```
F2FBus 在 `SPEAK_TO_LOCAL` 时, 对同地点的非目标 agent 各写一行。PerceptionBuilder JOIN `direct_message` 取内容 + 时间。

### 4.8 `script_event_log.sql` (新)

```sql
CREATE TABLE IF NOT EXISTS script_event_log (
    event_id       TEXT NOT NULL,                 -- 用户在 YAML 写明 (C2)
    triggered_at   INTEGER NOT NULL,
    payload        TEXT NOT NULL,                 -- JSON: trigger + effect 快照
    PRIMARY KEY (event_id, triggered_at)
);
CREATE INDEX IF NOT EXISTS idx_script_log_time
    ON script_event_log(triggered_at);
```
`event_id` 用户写 (C2 决议, 方便 reload 时去重); 同一 event_id 不同 `triggered_at` 视作"同一事件不同次触发"。

### 4.9 `chat_group.sql` (从 OASIS 搬来)

```sql
CREATE TABLE IF NOT EXISTS chat_group (
    group_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
```
来源: `oasis/social_platform/schema/chat_group.sql` 原样。`group_id` 在 world.db **全局唯一** (跨地点跨池有效)。OASIS 原表名带反引号 ``` `chat_group` ```, 本目录用普通标识符 (SQLite 不强制)。

### 4.10 `group_member.sql` (从 OASIS 搬来, 去 FK + 单数化)

```sql
CREATE TABLE IF NOT EXISTS group_member (
    group_id     INTEGER NOT NULL,
    agent_id     INTEGER NOT NULL,                -- 原 FK REFERENCES user(agent_id) 已删
    joined_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, agent_id)
    -- FK group_id REFERENCES chat_group(group_id) 也删 (避免 cascade)
);
CREATE INDEX IF NOT EXISTS idx_group_member_agent ON group_member(agent_id);
```
来源: `oasis/social_platform/schema/group_member.sql` (原表名 `group_members`)。改动:
- 表名 `group_members` → `group_member`
- 删 FK `agent_id REFERENCES user(agent_id)` (world.db 没有 user 表)
- 删 FK `group_id REFERENCES chat_group(group_id)` (避免 cascade 行为不一致)

LEAVE / KICK 同时触发: `INSERT INTO group_event` + `UPDATE direct_message SET delivered=-1 WHERE recipient_id=? AND group_id=? AND delivered=0` (B6 清未读)。

### 4.11 `group_message.sql` (从 OASIS 搬来, 去 FK + 单数化)

```sql
CREATE TABLE IF NOT EXISTS group_message (
    message_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    sender_id    INTEGER NOT NULL,                -- 原 FK REFERENCES user(agent_id) 已删
    content      TEXT NOT NULL,
    sent_at      DATETIME DEFAULT CURRENT_TIMESTAMP
    -- FK group_id REFERENCES chat_group(group_id) 也删
);
CREATE INDEX IF NOT EXISTS idx_group_message_group ON group_message(group_id, sent_at);
```
来源: `oasis/social_platform/schema/group_message.sql`。改动同 group_member。

注意: GroupMessageBus 发群消息时, 一份内容写两处 —— `group_message` (群消息真相) + 每成员一行 `direct_message(channel_type='GRP', group_id=?, recipient_id=member)` (用于 PerceptionBuilder 统一通过 direct_message 拉取 incoming, B6 失败重投也用同一字段集)。

### 4.12 `group_event.sql` (新, B6, LAYOUT §3.2 末尾完整 SQL)

```sql
CREATE TABLE IF NOT EXISTS group_event (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    agent_id     INTEGER NOT NULL,                -- 被加入/退出/踢出的 agent
    event_type   TEXT NOT NULL CHECK (event_type IN ('join','leave','kick')),
    occurred_at  INTEGER NOT NULL,
    actor_id     INTEGER                          -- 谁发起的 (kick: 踢人者; join/leave: = agent_id)
);
CREATE INDEX IF NOT EXISTS idx_group_event_lookup
    ON group_event(group_id, occurred_at);
```
PerceptionBuilder 每轮对 agent 所在每个 group 拉 `occurred_at == t-1` 的事件 → `obs.group_events` (1 轮透传后从 obs 自然消失, 但 DB 行保留用于 report)。

## 5. 暴露 API

### 5.1 公开 class / function 签名

无 (DDL 文件不暴露 API)。`world_db.py` 通过文件路径列表读取它们。

### 5.2 IPC / Flask / SQL

- **SQL 输出**: 12 张表 + 上述索引
- 加载顺序由 `world_db.py` 决定: `place → coverage → agent_location → relation → capability → direct_message → overhear → script_event_log → chat_group → group_member → group_message → group_event` (有 FK 引用的放后面: agent_location/coverage 引用 place; overhear 引用 direct_message)

## 6. 配置入口

无 (DDL 是静态)。schema 演进通过 `agent_world/persistence/migrations/` 后续添加 (LAYOUT §1 目录树已预留)。

## 7. 待决策 / 风险

- **群聊 FK 删除的影响**: 没有 FK 约束意味着脏数据 (group_member.group_id 指向不存在的 chat_group) 不会自动拒绝; 业务层 (GroupMessageBus) 要保证一致性。
- **created_at 类型不一致**: 群聊三表沿用 OASIS 的 `DATETIME DEFAULT CURRENT_TIMESTAMP`, 而其他新表用 `INTEGER NOT NULL` 存 `world.t`。这是有意为之 —— 群聊三表时间含义是"墙钟" (复盘 UI 显示用), `world.t` 含义在 `attempted_at / arrive_at / occurred_at / triggered_at / arrived_at / created_at(place) / granted_at` 等字段。MVP 暂不统一, 后续视 report_agent 需求决定是否给群聊三表也加一个 `world_t INTEGER` 列。
- **direct_message.delivered=-1 vs DELETE**: LAYOUT §3.2 说 `DELETE FROM direct_message ...`; 本 schema 设计成软删 (delivered=-1 + CHECK 包含 -1) 以保留审计。两条路线兼容, 选择由 `world_db.py` 实现层决定。
- **chat_group.name 是否唯一**: 当前 DDL 不强制 UNIQUE (沿用 OASIS); 业务上是否允许同名群留待 GroupMessageBus 决定。
- **place.attrs JSON 索引**: SQLite JSON1 扩展可建 generated column + 索引; MVP 不做, 全靠内存 PlaceStore 反查。
- **B1.1 arrive_at 与 OASIS 兼容性 (N5)**: 仅本 schema 持有 arrive_at; pool_*.db trace 不受影响。
