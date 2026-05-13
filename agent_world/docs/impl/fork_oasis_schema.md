# OASIS schema/*.sql fork 实现文档

> 路径: `vendor/oasis/oasis/social_platform/schema/*.sql`
> 对应 LAYOUT §: §4 OASIS 总表（schema 行）/ §3.2 群聊三表迁移到 world.db / §3.3 pool_*.db 13 张表 / A1, A3
> 上游依赖文档: 无（最底层 DDL）
> 下游依赖文档: `fork_oasis_database.md`, `fork_oasis_recsys.md`（外部）

## 1. 模块定位
OASIS `social_platform/schema/` 目录下放 16 个 `.sql` 文件，每个对应一张 pool 级表（ddl + index）。`database.py` 的 `create_db()` 把它们整批 `executescript` 到目标 SQLite。Agent World 在 fork 内做三件事：(1) **删 3 个**（chat_group / group_member / group_message，搬到 `agent_world/persistence/schema/world/`，A1）；(2) **改 user.sql**（去 AUTOINCREMENT、加 `agent_id UNIQUE NOT NULL`，A3）；(3) **改 rec.sql**（FK 字段名 `tweet` → `post`）。

输入：无（静态 SQL 文件）。
输出：`pool_*.db` 内 13 张表的 schema。

## 2. 借鉴来源

| 项 | 来源仓库 | 来源路径 (含行号) | 借鉴方式 | 备注 |
|---|---|---|---|---|
| `user.sql` | OASIS | `oasis/social_platform/schema/user.sql` | EDIT | 去 AUTOINCREMENT；加 agent_id UNIQUE NOT NULL |
| `rec.sql` | OASIS | `oasis/social_platform/schema/rec.sql` | EDIT | FK `tweet` → `post` |
| `chat_group.sql` | OASIS | `oasis/social_platform/schema/chat_group.sql` | DELETE | 搬到 `agent_world/persistence/schema/world/chat_group.sql`（A1） |
| `group_member.sql` | OASIS | `oasis/social_platform/schema/group_member.sql` | DELETE | 同上；搬运后去掉指向 user 的 FK（world.db 没 user 表） |
| `group_message.sql` | OASIS | `oasis/social_platform/schema/group_message.sql` | DELETE | 同上；搬运后去掉指向 user 的 FK |
| 其余 10 个 .sql | OASIS | `oasis/social_platform/schema/` | KEEP | post / follow / like / dislike / comment / comment_like / comment_dislike / mute / report / trace / product（11 个 KEEP，其中 product 可省，仍计入 13 张表） |

## 3. 关键改动 (相对来源仓库)

- **改动 1（user.sql）**：
  - `PRIMARY KEY AUTOINCREMENT` → `PRIMARY KEY`（INTEGER）
  - 新增 `agent_id INTEGER UNIQUE NOT NULL` 列约束
  - 目的：消除 sign_up 显式传 user_id 与 AUTOINCREMENT 的暧昧；保证 `user_id = agent_id` 由 schema 强制；与 `recsys.py:54-58` 的 "+1 偏移" 假设修正同步（在 `fork_oasis_recsys.md` 处理）
- **改动 2（rec.sql FK 修正）**：原 OASIS rec 表的 FK 字段命名 `tweet`（历史遗留 Twitter 命名），改为 `post` 与 post 表对齐。索引名同步改。
- **改动 3（删 3 个 .sql）**：
  - `chat_group.sql`、`group_member.sql`、`group_message.sql` 整文件从 `vendor/oasis/oasis/social_platform/schema/` 删除
  - 复制到 `agent_world/persistence/schema/world/`（与本 fork 文档无关；由 `world/persistence/world_db.md`（外部）维护）
  - 删除后需保证 `database.py:21-40` 的 schema 文件路径常量已同步删（详见 `fork_oasis_database.md`）
- **改动 4（其余 10 个 KEEP）**：post / follow / like / dislike / comment / comment_like / comment_dislike / mute / report / trace 文件不动；`product.sql` 也不动（场景不用时可跳过装载，但 schema 文件保留）。
- **改动 5（搬运到 world.db 的三个 .sql 改动）**：在新位置 `agent_world/persistence/schema/world/`：
  - `group_member.agent_id` 原 FK `REFERENCES user(agent_id)` 删除（world.db 无 user 表）；保留 `INTEGER NOT NULL`
  - `group_message.sender_id` 同上
  - 加索引 `(group_id, ...)` 与 `(agent_id, ...)` 等查询路径
  - 这部分改动属于 world DB 范畴，本文件仅记录"搬运 + FK 删除"的事实

## 4. 核心逻辑

### 4.1 数据结构

fork 后 `vendor/oasis/oasis/social_platform/schema/` 目录最终 13 个 `.sql` 文件：

```
user.sql            # EDIT
post.sql            # KEEP
follow.sql          # KEEP
like.sql            # KEEP
dislike.sql         # KEEP
comment.sql         # KEEP
comment_like.sql    # KEEP
comment_dislike.sql # KEEP
mute.sql            # KEEP
report.sql          # KEEP
rec.sql             # EDIT (FK tweet→post)
trace.sql           # KEEP
product.sql         # KEEP（可省加载，但文件保留）
```

`user.sql` 关键 DDL（改后）：
```sql
CREATE TABLE user (
    user_id INTEGER PRIMARY KEY,           -- 去 AUTOINCREMENT
    agent_id INTEGER UNIQUE NOT NULL,      -- 新增 UNIQUE NOT NULL
    name TEXT,
    bio TEXT,
    -- 其余字段 KEEP
    created_at INTEGER
);
CREATE INDEX idx_user_agent_id ON user(agent_id);   -- 新增（agent_id 查询热点）
```

`rec.sql` 关键 DDL（改后）：
```sql
CREATE TABLE rec (
    user_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,              -- 原 tweet_id → post_id
    score REAL,
    FOREIGN KEY (post_id) REFERENCES post(post_id),  -- 原 tweet → post
    PRIMARY KEY (user_id, post_id)
);
```

不变量：
- `user.user_id == agent_id`（由调用方 sign_up 显式传入；schema 不再用 AUTOINCREMENT 保证此约束）
- `rec.post_id` 必须存在于 `post.post_id`（FK 强制）

### 4.2 关键流程 / 算法

无运行时算法（纯 DDL）。但有以下"装载链路"约束：
1. `database.py:create_db()` 必须按依赖顺序加载（user 在 post 之前；post 在 rec 之前）；OASIS 原顺序已正确，fork 后继续沿用。
2. `recsys.py` 的 user_id ↔ agent_id 映射代码（L54-58）必须与新 schema 同步，详见 `fork_oasis_recsys.md`（外部）。

### 4.3 与其他模块的交互

- 上游调用方：`database.py:create_db()` 装载；`Platform` 内 SQL handler（INSERT user / post / rec / ...）。
- 下游被调方：无（最底层）。
- 共享状态：定义 pool_*.db 的物理 schema。

## 5. 暴露 API

### 5.1 公开 class / function 签名

无（纯 SQL 文件）。

### 5.2 IPC / Flask / SQL

SQL 文件清单（fork 后剩余）：13 个 .sql，路径 `vendor/oasis/oasis/social_platform/schema/{user,post,follow,like,dislike,comment,comment_like,comment_dislike,mute,report,rec,trace,product}.sql`。

## 6. 配置入口

无配置依赖。schema 文件路径在 `database.py` 内常量。

## 7. 待决策 / 风险

- 9.6 F：跨 DB 复盘性能（report_agent 联合查询 world.db.{direct_message, script_event_log} ∪ pool_*.db.trace）。schema 层面无需改；后期由 report_agent 改造期评估是否在 trace 上加额外索引。
- 隐含：删除 3 个 .sql 后，OASIS 原测试代码可能 `open(GROUP_SCHEMA_SQL).read()` → 失败；fork 期需 grep `chat_group / group_member / group_message` 全仓库一次清理。
- 隐含：`user.sql` 修改后，OASIS 任何依赖 AUTOINCREMENT 行为的代码（如假设新 user_id = max+1）都失效；`recsys.py:54-58` 必须同步改（外部文档）。
- 启动恢复：runner 启动时根据 world.db.relation 全量重建所有 pool 的 follow 表（drop & insert）——此重建过程依赖本文件 `follow.sql` 维持原 DDL（KEEP）；本 fork 不影响重建逻辑。
